# -*- coding: utf-8 -*-
"""Read-only importer for uploader 0.2 archives (no runtime state is stored here).

Public entry points: has_snapshots(root), load_library(root), scan_files(root),
and video_id(root, path). Paths returned to the existing player are native absolute
paths; IDs hash their *case-sensitive relative paths*, not account IDs or mtimes.
"""
import hashlib
import json
import math
import ntpath
import os
import re
import stat
from contextlib import contextmanager

SCHEMA = 'valorant-moments-archive/v1'
KINDS = {'WonderfulDb': 'full', 'WeGameWonderfulDb': 'wegame'}
MAX_DATABASE_SIZE = 64 * 1024 * 1024
MAX_MANIFEST_SIZE = 64 * 1024
# A flat media folder may contain thousands of clips plus a cover per clip.
# Keep a bounded listing without rejecting ordinary 4k-video libraries.
MAX_DIRECTORY_ENTRIES = 50000
MAX_TOTAL_ENTRIES = 100000
MAX_METADATA_RECORDS = 200000
MAX_DEPTH = 64
VIDEO_EXT = {'.mp4', '.mov', '.mkv', '.webm', '.m4v'}
EXCLUDED = {'.metadata', '.valorant-moments'}
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')


class ArchiveError(ValueError):
    pass


def _error(errors, path, error):
    errors.append({'path': os.fspath(path), 'error': str(error)})


def _component(name):
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or any(c in name for c in ('/', '\\', ':', '\x00'))
            or name.casefold().endswith('.partial')):
        raise ArchiveError('unsafe path component or .partial file')
    return name


def _is_link(st, path):
    return stat.S_ISLNK(st.st_mode) or (
        hasattr(os.path, 'isjunction') and os.path.isjunction(path))


class _Tree:
    """Bounded traversal; POSIX opens each component with O_NOFOLLOW + dir_fd.

    The path-based fallback is for the original Windows desktop application.
    NAS/POSIX reads use pinned descriptors, including the bytes passed to AES.
    """
    def __init__(self, root):
        original = os.path.abspath(os.fspath(root))
        st = os.lstat(original)
        if _is_link(st, original) or not stat.S_ISDIR(st.st_mode):
            raise ArchiveError('MEDIA_ROOT must be a real directory, not a symlink')
        self.root = os.path.realpath(original)
        self.cache = {}
        self.total = 0
        self.fd = None
        self.flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'):
            self.fd = os.open(self.root, self.flags)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)

    def path(self, parts):
        if len(parts) > MAX_DEPTH:
            raise ArchiveError('directory depth limit exceeded')
        for part in parts:
            _component(part)
        path = os.path.join(self.root, *parts)
        if os.path.commonpath((self.root, os.path.realpath(path))) != self.root:
            raise ArchiveError('path escapes real MEDIA_ROOT boundary')
        return path

    def checked(self, parts):
        """Check every existing component; missing suffixes are allowed."""
        path = self.root
        for i, part in enumerate(parts):
            _component(part)
            path = os.path.join(path, part)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                break
            if _is_link(st, path):
                raise ArchiveError('symlink/junction is forbidden')
            if i < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
                raise ArchiveError('non-directory path component')
        return self.path(parts)

    @contextmanager
    def directory(self, parts):
        self.checked(parts)
        fd = None
        try:
            if self.fd is None:
                yield None
                return
            fd = os.open('.', self.flags, dir_fd=self.fd)
            for part in parts:
                nxt = os.open(_component(part), self.flags, dir_fd=fd)
                os.close(fd)
                fd = nxt
            yield fd
        finally:
            if fd is not None:
                os.close(fd)

    def names(self, parts):
        parts = tuple(parts)
        if parts in self.cache:
            return self.cache[parts]
        names = []
        with self.directory(parts) as fd:
            with os.scandir(fd if fd is not None else self.path(parts)) as entries:
                for entry in entries:
                    self.total += 1
                    if len(names) >= MAX_DIRECTORY_ENTRIES or self.total > MAX_TOTAL_ENTRIES:
                        raise ArchiveError('directory entry limit exceeded')
                    names.append(entry.name)
        names.sort()
        self.cache[parts] = names
        return names

    def info(self, parts):
        path = self.checked(parts)
        if not parts:
            return os.fstat(self.fd) if self.fd is not None else os.lstat(path)
        with self.directory(parts[:-1]) as fd:
            st = (os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                  if fd is not None else os.lstat(path))
        if _is_link(st, path):
            raise ArchiveError('symlink/junction is forbidden')
        return st

    def read(self, parts, limit):
        path = self.checked(parts)
        with self.directory(parts[:-1]) as parent:
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            fd = (os.open(parts[-1], flags, dir_fd=parent)
                  if parent is not None else os.open(path, flags))
            try:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                    raise ArchiveError('not a regular file or size limit exceeded')
                chunks, length = [], 0
                while length <= limit:
                    chunk = os.read(fd, min(1024 * 1024, limit + 1 - length))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    length += len(chunk)
                after = os.fstat(fd)
                if length > limit:
                    raise ArchiveError('file size limit exceeded')
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ArchiveError('file changed while reading')
                return b''.join(chunks)
            finally:
                os.close(fd)

    def resolve_media(self, parts):
        """Exact component wins; otherwise exactly one casefold match is required."""
        for wanted in parts:
            _component(wanted)
            if wanted.casefold() in EXCLUDED:
                raise ArchiveError('metadata directories are not media')
        actual = []
        for i, wanted in enumerate(parts):
            names = self.names(actual)
            if wanted in names:
                name = wanted
            else:
                folded = [n for n in names if n.casefold() == wanted.casefold()]
                if len(folded) > 1:
                    raise ArchiveError('ambiguous casefold path component')
                if not folded:
                    return self.checked(tuple(actual) + tuple(parts[i:]))
                name = folded[0]
            actual.append(name)
            st = self.info(actual)
            if i < len(parts) - 1 and not stat.S_ISDIR(st.st_mode):
                raise ArchiveError('non-directory media component')
            if i == len(parts) - 1 and not stat.S_ISREG(st.st_mode):
                raise ArchiveError('media path is not a regular file')
        if not actual:
            raise ArchiveError('media path must name a file')
        return self.checked(actual)


def _windows_path(value):
    """Parse absolute drive/UNC paths without normalizing away traversal."""
    if not isinstance(value, str) or not value or '\x00' in value:
        raise ArchiveError('expected absolute Windows path')
    value = value.replace('/', '\\')
    if value[:8].casefold() == '\\\\?\\unc\\':
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    if value.startswith(('\\\\.\\', '\\??\\')):
        raise ArchiveError('Windows device path is forbidden')
    drive, tail = ntpath.splitdrive(value)
    is_drive = bool(re.fullmatch(r'[a-zA-Z]:', drive))
    is_unc = drive.startswith('\\\\') and len(drive[2:].split('\\')) == 2
    if not ((is_drive or is_unc) and (tail.startswith('\\') or (is_unc and not tail))):
        raise ArchiveError('expected absolute Windows path (no drive switching)')
    if is_unc:
        for component in drive[2:].split('\\'):
            _component(component)
    parts = tuple(_component(p) for p in tail.split('\\') if p)
    if len(parts) > MAX_DEPTH:
        raise ArchiveError('Windows path depth limit exceeded')
    return drive.casefold(), parts


def _map_media(tree, value, roots):
    drive, parts = _windows_path(value)
    candidates = []
    for root_drive, root_parts in roots:
        if (drive == root_drive and len(parts) > len(root_parts)
                and tuple(p.casefold() for p in parts[:len(root_parts)])
                == tuple(p.casefold() for p in root_parts)):
            candidates.append(root_parts)
    if not candidates:
        raise ArchiveError('Windows path is outside manifest source roots')
    prefix = max(candidates, key=len)
    return tree.resolve_media(parts[len(prefix):])


def video_id(root, path):
    """Stable across rescans, mtimes, accounts and fallback -> metadata upgrades."""
    root = os.path.realpath(os.path.abspath(os.fspath(root)))
    path = os.path.abspath(os.fspath(path))
    if os.path.commonpath((root, os.path.realpath(path))) != root:
        raise ArchiveError('video ID path escapes MEDIA_ROOT')
    relative = os.path.relpath(path, root).replace(os.sep, '/')
    return 'nas-' + hashlib.sha256(relative.encode('utf-8')).hexdigest()


def has_snapshots(root):
    """A broken/symlink archive entry still selects the importer for diagnostics."""
    metadata = os.path.join(os.fspath(root), '.valorant-moments')
    try:
        st = os.lstat(metadata)
        if _is_link(st, metadata) or not stat.S_ISDIR(st.st_mode):
            return True
        os.lstat(os.path.join(metadata, 'snapshots'))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _manifest(tree, snapshot):
    path = snapshot + ('manifest.json',)
    try:
        raw = tree.read(path, MAX_MANIFEST_SIZE)
    except FileNotFoundError as exc:
        raise ArchiveError('missing manifest.json; snapshot ignored') from exc
    try:
        manifest = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as exc:
        raise ArchiveError('invalid manifest JSON') from exc
    if not isinstance(manifest, dict) or manifest.get('schema') != SCHEMA:
        raise ArchiveError('unsupported manifest schema')
    if manifest.get('path_style') != 'windows' or manifest.get('video_root_relative') != '.':
        raise ArchiveError('unsupported manifest path_style/video_root_relative')
    kind = manifest.get('database_kind')
    if not isinstance(kind, str) or kind not in KINDS:
        raise ArchiveError('invalid manifest database_kind')
    roots = tuple(_windows_path(manifest.get(k))
                  for k in ('source_root', 'resolved_source_root'))
    _, source_parts = _windows_path(manifest.get('database_source'))
    if len(source_parts) < 2 or source_parts[-2].casefold() != kind.casefold():
        raise ArchiveError('database_source must name the declared Kind/account leaf')
    ident = source_parts[-1]  # Do NOT lowercase: this is the original AES identity.
    expected = kind + '/' + ident
    if manifest.get('database_file') != expected:
        raise ArchiveError('database_file must be exactly declared Kind/original account leaf')
    digest = manifest.get('database_sha256')
    if not isinstance(digest, str) or not _HEX64.fullmatch(digest) or digest != snapshot[-1]:
        raise ArchiveError('invalid database_sha256 or snapshot hash mismatch')
    size = manifest.get('database_size')
    if type(size) is not int or size <= 0 or size > MAX_DATABASE_SIZE:
        raise ArchiveError('invalid database_size or 64 MiB limit exceeded')
    database = snapshot + (kind, ident)
    if tree.info(database).st_size != size:
        raise ArchiveError('database_size mismatch')
    raw = tree.read(database, MAX_DATABASE_SIZE)
    if len(raw) != size:
        raise ArchiveError('database_size mismatch')
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ArchiveError('database_sha256 mismatch')
    return manifest, database, ident, roots, raw


def _fill_missing(preferred, other):
    """Keep preferred full-library data, filling holes with historical details."""
    pending = [(preferred, other)]
    while pending:
        target, source = pending.pop()
        for key, value in source.items():
            current = target.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                pending.append((current, value))
            elif current is None or current == '' or current == {} or current == []:
                target[key] = value
    return preferred


def _richness(value):
    pending, count = [value], 0
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
        else:
            count += int(current is not None and current != '')
    return count


def _quality(match):
    return (match.get('kind') == 'full', _richness(match.get('stats') or {}),
            _richness({k: v for k, v in match.items() if k != 'videos'}))


def _validate_match(match):
    if not isinstance(match['matches_id'], (str, int)) or isinstance(match['matches_id'], bool):
        raise ArchiveError('invalid match identity')
    match['matches_id'] = str(match['matches_id'])
    if not isinstance(match['stats'], dict):
        raise ArchiveError('invalid match stats')
    for key in ('agent_name', 'agent_id', 'map_id', 'map_name', 'map_image', 'mode'):
        if not isinstance(match[key], str):
            raise ArchiveError('invalid match text field: ' + key)
    if not isinstance(match['stats'].get('mode_name') or '', str):
        raise ArchiveError('invalid stats mode_name')
    for values, keys in [(match, ('matches_time', 'game_end_time'))] + [
            (video, ('duration_ms', 'size', 'time')) for video in match['videos']]:
        for key in keys:
            value = values[key]
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or value < 0 or value > 253402300799000):
                raise ArchiveError('invalid numeric metadata: ' + key)


def _merge_history(items):
    """Account + match + actual file identity, never raw video_id alone."""
    groups = {}
    for match in sorted(items, key=_quality, reverse=True):
        key = (match['openid'], match['matches_id'])
        current = groups.get(key)
        if current is None:
            groups[key] = match
            continue
        by_path = {v['src']: v for v in current['videos']}
        for video in match['videos']:
            old = by_path.get(video['src'])
            if old is None:
                current['videos'].append(video)
                by_path[video['src']] = video
            else:
                _fill_missing(old, video)
        _fill_missing(current, {k: v for k, v in match.items() if k != 'videos'})

    # One physical file may be repeated by different source archives/accounts.
    # Prefer its best metadata rather than producing colliding flattened IDs.
    seen, out = set(), []
    for match in sorted(groups.values(), key=_quality, reverse=True):
        videos = []
        for video in match['videos']:
            if video['src'] not in seen:
                seen.add(video['src'])
                videos.append(video)
        if not videos:
            continue
        match['videos'] = videos
        original = match['matches_id']
        match['original_matches_id'] = original
        identity = json.dumps([match['openid'], original], ensure_ascii=False, separators=(',', ':'))
        match['matches_id'] = 'nas-match-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()
        out.append(match)
    out.sort(key=lambda m: m.get('matches_time') or 0, reverse=True)
    return out


def load_library(root):
    """Load ALL valid historical snapshots; invalid ones are reported, not read."""
    # datasource imports this module lazily too: keep the dependency local.
    from datasource import _merge_match, try_decrypt

    meta = {'archive': True, 'databases': [], 'errors': []}
    matches, record_count = [], 0
    base = ('.valorant-moments', 'snapshots')
    try:
        with _Tree(root) as tree:
            for source in tree.names(base):
                if source.casefold().endswith('.partial'):
                    continue
                source_parts = base + (source,)
                try:
                    if not _HEX32.fullmatch(source):
                        raise ArchiveError('invalid source-key directory (expected 32 lowercase hex)')
                    snapshots = tree.names(source_parts)
                except (OSError, ValueError) as exc:
                    _error(meta['errors'], os.path.join(tree.root, *source_parts), exc)
                    continue
                for digest in snapshots:
                    if digest.casefold().endswith('.partial'):
                        continue
                    snapshot = source_parts + (digest,)
                    path = os.path.join(tree.root, *snapshot)
                    try:
                        if not _HEX64.fullmatch(digest):
                            raise ArchiveError('invalid snapshot hash directory')
                        manifest, database, ident, roots, raw = _manifest(tree, snapshot)
                        obj = try_decrypt(tree.path(database), ident, ciphertext=raw)
                        if not isinstance(obj, dict):
                            raise ArchiveError('unable to decrypt archived database using original account filename')
                        kind = KINDS[manifest['database_kind']]
                        meta['databases'].append({'path': tree.path(database), 'kind': kind,
                                                  'size': len(raw), 'ident': ident,
                                                  'sha256': digest})
                        for key, records in obj.items():
                            if not key.startswith('key_wonderful_list_') or not isinstance(records, list):
                                continue
                            account = key[len('key_wonderful_list_'):] or ident
                            for record in records:
                                record_count += 1
                                if isinstance(record, dict) and isinstance(record.get('videos'), list):
                                    record_count += len(record['videos'])
                                if record_count > MAX_METADATA_RECORDS:
                                    raise ArchiveError('metadata record/video limit exceeded')
                                if not isinstance(record, dict):
                                    _error(meta['errors'], path, 'invalid match record')
                                    continue
                                parsed = []
                                try:
                                    _merge_match(parsed, record, account, kind)
                                    for match in parsed:
                                        _validate_match(match)
                                except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                                    _error(meta['errors'], path, 'invalid match record: ' + str(exc))
                                    continue
                                for match in parsed:
                                    videos = []
                                    for video in match['videos']:
                                        try:
                                            video['src'] = _map_media(tree, video['src'], roots)
                                            if os.path.splitext(video['src'])[1].lower() not in VIDEO_EXT:
                                                raise ArchiveError('unsupported video extension')
                                            video['video_id'] = video_id(tree.root, video['src'])
                                        except (OSError, ValueError) as exc:
                                            _error(meta['errors'], path, 'invalid video path: ' + str(exc))
                                            continue
                                        if video['poster']:
                                            try:
                                                video['poster'] = _map_media(tree, video['poster'], roots)
                                            except (OSError, ValueError) as exc:
                                                _error(meta['errors'], path, 'invalid poster path: ' + str(exc))
                                                video['poster'] = ''
                                        videos.append(video)
                                    match['videos'] = videos
                                    matches.append(match)
                    except (OSError, ValueError, RuntimeError, RecursionError) as exc:
                        _error(meta['errors'], path, exc)
    except (OSError, ValueError) as exc:
        _error(meta['errors'], os.path.join(os.fspath(root), *base), exc)
    merged = _merge_history(matches)
    meta['count'] = len(merged)
    return merged, meta


def scan_files(root, errors=None):
    """Metadata-only, bounded fallback: never opens video or poster contents."""
    errors = errors if errors is not None else []
    out = []
    try:
        with _Tree(root) as tree:
            pending = [()]
            while pending:
                parts = pending.pop()
                try:
                    names = tree.names(parts)
                    files = []
                    for name in names:
                        if name.casefold() in EXCLUDED or name.casefold().endswith('.partial'):
                            continue
                        child = parts + (name,)
                        try:
                            st = tree.info(child)
                            if stat.S_ISDIR(st.st_mode):
                                if len(child) >= MAX_DEPTH:
                                    raise ArchiveError('directory depth limit exceeded')
                                pending.append(child)
                            elif stat.S_ISREG(st.st_mode):
                                files.append((name, st))
                        except (OSError, ValueError) as exc:
                            _error(errors, os.path.join(tree.root, *child), exc)
                    videos = [(n, st) for n, st in files if os.path.splitext(n)[1].lower() in VIDEO_EXT]
                    if not videos:
                        continue
                    covers = [n for n, _ in files if n.lower().startswith('cover')
                              and os.path.splitext(n)[1].lower() in ('.jpeg', '.jpg', '.png', '.webp')]
                    vlist = []
                    for i, (name, st) in enumerate(videos):
                        path = tree.checked(parts + (name,))
                        poster = tree.checked(parts + (covers[i],)) if i < len(covers) else ''
                        vlist.append({'video_id': video_id(tree.root, path), 'src': path, 'poster': poster,
                                      'duration_ms': 0, 'size': st.st_size, 'type': '', 'resolution': '',
                                      'level': '', 'time': int(st.st_mtime * 1000),
                                      'is_upload': False, 'clips_count': 0})
                    relative = '/'.join(parts)
                    out.append({'matches_id': 'nas-scan-' + hashlib.sha256(relative.encode('utf-8')).hexdigest(),
                                'openid': '', 'kind': 'scan',
                                'matches_time': max(v['time'] for v in vlist), 'game_end_time': 0,
                                'agent_name': '', 'agent_id': '', 'map_id': '', 'map_name': '',
                                'map_image': '', 'stats': {}, 'mode': '', 'videos': vlist})
                except (OSError, ValueError) as exc:
                    _error(errors, os.path.join(tree.root, *parts), exc)
    except (OSError, ValueError) as exc:
        _error(errors, root, exc)
    out.sort(key=lambda m: m['matches_time'], reverse=True)
    return out
