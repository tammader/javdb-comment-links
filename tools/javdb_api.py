import hashlib, time, urllib.request, urllib.parse, ssl, json, sys, csv, os

# ==== JavDB mobile API client (reverse-engineered from JavDB_1.9.29.apk) ====
# Signature scheme: jdsignature = "<unix_ts>.<part2>.<md5(unix_ts + part1)>"
# part1/part2 are AES-style obfuscated constants embedded in libapp.so, decrypted
# with md5(secret); secret = first 5 chars of the APK signing-cert DER hex ("30820").
PART1 = '71cf27bb3c0bcdf207b64abecddc970098c7421ee7203b9cdae54478478a199e7d5a6e1a57691123c1a931c057842fb73ba3b3c83bcd69c17ccf174081e3d8aa'
PART2 = 'lpw6vgqzsp'

DEFAULT_BASES = ['https://jdforrepam.com', 'https://staging.letidi.com']


class JavDB:
    def __init__(self, base=None, app_version='1.9.29', token=None, language='zh', timeout=25,
                 username=None, password=None):
        self.base = base or DEFAULT_BASES[0]
        self.app_version = app_version
        self.token = token
        self.language = language
        self.timeout = timeout
        self.username = username
        self.password = password
        self._device = {
            'device_uuid': 'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
            'device_name': 'SM-G991B',
            'device_model': 'samsung SM-G991B',
            'platform': 'android',
            'app_channel': 'official',
            'system_version': '12',
            'app_version': app_version,
            'app_version_number': app_version,
        }
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE

    def login(self):
        body = {'username': self.username, 'password': self.password}
        body.update(self._device)
        r = self.request('/api/v1/sessions', method='POST', body=body, auth=False)
        if r.get('success') == 1 and (r.get('data') or {}).get('token'):
            self.token = r['data']['token']
            return True
        raise RuntimeError('login failed: %s' % r.get('message'))

    def _sign(self):
        ts = int(time.time())
        h = hashlib.md5(('%d%s' % (ts, PART1)).encode()).hexdigest()
        return '%d.%s.%s' % (ts, PART2, h)

    def _headers(self):
        h = {
            'jdsignature': self._sign(),
            'accept-language': self.language,
            'accept': 'application/json',
            'user-agent': 'Dart/3.5 (dart:io)',
        }
        if self.token:
            h['authorization'] = 'Bearer %s' % self.token
        return h

    def request(self, path, params=None, method='GET', body=None, auth=False, _retry=True):
        p = dict(params or {})
        p.setdefault('platform', 'android')
        p.setdefault('app_channel', 'official')
        p.setdefault('app_version', self.app_version)
        p.setdefault('app_version_number', self.app_version)
        qs = urllib.parse.urlencode(p)
        url = self.base + path + ('?' + qs if qs else '')
        headers = self._headers()
        data = None
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            headers['content-type'] = 'application/json'
        req = urllib.request.Request(url, headers=headers, method=method, data=data)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx)
            raw = resp.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            raw = e.read().decode('utf-8', 'replace')
        try:
            r = json.loads(raw)
        except Exception:
            return {'success': 0, 'raw': raw}
        # auto re-login once on JWT expiry
        if (r.get('action') == 'JWTVerificationError' and _retry and self.username and self.password):
            try:
                self.login()
                return self.request(path, params=params, method=method, body=body, auth=auth, _retry=False)
            except Exception:
                pass
        return r

    # ---------- endpoint wrappers ----------
    def startup(self):
        return self.request('/api/v1/startup')

    def movies_latest(self, page=1):
        return self.request('/api/v1/movies/latest', {'page': page})

    def movies_top(self, page=1):
        return self.request('/api/v1/movies/top', {'page': page})

    def movie_detail(self, movie_id):
        return self.request('/api/v4/movies/%s' % movie_id)

    def movie_magnets(self, movie_id):
        return self.request('/api/v1/movies/%s/magnets' % movie_id)

    def search(self, keyword, page=1):
        return self.request('/api/v2/search', {'q': keyword, 'page': page})

    # ---------- comments / reviews ----------
    def movie_reviews(self, movie_id, page=1, review_type=None, sort=None):
        params = {'page': page}
        if review_type:
            params['type'] = review_type
        if sort:
            params['sort_by'] = sort
        return self.request('/api/v1/movies/%s/reviews' % movie_id, params)

    def hot_reviews(self, period='weekly', page=1):
        return self.request('/api/v1/reviews/hotly', {'period': period, 'page': page})

    def user_reviews(self, user_id, page=1):
        return self.request('/api/v2/users/%s/reviews' % user_id, {'page': page})

    # ---------- high-level crawlers ----------
    @staticmethod
    def _meta_has_next(resp):
        d = resp.get('data') or {}
        for key in ('meta', 'pagination'):
            m = d.get(key)
            if isinstance(m, dict):
                cp = m.get('current_page') or m.get('page')
                tp = m.get('total_pages') or m.get('last_page')
                if tp is not None and cp is not None:
                    return int(cp) < int(tp)
        # fall back: page full of results implies more
        return None

    def crawl_movie_reviews(self, movie_id, max_pages=100, sleep=0.4):
        """Yield all reviews for a movie, following pagination."""
        page = 1
        while page <= max_pages:
            r = self.movie_reviews(movie_id, page=page)
            if r.get('success') != 1:
                break
            reviews = (r.get('data') or {}).get('reviews') or []
            for rv in reviews:
                yield rv
            nxt = self._meta_has_next(r)
            if nxt is False:
                break
            if not reviews:
                break
            page += 1
            time.sleep(sleep)

    def crawl_hot_reviews(self, period='weekly', max_pages=50, sleep=0.4):
        page = 1
        while page <= max_pages:
            r = self.hot_reviews(period=period, page=page)
            if r.get('success') != 1:
                break
            reviews = (r.get('data') or {}).get('reviews') or []
            for rv in reviews:
                yield rv
            nxt = self._meta_has_next(r)
            if nxt is False:
                break
            if not reviews:
                break
            page += 1
            time.sleep(sleep)


def _dump_reviews(reviews, outfile):
    rows = []
    for rv in reviews:
        mv = rv.get('movie') or {}
        rows.append({
            'review_id': rv.get('id'),
            'user_id': rv.get('user_id'),
            'username': rv.get('username'),
            'score': rv.get('score'),
            'likes_count': rv.get('likes_count'),
            'created_at': rv.get('created_at'),
            'movie_id': mv.get('id'),
            'movie_number': mv.get('number'),
            'movie_title': mv.get('title'),
            'content': rv.get('content'),
        })
    fields = list(rows[0].keys()) if rows else ['review_id']
    with open(outfile, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)
    return len(rows)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='JavDB mobile API client / comment crawler')
    sub = ap.add_subparsers(dest='cmd')

    sub.add_parser('startup')
    p_lat = sub.add_parser('latest'); p_lat.add_argument('--page', type=int, default=1)
    p_s = sub.add_parser('search'); p_s.add_argument('q'); p_s.add_argument('--page', type=int, default=1)
    p_mr = sub.add_parser('movie-reviews'); p_mr.add_argument('movie_id'); p_mr.add_argument('--pages', type=int, default=100); p_mr.add_argument('--out')
    p_hr = sub.add_parser('hot-reviews'); p_hr.add_argument('--period', default='weekly'); p_hr.add_argument('--pages', type=int, default=50); p_hr.add_argument('--out')
    p_ur = sub.add_parser('user-reviews'); p_ur.add_argument('user_id'); p_ur.add_argument('--page', type=int, default=1)

    args = ap.parse_args()
    c = JavDB()
    if args.cmd == 'startup':
        print(json.dumps(c.startup(), ensure_ascii=False, indent=2)[:2000])
    elif args.cmd == 'latest':
        print(json.dumps(c.movies_latest(args.page), ensure_ascii=False, indent=2)[:2000])
    elif args.cmd == 'search':
        print(json.dumps(c.search(args.q, args.page), ensure_ascii=False, indent=2)[:3000])
    elif args.cmd == 'movie-reviews':
        revs = list(c.crawl_movie_reviews(args.movie_id, max_pages=args.pages))
        if args.out:
            n = _dump_reviews(revs, args.out); print('wrote %d reviews -> %s' % (n, args.out))
        else:
            print(json.dumps(revs, ensure_ascii=False, indent=2)[:4000])
    elif args.cmd == 'hot-reviews':
        revs = list(c.crawl_hot_reviews(args.period, max_pages=args.pages))
        if args.out:
            n = _dump_reviews(revs, args.out); print('wrote %d reviews -> %s' % (n, args.out))
        else:
            print(json.dumps(revs, ensure_ascii=False, indent=2)[:4000])
    elif args.cmd == 'user-reviews':
        print(json.dumps(c.user_reviews(args.user_id, args.page), ensure_ascii=False, indent=2)[:3000])
    else:
        ap.print_help()
