# -*- coding: utf-8 -*-
"""Single-worker, LAN-only NAS adapter for the upstream FastAPI application.

Run ``python nas_server.py``; no browser is opened. VM_MEDIA_ROOT (default
/media) is immutable through the API. VM_DATA_DIR (default /data) owns *all*
writable state. VM_SCAN_INTERVAL is a positive number of seconds (default 120).
TZ defaults to Asia/Shanghai. Importers see VM_NAS_MODE=1 before core is imported.

There is deliberately no login, upload, shell, media deletion, or export API.
Do not publish this service to the Internet. Use only a trusted home LAN, a
read-only media bind mount, and a separate writable data bind mount. Never run
multiple Uvicorn workers; an advisory data-directory lock also rejects them.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import errno
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from urllib.parse import urlsplit

# The datasource module is imported transitively by core; set this FIRST.
os.environ["VM_NAS_MODE"] = "1"
os.environ.setdefault("TZ", "Asia/Shanghai")
if hasattr(time, "tzset"):
    time.tzset()

import app as core  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse, Response, StreamingResponse  # noqa: E402


DATA_DIR = Path(os.environ.get("VM_DATA_DIR", "/data")).expanduser().resolve()
MEDIA_ROOT = Path(os.environ.get("VM_MEDIA_ROOT", "/media")).expanduser().resolve()
SCAN_INTERVAL = float(os.environ.get("VM_SCAN_INTERVAL", "120"))
if not math.isfinite(SCAN_INTERVAL) or SCAN_INTERVAL <= 0:
    raise ValueError("VM_SCAN_INTERVAL must be a finite positive number")
if DATA_DIR == MEDIA_ROOT or DATA_DIR in MEDIA_ROOT.parents or MEDIA_ROOT in DATA_DIR.parents:
    raise ValueError("VM_DATA_DIR and VM_MEDIA_ROOT must be separate, non-nested directories")
_CODE_DIR = Path(core.BASE).resolve()
if MEDIA_ROOT == _CODE_DIR or MEDIA_ROOT in _CODE_DIR.parents:
    raise ValueError("VM_MEDIA_ROOT must not contain the application directory")

HOST, PORT = "0.0.0.0", 8765
_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
_POSTER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_PRIVATE_PARTS = {"wonderfuldb", "wegamewonderfuldb", ".metadata", ".valorant-moments", ".git", "__pycache__"}
_NOFOLLOW = os.O_NOFOLLOW | os.O_CLOEXEC
_logger = logging.getLogger(__name__)

# BASE intentionally remains the source directory: / and /static still serve
# the upstream frontend, but no writable path falls back to BASE.
for _name, _relative in {
    "CONFIG": "config.json", "INDEX": "library.json", "OVERRIDES": "overrides.json",
    "THUMBS": "thumbs", "TAGS_FILE": "tags.json", "TRASH_INDEX": "trash_index.json",
}.items():
    setattr(core, _name, str(DATA_DIR / _relative))

_state_lock = threading.RLock()
_original_apply_tags = core.apply_tags


def _read_state(path: str, default):
    """Never follow a state-file symlink, including stale imported state."""
    with _state_lock:
        try:
            fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                result = json.load(handle)
            return result if isinstance(result, type(default)) else default
        except (OSError, ValueError):
            return default


def _atomic_json(path: str, value):
    """Unique private temporary files + locking avoid fixed '.tmp' races."""
    with _state_lock:
        fd, temporary = tempfile.mkstemp(prefix=Path(path).name + ".", suffix=".tmp", dir=DATA_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                # mkstemp produced this exact, data-directory-local path.
                Path(temporary).unlink()


def _fixed_config(value=None):
    cfg = dict(value if isinstance(value, dict) else _read_state(core.CONFIG, {}))
    cfg.update(root=str(MEDIA_ROOT), host=HOST, port=PORT, open_browser=False,
               trashDir=str(DATA_DIR / "trash"), exportDir=str(DATA_DIR / "exports"))
    return cfg


def _save_config(value):
    _atomic_json(core.CONFIG, _fixed_config(value))


def _load_tags():
    value = _read_state(core.TAGS_FILE, {})
    if not isinstance(value.get("tags"), list) or not value["tags"]:
        value["tags"] = [dict(tag) for tag in core.DEFAULT_TAGS]
    if not isinstance(value.get("videoTags"), dict):
        value["videoTags"] = {}
    return value


def _apply_overrides(index, overrides):
    for record in index["records"]:
        override = overrides.get(record["id"]) or {}
        record["fav"] = bool(override.get("fav", False))
        record["hidden"] = bool(override.get("hidden", False))
        record["name"] = override.get("name") or Path(record["src"]).stem


def _save_overrides(value):
    with _state_lock:
        _atomic_json(core.OVERRIDES, value)
        if core._state["index"] is not None:
            # A route may still hold the previous index while a scan publishes.
            # Apply to the current index too, without touching media on disk.
            _apply_overrides(core._state["index"], value)


def _apply_tags(index):
    with _state_lock:
        return _original_apply_tags(index)


def _save_tags(value):
    with _state_lock:
        _atomic_json(core.TAGS_FILE, value)
        if core._state["index"] is not None:
            _apply_tags(core._state["index"])


def _require_root(root):
    if root in (None, ""):
        return
    if not isinstance(root, str) or Path(os.path.abspath(root)) != MEDIA_ROOT:
        raise PermissionError("the media root cannot be changed")


class ScanCoordinator:
    """One scan reservation shared by startup, ensure, manual and timer scans."""

    def __init__(self):
        self.condition = threading.Condition(_state_lock)
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.worker = None
        self.pending = False
        self.lock_fd = None
        self.attempts = 0
        self.completed = 0
        self.failed = 0
        self.coalesced = 0

    def prepare(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not MEDIA_ROOT.is_dir():
            raise RuntimeError("VM_MEDIA_ROOT must be an existing media directory")
        for name in ("thumbs", "trash"):
            directory = DATA_DIR / name
            if directory.is_symlink():
                raise RuntimeError("state directories must not be symlinks")
            directory.mkdir(exist_ok=True)
        self.lock_fd = os.open(DATA_DIR / ".nas-server.lock", os.O_CREAT | os.O_RDWR | _NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.lock_fd)
            self.lock_fd = None
            raise RuntimeError("VM_DATA_DIR is already in use; run exactly one worker") from None
        self.stopping.clear()
        self.wake.clear()
        self.pending = False
        _save_config(_fixed_config())

    def _reserve(self):
        # Caller holds condition's RLock. Set building before acknowledging an
        # API trigger, not later in the new thread (upstream has that race).
        if self.stopping.is_set():
            return False
        if core._state["building"]:
            self.coalesced += 1
            self.condition.notify_all()
            return False
        core._state["building"] = True
        core._state["last_error"] = None
        self.attempts += 1
        return True

    def _run_reserved(self):
        try:
            previous = core._state["index"]
            index = core.library.build(str(MEDIA_ROOT), previous)
            with self.condition:
                # Read overlays at publication time, so scanning cannot lose
                # favorites/tags/renames saved while the importer was running.
                _apply_overrides(index, core.load_overrides())
                core.apply_tags(index)
                _atomic_json(core.INDEX, index)
                core._state["index"] = index
                self.completed += 1
            return index
        except Exception as exc:
            with self.condition:
                self.failed += 1
                # No paths, accounts, metadata or exception text in HTTP status.
                core._state["last_error"] = "scan_failed"
            _logger.warning("NAS index scan failed (%s)", type(exc).__name__)
            raise
        finally:
            with self.condition:
                core._state["building"] = False
                self.condition.notify_all()

    def scan(self, root=None):
        _require_root(root)
        with self.condition:
            if core._state["building"]:
                self.coalesced += 1
                self.condition.notify_all()
                self.condition.wait_for(lambda: not core._state["building"])
                if core._state["index"] is None:
                    raise RuntimeError("index scan failed")
                return core._state["index"]
            if not self._reserve():
                return core._state["index"]
        return self._run_reserved()

    def ensure(self, force=False):
        with self.condition:
            cached = core._state["index"]
            if cached is not None and not force:
                return cached  # No traversal, reconciliation, or per-file stat.
        return self.scan()

    def request(self, root=None):
        _require_root(root)
        with self.condition:
            if not self._reserve():
                return False
            self.pending = True
            self.wake.set()
            return True

    def start_worker(self):
        self.worker = threading.Thread(target=self._worker, name="nas-index-scanner", daemon=True)
        self.worker.start()

    def _worker(self):
        while not self.stopping.is_set():
            self.wake.wait(SCAN_INTERVAL)
            with self.condition:
                # Clear under the reservation lock and inspect pending even if
                # wait() timed out just before an API trigger set the event.
                self.wake.clear()
                if self.stopping.is_set():
                    break
                if self.pending:
                    self.pending = False
                    reserved = True
                else:
                    reserved = self._reserve()
            if reserved:
                try:
                    self._run_reserved()
                except Exception:
                    # Retain the previous good cache; retry at the next interval.
                    pass

    def stop(self):
        self.stopping.set()
        self.wake.set()
        if self.worker is not None:
            self.worker.join()  # Do not leave a to_thread scan writing after exit.
            self.worker = None
        with self.condition:
            if self.pending:
                self.pending = False
                core._state["building"] = False
                self.condition.notify_all()
            self.condition.wait_for(lambda: not core._state["building"])
        if self.lock_fd is not None:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            os.close(self.lock_fd)
            self.lock_fd = None

    def health(self):
        with self.condition:
            index = core._state["index"]
            return {"ok": index is not None, "scanning": core._state["building"],
                    "scans": {"attempted": self.attempts, "completed": self.completed,
                              "failed": self.failed, "coalesced": self.coalesced},
                    "videos": len(index["records"]) if index else 0}


controller = ScanCoordinator()
core._state.update(index=None, building=False, last_error=None, lock=_state_lock)
core._RECS = {}
core.load_config = _fixed_config
core.save_config = _save_config
core.load_overrides = lambda: _read_state(core.OVERRIDES, {})
core.save_overrides = _save_overrides
core.load_tags = _load_tags
core.save_tags = _save_tags
core.apply_tags = _apply_tags
core.load_trash_index = lambda: _read_state(core.TRASH_INDEX, {})
core.save_trash_index = lambda value: _atomic_json(core.TRASH_INDEX, value)
core._trash_dir = lambda: str(DATA_DIR / "trash")
core.ensure_index = controller.ensure
core.build_index = controller.scan
core.rebuild_async = controller.request


def _media_parts(path, extensions=None):
    if not isinstance(path, (str, os.PathLike)) or not os.path.isabs(path):
        raise PermissionError("media path is not allowed")
    candidate = Path(os.path.abspath(path))
    try:
        parts = candidate.relative_to(MEDIA_ROOT).parts
    except ValueError:
        raise PermissionError("media path is not allowed") from None
    if not parts or any(part.lower() in _PRIVATE_PARTS for part in parts):
        raise PermissionError("media path is not allowed")
    if extensions is not None and candidate.suffix.lower() not in extensions:
        raise PermissionError("not a supported media file")
    return parts


def _open_inside_media(path, *, directory=False, extensions=None):
    """Open each component relative to an fd, without following any symlink.

    A resolve/check followed by FileResponse/open is TOCTOU-prone. Component
    opens plus O_NOFOLLOW keep the actually streamed fd beneath the media root,
    even if an old index or a concurrently replaced link is malicious. In-root
    symlinks are intentionally rejected as well; copy real files into the mount.
    """
    if directory and Path(os.path.abspath(path)) == MEDIA_ROOT:
        parts = ()
    else:
        parts = _media_parts(path, extensions)
    fd = os.open(MEDIA_ROOT, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
    try:
        for number, part in enumerate(parts):
            flags = os.O_RDONLY | _NOFOLLOW
            if directory or number < len(parts) - 1:
                flags |= os.O_DIRECTORY
            else:
                # A poisoned index must not block the worker on a FIFO/device.
                flags |= os.O_NONBLOCK
            following = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = following
        info = os.fstat(fd)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise PermissionError("not a regular media file")
        return fd, info
    except BaseException:
        os.close(fd)
        raise


class MediaResponse(StreamingResponse):
    def __init__(self, fd, size, path, request):
        self.handle = os.fdopen(fd, "rb")
        start, end, status_code = 0, size - 1, 200
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
        requested = request.headers.get("range")
        if requested:
            if len(requested) > 128:
                self.handle.close()
                raise ValueError("invalid range")
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
            if not match or not any(match.groups()) or not size:
                self.handle.close()
                raise ValueError("invalid range")
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                suffix = int(last)
                if not suffix:
                    self.handle.close()
                    raise ValueError("invalid range")
                start = max(0, size - suffix)
            if start > end or start >= size:
                self.handle.close()
                raise ValueError("invalid range")
            status_code = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        length = max(0, end - start + 1)
        headers["Content-Length"] = str(length)

        def chunks():
            self.handle.seek(start)
            remaining = length
            while remaining:
                chunk = self.handle.read(min(512 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

        body = iter(()) if request.method == "HEAD" else chunks()
        super().__init__(body, status_code=status_code, media_type=core.guess_mime(path), headers=headers)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.handle.close()


def _origin(value, *, referer=False):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            return None
        if not referer and (parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        return parsed.scheme.lower(), parsed.hostname.lower(), port
    except ValueError:
        return None


def _same_origin_write(request):
    target = _origin(f"{request.scope.get('scheme', 'http')}://{request.headers.get('host', '')}")
    if target is None:
        return False
    for key in ("host", "origin", "referer", "sec-fetch-site", "content-type"):
        if len(request.headers.getlist(key)) > 1:
            return False
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    site = request.headers.get("sec-fetch-site")
    if origin is not None and _origin(origin) != target:
        return False
    if referer is not None and _origin(referer, referer=True) != target:
        return False
    if site is not None and site.lower() != "same-origin":
        return False
    # Header-less JSON clients (curl/health tooling) remain usable on the LAN.
    return True


def _is_json(request):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    return content_type == "application/json" or bool(re.fullmatch(r"application/[a-z0-9.+-]+\+json", content_type))


def _forbidden(message="NAS mode forbids this operation"):
    return JSONResponse({"error": message}, status_code=403)


def _browse(path):
    target = str(MEDIA_ROOT) if not path else path
    fd, _ = _open_inside_media(target, directory=True)
    try:
        directories = []
        for name in sorted(os.listdir(fd)):
            if name.startswith("$") or name.lower() in _PRIVATE_PARTS:
                continue
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                directories.append({"name": name, "path": str(Path(os.path.abspath(target)) / name)})
            if len(directories) >= 200:
                break
        current = Path(os.path.abspath(target))
        return {"path": str(current), "parent": "" if current == MEDIA_ROOT else str(current.parent),
                "dirs": directories, "drives": []}
    finally:
        os.close(fd)


class NASSafetyMiddleware:
    """Outer, fail-closed policy; upstream route handlers remain untouched."""

    def __init__(self, app):
        self.app = app
        self.write_lock = asyncio.Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def safe_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-content-type-options", b"nosniff"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                    (b"referrer-policy", b"same-origin"),
                    (b"x-frame-options", b"DENY"),
                ]
            await send(message)

        request = Request(scope, receive)
        path = scope["path"].rstrip("/") or "/"
        method = request.method
        dangerous = path in {"/api/batch/delete", "/api/trash/restore", "/api/trash/purge", "/api/broken/clean"}
        dangerous = dangerous or (path.startswith("/api/") and "export" in path.split("/"))
        if dangerous:
            response = _forbidden()
        elif method not in {"GET", "HEAD", "OPTIONS"}:
            allowed = (
                (method == "POST" and re.fullmatch(r"/api/video/[^/]+/(fav|hide|rename)", path))
                or (method == "POST" and path in {"/api/batch/fav", "/api/batch/hide", "/api/batch/tag", "/api/tags", "/api/rebuild"})
                or (method in {"PATCH", "DELETE"} and re.fullmatch(r"/api/tags/[^/]+", path))
            )
            if not allowed:
                response = _forbidden()
            elif not _same_origin_write(request):
                response = _forbidden("cross-origin writes are forbidden")
            elif (method != "DELETE" or request.headers.get("content-length", "0") != "0"
                  or "transfer-encoding" in request.headers) and not _is_json(request):
                response = JSONResponse({"error": "application/json is required"}, status_code=415)
            elif path == "/api/rebuild":
                response = await self.rebuild(request)
            else:
                # Protect whole read/modify/save transactions across async body
                # reads. Scan publication additionally uses the threading lock.
                async with self.write_lock:
                    return await self.app(scope, receive, safe_send)
        elif method in {"GET", "HEAD"} and path == "/health":
            response = JSONResponse(controller.health())
        elif method in {"GET", "HEAD"} and path == "/api/browse":
            try:
                response = JSONResponse(await asyncio.to_thread(_browse, request.query_params.get("path", "")))
            except (PermissionError, ValueError):
                response = _forbidden("directory is outside the media root")
            except OSError as exc:
                response = _forbidden("directory links are forbidden") if exc.errno in {errno.ELOOP, errno.ENOTDIR} else JSONResponse({"error": "directory unavailable"}, status_code=404)
        elif method in {"GET", "HEAD"} and path == "/api/status":
            index = core.ensure_index()
            response = JSONResponse({"building": core._state["building"], "error": core._state["last_error"],
                                     "root": str(MEDIA_ROOT), "generated": index.get("generated"),
                                     "mode": index.get("mode"), "meta": {"count": len(index["records"])},
                                     "stats": index.get("stats"), "elapsed": index.get("elapsed"),
                                     "disk": core._disk_info(str(MEDIA_ROOT)), "nasMode": True})
        elif method in {"GET", "HEAD"} and path == "/api/trash":
            # NAS media is read-only; never expose /data/trash or its filenames.
            response = JSONResponse({"dir": "", "count": 0, "items": [], "totalSize": 0, "readOnly": True})
        elif method in {"GET", "HEAD"} and path == "/api/broken":
            items = []
            for record in core.ensure_index()["records"]:
                if not record.get("exists", True) or record.get("size", 0) < 1024:
                    items.append({key: record.get(key) for key in ("id", "name", "date", "agent", "type", "hidden")}
                                 | {"sizeText": core.fmt_bytes(record.get("size", 0)), "reason": "文件缺失或异常小（索引扫描结果）"})
            response = JSONResponse({"count": len(items), "items": items})
        elif method in {"GET", "HEAD"} and re.fullmatch(r"/api/video/[^/]+", path):
            record = core._find(path.rsplit("/", 1)[1])
            # Public display fields only, never raw openid/src/database metadata.
            response = JSONResponse(core._item(record)) if record else JSONResponse({"error": "not found"}, status_code=404)
        elif path.startswith("/media/"):
            response = self.media(path, request)
        else:
            return await self.app(scope, receive, safe_send)
        await response(scope, receive, safe_send)

    async def rebuild(self, request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError
            root = body.get("root", "")
            if root is None:
                root = ""
            if not isinstance(root, str):
                raise ValueError
            _require_root(root.strip())
        except PermissionError:
            return _forbidden("the media root cannot be changed")
        except (ValueError, TypeError):
            return JSONResponse({"error": "a JSON object with an optional root is required"}, status_code=400)
        scheduled = controller.request()
        return JSONResponse({"ok": True, "root": str(MEDIA_ROOT), "scheduled": scheduled,
                             "building": core._state["building"]})

    def media(self, path, request):
        if request.method not in {"GET", "HEAD"}:
            return _forbidden()
        match = re.fullmatch(r"/media/(video|poster|poster-by-match)/([^/]+)", path)
        if not match:
            return _forbidden("only indexed media under the media root is available")
        kind, identity = match.groups()
        if kind == "poster-by-match":
            record = next((item for item in core.ensure_index()["records"]
                           if item["match"] == identity and item.get("poster")), None)
            target = record.get("poster") if record else None
        else:
            record = core._find(identity)
            if record is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            target = record.get("src" if kind == "video" else "poster")
        if not target:
            return Response(status_code=204) if kind != "video" else _forbidden("media path is not allowed")
        extensions = _VIDEO_EXTENSIONS if kind == "video" else _POSTER_EXTENSIONS
        try:
            fd, info = _open_inside_media(target, extensions=extensions)
        except (PermissionError, ValueError):
            return _forbidden("media path is not allowed")
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                return _forbidden("media links are forbidden")
            return JSONResponse({"error": "media unavailable"}, status_code=404)
        try:
            return MediaResponse(fd, info.st_size, target, request)
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{info.st_size}"})


_original_lifespan = core.app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with _original_lifespan(application):
        try:
            await asyncio.to_thread(controller.prepare)
            # Startup always scans; no request can serve a stale persisted index
            # instead of validating the currently mounted media library.
            await asyncio.to_thread(controller.scan)
            controller.start_worker()
            yield
        finally:
            await asyncio.to_thread(controller.stop)


app = core.app
app.router.lifespan_context = lifespan
app.add_middleware(NASSafetyMiddleware)


if __name__ == "__main__":
    core.uvicorn.run(app, host=HOST, port=PORT, workers=1, proxy_headers=False, log_level="info")
