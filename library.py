# -*- coding: utf-8 -*-
"""把解密后的对局数据整理成前端可直接用的扁平索引。"""
import json
import os
import re
import time

import datasource
import mapping


def _norm_res(s):
    """'1920\\rx720\\r' / '1920x720x' -> '1920x720'"""
    s = (s or '').replace('\r', 'x').replace('\n', 'x')
    parts = [p for p in re.split(r'[xX]', s) if p.strip()]
    if len(parts) >= 2:
        return '%sx%s' % (parts[0].strip(), parts[1].strip())
    return s.strip()


def build(root, existing=None):
    """existing: 上一次的索引，用来继承 fav / name / hidden 这些用户改过的字段。"""
    t0 = time.time()
    matches, meta = datasource.load_library(root)
    mode = 'archive' if meta.get('archive') else 'db'
    if not matches or mode == 'archive' or os.environ.get('VM_NAS_MODE') == '1':
        if not matches and mode != 'archive':
            mode = 'scan'
        # A newer snapshot need not mention every existing file. Preserve all
        # imported history, then add genuinely unassociated files from disk.
        known = {os.path.normcase(os.path.abspath(v['src']))
                 for match in matches for v in match.get('videos', []) if v.get('src')}
        for match in datasource.scan_files(root, errors=meta.setdefault('errors', [])):
            match['videos'] = [v for v in match['videos']
                               if os.path.normcase(os.path.abspath(v['src'])) not in known]
            if match['videos']:
                matches.append(match)

    prev = {}
    if existing:
        for r in existing.get('records', []):
            prev[r['id']] = r

    records = []          # 一条记录 = 一个视频文件
    for m in matches:
        agent_en = m.get('agent_name') or ''
        agent_grp = mapping.agent_key(agent_en)
        agent_name = mapping.agent_cn(agent_en)
        map_gi = mapping.map_cn(m.get('map_id'), m.get('map_name'))
        map_grp = mapping.map_key(m.get('map_id'), m.get('map_name'))
        stats = m.get('stats') or {}
        mode_name = stats.get('mode_name') or m.get('mode') or ''
        mode_cn = mapping.mode_cn(mode_name)
        won = stats.get('has_won')
        rw = stats.get('rounds_won')
        rl = stats.get('rounds_lost')
        mtime = m.get('matches_time') or 0
        date = time.strftime('%Y-%m-%d', time.localtime(mtime / 1000)) if mtime else ''
        time_s = time.strftime('%H:%M', time.localtime(mtime / 1000)) if mtime else ''

        for v in m.get('videos') or []:
            src = v.get('src') or ''
            if not src:
                continue
            rec_id = v.get('video_id') or os.path.splitext(os.path.basename(src))[0]
            dur = v.get('duration_ms') or 0
            old = prev.get(rec_id) or {}
            rec = {
                'id': rec_id,
                'match': m.get('matches_id') or '',
                'src': src,
                'poster': v.get('poster') or '',
                'name': old.get('rename') or os.path.splitext(os.path.basename(src))[0],
                'duration': round(dur / 1000) if dur else 0,
                'size': v.get('size') or _safe_size(src),
                'type': v.get('type') or '',
                'res': _norm_res(v.get('resolution') or ''),
                'time': mtime,
                'date': date,
                'clock': time_s,
                'agent': agent_name,
                'agentEn': agent_en,
                'agentGrp': agent_grp,
                'agentIcon': mapping.agent_icon_id(agent_en),
                'agentImg': mapping.agent_icon_url(agent_name),
                'agentPortrait': mapping.agent_portrait_url(agent_name),
                'map': map_gi,
                'mapGrp': map_grp,
                'mapCover': mapping.map_cover_url(m.get('map_id'), m.get('map_name')),
                'mode': mode_cn,
                'modeIcon': mapping.mode_icon_url(mode_cn),
                'won': won if isinstance(won, bool) else None,
                'score': '%s:%s' % (rw, rl) if rw is not None and rl is not None else '',
                'roundsWon': rw, 'roundsLost': rl,
                'kills': stats.get('kills'),
                'deaths': stats.get('deaths'),
                'assists': stats.get('assists'),
                'kda': _kda(stats),
                'scorePoint': stats.get('score'),
                'level': stats.get('game_level') or '',
                'openid': m.get('openid') or '',
                'account': (m.get('openid') or '')[-4:] if m.get('openid') else '',
                'exists': _exists(src),
            }
            if old.get('fav'):
                rec['fav'] = True
            if old.get('hidden'):
                rec['hidden'] = True
            if old.get('rename'):
                rec['rename'] = old['rename']
            if isinstance(old.get('tags'), list):
                rec['tags'] = list(old['tags'])
            records.append(rec)

    # 统计聚合，给前端筛选下拉用（带数量，前端按数量排序）
    from collections import Counter
    cnt_agent = Counter(r['agentGrp'] for r in records if r['agentGrp'])
    cnt_type = Counter(r['type'] for r in records if r['type'])
    # 地图按中文名合并计数（不同内部代号可能同名，如 遭遇战 / 射击场）
    cnt_map_name = Counter(r['map'] for r in records if r['map'])
    # 每个中文名取一个代表 key（优先数量最多的那个 key）
    name_to_key = {}
    key_count = Counter(r['mapGrp'] for r in records if r['mapGrp'] and r['map'])
    for r in records:
        if not r['map']:
            continue
        k = r['map']
        cur = name_to_key.get(k)
        if cur is None or key_count[r['mapGrp']] > key_count[cur]:
            name_to_key[k] = r['mapGrp']
    years, accounts = set(), set()
    names_agent, names_map = {}, {}
    for r in records:
        if r['agentGrp']:
            names_agent[r['agentGrp']] = r['agent']
        if r['mapGrp']:
            names_map[r['mapGrp']] = r['map']
        if r['date']:
            years.add(r['date'][:4])
        if r['account']:
            accounts.add(r['account'])

    cnt_mode = Counter(r['mode'] for r in records if r['mode'])
    # 年月统计（详细日期放到 facets.months，供时间轴使用）
    cnt_month = Counter()
    for r in records:
        d = r.get('date') or ''
        if len(d) >= 7:
            cnt_month[d[:7]] += 1
    type_order = mapping.VIDEO_TYPE_ORDER
    types_sorted = [t for t in type_order if t in cnt_type] + \
                   sorted([t for t in cnt_type if t not in type_order],
                          key=lambda t: -cnt_type[t])

    index = {
        'version': 2,
        'generated': int(time.time()),
        'root': root,
        'mode': mode,
        'meta': meta,
        'records': records,
        'facets': {
            'agents': sorted([{'key': k, 'name': names_agent[k], 'count': n,
                               'img': mapping.agent_icon_url(names_agent[k])}
                              for k, n in cnt_agent.items()], key=lambda x: -x['count']),
            'maps': sorted([{'key': name_to_key.get(n, n), 'name': n, 'count': c,
                             'cover': mapping.map_cover_url(name_to_key.get(n, n), n)}
                            for n, c in cnt_map_name.items()], key=lambda x: -x['count']),
            'types': [{'name': t, 'count': cnt_type[t]} for t in types_sorted],
            'modes': sorted([{'name': k, 'count': n} for k, n in cnt_mode.items()],
                            key=lambda x: -x['count']),
            'months': [{'ym': ym, 'year': ym[:4], 'month': int(ym[5:7]), 'count': n}
                       for ym, n in sorted(cnt_month.items(), reverse=True)],
            'years': sorted(years, reverse=True),
            'accounts': sorted(accounts),
        },
        'stats': {
            'videos': len(records),
            'matches': len({r['match'] for r in records}),
            'totalSize': sum(r['size'] for r in records),
            'totalDuration': sum(r['duration'] for r in records),
        },
        'elapsed': round(time.time() - t0, 2),
    }
    return index


def _kda(stats):
    k = stats.get('kills')
    d = stats.get('deaths')
    a = stats.get('assists')
    if k is None or d is None:
        return ''
    return '%s/%s/%s' % (k, d, a if a is not None else 0)


def _exists(p):
    try:
        return os.path.isfile(p)
    except Exception:
        return False


def _safe_size(p):
    try:
        return os.path.getsize(p)
    except Exception:
        return 0


def save(index, path):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)


def load(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)
