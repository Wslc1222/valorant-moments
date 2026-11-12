"""Only synthetic JSON/AES databases and tiny placeholder media; no NAS access."""
import hashlib
import json
import os
import string
from pathlib import Path

import pytest
from Crypto.Cipher import AES

import datasource
import library
import nas_archive

ROOT = r'D:\游戏 时刻\ACLOS-HIGHLIGHT'
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _normal_root(value):
    if value.startswith('\\\\?\\'):
        value = value[4:]
    return value.translate(_ASCII_LOWER)


def _media(root, relative):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'synthetic media placeholder, not a video')
    return path


def _video(relative, *, root=ROOT, ident='same-client-id', poster='', **extra):
    obj = {'video_id': ident, 'video_src': root + '\\' + relative.replace('/', '\\'),
           'video_poster': root + '\\' + poster.replace('/', '\\') if poster else '',
           'video_duration': 3500, 'video_size': 40, 'video_type': 'Kill',
           'video_resolution': '1920\rx1080\r', 'video_time': 1700000000000}
    obj.update(extra)
    return obj


def _match(ident, videos, *, stats=None, timestamp=1700000000000):
    return {'matches_id': ident, 'matches_time': timestamp,
            'agent': {'agent_name': 'Jett'}, 'map': {'map_name': 'Synthetic Map'},
            'stats': stats or {}, 'videos': videos}


def _archive(root, records, *, kind='WonderfulDb', account='SyntheticAccountABC',
             source=ROOT, resolved=None, source_key=None, manifest_updates=None):
    key_length = 32 if kind == 'WonderfulDb' else 16
    digest = hashlib.sha256(account.encode('utf-8')).hexdigest()
    key, iv = digest[:key_length].encode('ascii'), digest[:16].encode('ascii')
    obj = {'key_wonderful_list_' + account: records}
    plaintext = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    pad = 16 - len(plaintext) % 16
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(plaintext + bytes([pad]) * pad).hex().encode('ascii')
    sha = hashlib.sha256(ciphertext).hexdigest()
    source_key = source_key or hashlib.sha256(_normal_root(source).encode('utf-8')).hexdigest()[:32]
    snapshot = root / '.valorant-moments' / 'snapshots' / source_key / sha
    database = snapshot / kind / account
    database.parent.mkdir(parents=True, exist_ok=True)
    database.write_bytes(ciphertext)
    manifest = {'schema': nas_archive.SCHEMA, 'source_root': _normal_root(source),
                'resolved_source_root': _normal_root(resolved or source),
                'database_source': r'C:\Synthetic\AppData\ACLOS' + '\\' + kind + '\\' + account,
                'database_kind': kind, 'database_file': kind + '/' + account,
                'database_sha256': sha, 'database_size': len(ciphertext),
                'video_root_relative': '.', 'path_style': 'windows'}
    manifest.update(manifest_updates or {})
    manifest_path = snapshot / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    return snapshot, database, manifest_path, manifest


def _videos(matches):
    return [video for match in matches for video in match['videos']]


def _errors(meta):
    return '\n'.join(error['error'] for error in meta['errors'])


@pytest.fixture(autouse=True)
def _nas_mode(monkeypatch):
    monkeypatch.setenv('VM_NAS_MODE', '1')


@pytest.mark.parametrize('kind', ['WonderfulDb', 'WeGameWonderfulDb'])
def test_real_aes_both_kinds_original_account_filename(tmp_path, kind):
    path = _media(tmp_path, 'Account/record/Round/Ace.mp4')
    poster = _media(tmp_path, 'Account/record/Round/Cover.JPEG')
    # Both source spellings are Windows case-insensitive, including extended prefix.
    video = _video('ACCOUNT/RECORD/ROUND/ACE.MP4', root='\\\\?\\' + ROOT,
                   poster='ACCOUNT/RECORD/ROUND/COVER.JPEG')
    _, database, _, _ = _archive(tmp_path, [_match('m1', [video])], kind=kind)
    matches, meta = datasource.load_library(tmp_path)
    assert not meta['errors']
    assert meta['archive'] is True
    assert meta['databases'][0]['kind'] == ('full' if kind == 'WonderfulDb' else 'wegame')
    assert meta['databases'][0]['ident'] == 'SyntheticAccountABC'
    assert Path(_videos(matches)[0]['src']) == path
    assert Path(_videos(matches)[0]['poster']) == poster
    assert datasource.try_decrypt(database, 'SyntheticAccountABC')
    assert datasource.try_decrypt(database, 'syntheticaccountabc') is None
    index = library.build(str(tmp_path))
    assert index['mode'] == 'archive'
    assert index['records'][0]['agentEn'] == 'Jett'
    assert len(index['records']) == 1


def test_resolved_root_alias_and_unc_extended_prefix(tmp_path):
    path = _media(tmp_path, '账号 空格/record/clip.mp4')
    resolved = r'\\SyntheticServer\共享 中文\时刻'
    src = '\\\\?\\UNC\\SyntheticServer\\共享 中文\\时刻\\账号 空格\\RECORD\\clip.mp4'
    video = _video('unused')
    video['video_src'] = src
    _archive(tmp_path, [_match('alias', [video])], resolved=resolved)
    matches, meta = datasource.load_library(tmp_path)
    assert not meta['errors']
    assert Path(_videos(matches)[0]['src']) == path


def test_checked_ciphertext_is_not_reopened_by_decryptor(tmp_path, monkeypatch):
    _media(tmp_path, 'record/clip.mp4')
    _archive(tmp_path, [_match('m', [_video('record/clip.mp4')])])
    monkeypatch.setattr(datasource, '_read_hex', lambda *args: pytest.fail('validated database reopened'))
    matches, meta = datasource.load_library(tmp_path)
    assert not meta['errors']
    assert len(_videos(matches)) == 1


def test_raw_sha_corruption_rejected_before_decryption(tmp_path, monkeypatch):
    _, database, _, _ = _archive(tmp_path, [_match('m', [_video('a.mp4')])])
    raw = database.read_bytes()
    database.write_bytes((b'0' if raw[:1] != b'0' else b'1') + raw[1:])
    monkeypatch.setattr(datasource, 'try_decrypt', lambda *args, **kwargs: pytest.fail('corrupt bytes decrypted'))
    matches, meta = datasource.load_library(tmp_path)
    assert matches == []
    assert 'database_sha256 mismatch' in _errors(meta)


def test_missing_manifest_reported_and_fallback_still_visible(tmp_path):
    _media(tmp_path, 'record/new.mp4')
    _, _, manifest, _ = _archive(tmp_path, [_match('m', [_video('record/new.mp4')])])
    manifest.unlink()
    matches, meta = datasource.load_library(tmp_path)
    assert matches == []
    assert 'missing manifest.json' in _errors(meta)
    index = library.build(str(tmp_path))
    assert index['mode'] == 'archive'
    assert len(index['records']) == 1
    assert index['records'][0]['agentEn'] == ''


@pytest.mark.parametrize('updates, expected', [
    ({'schema': 'other/v1'}, 'schema'),
    ({'database_size': 1}, 'database_size'),
    ({'database_size': True}, 'database_size'),
    ({'database_size': 64 * 1024 * 1024 + 1}, '64 MiB'),
    ({'database_sha256': '0' * 64}, 'snapshot hash'),
    ({'database_kind': 'full'}, 'database_kind'),
    ({'database_kind': []}, 'database_kind'),
    ({'database_file': '../WonderfulDb/SyntheticAccountABC'}, 'database_file'),
    ({'database_file': 'WonderfulDb/../SyntheticAccountABC'}, 'database_file'),
    ({'database_file': 'WeGameWonderfulDb/SyntheticAccountABC'}, 'database_file'),
    ({'database_file': 'WonderfulDb/OtherAccount'}, 'database_file'),
    ({'database_file': '/WonderfulDb/SyntheticAccountABC'}, 'database_file'),
    ({'database_file': r'WonderfulDb\SyntheticAccountABC'}, 'database_file'),
    ({'database_source': r'C:\Synthetic\WonderfulDb\..\SyntheticAccountABC'}, 'unsafe path'),
    ({'source_root': r'D:\good\..\bad'}, 'unsafe path'),
    ({'resolved_source_root': 'relative/root'}, 'absolute Windows'),
    ({'path_style': 'posix'}, 'path_style'),
    ({'video_root_relative': '..'}, 'video_root_relative'),
])
def test_manifest_contract_and_database_scope_rejected(tmp_path, updates, expected):
    _archive(tmp_path, [_match('m', [_video('clip.mp4')])], manifest_updates=updates)
    matches, meta = datasource.load_library(tmp_path)
    assert matches == []
    assert expected in _errors(meta)


def test_manifest_limit_and_actual_database_limit(tmp_path, monkeypatch):
    _, _, manifest, _ = _archive(tmp_path, [_match('m', [_video('clip.mp4')])])
    manifest.write_bytes(b' ' * (nas_archive.MAX_MANIFEST_SIZE + 1))
    matches, meta = datasource.load_library(tmp_path)
    assert not matches
    assert 'size limit' in _errors(meta)

    other = tmp_path / 'other'
    other.mkdir()
    _archive(other, [_match('m', [_video('clip.mp4')])])
    monkeypatch.setattr(nas_archive, 'MAX_DATABASE_SIZE', 128)
    matches, meta = datasource.load_library(other)
    assert not matches
    assert 'limit' in _errors(meta)


def test_all_historical_snapshots_merge_and_full_stats_win(tmp_path):
    for name in ('old.mp4', 'common.mp4', 'new.mp4', 'extra.mp4'):
        _media(tmp_path, 'record/' + name)
    old = _match('old-match', [_video('record/old.mp4')])
    full = _match('common-match', [_video('record/common.mp4')],
                  stats={'kills': 12, 'deaths': 0, 'has_won': False,
                         'rounds_won': 0, 'rounds_lost': 13, 'detail': {'headshots': 4}})
    _archive(tmp_path, [old, full])
    # Later full snapshot omits old-match and some detailed fields entirely.
    _archive(tmp_path, [_match('new-match', [_video('record/new.mp4')]),
                        _match('common-match', [_video('record/common.mp4')], stats={'kills': 12})])
    _archive(tmp_path, [_match('common-match', [_video('record/extra.mp4')],
                              stats={'kills': 99, 'deaths': 99, 'assists': 3,
                                     'has_won': True, 'detail': {'assists': 3}})],
             kind='WeGameWonderfulDb')
    matches, meta = datasource.load_library(tmp_path)
    assert not meta['errors']
    assert len(meta['databases']) == 3
    assert len(matches) == 3
    assert len(_videos(matches)) == 4
    common = next(m for m in matches if m['original_matches_id'] == 'common-match')
    assert common['kind'] == 'full'
    assert common['stats']['kills'] == 12
    assert common['stats']['deaths'] == 0
    assert common['stats']['rounds_won'] == 0
    assert common['stats']['has_won'] is False
    assert common['stats']['assists'] == 3
    assert common['stats']['detail'] == {'headshots': 4, 'assists': 3}


def test_ids_never_collide_across_accounts_directories_or_missing_client_id(tmp_path):
    for relative in ('one/clip.mp4', 'two/clip.mp4', 'two/other.mp4'):
        _media(tmp_path, relative)
    _archive(tmp_path, [_match('same-match', [_video('one/clip.mp4')])], account='AccountOne')
    _archive(tmp_path, [_match('same-match', [_video('two/clip.mp4'),
                                            _video('two/other.mp4', ident='')])], account='AccountTwo')
    index = library.build(str(tmp_path))
    assert len(index['records']) == 3
    assert len({r['id'] for r in index['records']}) == 3
    assert len({r['match'] for r in index['records']}) == 2
    assert all(r['id'].startswith('nas-') for r in index['records'])


def test_same_actual_file_across_sources_can_merge(tmp_path):
    _media(tmp_path, 'record/clip.mp4')
    _archive(tmp_path, [_match('m', [_video('record/clip.mp4')])])
    alias = r'E:\SyntheticAlias'
    _archive(tmp_path, [_match('m', [_video('record/clip.mp4', root=alias, ident='different-id')])],
             source=alias, account='OtherAccount')
    matches, meta = datasource.load_library(tmp_path)
    assert not meta['errors']
    assert len(meta['databases']) == 2
    assert len(_videos(matches)) == 1
    assert len(library.build(str(tmp_path))['records']) == 1


def test_fallback_additions_mtime_and_metadata_upgrade_keep_state_id(tmp_path):
    first = _media(tmp_path, 'record/round/clip.mp4')
    initial = library.build(str(tmp_path))
    assert initial['mode'] == 'scan'
    saved = initial['records'][0]
    saved.update({'fav': True, 'tags': ['synthetic-tag'], 'rename': 'my clip', 'hidden': True})
    initial_id = saved['id']
    os.utime(first, (1800000000, 1800000000))
    assert library.build(str(tmp_path))['records'][0]['id'] == initial_id

    _archive(tmp_path, [_match('m', [_video('RECORD/ROUND/CLIP.MP4')])])
    _media(tmp_path, 'record/other/clip.mp4')  # Added to disk, absent from all snapshots.
    index = library.build(str(tmp_path), existing=initial)
    assert index['mode'] == 'archive'
    assert len(index['records']) == 2
    upgraded = next(r for r in index['records'] if r['src'] == str(first))
    assert upgraded['id'] == initial_id
    assert upgraded['agentEn'] == 'Jett'
    assert upgraded['fav'] is True and upgraded['hidden'] is True
    assert upgraded['tags'] == ['synthetic-tag']
    assert upgraded['name'] == 'my clip'
    assert upgraded['rename'] == 'my clip'
    assert len({r['id'] for r in index['records']}) == 2


@pytest.mark.parametrize('src', [
    ROOT + r'\record\..\clip.mp4',
    ROOT + r'\record\E:\clip.mp4',
    ROOT + r'\record\clip.mp4:alternate',
    ROOT + r'2\record\clip.mp4',
    r'E:\elsewhere\record\clip.mp4',
    r'D:record\clip.mp4',
    r'\record\clip.mp4',
    ROOT + r'\.metadata\clip.mp4',
    ROOT + r'\missing\.valorant-moments\clip.mp4',
    ROOT + r'\record.partial\clip.mp4',
])
def test_windows_scope_traversal_drive_and_metadata_paths_rejected(tmp_path, src):
    video = _video('record/clip.mp4')
    video['video_src'] = src
    _archive(tmp_path, [_match('m', [video])])
    matches, meta = datasource.load_library(tmp_path)
    assert not _videos(matches)
    assert 'invalid video path' in _errors(meta)


def test_poster_scope_failure_does_not_drop_safe_video(tmp_path):
    path = _media(tmp_path, 'record/clip.mp4')
    video = _video('record/clip.mp4')
    video['video_poster'] = r'C:\outside\cover.jpeg'
    _archive(tmp_path, [_match('m', [video])])
    matches, meta = datasource.load_library(tmp_path)
    assert Path(_videos(matches)[0]['src']) == path
    assert _videos(matches)[0]['poster'] == ''
    assert 'invalid poster path' in _errors(meta)


def test_component_casefold_is_unique_and_exact_wins(tmp_path, monkeypatch):
    path = _media(tmp_path, 'record/Exact.mp4')
    _archive(tmp_path, [_match('m', [_video('RECORD/exact.MP4')])])
    matches, meta = datasource.load_library(tmp_path)
    assert not meta['errors']
    assert Path(_videos(matches)[0]['src']) == path

    original = nas_archive._Tree.names

    def fake_collision(self, parts):
        names = original(self, parts)
        if tuple(parts) == ('record',):
            return sorted(set(names + ['EXACT.MP4']))
        return names

    # Synthetic listing makes this assertion portable to case-insensitive macOS.
    monkeypatch.setattr(nas_archive._Tree, 'names', fake_collision)
    matches, meta = datasource.load_library(tmp_path)
    assert not _videos(matches)
    assert 'ambiguous casefold' in _errors(meta)
    with nas_archive._Tree(tmp_path) as tree:
        assert tree.resolve_media(('record', 'Exact.mp4')) == str(path)


def test_no_basename_search_between_directories(tmp_path):
    path = _media(tmp_path, 'other/clip.mp4')
    _archive(tmp_path, [_match('m', [_video('absent/clip.mp4')])])
    index = library.build(str(tmp_path))
    records = index['records']
    assert len(records) == 2
    known = next(r for r in records if r['agentEn'] == 'Jett')
    assert known['src'] != str(path)
    assert known['exists'] is False
    assert next(r for r in records if r['src'] == str(path))['agentEn'] == ''


@pytest.mark.parametrize('what', ['database', 'manifest', 'kind_directory', 'snapshot_directory'])
def test_archive_symlinks_are_never_followed(tmp_path, what, monkeypatch):
    root = tmp_path / 'media'
    root.mkdir()
    snapshot, database, manifest, _ = _archive(root, [_match('m', [_video('clip.mp4')])])
    if what in ('database', 'manifest'):
        target = database if what == 'database' else manifest
        copy = tmp_path / ('synthetic-' + what)
        copy.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(copy)
    else:
        target = database.parent if what == 'kind_directory' else snapshot
        copy = tmp_path / ('synthetic-' + what)
        target.rename(copy)
        target.symlink_to(copy, target_is_directory=True)
    monkeypatch.setattr(datasource, 'try_decrypt', lambda *args, **kwargs: pytest.fail('linked database decrypted'))
    matches, meta = datasource.load_library(root)
    assert not matches
    assert meta['errors']
    assert any(word in _errors(meta) for word in ('symlink', 'boundary'))


@pytest.mark.parametrize('link_type', ['directory', 'file', 'internal_file'])
def test_metadata_media_links_are_rejected(tmp_path, link_type):
    root = tmp_path / 'media'
    root.mkdir()
    outside = _media(tmp_path, 'outside/clip.mp4')
    if link_type == 'directory':
        (root / 'linked').symlink_to(outside.parent, target_is_directory=True)
        relative = 'linked/clip.mp4'
    else:
        target = _media(root, 'real/clip.mp4') if link_type == 'internal_file' else outside
        (root / 'linked.mp4').symlink_to(target)
        relative = 'linked.mp4'
    _archive(root, [_match('m', [_video(relative)])])
    matches, meta = datasource.load_library(root)
    assert not _videos(matches)
    assert 'invalid video path' in _errors(meta)
    assert 'symlink' in _errors(meta)


def test_safe_scan_exclusions_links_record_and_same_name_ids(tmp_path):
    root = tmp_path / 'media'
    root.mkdir()
    good = {_media(root, 'record/a/same.mp4'), _media(root, 'record/b/same.mp4')}
    _media(root, '.metadata/hidden.mp4')
    _media(root, '.valorant-moments/hidden.mp4')
    _media(root, 'inflight.partial/hidden.mp4')
    _media(root, 'record/clip.mp4.partial')
    outside = _media(tmp_path, 'outside/secret.mp4')
    (root / 'file-link.mp4').symlink_to(outside)
    (root / 'directory-link').symlink_to(outside.parent, target_is_directory=True)
    (root / 'internal-link.mp4').symlink_to(next(iter(good)))
    errors = []
    matches = datasource.scan_files(root, errors=errors)
    videos = _videos(matches)
    assert {Path(v['src']) for v in videos} == good
    assert len({v['video_id'] for v in videos}) == 2
    assert errors
    assert all(v['src'].startswith(str(root) + os.sep) for v in videos)


def test_partial_snapshot_ignored_without_opening(tmp_path, monkeypatch):
    base = tmp_path / '.valorant-moments' / 'snapshots'
    (base / ('a' * 32 + '.partial')).mkdir(parents=True)
    (base / ('b' * 32) / ('c' * 64 + '.partial')).mkdir(parents=True)
    monkeypatch.setattr(datasource, 'try_decrypt', lambda *args, **kwargs: pytest.fail('partial decrypted'))
    matches, meta = datasource.load_library(tmp_path)
    assert not matches and not meta['errors']


def test_media_directory_budget_accepts_four_thousand_clips_and_covers(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    @contextmanager
    def synthetic_entries(_directory):
        names = [f'clip-{i:05d}.{ext}' for i in range(4000) for ext in ('mp4', 'jpeg')]
        yield iter(SimpleNamespace(name=name) for name in names)

    with nas_archive._Tree(tmp_path) as tree:
        monkeypatch.setattr(nas_archive.os, 'scandir', synthetic_entries)
        assert len(tree.names(())) == 8000


def test_directory_entry_limit_is_diagnostic(tmp_path, monkeypatch):
    _archive(tmp_path, [_match('m', [_video('clip.mp4')])])
    _media(tmp_path, 'extra-1.mp4')
    _media(tmp_path, 'extra-2.mp4')
    monkeypatch.setattr(nas_archive, 'MAX_DIRECTORY_ENTRIES', 1)
    errors = []
    assert datasource.scan_files(tmp_path, errors=errors) == []
    assert 'entry limit' in '\n'.join(e['error'] for e in errors)


def test_nas_does_not_search_ancestor_bare_databases(tmp_path, monkeypatch):
    database = tmp_path / 'ACLOS' / 'WonderfulDb' / 'SyntheticAccount'
    database.parent.mkdir(parents=True)
    database.write_bytes(b'0' * 256)
    root = tmp_path / 'ACLOS' / 'media'
    root.mkdir()
    assert datasource.find_db_files(root) == []
    monkeypatch.setenv('VM_NAS_MODE', '0')
    assert datasource.find_db_files(root)[0]['path'] == str(database)
