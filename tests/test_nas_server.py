"""Synthetic-only NAS adapter tests; no real media, NAS, or network service.

Run with the project's test environment (pytest + httpx + requirements-nas).
Every media/state path is created beneath pytest's tmp_path.
"""
import importlib.util
import json
from pathlib import Path
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest


PROJECT = Path(__file__).resolve().parents[1]
PAYLOAD = bytes(range(256)) * 16


def _load_module(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def nas(tmp_path, monkeypatch):
    media, data = tmp_path / "media", tmp_path / "state"
    media.mkdir()
    data.mkdir()
    match = media / "match-a"
    match.mkdir()
    video = match / "clip-a.mp4"
    video.write_bytes(PAYLOAD)
    poster = match / "cover-0.jpeg"
    poster.write_bytes(b"\xff\xd8synthetic-poster\xff\xd9")
    monkeypatch.setenv("VM_MEDIA_ROOT", str(media))
    monkeypatch.setenv("VM_DATA_DIR", str(data))
    monkeypatch.setenv("VM_SCAN_INTERVAL", "3600")
    monkeypatch.setenv("VM_NAS_MODE", "1")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    # Upstream route functions use module globals, so isolate core itself, not
    # just the adapter, from tests of the normal desktop application.
    core = _load_module("app", PROJECT / "app.py", monkeypatch)
    adapter = _load_module("_nas_server_test", PROJECT / "nas_server.py", monkeypatch)
    result = SimpleNamespace(adapter=adapter, core=core, app=adapter.app,
                             media=media, data=data, video=video, poster=poster,
                             outside=tmp_path / "outside")
    yield result
    adapter.controller.stop()


def _first_video(client):
    response = client.get("/api/videos")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    return response.json()["items"][0]


def _inject(nas, **changes):
    with nas.adapter.controller.condition:
        index = dict(nas.core._state["index"])
        record = dict(index["records"][0], id="poison", match="poison-match")
        record.update(changes)
        index["records"] = [*index["records"], record]
        nas.core._state["index"] = index
        nas.core._RECS = {}
        return record


def _wait_idle(nas, *, completed=None, failed=None):
    control = nas.adapter.controller
    with control.condition:
        assert control.condition.wait_for(
            lambda: (not nas.core._state["building"]
                     and (completed is None or control.completed >= completed)
                     and (failed is None or control.failed >= failed)),
            timeout=5,
        ), "synthetic index scan did not finish"


def test_state_is_persistent_and_all_paths_are_under_data(nas):
    (nas.data / "config.json").write_text(json.dumps({
        "root": "/etc", "host": "127.0.0.1", "port": 1234,
        "open_browser": True, "trashDir": "/not-allowed", "exportDir": "/not-allowed",
    }))
    with TestClient(nas.app) as client:
        assert nas.app is nas.core.app
        item = _first_video(client)
        identity = item["id"]
        cfg = nas.core.load_config()
        assert cfg["root"] == str(nas.media)
        assert (cfg["host"], cfg["port"], cfg["open_browser"]) == ("0.0.0.0", 8765, False)
        assert cfg["trashDir"] == str(nas.data / "trash")
        assert cfg["exportDir"] == str(nas.data / "exports")
        for name in ("CONFIG", "INDEX", "OVERRIDES", "TAGS_FILE", "TRASH_INDEX", "THUMBS"):
            assert Path(getattr(nas.core, name)).parent == nas.data
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.post(f"/api/video/{identity}/fav", json={}).json()["fav"] is True
        assert client.post(f"/api/video/{identity}/rename", json={"name": "NAS favorite"}).status_code == 200
        tag = client.post("/api/tags", json={"name": "NAS tag", "color": "#ff4655"}).json()
        assert "id" in tag
        assert client.post("/api/batch/tag", json={"ids": [identity], "tagIds": [tag["id"]]}).status_code == 200
        assert client.post(f"/api/video/{identity}/hide", json={}).json()["hidden"] is True
        nas.core.save_trash_index({"example": str(nas.video)})
        for filename in ("config.json", "library.json", "overrides.json", "tags.json", "trash_index.json"):
            assert (nas.data / filename).is_file()
        assert json.loads((nas.data / "overrides.json").read_text())[identity]["fav"] is True
        assert json.loads((nas.data / "tags.json").read_text())["videoTags"][identity] == [tag["id"]]
        assert not list(nas.data.glob("*.tmp"))

    # A fresh lifespan scans again, but state survives and overlays are applied.
    with TestClient(nas.app) as client:
        items = client.get("/api/videos", params={"hidden": "all"}).json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == identity
        assert items[0]["fav"] is True
        assert items[0]["hidden"] is True
        assert items[0]["name"] == "NAS favorite"
        assert items[0]["tags"] == [tag["id"]]
        assert client.get("/api/tags").json()["tags"]
        assert client.post(f"/api/video/{identity}/hide", json={}).json()["hidden"] is False
        assert client.get("/api/videos", params={"fav": "1", "tag": tag["id"]}).json()["total"] == 1


def test_startup_scan_periodic_refresh_and_clean_shutdown(nas, monkeypatch):
    monkeypatch.setattr(nas.adapter, "SCAN_INTERVAL", 0.04)
    original = nas.core.library.build
    calls = []

    def counted(root, existing=None):
        calls.append(root)
        return original(root, existing)

    monkeypatch.setattr(nas.core.library, "build", counted)
    with TestClient(nas.app) as client:
        assert _first_video(client)
        assert calls and set(calls) == {str(nas.media)}
        assert client.get("/health").json()["scans"]["completed"] >= 1
        worker = nas.adapter.controller.worker
        extra = nas.media / "match-a" / "clip-b.mp4"
        extra.write_bytes(PAYLOAD)
        control = nas.adapter.controller
        with control.condition:
            assert control.condition.wait_for(
                lambda: len(nas.core._state["index"]["records"]) == 2, timeout=5)
        assert client.get("/api/videos").json()["total"] == 2
    assert not worker.is_alive()
    assert nas.adapter.controller.worker is None
    assert nas.adapter.controller.lock_fd is None
    assert nas.core._state["building"] is False


def test_cached_normal_requests_do_not_stat_the_media_library(nas, monkeypatch):
    with TestClient(nas.app) as client:
        identity = _first_video(client)["id"]
        original_stat = nas.adapter.os.stat
        touched = []

        def tracked_stat(path, *args, **kwargs):
            if isinstance(path, (str, Path)) and str(path).endswith(".mp4"):
                touched.append(str(path))
            return original_stat(path, *args, **kwargs)

        def unexpected(*args, **kwargs):
            raise AssertionError("normal requests must not scan/reconcile")

        monkeypatch.setattr(nas.adapter.os, "stat", tracked_stat)
        monkeypatch.setattr(nas.core.library, "build", unexpected)
        monkeypatch.setattr(nas.core, "_reconcile_missing", unexpected)
        for _ in range(8):
            for endpoint in ("/api/videos", "/api/status", "/api/facets", "/api/days", "/api/tags", f"/api/video/{identity}"):
                assert client.get(endpoint).status_code == 200
        assert touched == []


@pytest.mark.parametrize("endpoint", [
    "/api/batch/delete", "/api/trash/restore", "/api/trash/purge", "/api/broken/clean",
    "/api/batch/export", "/api/video/clip-a/export", "/api/video/unknown/export",
])
def test_dangerous_routes_are_forbidden_before_upstream_handlers(nas, endpoint):
    with TestClient(nas.app) as client:
        before = nas.video.read_bytes()
        for method in ("GET", "POST", "PATCH", "DELETE"):
            response = client.request(method, endpoint, content=b"not json", headers={"content-type": "text/plain"})
            assert response.status_code == 403
        assert client.post(endpoint + "/", json={}).status_code == 403
        assert nas.video.read_bytes() == before
        assert not (nas.data / "exports").exists()
        assert list((nas.data / "trash").iterdir()) == []


def test_browse_is_confined_and_root_cannot_be_changed(nas):
    nas.outside.mkdir()
    (nas.media / "outside-link").symlink_to(nas.outside, target_is_directory=True)
    (nas.media / "WonderfulDb").mkdir()
    with TestClient(nas.app) as client:
        root = client.get("/api/browse").json()
        assert root["path"] == str(nas.media)
        assert root["parent"] == "" and root["drives"] == []
        assert [entry["name"] for entry in root["dirs"]] == ["match-a"]
        child = client.get("/api/browse", params={"path": str(nas.media / "match-a")}).json()
        assert child["parent"] == str(nas.media)
        for path in (str(nas.outside), str(nas.data), "/data", "/app", "/etc", "/", str(nas.media / "outside-link")):
            assert client.get("/api/browse", params={"path": path}).status_code == 403
            assert client.post("/api/rebuild", json={"root": path}).status_code == 403
        assert client.post("/api/rebuild", json={"root": str(nas.media / "match-a")}).status_code == 403
        assert client.post("/api/rebuild", json={}).status_code == 200
        _wait_idle(nas)
        assert client.post("/api/rebuild", json={"root": str(nas.media)}).status_code == 200
        _wait_idle(nas)
        assert nas.core.load_config()["root"] == str(nas.media)
        assert client.post("/api/rebuild", json=[]).status_code == 400
        assert client.post("/api/rebuild", json={"root": 123}).status_code == 400


@pytest.mark.parametrize("headers", [
    {"origin": "https://evil.example"},
    {"origin": "null"},
    {"origin": "http://testserver:8000"},
    {"origin": "https://testserver"},
    {"referer": "https://evil.example/page"},
    {"sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "same-site"},
    {"origin": "http://testserver", "referer": "https://evil.example/page"},
    {"origin": "https://evil.example", "sec-fetch-site": "same-origin"},
])
def test_cross_origin_writes_are_rejected(nas, headers):
    with TestClient(nas.app) as client:
        identity = _first_video(client)["id"]
        assert client.post(f"/api/video/{identity}/fav", json={}, headers=headers).status_code == 403
        assert client.post("/api/rebuild", json={}, headers=headers).status_code == 403
        assert client.patch("/api/tags/t_good", json={"name": "bad"}, headers=headers).status_code == 403
        assert client.delete("/api/tags/t_good", headers=headers).status_code == 403
        assert _first_video(client)["fav"] is False
        assert not (nas.data / "overrides.json").exists()


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data", ""])
def test_simple_cross_site_content_types_are_not_json_writes(nas, content_type):
    with TestClient(nas.app) as client:
        identity = _first_video(client)["id"]
        headers = {"origin": "http://testserver"}
        if content_type:
            headers["content-type"] = content_type
        assert client.post(f"/api/video/{identity}/fav", content=b"{}", headers=headers).status_code == 415
        assert client.post("/api/rebuild", content=b"{}", headers=headers).status_code == 415
        assert _first_video(client)["fav"] is False


def test_same_origin_favorites_tags_hide_and_rename_still_work(nas):
    headers = {"origin": "http://testserver", "referer": "http://testserver/", "sec-fetch-site": "same-origin"}
    with TestClient(nas.app) as client:
        identity = _first_video(client)["id"]
        assert client.post(f"/api/video/{identity}/fav", json={}, headers=headers).json()["fav"] is True
        assert client.post("/api/batch/fav", json={"ids": [identity], "value": False}, headers=headers).status_code == 200
        assert client.post(f"/api/video/{identity}/rename", json={"name": "Friendly name"}, headers=headers).json()["name"] == "Friendly name"
        tag = client.post("/api/tags", json={"name": "LAN tag"}, headers=headers).json()
        assert client.patch(f"/api/tags/{tag['id']}", json={"color": "#24b7af"}, headers=headers).status_code == 200
        assert client.post("/api/batch/tag", json={"ids": [identity], "tagIds": [tag["id"]]}, headers=headers).status_code == 200
        item = _first_video(client)
        assert item["name"] == "Friendly name" and item["tags"] == [tag["id"]]
        assert client.post("/api/batch/hide", json={"ids": [identity], "value": True}, headers=headers).status_code == 200
        assert client.get("/api/videos").json()["total"] == 0
        assert client.post(f"/api/video/{identity}/hide", json={}, headers=headers).json()["hidden"] is False
        # The existing frontend uses a bodyless DELETE with no Content-Type.
        assert client.delete(f"/api/tags/{tag['id']}", headers=headers).status_code == 200
        assert _first_video(client)["tags"] == []


def test_video_poster_and_single_ranges_work(nas):
    with TestClient(nas.app) as client:
        identity = _first_video(client)["id"]
        endpoint = f"/media/video/{identity}"
        full = client.get(endpoint)
        assert full.status_code == 200 and full.content == PAYLOAD
        assert full.headers["x-content-type-options"] == "nosniff"
        partial = client.get(endpoint, headers={"range": "bytes=4-11"})
        assert partial.status_code == 206 and partial.content == PAYLOAD[4:12]
        assert partial.headers["content-range"] == f"bytes 4-11/{len(PAYLOAD)}"
        assert partial.headers["content-length"] == "8"
        assert client.get(endpoint, headers={"range": "bytes=-8"}).content == PAYLOAD[-8:]
        assert client.head(endpoint, headers={"range": "bytes=0-3"}).status_code == 206
        assert client.head(endpoint).content == b""
        for invalid in ("bytes=999999-", "bytes=9-1", "bytes=-0", "bytes=", "bytes=0-1,4-5", "bytes=" + "9" * 5000 + "-"):
            response = client.get(endpoint, headers={"range": invalid})
            assert response.status_code == 416
            assert response.headers["content-range"] == f"bytes */{len(PAYLOAD)}"
        poster = client.get(f"/media/poster/{identity}")
        assert poster.status_code == 200 and poster.content == nas.poster.read_bytes()
        record = nas.core._find(identity)
        assert client.get(f"/media/poster-by-match/{record['match']}").content == nas.poster.read_bytes()


def test_poisoned_index_cannot_stream_state_database_or_symlink_targets(nas):
    nas.outside.mkdir()
    outside_video = nas.outside / "secret.mp4"
    outside_video.write_bytes(b"outside-secret")
    outside_poster = nas.outside / "secret.jpeg"
    outside_poster.write_bytes(b"outside-poster-secret")
    private_db = nas.media / "WonderfulDb"
    private_db.mkdir()
    (private_db / "account.mp4").write_bytes(b"database-secret")
    linked = nas.media / "linked.mp4"
    linked.symlink_to(outside_video)
    nested_link = nas.media / "linked-directory"
    nested_link.symlink_to(nas.outside, target_is_directory=True)
    in_root_link = nas.media / "in-root-link.mp4"
    in_root_link.symlink_to(nas.video)
    with TestClient(nas.app) as client:
        # Simulate malicious old index entries independently of importer safety.
        for target in (outside_video, nas.data / "config.json", private_db / "account.mp4",
                       linked, nested_link / "secret.mp4", in_root_link):
            _inject(nas, src=str(target), poster=str(outside_poster))
            response = client.get("/media/video/poison")
            assert response.status_code == 403
            assert b"outside-secret" not in response.content
            assert client.get("/media/poster/poison").status_code == 403
            assert client.get("/media/poster-by-match/poison-match").status_code == 403
        for endpoint in ("/media/trash/secret.mp4", "/media/trash-poster/secret.mp4", "/media/anything/secret", "/media/video/poison/extra"):
            assert client.get(endpoint).status_code == 403
        for endpoint in ("/config.json", "/library.json", "/data/overrides.json", "/static/%2e%2e/config.json"):
            assert client.get(endpoint).status_code == 404
        assert client.get("/api/trash").json()["items"] == []


def test_health_status_and_detail_do_not_expose_private_metadata(nas):
    with TestClient(nas.app) as client:
        identity = _first_video(client)["id"]
        nas.core._state["index"]["meta"] = {"databases": [{"ident": "private-account", "path": "/private/database"}]}
        nas.core._find(identity)["openid"] = "private-account"
        health = client.get("/health")
        assert health.status_code == 200
        assert set(health.json()) == {"ok", "scanning", "scans", "videos"}
        assert health.json()["ok"] is True and health.json()["videos"] == 1
        assert "private" not in health.text and str(nas.data) not in health.text
        status = client.get("/api/status")
        assert "private-account" not in status.text and "/private/database" not in status.text
        detail = client.get(f"/api/video/{identity}").json()
        assert "src" not in detail and "poster" not in detail and "openid" not in detail
        assert detail["id"] == identity


def test_concurrent_ensure_build_and_rebuild_are_coalesced_without_lost_state(nas, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    with TestClient(nas.app) as client:
        identity = _first_video(client)["id"]
        original = nas.core.library.build

        def blocked(root, existing=None):
            calls.append(root)
            entered.set()
            assert release.wait(5), "test did not release the synthetic scanner"
            return original(root, existing)

        monkeypatch.setattr(nas.core.library, "build", blocked)
        executor = ThreadPoolExecutor(max_workers=10)
        try:
            first = client.post("/api/rebuild", json={})
            assert first.status_code == 200 and first.json()["scheduled"] is True
            assert entered.wait(2)
            cached = nas.core.ensure_index()
            rebuilds = [executor.submit(client.post, "/api/rebuild", json={}) for _ in range(6)]
            forced = executor.submit(nas.core.ensure_index, True)
            direct = executor.submit(nas.core.build_index, str(nas.media))
            responses = [future.result(timeout=2) for future in rebuilds]
            assert all(response.status_code == 200 and response.json()["scheduled"] is False for response in responses)
            with nas.adapter.controller.condition:
                assert nas.adapter.controller.condition.wait_for(
                    lambda: nas.adapter.controller.coalesced >= 8, timeout=2)
            assert nas.core.ensure_index() is cached
            # Mutable state remains usable while the expensive importer runs.
            assert client.post(f"/api/video/{identity}/fav", json={}).json()["fav"] is True
            tag = client.post("/api/tags", json={"name": "During scan"}).json()
            assert client.post("/api/batch/tag", json={"ids": [identity], "tagIds": [tag["id"]]}).status_code == 200
            release.set()
            assert forced.result(timeout=5) is not None
            assert direct.result(timeout=5) is not None
            _wait_idle(nas, completed=2)
            assert len(calls) == 1
            item = _first_video(client)
            assert item["fav"] is True and item["tags"] == [tag["id"]]
            health = client.get("/health").json()
            assert health["scans"]["coalesced"] >= 6
            assert health["scans"]["failed"] == 0
            assert not list(nas.data.glob("*.tmp"))
            assert json.loads((nas.data / "library.json").read_text())["records"]
        finally:
            release.set()
            executor.shutdown(wait=True)


def test_failed_scan_retains_cache_and_does_not_leak_error_text(nas, monkeypatch):
    with TestClient(nas.app) as client:
        cached = nas.core.ensure_index()

        def failed(*args, **kwargs):
            raise RuntimeError("private-account /private/metadata secret")

        monkeypatch.setattr(nas.core.library, "build", failed)
        assert client.post("/api/rebuild", json={}).status_code == 200
        _wait_idle(nas, failed=1)
        assert nas.core.ensure_index() is cached
        assert client.get("/api/videos").status_code == 200
        health = client.get("/health")
        assert health.json()["ok"] is True and health.json()["scans"]["failed"] == 1
        status = client.get("/api/status")
        assert status.json()["error"] == "scan_failed"
        assert "private-account" not in status.text and "secret" not in health.text


def test_data_lock_rejects_a_second_worker(nas):
    with TestClient(nas.app):
        other = nas.adapter.ScanCoordinator()
        with pytest.raises(RuntimeError, match="one worker"):
            other.prepare()
        assert other.lock_fd is None


def test_no_unknown_write_or_upload_terminal_api(nas):
    with TestClient(nas.app) as client:
        for path in ("/api/upload", "/api/terminal", "/api/execute", "/api/future-write"):
            assert client.post(path, json={}).status_code == 403
