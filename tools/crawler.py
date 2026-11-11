# -*- coding: utf-8 -*-
"""JavDB 评论区 magnet/ed2k 定时爬取器（云端版）

流程：
  1. 用 139 个人云客户端（TyPersonal）登录，按名字定位要抓的顶层目录
  2. 递归列出所有视频文件，从文件名提取番号
  3. 用 JavDB 移动端 API（登录 + 签名）把番号→影片ID
  4. 抓每部影片评论区，筛选 created_at >= CUTOFF 的 magnet/ed2k
  5. 去重，输出 CSV/TXT；增量状态记录在 state 文件，下次只处理新增

环境变量：
  YDY_AUTHORIZATION   139 个人云 authorization（必填）
  JAVDB_USERNAME      JavDB 登录邮箱（必填）
  JAVDB_PASSWORD      JavDB 登录密码（必填）
  YDY_FOLDERS         要抓的目录名，逗号分隔（必填），从根目录按名字找
  CUTOFF              起始日期（默认 2026-09-01）
  OUT_PREFIX          输出文件名前缀（默认 javdb_yiyuan）
  SLEEP_REQ           每请求间隔秒（默认 1.6）
  STATE_FILE          增量状态文件（默认 state.json）
  MAX_CODES           限制本次最多处理多少番号（调试用，0=不限）
"""
import json
import os
import re
import sys
import time
import csv

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from javdb_api import JavDB
from ty_cloud_personal import TyPersonal

CUTOFF = os.environ.get('CUTOFF', '2026-09-01')
# 多目录合并：YDY_FOLDERS 逗号分隔，为 139 个人云根目录下的文件夹名（按名字定位，不是完整路径）
# 具体目录名由环境变量传入，源码里不写死（公开仓库脱敏）
FOLDERS = [x.strip() for x in os.environ.get('YDY_FOLDERS', '').split(',') if x.strip()]
OUT_PREFIX = os.environ.get('OUT_PREFIX', 'javdb_yiyuan')
SLEEP_REQ = float(os.environ.get('SLEEP_REQ', '1.6'))
STATE_FILE = os.environ.get('STATE_FILE', 'state.json')
MAX_CODES = int(os.environ.get('MAX_CODES', '0'))

VIDEO_EXT = ('.mp4', '.mkv', '.avi', '.wmv', '.mov', '.flv', '.ts', '.m2ts', '.rmvb', '.iso', '.strm')
SKIP_FC2 = os.environ.get('SKIP_FC2', '1') == '1'   # 默认跳过 FC2/PPV
MAGNET_RE = re.compile(r'magnet:\?xt=urn:btih:[A-Za-z0-9]+(?:&[^\s"\'<>\)\]]*)?', re.I)
ED2K_RE = re.compile(r'ed2k://\|file\|[^|\r\n]+\|\d+\|[0-9A-Fa-f]{32}\|(?:h=[A-Za-z0-9]+)?/?', re.I)


# ---------------- 番号处理 ----------------
def parse_filename(filename):
    base = re.sub(r'\.[^.]+$', '', filename)
    low = base.lower()
    low = re.sub(r'^.*@', '', low)
    # FC2 family
    m = re.search(r'fc2[\s\-_]*ppv[\s\-_]*(\d{3,8})', low)
    if not m:
        m = re.search(r'fc2[\s\-_]*(\d{5,8})', low)
    if not m:
        m = re.search(r'^fc(\d{6,8})', low)
    if not m:
        m = re.search(r'^ppv[\s\-_]*(\d{5,8})', low)
    if m:
        return ('FC2', m.group(1))
    # normal
    m = re.search(r'([a-z]{2,10})[-_ ]?(\d{2,6})', low)
    if not m:
        return None
    return (m.group(1).upper(), m.group(2))


def is_fc2(filename):
    return bool(re.search(r'fc2|ppv|^fc\d', filename.lower()))


def make_candidates(label, digits):
    if label.upper() == 'FC2':
        iv = int(digits)
        return ['FC2-%d' % iv, 'FC2-PPV-%d' % iv]
    cands = []
    try:
        iv = int(digits)
        cands.append('%s-%d' % (label, iv))
        cands.append('%s-%03d' % (label, iv))
        cands.append('%s-%04d' % (label, iv))
    except Exception:
        pass
    cands.append('%s-%s' % (label, digits))
    out = []
    for c in cands:
        if c not in out:
            out.append(c)
    return out


def norm_number(s):
    if not s:
        return None
    m = re.match(r'FC2[-\s_]*(?:PPV[-\s_]*)?0*(\d+)', s.strip(), re.I)
    if m:
        return 'FC2-%d' % int(m.group(1))
    m = re.match(r'([A-Za-z]{2,10})[-_ ]?0*(\d+)', s.strip())
    if not m:
        return s.strip().upper()
    return '%s-%d' % (m.group(1).upper(), int(m.group(2)))


def extract_links(text):
    links = []
    if not text:
        return links
    for m in MAGNET_RE.findall(text):
        links.append(('magnet', m.rstrip('.,;')))
    for e in ED2K_RE.findall(text):
        links.append(('ed2k', e.rstrip('.,;')))
    return links


# ---------------- 139 目录遍历 ----------------
def walk_ydy(client, folder_id, acc, path='', depth=0):
    if depth > 6:
        return
    for it in client.list_dir(folder_id):
        name = it['name']
        full = path + '/' + name if path else name
        if it['is_dir']:
            walk_ydy(client, it['id'], acc, full, depth + 1)
        elif name.lower().endswith(VIDEO_EXT):
            acc.append({'path': full, 'filename': name, 'size': it['size']})


# ---------------- JavDB 抓取 ----------------
def _safe(fn, *a, **kw):
    """对瞬时网络/SSL 错误重试几次再放弃（不影响永久性错误）。"""
    last = None
    for attempt in range(4):
        try:
            return fn(*a, **kw)
        except Exception as e:
            last = e
            print('  ~ 网络重试 %d: %s' % (attempt + 1, str(e)[:80]), flush=True)
            time.sleep(3 * (attempt + 1))
    raise last


def resolve_movie(client, label, digits):
    target = norm_number('%s-%s' % (label, digits))
    for q in make_candidates(label, digits):
        r = _safe(client.search, q, page=1)
        if r.get('action') == 'BannedUser':
            raise RuntimeError('BANNED')
        if r.get('success') != 1:
            time.sleep(SLEEP_REQ)
            continue
        for mv in ((r.get('data') or {}).get('movies') or []):
            if norm_number(mv.get('number')) == target:
                return mv
        time.sleep(SLEEP_REQ)
    return None


def crawl_movie_reviews(client, mid, code, state, folders=''):
    page = 1
    while True:
        r = _safe(client.movie_reviews, mid, page=page)
        if r.get('action') == 'BannedUser':
            raise RuntimeError('BANNED')
        if r.get('success') != 1:
            break
        reviews = (r.get('data') or {}).get('reviews') or []
        if not reviews:
            break
        for rv in reviews:
            ct = (rv.get('created_at') or '')[:10]
            if not ct or ct < CUTOFF:
                continue
            mv = rv.get('movie') or {}
            for ltype, link in extract_links(rv.get('content') or ''):
                if link in state['seen_links']:
                    continue
                state['seen_links'].append(link)
                state['rows'].append({
                    'code': code, 'movie_id': mid, 'movie_number': mv.get('number'),
                    'folders': folders,
                    'review_id': rv.get('id'), 'review_created_at': rv.get('created_at'),
                    'username': rv.get('username'), 'link_type': ltype, 'link': link,
                })
        page += 1
        time.sleep(SLEEP_REQ)


def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE, encoding='utf-8'))
    return {'resolved': {}, 'no_match': [], 'seen_links': [], 'rows': []}


def save_state(st):
    tmp = STATE_FILE + '.tmp'
    json.dump(st, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def main():
    juser = os.environ.get('JAVDB_USERNAME', '').strip()
    jpass = os.environ.get('JAVDB_PASSWORD', '').strip()
    if not juser or not jpass:
        print('!! 缺少 JAVDB_USERNAME / JAVDB_PASSWORD')
        sys.exit(2)

    # ---- 番号来源：优先读清单文件（GitHub 海外连不到 139，清单由本机生成入库）----
    codes_file = os.environ.get('MOVIE_CODES_FILE', '').strip()
    if codes_file and os.path.exists(codes_file):
        data = json.load(open(codes_file, encoding='utf-8'))
        codes = {}
        for c in data.get('codes', []):
            key = c['code']
            codes[key] = {'label': c['label'], 'digits': c['digits'],
                          'folders': set(c.get('folders') or [])}
        keys = sorted(codes.items())
        print('从清单 %s 读取番号: %d（来源目录: %s）'
              % (codes_file, len(keys), ','.join(data.get('generated_from') or [])))
        if MAX_CODES:
            keys = keys[:MAX_CODES]
    else:
        auth = os.environ.get('YDY_AUTHORIZATION', '').strip()
        if not auth:
            print('!! 既没有 MOVIE_CODES_FILE，也没有 YDY_AUTHORIZATION')
            sys.exit(2)
        # 1) 139 列目录（多目录合并成一个清单）
        ty = TyPersonal(auth)
        if not ty.login():
            print('!! 139 登录失败（authorization 可能过期）')
            sys.exit(3)
        print('139 host:', ty.host, '| token 剩余 %.1f 天' % ty.expires_in_days())
        files = []
        for folder_name in FOLDERS:
            fo = ty.find_dir(folder_name, '/')
            if not fo:
                print('!! 根目录找不到文件夹: %s（跳过）' % folder_name)
                continue
            one = []
            walk_ydy(ty, fo['id'], one)
            for f in one:
                f['folder'] = folder_name
            files.extend(one)
            print('目录 %s 下视频文件: %d' % (folder_name, len(one)))
        print('合并后视频文件: %d' % len(files))
        if not files:
            print('!! 所有目录都没取到视频')
            sys.exit(4)
        # 2) 番号（跨目录合并去重）
        codes = {}
        for f in files:
            if SKIP_FC2 and is_fc2(f['filename']):
                continue
            p = parse_filename(f['filename'])
            if not p:
                continue
            label, digits = p
            key = '%s-%d' % (label, int(digits)) if digits.isdigit() else '%s-%s' % (label, digits)
            d = codes.setdefault(key, {'label': label, 'digits': digits, 'folders': set()})
            d['folders'].add(f.get('folder', ''))
        keys = sorted(codes.items())
        print('合并唯一番号: %d' % len(keys))
        if MAX_CODES:
            keys = keys[:MAX_CODES]

    # 3) JavDB 登录
    jc = JavDB(username=juser, password=jpass)
    jc.login()

    state = load_state()
    print('已处理 %d / 无匹配 %d / 已有链接 %d' %
          (len(state['resolved']), len(state['no_match']), len(state['rows'])))

    for i, (key, info) in enumerate(keys, 1):
        if key in state['resolved'] or key in state['no_match']:
            continue
        try:
            mv = resolve_movie(jc, info['label'], info['digits'])
            time.sleep(SLEEP_REQ)
            if not mv:
                state['no_match'].append(key)
                save_state(state)
                print('[%d/%d] %s NO MATCH' % (i, len(keys), key))
                continue
            mid = mv['id']
            state['resolved'][key] = {'movie_id': mid, 'number': mv.get('number')}
            save_state(state)
            fold = '、'.join(sorted(info.get('folders') or []))
            crawl_movie_reviews(jc, mid, key, state, folders=fold)
            save_state(state)
            print('[%d/%d] %s -> %s(%s) links=%d' % (i, len(keys), key, mv.get('number'), mid, len(state['rows'])))
            time.sleep(1.0)
        except RuntimeError as e:
            print('!! %s @%s，保存并退出' % (e, key))
            save_state(state)
            sys.exit(5)
        except Exception as e:
            # 单项的意外错误(重试后仍失败)不拖垮整轮：跳过它，下次再试
            print('!! %s 异常跳过：%s' % (key, str(e)[:120]))
            save_state(state)
            time.sleep(SLEEP_REQ)

    # 4) 输出
    fields = ['code', 'folders', 'movie_id', 'movie_number', 'review_id', 'review_created_at',
              'username', 'link_type', 'link']
    csv_path = OUT_PREFIX + '_magnet_ed2k_' + CUTOFF + '+.csv'
    txt_path = OUT_PREFIX + '_links_' + CUTOFF + '+.txt'
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in state['rows']:
            w.writerow({k: r.get(k, '') for k in fields})
    with open(txt_path, 'w', encoding='utf-8') as f:
        for r in state['rows']:
            f.write(r['link'] + '\n')

    print('DONE resolved=%d no_match=%d links=%d magnet=%d ed2k=%d' % (
        len(state['resolved']), len(state['no_match']), len(state['rows']),
        sum(1 for r in state['rows'] if r['link_type'] == 'magnet'),
        sum(1 for r in state['rows'] if r['link_type'] == 'ed2k')))
    print('输出: %s , %s' % (csv_path, txt_path))


if __name__ == '__main__':
    main()
