"""Tests for the Oura connector against a fake Oura built from Oura's API v2 documentation and its official sandbox
payloads (tests/fixtures/*.json were fetched from https://api.ouraring.com/v2/sandbox/usercollection/<type>).

Covers: setup keys, the key test at both token endpoints, OAuth state, the documented -> newer token endpoint
fallback, single-use refresh-token rotation, windowed + next_token-paged backfill with 401/429 handling, incremental
overlap, scopes the owner did not share, deletions, the wizard.health-data/1 contract (normalized days, paging,
changed_since cursor, dataset id, box-only reads), the activity-effort mapping, HTTP/MCP guards, disconnect.
Run: python3 -m unittest discover -s tests       (OURA_LIVE_TEST=1 also probes Oura's real token endpoint)"""
import copy
import datetime as dt
import importlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / 'tests' / 'fixtures'
CID, SECRET = 'test-client-id-0001', 'test-secret-value-XYZ-123'
DAYS = 60
PAGE = 10           # small pages so every window pages with next_token


def fixture(kind, i=0):
    return copy.deepcopy(json.loads((FIX / f'{kind}.json').read_text())['data'][i])


def make_data(today):
    """DAYS days of documents ending yesterday, shaped exactly like Oura's sandbox documents."""
    out = {k: [] for k in ('daily_readiness', 'daily_sleep', 'sleep', 'daily_activity', 'workout', 'daily_spo2')}
    for i in range(DAYS):
        day = (today - dt.timedelta(days=DAYS - i)).isoformat()
        ts = day + 'T00:00:00.000+07:00'
        r = fixture('daily_readiness'); r.update(id=f'dr-{day}', day=day, timestamp=ts, score=[20, 50, 80][i % 3], temperature_deviation=-0.12)
        out['daily_readiness'].append(r)
        s = fixture('daily_sleep'); s.update(id=f'ds-{day}', day=day, timestamp=ts, score=70 + i % 20)
        out['daily_sleep'].append(s)
        prev = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
        night = fixture('sleep', 1)       # the sandbox long_sleep document
        night.update(id=f'sl-{day}', day=day, type='long_sleep', bedtime_start=prev + 'T23:10:00+07:00', bedtime_end=day + 'T07:00:00+07:00',
                     time_in_bed=28200, total_sleep_duration=25200, awake_time=3000, light_sleep_duration=14400, deep_sleep_duration=5400,
                     rem_sleep_duration=5400, average_hrv=40 + i % 9, lowest_heart_rate=50 + i % 4, average_heart_rate=58.5, average_breath=14.6,
                     efficiency=89, latency=600)
        out['sleep'].append(night)
        if i % 5 == 0:
            nap = fixture('sleep', 0)     # the sandbox late_nap document
            nap.update(id=f'nap-{day}', day=day, bedtime_start=day + 'T14:00:00+07:00', bedtime_end=day + 'T14:40:00+07:00')
            out['sleep'].append(nap)
        if i % 7 == 0:
            rest = fixture('sleep', 0)
            rest.update(id=f'rest-{day}', day=day, type='rest', total_sleep_duration=0)
            out['sleep'].append(rest)
        a = fixture('daily_activity')
        a.update(id=f'da-{day}', day=day, timestamp=ts, score=60 + i % 30, steps=6000 + 100 * i, total_calories=2300 + i, active_calories=350,
                 low_activity_time=10800, low_activity_met_minutes=360, medium_activity_time=1800 + 60 * (i % 10), medium_activity_met_minutes=150 + 20 * (i % 10),
                 high_activity_time=600 * (i % 4), high_activity_met_minutes=80 * (i % 4))
        out['daily_activity'].append(a)
        if i % 3 == 0:
            w = fixture('workout')
            w.update(id=f'wo-{day}', day=day, activity='running', start_datetime=day + 'T17:00:00+07:00', end_datetime=day + 'T17:45:00+07:00',
                     calories=420.4, distance=7012.3, intensity='hard', source='confirmed', label=None)
            out['workout'].append(w)
        o = fixture('daily_spo2'); o.update(id=f'sp-{day}', day=day, spo2_percentage={'average': 96.8})
        out['daily_spo2'].append(o)
    return out


class Fake:
    def __init__(self):
        self.today = dt.datetime.now(dt.timezone.utc).date()
        self.data = make_data(self.today)
        self.home = 'moi'                # which token endpoint knows this client (new-portal application)
        self.access, self.refresh = None, None
        self.used_refresh = set()
        self.calls = []
        self.force_401 = 0
        self.force_429 = 0
        self.forbid = set()              # collections answering 403 (scope not shared)
        self.revoked = False


FAKE = Fake()


class FakeOura(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body, headers=None):
        b = json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def issue(self):
        FAKE.access = 'acc-' + os.urandom(6).hex()
        FAKE.refresh = 'ref-' + os.urandom(6).hex()
        return {'token_type': 'bearer', 'access_token': FAKE.access, 'refresh_token': FAKE.refresh, 'expires_in': 86400}

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        f = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        where = {'/oauth/token': 'legacy', '/oauth/v2/ext/oauth-token': 'moi'}.get(self.path)
        FAKE.calls.append(('POST', where, f.get('grant_type')))
        if not where:
            return self.reply(404, {})
        bad_client = (lambda: self.reply(400, {'status': 400, 'error': 'invalid_client', 'error_description': 'Invalid client_id.'})) if where == 'legacy' \
            else (lambda: self.reply(401, {'error': 'invalid_client'}))       # the two real answers, captured from Oura
        if where != FAKE.home or f.get('client_id') != CID or f.get('client_secret') != SECRET:
            return bad_client()
        if f.get('grant_type') == 'authorization_code':
            if f.get('code') == 'good-code' and f.get('redirect_uri'):
                return self.reply(200, self.issue())
            return self.reply(400, {'error': 'invalid_grant'})
        if f.get('grant_type') == 'refresh_token':
            if f.get('refresh_token') == FAKE.refresh and f.get('refresh_token') not in FAKE.used_refresh:
                FAKE.used_refresh.add(f['refresh_token'])        # single-use, as Oura documents
                return self.reply(200, self.issue())
            return self.reply(400, {'error': 'invalid_grant'})
        return self.reply(400, {'error': 'unsupported_grant_type'})

    def do_GET(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        FAKE.calls.append(('GET', u.path, q))
        if not self.headers.get('User-Agent'):
            return self.reply(403, {'detail': 'User-Agent required'})
        if u.path == '/oauth/revoke':
            FAKE.revoked = q.get('access_token') == FAKE.access
            return self.reply(200, {})
        if self.headers.get('Authorization') != 'Bearer ' + str(FAKE.access):
            return self.reply(401, {'detail': 'The access token provided is expired, revoked, malformed, or invalid for other reasons.'})
        if FAKE.force_401:
            FAKE.force_401 -= 1
            return self.reply(401, {'detail': 'expired'})
        if FAKE.force_429:
            FAKE.force_429 -= 1
            return self.reply(429, {'detail': 'Request Rate Limit Exceeded.'}, {'Retry-After': '0'})
        if u.path == '/v2/usercollection/personal_info':
            return self.reply(200, {'id': 'user-1', 'age': 40, 'weight': 70.0, 'height': 1.75, 'biological_sex': 'female', 'email': 'person@example.com'})
        kind = u.path.rsplit('/', 1)[-1]
        if not u.path.startswith('/v2/usercollection/') or kind not in FAKE.data:
            return self.reply(404, {'detail': 'Not Found'})
        if kind in FAKE.forbid:
            return self.reply(403, {'detail': 'Access forbidden.'})
        start = q.get('start_date') or (FAKE.today - dt.timedelta(days=1)).isoformat()
        end = q.get('end_date') or FAKE.today.isoformat()
        rows = sorted([r for r in FAKE.data[kind] if start <= r['day'] < end], key=lambda r: (r['day'], r['id']))   # end_date excluded, as the sandbox does
        off = int(q.get('next_token') or 0)
        page = rows[off:off + PAGE]
        return self.reply(200, {'data': page, 'next_token': str(off + PAGE) if off + PAGE < len(rows) else None})


def http(port, method, path, body=None, headers=None):
    h = {'Host': f'127.0.0.1:{port}'}
    if body is not None:
        h['Content-Type'] = 'application/json'
    h.update(headers or {})
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class OuraTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(('127.0.0.1', 0), FakeOura)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        fp = cls.fake.server_address[1]
        cls.tmp = tempfile.mkdtemp()
        os.environ.update({'DATA_DIR': cls.tmp + '/private', 'OURA_AUTH_URL': f'http://127.0.0.1:{fp}/oauth/authorize',
                           'OURA_TOKEN_URLS': f'http://127.0.0.1:{fp}/oauth/token,http://localhost:{fp}/oauth/v2/ext/oauth-token',
                           'OURA_REVOKE_URL': f'http://127.0.0.1:{fp}/oauth/revoke', 'OURA_API_BASE': f'http://127.0.0.1:{fp}',
                           'OURA_REQUEST_GAP': '0', 'OURA_BACKOFF': '0.01', 'PORT': '18998', 'WIZARD_TRUSTED_HOSTS': 'box.example.ts.net'})
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = importlib.reload(server)
        cls.app = ThreadingHTTPServer(('127.0.0.1', 0), cls.S.Handler)
        threading.Thread(target=cls.app.serve_forever, daemon=True).start()
        cls.port = cls.app.server_address[1]
        cls.S.TRUSTED_HOSTS.add(f'127.0.0.1:{cls.port}')

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()
        cls.app.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def files_text(self):
        return ''.join(p.read_bytes().decode('latin-1') for p in Path(self.tmp).rglob('*') if p.is_file())

    def wait_sync(self, timeout=30):
        t = time.time()
        time.sleep(0.05)
        while time.time() - t < timeout:
            if not self.S.SYNC_LOCK.locked() and self.S._state().get('last_sync') and not self.S._state().get('syncing'):
                return
            time.sleep(0.05)
        self.fail('sync did not finish')

    def gets(self, kind):
        return [c for c in FAKE.calls if c[0] == 'GET' and c[1] == '/v2/usercollection/' + kind]

    def test_flow(self):
        S = self.S
        legacy, moi = S.TOKEN_URLS
        # ---- from scratch
        self.assertEqual(S.status_info()['status'], 'not_configured')
        info = S.setup_info()
        self.assertEqual(info['scopes'], ['email', 'daily', 'workout', 'spo2'])
        self.assertFalse(info['personal_tokens'])
        self.assertEqual(info['default_redirect'], 'http://localhost:3000/callback')
        self.assertEqual(info['dashboard_url'], 'https://developer.ouraring.com/applications')
        with self.assertRaises(S.Problem):
            S.login_start()

        # ---- step 4: keys through the HTTP API the form uses; never echoed; Test at both token endpoints
        self.assertEqual(http(self.port, 'POST', '/api/keys', {'client_id': 'x', 'client_secret': SECRET})[0], 400)
        code, body = http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': 'wrong-secret-value', 'redirect_uri': 'http://localhost:3000/callback'})
        self.assertEqual(code, 200)
        FAKE.calls.clear()
        r = json.loads(http(self.port, 'POST', '/api/keys/test', {})[1])
        self.assertEqual(r['result'], 'invalid')
        self.assertIn('did not accept', r['message'])
        self.assertEqual([c[1] for c in FAKE.calls], ['legacy', 'moi'])
        code, body = http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': SECRET, 'redirect_uri': 'http://localhost:3000/callback'})
        self.assertEqual(code, 200)
        for s in (SECRET.encode(), CID.encode()):
            self.assertNotIn(s, body)
        self.assertTrue(json.loads(body)['keys_saved'])
        r = json.loads(http(self.port, 'POST', '/api/keys/test', {})[1])
        self.assertEqual(r['result'], 'valid', r)           # legacy: invalid_client, newer server: invalid_grant
        self.assertIn('recognised', r['message'])
        self.assertEqual(S._keys()['token_url'], moi)
        self.assertEqual(oct(os.stat(S.DATA).st_mode & 0o777), '0o700')
        self.assertEqual(oct(os.stat(S.DATA / 'client.json').st_mode & 0o777), '0o600')
        self.assertEqual(http(self.port, 'POST', '/api/keys', {'client_id': CID, 'client_secret': SECRET, 'redirect_uri': 'http://box.example/callback'})[0], 400)

        # ---- step 5: OAuth
        start = S.login_start()
        q = {k: v[0] for k, v in parse_qs(urlsplit(start['authorize_url']).query).items()}
        self.assertEqual((q['response_type'], q['client_id'], q['scope'], q['redirect_uri']), ('code', CID, 'email daily workout spo2', 'http://localhost:3000/callback'))
        self.assertGreaterEqual(len(q['state']), 16)
        self.assertTrue(start['paste_back'])
        with self.assertRaises(S.Problem):
            S.login_finish('http://localhost:3000/callback?code=good-code&state=not-the-state')
        with self.assertRaises(S.Problem) as e:
            S.login_finish(f"http://localhost:3000/callback?error=access_denied&state={q['state']}")
        self.assertIn('not to allow', e.exception.message)
        # forget the remembered endpoint so the documented one is tried first and the fallback is exercised
        k = S._keys(); k.pop('token_url'); S._write_secret('client.json', json.dumps(k))
        state = parse_qs(urlsplit(S.login_start()['authorize_url']).query)['state'][0]
        FAKE.calls.clear()
        FAKE.force_429, FAKE.force_401 = 1, 1        # first data calls: one rate limit, one expired token
        code, body = http(self.port, 'POST', '/api/oauth/finish', {'url': f'http://localhost:3000/callback?code=good-code&state={state}'})
        self.assertEqual(code, 200, body)
        self.wait_sync()
        posts = [c for c in FAKE.calls if c[0] == 'POST']
        self.assertEqual(posts[:2], [('POST', 'legacy', 'authorization_code'), ('POST', 'moi', 'authorization_code')])
        self.assertIn(('POST', 'moi', 'refresh_token'), posts)           # the forced 401 refreshed at the server that issued the token
        self.assertNotIn(('POST', 'legacy', 'refresh_token'), posts)
        self.assertEqual(S._tokens()['token_url'], moi)
        with self.assertRaises(S.Problem):          # state is single-use
            S.login_finish(f'http://localhost:3000/callback?code=good-code&state={state}')
        st = S.status_info()
        self.assertEqual(st['status'], 'connected', st)
        self.assertEqual(st['counts'], {'days': DAYS, 'sleeps': DAYS + DAYS // 5, 'workouts': DAYS // 3}, st)
        self.assertTrue(st['setup']['backfilled'])
        self.assertEqual(st['account'], {'name': None, 'email': 'person@example.com'})
        self.assertEqual(st['source'], {'id': 'wizard-oura', 'provider': 'oura', 'name': 'Oura', 'app_path': '/wizard-oura/'})
        self.assertEqual(st['scales']['load']['scale_max'], 21)
        self.assertEqual(st['scales']['load']['targets'], {'high': [14, 18], 'medium': [10, 14], 'low': [0, 10]})
        self.assertEqual(oct(os.stat(S.DATA / 'tokens.json').st_mode & 0o777), '0o600')
        # backfill: 90-day windows back from today+2, each paged with next_token, stops after two empty windows
        rd = self.gets('daily_readiness')
        windows = sorted({(c[2]['start_date'], c[2]['end_date']) for c in rd}, reverse=True)
        self.assertEqual(windows[0][1], (FAKE.today + dt.timedelta(days=2)).isoformat())
        self.assertEqual(len(windows), 3, windows)
        for (a, b), (_, d2) in zip(windows, windows[1:]):
            self.assertEqual(d2, a)                                        # consecutive windows, no gap
            self.assertEqual((dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days, 90)
        self.assertTrue(any(c[2].get('next_token') for c in rd))
        self.assertTrue(all('/oauth/' not in c[1] for c in self.gets('sleep')))
        self.assertEqual(st['history_from'], FAKE.data['daily_readiness'][0]['day'])
        self.assertEqual(st['latest_date'], FAKE.data['daily_readiness'][-1]['day'])

        # ---- the contract: normalized days, oldest first, complete records
        last = FAKE.data['daily_readiness'][-1]['day']
        r = S.contract_days({'from': last, 'to': last})
        self.assertEqual((r['contract'], r['source'], r['dataset']), ('wizard.health-data/1', 'wizard-oura', st['dataset']))
        d = r['days'][0]
        check_day(self, d)
        i = DAYS - 1
        self.assertEqual(d['readiness'], {'score': [20, 50, 80][i % 3], 'band': {20: 'low', 50: 'medium', 80: 'high'}[[20, 50, 80][i % 3]],
                                          'state': 'scored', 'calibrating': None})
        self.assertEqual((d['hrv_ms'], d['resting_hr_bpm'], d['respiratory_rate'], d['spo2_pct'], d['skin_temp_delta_c']),
                         (40 + i % 9, 50 + i % 4, 14.6, 96.8, -0.12))
        sl = d['sleep']
        self.assertEqual((sl['asleep_h'], sl['in_bed_h'], sl['awake_h'], sl['light_h'], sl['deep_h'], sl['rem_h']), (7.0, 7.83, 0.83, 4.0, 1.5, 1.5))
        self.assertEqual((sl['score'], sl['efficiency_pct'], sl['need_h']), (70 + i % 20, 89, None))
        self.assertEqual(sl['end'], last + 'T07:00:00+07:00')
        self.assertEqual(d['load']['scale_max'], 21)
        self.assertEqual(d['load']['label'], 'Activity effort')
        self.assertEqual(d['load']['value'], S.effort_load(FAKE.data['daily_activity'][i]))
        self.assertEqual((d['calories_kcal'], d['steps']), (2300 + i, 6000 + 100 * i))
        self.assertEqual(d['extras']['oura']['activity_score'], 60 + i % 30)
        self.assertEqual(d['extras']['oura']['sleep_id'], f'sl-{last}')
        # naps and rest periods
        nap_day = FAKE.data['daily_readiness'][55]['day']
        nd = S.contract_days({'from': nap_day, 'to': nap_day})['days'][0]
        self.assertEqual(nd['extras']['oura']['sleep_type'], 'long_sleep')
        self.assertEqual(len(nd['sleep']['naps']), 1)
        rest_day = FAKE.data['daily_readiness'][56]['day']
        self.assertEqual(S.contract_days({'from': rest_day, 'to': rest_day})['days'][0]['sleep']['naps'], [])   # 'rest' is not sleep
        withw = [x for x in S.contract_days({'limit': 500})['days'] if x['workouts']]
        self.assertEqual(len(withw), DAYS // 3)
        w = withw[0]['workouts'][0]
        self.assertEqual((w['sport'], w['minutes'], w['calories_kcal'], w['distance_m'], w['load'], w['extras']['oura']['intensity']),
                         ('running', 45, 420, 7012, None, 'hard'))
        with self.assertRaises(S.Problem):
            S.contract_days({'from': 'yesterday'})
        with self.assertRaises(S.Problem):
            S.contract_days({'changed_since': 'w1.5'})
        # paging: pages of 25, oldest first, no gaps or repeats, same cursor on every page
        seen, page, cursors = [], None, set()
        while True:
            r = S.contract_days(dict({'limit': 25}, **({'page': page} if page else {})))
            seen += [x['date'] for x in r['days']]
            cursors.add(r['cursor'])
            page = r['next_page']
            if not page:
                break
        self.assertEqual(len(seen), DAYS)
        self.assertEqual(seen, sorted(set(seen)))
        self.assertEqual(len(cursors), 1)
        cursor = cursors.pop()
        # changed_since: an unchanged re-sync re-emits nothing; a re-scored sleep, a new workout and a deleted nap do
        self.assertEqual(S.contract_days({'changed_since': cursor})['days'], [])
        S.sync(full=True)
        self.assertEqual(S.contract_days({'changed_since': cursor})['days'], [], 'an unchanged overlap re-sync must not re-emit days')
        d40, d50 = FAKE.data['daily_sleep'][40]['day'], FAKE.data['daily_readiness'][50]['day']
        FAKE.data['daily_sleep'][40]['score'] = 61
        FAKE.data['workout'].append(dict(fixture('workout'), id='wo-new', day=last, activity='cycling',
                                         start_datetime=last + 'T08:00:00+07:00', end_datetime=last + 'T08:30:00+07:00'))
        FAKE.data['sleep'] = [x for x in FAKE.data['sleep'] if x['id'] != f'nap-{d50}']
        S.sync(full=True)          # the overlap window covers only the last days; a full pass reaches day 40
        ch = S.contract_days({'changed_since': cursor})
        self.assertEqual([x['date'] for x in ch['days']], sorted([d40, d50, last]))
        byd = {x['date']: x for x in ch['days']}
        self.assertEqual(byd[d40]['sleep']['score'], 61)
        self.assertEqual(byd[d50]['sleep']['naps'], [])
        self.assertEqual(sorted(x['sport'] for x in byd[last]['workouts']), ['cycling'] + (['running'] if (DAYS - 1) % 3 == 0 else []))
        self.assertNotEqual(ch['cursor'], cursor)
        self.assertEqual(S.contract_days({'changed_since': ch['cursor']})['days'], [])
        self.assertEqual(len(S.contract_days({'changed_since': ''})['days']), DAYS)

        # ---- token refresh rotates (single-use) and the new refresh token is persisted first
        t = S._tokens()
        old_refresh = t['refresh_token']
        t['expires_at'] = time.time() - 10
        S._save_tokens(t)
        S.api_get('/v2/usercollection/personal_info')
        t2 = S._tokens()
        self.assertNotEqual(t2['refresh_token'], old_refresh)
        self.assertEqual((t2['refresh_token'], t2['access_token']), (FAKE.refresh, FAKE.access))
        self.assertIn(old_refresh, FAKE.used_refresh)

        # ---- incremental sync asks from last sync minus the overlap; a scope the owner did not share is skipped
        FAKE.calls.clear()
        FAKE.forbid = {'daily_spo2'}
        S.sync()
        FAKE.forbid = set()
        rd = self.gets('daily_readiness')
        self.assertTrue(rd)
        self.assertEqual({c[2]['start_date'] for c in rd}, {(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=S.OVERLAP_DAYS)).date().isoformat()})
        st = S.status_info()
        self.assertEqual(st['setup']['not_shared'], ['daily_spo2'])
        self.assertIsNone(st['error'])
        self.assertEqual(st['counts']['days'], DAYS)
        self.assertEqual(S.contract_days({'from': last, 'to': last})['days'][0]['spo2_pct'], 96.8)   # kept, not pruned

        # ---- a required collection refused (membership ended) surfaces as the sync error
        FAKE.forbid = {'daily_readiness'}
        with self.assertRaises(S.OuraError):
            S.sync()
        FAKE.forbid = set()
        self.assertIn('Membership', S.status_info()['error'])
        S.sync()

        # ---- 429 that never clears -> paused for 15 minutes, data still readable
        FAKE.force_429 = 99
        with self.assertRaises(S.OuraError):
            S.sync()
        FAKE.force_429 = 0
        self.assertEqual(S.status_info()['status'], 'rate_limited')
        self.assertEqual(len(S.contract_days({})['days']), DAYS)
        S._update_state(rate_limited_until=None)

        # ---- HTTP + MCP
        names = [t['name'] for t in json.loads(http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})[1])['result']['tools']]
        self.assertEqual(names, ['health_data_status', 'health_data_sync', 'health_data_days'])
        for name, args in [('health_data_status', {}), ('health_data_days', {'from': last}), ('health_data_days', {'changed_since': ''})]:
            code, body = http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
            res = json.loads(body)['result']
            self.assertFalse(res['isError'], (name, res))
            for secret in (SECRET, FAKE.access, FAKE.refresh, CID):
                self.assertNotIn(secret, body.decode())
        self.assertEqual(http(self.port, 'POST', '/mcp', {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'}, {'Tailscale-User-Login': 'a@b.c'})[0], 403)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'evil.example:18998'})[0], 403)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'box.example.ts.net:8444'})[0], 200)
        self.assertEqual(http(self.port, 'POST', '/api/sync', {}, {'Host': 'box.example.ts.net:8444', 'Origin': 'https://evil.example'})[0], 403)
        for path in ('/api/status', '/api/setup', '/api/days', '/api/days?changed_since=', '/wizard-oura/api/status', '/healthz', '/', '/icon.svg'):
            code, body = http(self.port, 'GET', path)
            self.assertEqual(code, 200, path)
            for secret in (SECRET, FAKE.access, FAKE.refresh):
                self.assertNotIn(secret, body.decode(), path)
        for hdr in ('X-Forwarded-For', 'Tailscale-User-Login', 'Forwarded', 'X-Real-IP', 'CF-Connecting-IP'):
            self.assertEqual(http(self.port, 'GET', '/api/days', headers={hdr: 'x'})[0], 403, hdr)
        self.assertEqual(http(self.port, 'GET', '/api/status', headers={'Host': 'box.example.ts.net:8444', 'X-Forwarded-For': '100.1.2.3'})[0], 200)

        # ---- the app's own callback route (https Redirect URI), with an RFC 9207 issuer naming the newer server
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        state = parse_qs(urlsplit(S.login_start()['authorize_url']).query)['state'][0]
        FAKE.calls.clear()
        S.TOKEN_URLS[1] = moi.replace('localhost', 'moi.ouraring.com')     # pretend the issuer host matches the newer endpoint
        self.assertEqual(S._issuer_first('https://moi.ouraring.com/oauth/v2/oauth-anonymous'), S.TOKEN_URLS[1])
        self.assertIsNone(S._issuer_first('https://evil.example'))
        S.TOKEN_URLS[1] = moi
        code, body = http(self.port, 'GET', f'/wizard-oura/callback?code=good-code&state={state}')
        self.assertEqual(code, 200)
        self.assertIn(b'Oura connected', body)
        self.assertEqual([c for c in FAKE.calls if c[0] == 'POST'][0], ('POST', 'moi', 'authorization_code'))   # remembered endpoint first
        self.wait_sync()

        # ---- refresh refused -> needs sign-in, tokens removed, data kept
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        FAKE.refresh = 'rotated-elsewhere'
        t = S._tokens(); t['expires_at'] = 0; S._save_tokens(t)
        with self.assertRaises(S.Problem):
            S.api_get('/v2/usercollection/personal_info')
        st = S.status_info()
        self.assertEqual(st['status'], 'needs_login')
        self.assertFalse((S.DATA / 'tokens.json').exists())
        self.assertEqual(st['counts']['days'], DAYS)
        ds = st['dataset']

        # ---- disconnect wipes data and revokes; forget wipes keys from every file
        state = parse_qs(urlsplit(S.login_start()['authorize_url']).query)['state'][0]
        S.login_finish(f'http://localhost:3000/callback?code=good-code&state={state}')
        self.wait_sync()
        while S.SYNC_LOCK.locked():
            time.sleep(0.05)
        access, refresh = FAKE.access, FAKE.refresh
        code, body = http(self.port, 'POST', '/api/disconnect', {'forget_keys': True})
        self.assertEqual((code, json.loads(body)['status']), (200, 'not_configured'))
        self.assertTrue(FAKE.revoked)
        blob = self.files_text()
        for secret in (SECRET, CID, access, refresh):
            self.assertNotIn(secret, blob)
        st = S.status_info()
        self.assertEqual(st['counts'], {'days': 0, 'sleeps': 0, 'workouts': 0})
        self.assertNotEqual(st['dataset'], ds, 'readers must see the copy was wiped')


class MappingTest(unittest.TestCase):
    """Oura's own sandbox documents (2025-01-01/02) mapped onto the contract, field by field."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'app'))
        import server
        cls.S = server

    def docs(self, day):
        out = {}
        for kind in ('daily_readiness', 'daily_sleep', 'sleep', 'daily_activity', 'workout', 'daily_spo2'):
            out[kind] = [self.S._slim(kind, x) for x in json.loads((FIX / f'{kind}.json').read_text())['data'] if x['day'] == day]
        return out

    def test_sandbox_day(self):
        d = self.S._n_day('2025-01-02', self.docs('2025-01-02'), ['2026-10-31T00:00:00.000Z'])
        check_day(self, d)
        raw = {k: json.loads((FIX / f'{k}.json').read_text())['data'] for k in ('daily_readiness', 'daily_sleep', 'sleep', 'daily_activity', 'daily_spo2', 'workout')}
        rd = next(x for x in raw['daily_readiness'] if x['day'] == '2025-01-02')
        sl = next(x for x in raw['sleep'] if x['day'] == '2025-01-02')
        ds = next(x for x in raw['daily_sleep'] if x['day'] == '2025-01-02')
        act = next(x for x in raw['daily_activity'] if x['day'] == '2025-01-02')
        self.assertEqual(sl['type'], 'long_sleep')
        self.assertEqual(d['readiness']['score'], rd['score'])
        self.assertEqual(d['readiness']['band'], self.S._band(rd['score']))
        self.assertEqual(d['hrv_ms'], sl['average_hrv'])
        self.assertEqual(d['resting_hr_bpm'], sl['lowest_heart_rate'])
        self.assertEqual(d['respiratory_rate'], sl['average_breath'])
        self.assertEqual(d['skin_temp_delta_c'], rd['temperature_deviation'])
        self.assertEqual(d['sleep']['asleep_h'], round(sl['total_sleep_duration'] / 3600, 2))
        self.assertEqual(d['sleep']['in_bed_h'], round(sl['time_in_bed'] / 3600, 2))
        self.assertEqual(d['sleep']['deep_h'], round(sl['deep_sleep_duration'] / 3600, 2))
        self.assertEqual(d['sleep']['score'], ds['score'])
        self.assertIsNone(d['sleep']['need_h'])
        self.assertEqual((d['sleep']['start'], d['sleep']['end']), (sl['bedtime_start'], sl['bedtime_end']))
        self.assertEqual(d['calories_kcal'], act['total_calories'])
        self.assertEqual(d['steps'], act['steps'])
        self.assertEqual(d['extras']['oura']['activity_score'], act['score'])
        self.assertEqual(d['extras']['oura']['readiness_contributors'], rd['contributors'])
        self.assertEqual(d['spo2_pct'], next(x for x in raw['daily_spo2'] if x['day'] == '2025-01-02')['spo2_percentage']['average'])
        self.assertEqual(d['load']['value'], self.S.effort_load(act))
        self.assertEqual(len(d['workouts']), 1)
        self.assertEqual(d['workouts'][0]['sport'], next(x for x in raw['workout'] if x['day'] == '2025-01-02')['activity'])
        # 2025-01-01 has only a late_nap: it becomes the day's sleep (no long_sleep), with no naps left over
        d1 = self.S._n_day('2025-01-01', self.docs('2025-01-01'), [])
        self.assertEqual(d1['extras']['oura']['sleep_type'], 'late_nap')
        self.assertEqual(d1['sleep']['naps'], [])
        # heavy per-minute series are not stored
        self.assertNotIn('heart_rate', self.docs('2025-01-02')['sleep'][0])
        self.assertNotIn('met', self.docs('2025-01-02')['daily_activity'][0])

    def test_empty_and_partial(self):
        d = self.S._n_day('2026-01-01', {}, [])
        check_day(self, d)
        self.assertEqual((d['readiness'], d['sleep'], d['load'], d['workouts']), (None, None, None, []))
        d = self.S._n_day('2026-01-01', {'daily_readiness': [{'id': 'x', 'day': '2026-01-01', 'score': None, 'contributors': {}}]}, [])
        self.assertEqual(d['readiness'], {'score': None, 'band': None, 'state': 'pending', 'calibrating': None})

    def test_effort_load(self):
        """Anchors documented in server.py and docs/health-data-contract.md."""
        L = self.S.effort_load

        def day(low_min=0, low_met=0.0, med_min=0, med_met=0.0, high_min=0, high_met=0.0):
            return {'low_activity_time': low_min * 60, 'low_activity_met_minutes': low_min * low_met,
                    'medium_activity_time': med_min * 60, 'medium_activity_met_minutes': med_min * med_met,
                    'high_activity_time': high_min * 60, 'high_activity_met_minutes': high_min * high_met}
        desk = L(day(low_min=120, low_met=2.0, med_min=10, med_met=4.0))
        active = L(day(low_min=180, low_met=2.0, med_min=60, med_met=5.0, high_min=15, high_met=6.7))
        run = L(day(low_min=180, low_met=2.0, med_min=45, med_met=4.5, high_min=60, high_met=9.0))
        huge = L(day(low_min=200, low_met=2.0, med_min=60, med_met=5.0, high_min=200, high_met=10.0))
        self.assertTrue(6 <= desk <= 8.5, desk)
        self.assertTrue(12 <= active <= 15, active)
        self.assertTrue(16.5 <= run <= 18.5, run)
        self.assertEqual(huge, 21.0)
        self.assertEqual(L(day()), 0.0)
        self.assertIsNone(L({}))
        self.assertLess(desk, active)
        self.assertLess(active, run)


@unittest.skipUnless(os.environ.get('OURA_LIVE_TEST') == '1', 'set OURA_LIVE_TEST=1 to call the real Oura token endpoints')
class LiveTokenEndpointTest(unittest.TestCase):
    """The Test button against Oura's REAL token endpoints with made-up credentials must say 'not accepted'."""

    def test_fake_credentials_rejected(self):
        tmp = tempfile.mkdtemp()
        try:
            env = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith('OURA_') and k != 'OURA_LIVE_TEST'}
            os.environ['DATA_DIR'] = tmp
            sys.path.insert(0, str(ROOT / 'app'))
            import server
            S = importlib.reload(server)
            self.assertTrue(S.TOKEN_URLS[0].startswith('https://api.ouraring.com/'))
            S.save_keys({'client_id': 'wizardFakeClientId0001', 'client_secret': 'wizard-fake-secret-0001', 'redirect_uri': 'http://localhost:3000/callback'})
            r = S.test_keys()
            print('\nLIVE Oura token endpoints ->', r)
            self.assertEqual(r['result'], 'invalid', r)
            self.assertIn('did not accept', r['message'])
            os.environ.update(env)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


DAY_KEYS = {'date', 'updated_at', 'readiness', 'hrv_ms', 'resting_hr_bpm', 'spo2_pct', 'skin_temp_c', 'skin_temp_delta_c', 'respiratory_rate',
            'sleep', 'load', 'calories_kcal', 'energy_kj', 'avg_hr_bpm', 'max_hr_bpm', 'steps', 'workouts', 'extras'}
SLEEP_KEYS = {'start', 'end', 'asleep_h', 'in_bed_h', 'awake_h', 'light_h', 'deep_h', 'rem_h', 'score', 'efficiency_pct', 'respiratory_rate', 'need_h', 'naps'}
WORKOUT_KEYS = {'id', 'sport', 'start', 'end', 'minutes', 'load', 'avg_hr_bpm', 'max_hr_bpm', 'calories_kcal', 'energy_kj', 'distance_m', 'zones_min', 'extras'}


def check_day(t, d):
    """The day record shape promised by docs/health-data-contract.md."""
    t.assertTrue(DAY_KEYS <= set(d), DAY_KEYS - set(d))
    t.assertRegex(d['date'], r'^\d{4}-\d{2}-\d{2}$')
    if d['readiness']:
        t.assertIn(d['readiness']['band'], ('high', 'medium', 'low', None))
        t.assertIn(d['readiness']['state'], ('scored', 'pending', 'unscorable'))
    if d['sleep']:
        t.assertTrue(SLEEP_KEYS <= set(d['sleep']), SLEEP_KEYS - set(d['sleep']))
    if d['load']:
        t.assertTrue({'value', 'max', 'label', 'scale_max'} <= set(d['load']))
        t.assertEqual(d['load']['max'], d['load']['scale_max'])
        t.assertTrue(0 <= d['load']['value'] <= d['load']['scale_max'])
    for w in d['workouts']:
        t.assertTrue(WORKOUT_KEYS <= set(w), WORKOUT_KEYS - set(w))
    t.assertEqual(set(d['extras']), {'oura'})


class IconTest(unittest.TestCase):
    """Own icon: full-colour store square (icon.png from wizard.md) and a one-colour silhouette at web/icon.svg the
    shell tints. A generic ring with a pulse line; never Oura's own logo or wordmark."""
    root = ROOT

    def test_icons(self):
        self.assertIn('\nicon: icon.png\n', (self.root / 'wizard.md').read_text())
        png = (self.root / 'icon.png').read_bytes()
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual((int.from_bytes(png[16:20], 'big'), int.from_bytes(png[20:24], 'big')), (1024, 1024))
        mask = (self.root / 'web' / 'icon.svg').read_text()
        self.assertIn('viewBox="0 0 512 512"', mask)
        self.assertNotRegex(mask, r'<text|<image|href=|fill="#(?!fff|000)')
        self.assertNotIn('OURA', (mask + (self.root / "tools" / "icon.svg").read_text()).upper())


if __name__ == '__main__':
    unittest.main()
