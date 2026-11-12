# -*- coding: utf-8 -*-
"""
数据源发现与解密。

ACLOS（无畏契约客户端录制组件）把"无畏时刻"的元数据存在加密 KV 文件里：
  %APPDATA%\\ACLOS\\WonderfulDb\\<openid>         完整版
  %APPDATA%\\ACLOS\\WeGameWonderfulDb\\<gopenid>  精简版

加密规则（来自 sdk/utils/safeUtils.js）：
  key = sha256(openid).hexdigest()[:32]  (utf8 字节, AES-256-CBC)
  iv  = sha256(openid).hexdigest()[:16]
  WeGame 精简版：key = iv = sha256(gopenid).hexdigest()[:16] (AES-128-CBC)
  密文以 hex 文本形式落盘。

本模块支持"用户指定任意文件夹"作为根目录：
  1. 在根目录下递归寻找 WonderfulDb / WeGameWonderfulDb 目录（以及直接含账号文件的情况）；
  2. 找到后尝试用文件名的 openid 解密，得到 {key_wonderful_list_<id>: [...]}；
  3. 若找不到数据库，退回纯文件扫描模式（只有文件、没有英雄/地图）。
"""
import hashlib
import json
import os
import re

try:
    from Crypto.Cipher import AES
    _HAS_CRYPTO = True
except Exception:  # pragma: no cover
    _HAS_CRYPTO = False


# ---------------------------------------------------------------- 解密

def _derive(openid, length):
    return hashlib.sha256(openid.encode('utf-8')).hexdigest()[:length].encode('utf-8')


def _aes_decrypt_hex(cipher_hex, key, iv):
    data = bytes.fromhex(cipher_hex)
    if not data:
        return None
    if len(data) % 16:
        data = data[: len(data) - (len(data) % 16)]
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    # 去掉 PKCS7 或自定义填充（1..16）
    last = dec[-1]
    if 1 <= last <= 16 and dec[-last:] == bytes([last]) * last:
        dec = dec[:-last]
    else:
        dec = dec.rstrip(b'\x00')
    return dec.decode('utf-8', 'ignore')


def _read_hex(path):
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        raw = f.read().strip()
    # 只保留 hex 字符
    raw = re.sub(r'[^0-9a-fA-F]', '', raw)
    return raw


def try_decrypt(path, ident, *, ciphertext=None):
    """解密数据库；ciphertext 可传入已经安全读取并校验的原始归档字节。"""
    if not _HAS_CRYPTO:
        raise RuntimeError('缺少 pycryptodome，请先执行: pip install pycryptodome')
    if ciphertext is None:
        raw = _read_hex(path)
    else:
        text = ciphertext.decode('utf-8', 'ignore') if isinstance(ciphertext, bytes) else ciphertext
        raw = re.sub(r'[^0-9a-fA-F]', '', text)
    if len(raw) < 64:
        return None
    candidates = [
        (_derive(ident, 32), _derive(ident, 16)),   # AES-256-CBC 完整版
        (_derive(ident, 16), _derive(ident, 16)),   # AES-128-CBC 精简版
    ]
    for key, iv in candidates:
        try:
            text = _aes_decrypt_hex(raw, key, iv)
            if not text:
                continue
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- 根目录发现

DB_DIR_NAMES = ('WonderfulDb', 'WeGameWonderfulDb')


def _db_candidates():
    """候选 ACLOS 根位置（按优先级）。"""
    appdata = os.environ.get('APPDATA') or os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming')
    local = os.environ.get('LOCALAPPDATA') or os.path.join(os.path.expanduser('~'), 'AppData', 'Local')
    return [
        os.path.join(appdata, 'ACLOS'),
        os.path.join(local, 'ACLOS'),
        os.path.join(os.path.expanduser('~'), 'AppData', 'ACLOS'),
    ]


def find_db_files(root):
    """寻找可解的 ACLOS 数据库文件。

    查找顺序（命中即停，不回退到无关位置）：
      1. 根目录自身就是 WonderfulDb / WeGameWonderfulDb；
      2. 根目录下的同名子目录（用户直接选了 ACLOS 或 %APPDATA%）；
      3. 根目录的祖先链上任意一层的同名子目录（最多 6 层）；
      4. 路径中出现 AppData 时，去其兄弟目录 Roaming\\ACLOS / Local\\ACLOS 找，
         以及这些 AppData 的 ACLOS 目录（覆盖 aclos-highlight 与 Roaming\\ACLOS 平行的情况）。

    这样：选了含数据的目录 -> 用数据库；选了不含数据的目录 -> 走纯文件扫描。
    """
    found = []
    nas_mode = os.environ.get('VM_NAS_MODE') == '1'
    root_real = os.path.realpath(os.path.abspath(root))

    def in_scope(path):
        if not nas_mode:
            return True
        try:
            relative = os.path.relpath(os.path.abspath(path), root_real)
            if relative == os.pardir or relative.startswith(os.pardir + os.sep):
                return False
            current = root_real
            for component in relative.split(os.sep):
                current = os.path.join(current, component)
                if os.path.islink(current):
                    return False
            return os.path.commonpath((root_real, os.path.realpath(path))) == root_real
        except (OSError, ValueError):
            return False

    def add_dir(d, kind):
        if not in_scope(d) or not os.path.isdir(d):
            return
        try:
            names = os.listdir(d)
        except OSError:
            return
        for name in names:
            p = os.path.join(d, name)
            if not in_scope(p) or name.casefold().endswith('.partial') or not os.path.isfile(p):
                continue
            # snapshot<id> 是截图副库，跳过
            if name.startswith('snapshot'):
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                continue
            if size < 128:
                continue
            found.append({'path': p, 'name': name, 'size': size,
                          'kind': kind, 'ident': name})

    def scan_base(base):
        """在某个 ACLOS 根下找 WonderfulDb / WeGameWonderfulDb。"""
        if not os.path.isdir(base):
            return
        bn = os.path.basename(os.path.normpath(base))
        if bn in DB_DIR_NAMES:
            add_dir(base, 'wegame' if bn == 'WeGameWonderfulDb' else 'full')
            return
        for d in DB_DIR_NAMES:
            sub = os.path.join(base, d)
            if os.path.isdir(sub):
                add_dir(sub, 'wegame' if d == 'WeGameWonderfulDb' else 'full')

    root_abs = os.path.abspath(root)

    # 1 + 2) 根目录自身 / 根目录下一层
    scan_base(root_abs)

    # 3) 祖先链最多 6 层
    if not found and not nas_mode:
        cur = root_abs
        for _ in range(6):
            nxt = os.path.dirname(cur)
            if not nxt or nxt == cur:
                break
            cur = nxt
            scan_base(cur)
            if found:
                break

    # 4) AppData 兄弟分支（真实安装布局就在这一层命中）
    if not found and not nas_mode:
        cur = root_abs
        for _ in range(8):
            nxt = os.path.dirname(cur)
            if not nxt or nxt == cur:
                break
            cur = nxt
            if os.path.basename(cur).lower() in ('appdata', 'appdata (x86)'):
                for base in (os.path.join(cur, 'ACLOS'),
                             os.path.join(cur, 'Roaming', 'ACLOS'),
                             os.path.join(cur, 'Local', 'ACLOS')):
                    scan_base(base)
                if found:
                    break

    # 去重，full 优先
    found.sort(key=lambda x: (0 if x['kind'] == 'full' else 1, -x['size']))
    seen = set()
    out = []
    for f in found:
        if f['path'] in seen:
            continue
        seen.add(f['path'])
        out.append(f)
    return out


def load_library(root):
    """读取根目录下所有可解密数据库，合并成统一的对局列表。

    返回 (matches, meta)
      matches: [{matches_id, matches_time, agent_name, map_id, map_name,
                 stats, videos:[{video_id, src, poster, duration_ms, size,
                                 type, resolution, level}]}]
      meta:    诊断信息（用了哪些库、账号、失败原因）
    """
    # Archive import is selected even for an invalid archive, so errors remain
    # visible and we never fall through to an unrelated ancestor database.
    import nas_archive
    if nas_archive.has_snapshots(root):
        return nas_archive.load_library(root)

    matches = []
    meta = {'databases': [], 'errors': []}
    for dbf in find_db_files(root):
        obj = None
        err = None
        # 文件名就是 openid，先用它；失败再试别的候选 id
        idents = [dbf['ident']]
        if len(matches):
            for m in matches:
                if m.get('openid') and m['openid'] not in idents:
                    idents.append(m['openid'])
        for ident in idents:
            try:
                obj = try_decrypt(dbf['path'], ident)
            except Exception as e:
                err = str(e)
                obj = None
            if obj:
                break
        if not obj:
            meta['errors'].append({'path': dbf['path'], 'error': err or '无法解密（账号 ID 不匹配）'})
            continue

        meta['databases'].append({'path': dbf['path'], 'kind': dbf['kind'],
                                  'size': dbf['size'], 'ident': dbf['ident']})
        for key, value in obj.items():
            if not key.startswith('key_wonderful_list_'):
                continue
            openid = key[len('key_wonderful_list_'):]
            if not isinstance(value, list):
                continue
            for m in value:
                _merge_match(matches, m, openid, dbf['kind'])

    # 合并重复对局（full 与 wegame 两份库都可能有）
    merged = _dedupe(matches)
    merged.sort(key=lambda x: x.get('matches_time') or 0, reverse=True)
    meta['count'] = len(merged)
    return merged, meta


def _norm_video(v, playlist_map):
    src = v.get('video_src') or ''
    poster = v.get('video_poster') or ''
    return {
        'video_id': v.get('video_id') or '',
        'src': src,
        'poster': poster,
        'duration_ms': v.get('video_duration') or 0,
        'size': v.get('video_size') or 0,
        'type': (v.get('video_type') or '').strip(),
        'resolution': (v.get('video_resolution') or '').replace('\r', 'x').replace('xx', 'x').strip(),
        'level': str(v.get('video_level') or ''),
        'time': v.get('video_time') or 0,
        'is_upload': bool(v.get('is_upload')),
        'clips_count': v.get('clips_count') or 0,
    }


def _merge_match(store, m, openid, kind):
    mid = m.get('matches_id') or ''
    if not mid:
        return
    videos = [_norm_video(v, None) for v in (m.get('videos') or [])]
    store.append({
        'matches_id': mid,
        'openid': openid,
        'kind': kind,
        'matches_time': m.get('matches_time') or 0,
        'game_end_time': m.get('gameEndTime') or 0,
        'agent_name': (m.get('agent') or {}).get('agent_name') or '',
        'agent_id': (m.get('agent') or {}).get('agent_id') or '',
        'map_id': (m.get('map') or {}).get('map_id') or '',
        'map_name': (m.get('map') or {}).get('map_name') or '',
        'map_image': (m.get('map') or {}).get('map_image') or '',
        'stats': m.get('stats') or {},
        'mode': m.get('mode') or '',
        'videos': videos,
    })


def _dedupe(items):
    best = {}
    for m in items:
        k = m['matches_id']
        cur = best.get(k)
        if cur is None:
            best[k] = m
            continue
        # 合并：保留字段更多的，视频取并集
        have = {v['video_id'] for v in cur['videos']}
        for v in m['videos']:
            if v['video_id'] not in have:
                cur['videos'].append(v)
        for field in ('agent_name', 'map_name', 'map_id', 'map_image'):
            if not cur.get(field) and m.get(field):
                cur[field] = m[field]
        if kind_rank(m) < kind_rank(cur):
            cur['kind'] = m['kind']
    return list(best.values())


def kind_rank(m):
    return 0 if m.get('kind') == 'full' else 1


# ---------------------------------------------------------------- 纯文件扫描兜底

VIDEO_EXT = {'.mp4', '.mov', '.mkv', '.webm', '.m4v'}


def scan_files(root, errors=None):
    """安全的普通视频扫描；包含 record，排除元数据、partial 与所有链接。"""
    import nas_archive
    return nas_archive.scan_files(root, errors=errors)


if __name__ == '__main__':
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(r'~\AppData\ACLOS\aclos-highlight')
    print('根目录:', root)
    for d in find_db_files(root):
        print('  发现库:', d['kind'], d['size'], d['path'])
    ms, meta = load_library(root)
    print('对局数:', len(ms))
    if meta['errors']:
        print('错误:', meta['errors'][:3])
    if ms:
        from collections import Counter
        c = Counter(m['agent_name'] for m in ms)
        print('英雄:', c.most_common(10))
        print('视频条目:', sum(len(m['videos']) for m in ms))
