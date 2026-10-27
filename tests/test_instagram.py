"""Graph responses are synthetic and restricted to temporary SQLite databases."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from food_trends.config import InstagramConfig, load_env
from food_trends.storage import Storage, utcnow
from food_trends.service import CollectionService
from food_trends.instagram_graph import GraphClient, GraphError
from food_trends.instagram_service import InstagramService
from food_trends.api import make_server
from food_trends.__main__ import instagram_command
from test_mvp import fixture

FAKE_TOKEN = 'synthetic-credential-do-not-log'
MEDIA = {'id': '1001', 'timestamp': '2026-10-20T12:00:00+0000',
         'media_type': 'IMAGE', 'permalink': 'https://www.instagram.com/p/synthetic/',
         'caption': 'unnecessary private caption', 'username': 'never_store_author'}


class Transport:
    def __init__(self):
        self.calls = []
        self.remote_ids = []
        self.media_pages = [{'data': [MEDIA, MEDIA]}]
        self.error = None
        self.media_error = None
        self.token_valid = True
        self.token_app = 'synthetic-app'
        self.token_scopes = ['instagram_basic', 'pages_show_list', 'pages_read_engagement']
        self.outbound_urls = []
        self.outbound_methods = []

    def __call__(self, request, timeout):
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)
        path = parsed.path.split('/', 2)[-1]
        self.outbound_urls.append(request.full_url)
        self.outbound_methods.append(request.get_method())
        batch = request.data and parse_qs(request.data.decode()).get('batch')
        if batch:
            inner = json.loads(batch[0])[0]
            assert inner['method'] == 'GET'
            parsed = urlsplit(inner['relative_url'])
            path, query = parsed.path, parse_qs(parsed.query)
        elif request.data:
            query = parse_qs(request.data.decode())
        self.calls.append((path, query, request.headers))
        if self.error:
            raise self.error
        if path.endswith('recently_searched_hashtags'):
            result = {'data': [{'id': n} for n in self.remote_ids]}
        elif path == 'ig_hashtag_search':
            result = {'data': [{'id': '900'+str(len(query['q'][0]))}]}
        elif path.endswith('recent_media'):
            if self.media_error:
                raise self.media_error
            index = int(query.get('after', ['0'])[0])
            result = self.media_pages[index]
            if isinstance(result, Exception):
                raise result
        elif path == 'me/accounts':
            result = {'data': [{'id': '1397807253414629', 'instagram_business_account': {'id': '17841473668787556'}}]}
        elif path == 'debug_token':
            result = {'data': {'is_valid': self.token_valid, 'expires_at': int(time.time())+3600,
                               'issued_at':int(time.time())-60, 'type':'USER', 'app_id':self.token_app,
                               'scopes':self.token_scopes}}
        elif path == 'oauth/access_token':
            result = {'access_token': 'synthetic-new-token', 'expires_in': 3600}
        else:
            raise AssertionError('Unexpected Graph path')
        if batch:
            result = [{'code':200,'body':json.dumps(result)}]
        return io.BytesIO(json.dumps(result).encode())


class InstagramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Storage.create(Path(self.temp.name)/'test.sqlite3')
        self.config = InstagramConfig(token=FAKE_TOKEN)
        self.transport = Transport()
        self.sleeps = []
        self.client = GraphClient(self.config, self.transport, self.sleeps.append)
        self.ig = InstagramService(self.store, self.config, self.client)
        self.rss = CollectionService(self.store, fetcher=lambda _: (fixture('두쫀쿠'), 200))
        self.rss.collect()
        self.candidate = self.store.trends()[0]['id']

    def tearDown(self):
        self.ig.stop_event.set()
        if self.ig.thread:
            self.ig.thread.join(2)
        self.temp.cleanup()

    def start(self, name='두쫀쿠'):
        return self.ig.start_tracking(name, self.candidate)

    def test_env_loading_preserves_process_and_existing_file(self):
        path = Path(self.temp.name)/'.env'
        text = "INSTAGRAM_ACCESS_TOKEN='synthetic-from-env'\nINSTAGRAM_COLLECTION_INTERVAL_MINUTES=75\nEXISTING=keep\n"
        path.write_text(text, encoding='utf-8')
        with patch.dict(os.environ, {'EXISTING':'process'}, clear=True):
            load_env(path)
            conf = InstagramConfig.from_env()
            self.assertTrue(conf.configured)
            self.assertEqual(conf.interval_minutes, 75)
            self.assertEqual(os.environ['EXISTING'], 'process')
            self.assertNotIn(conf.token, repr(conf))
        self.assertEqual(path.read_text(encoding='utf-8'), text)

    def test_no_token_no_requests_and_rss_unaffected(self):
        ig = InstagramService(self.store, replace(self.config, token=''))
        self.assertEqual(ig.status()['connection']['status'], 'not_configured')
        ig.start()
        self.assertIsNotNone(ig.thread)  # local retention maintenance also runs without credentials
        ig.stop_event.set();ig.thread.join(2)
        with self.assertRaises(GraphError):
            ig.start_tracking('두쫀쿠', self.candidate)
        self.assertTrue(self.rss.collect()['success'])
        self.assertEqual(len(ig.store.tags()), 0)

    def test_discovery_never_calls_instagram_without_approval(self):
        self.ig.collect()
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.ig.store.tags(), [])

    def test_pagination_dedup_cache_metrics_and_privacy(self):
        self.transport.media_pages = [
            {'data': [MEDIA, MEDIA], 'paging': {'next': 'https://untrusted.invalid/?access_token=do-not-follow', 'cursors': {'after':'1'}}},
            {'data': [MEDIA, dict(MEDIA, id='1002')]},
        ]
        result = self.start()
        run = result['run']
        self.assertTrue(result['success'])
        self.assertEqual((run['returned_count'],run['new_count'],run['duplicate_count'],run['request_count']), (4,2,2,4))
        self.assertEqual(self.client.request_count, 4)
        prior = len(self.transport.calls)
        self.assertTrue(self.start()['already_tracking'])
        self.assertEqual(len(self.transport.calls), prior)
        self.ig.collect()
        paths = [c[0] for c in self.transport.calls]
        self.assertEqual(paths.count('ig_hashtag_search'), 1)
        status = self.ig.status(self.candidate, include_media=True)
        h = status['hashtags'][0]
        self.assertEqual((h['total_observed'],h['new_24h'],h['daily'][0]['new_count'],len(h['daily']),len(h['hourly'])), (2,2,2,1,1))
        self.assertIsNone(h['change_24h'])
        self.assertNotEqual(h['media'][0]['published_at'], h['media'][0]['first_seen'])
        serialized = json.dumps(status)
        self.assertEqual(h['media'][0]['caption'],MEDIA['caption'])
        for secret in [FAKE_TOKEN, MEDIA['username'], 'do-not-follow']:
            self.assertNotIn(secret, serialized)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM instagram_media').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM instagram_searches').fetchone()[0], 1)

    def test_many_to_many_and_candidate_reuse(self):
        self.start()
        self.start('디저트')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM instagram_media').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM instagram_hashtag_media').fetchone()[0], 2)
        self.rss.fetcher = lambda _: (fixture('새 후보'),200)
        self.rss.collect()
        candidate = self.store.trends()[0]['id']
        calls = len(self.transport.calls)
        self.ig.start_tracking('두쫀쿠',candidate)
        self.assertEqual(len(self.transport.calls), calls)
        self.assertEqual(len(self.ig.status(candidate)['hashtags']), 1)

    def test_budget_remote_near_limit_blocks_new_search_but_allows_current(self):
        self.start()
        self.transport.remote_ids = [str(20000+n) for n in range(29)]
        self.ig.sync_budget()
        result = self.start('신규디저트')
        self.assertFalse(result['success'])
        self.assertEqual(result['error_type'], 'budget')
        self.assertEqual([c[0] for c in self.transport.calls].count('ig_hashtag_search'), 1)
        self.assertTrue(self.ig.collect(self.ig.store.tags()[0]['id'])['success'])
        self.assertTrue(self.ig.budget()['reference_only'])

    def test_budget_window_does_not_slide_and_cached_id_reuse_after_expiry(self):
        self.start()
        with self.store.connect() as db:
            before = db.execute('SELECT searched_at FROM instagram_searches').fetchone()[0]
        self.ig.collect()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT searched_at FROM instagram_searches').fetchone()[0], before)
            expired = (datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
            db.execute('UPDATE instagram_searches SET searched_at=?', (expired,))
        self.ig.collect()
        self.assertEqual([c[0] for c in self.transport.calls].count('ig_hashtag_search'), 1)
        self.assertEqual(len(self.ig.store.usage()), 1)

    def test_retries_auth_permission_and_safe_errors(self):
        self.start()
        for error,kind,retries in [(GraphError('rate_limit',4),'rate_limit',3), (TimeoutError('unsafe '+FAKE_TOKEN),'network',3), (GraphError('authentication',190,463),'authentication',1), (GraphError('permission',10),'permission',1)]:
            self.ig.store.state('connection',{'status':'unverified'})
            self.transport.media_error = error
            before = self.client.request_count
            result = self.ig.collect()['results'][0]
            self.assertFalse(result['success'])
            self.assertEqual(result['error_type'],kind)
            self.assertEqual(self.client.request_count-before,retries)
            self.assertNotIn(FAKE_TOKEN,json.dumps(result))
            self.assertEqual(self.ig.status()['connection']['status'],kind)
            self.assertTrue(self.rss.collect()['success'])
        self.transport.media_error = None
        self.ig.store.state('connection',{'status':'unverified'})
        self.assertTrue(self.ig.collect()['success'])

    def test_http_error_payload_is_sanitized(self):
        error_body = json.dumps({'error':{'code':190,'error_subcode':463,'message':FAKE_TOKEN}}).encode()
        self.transport.error = HTTPError('https://graph.facebook.com/?secret='+FAKE_TOKEN,400,FAKE_TOKEN,{},io.BytesIO(error_body))
        with self.assertRaises(GraphError) as raised:
            self.client.get('me/accounts')
        self.assertEqual(raised.exception.kind,'authentication')
        self.assertNotIn(FAKE_TOKEN,str(raised.exception))

    def test_partial_page_failure_and_page_cap_preserve_observations(self):
        self.transport.media_pages = [{'data':[MEDIA], 'paging':{'next':'unused','cursors':{'after':'1'}}}, GraphError('network')]
        result = self.start()
        self.assertEqual(result['run']['status'],'partial')
        self.assertEqual(result['run']['new_count'],1)
        self.assertEqual(self.ig.status()['hashtags'][0]['daily'][0]['successful_runs'],0)
        limited = InstagramService(self.store, replace(self.config,max_pages=1), GraphClient(replace(self.config,max_pages=1),self.transport,self.sleeps.append))
        result = limited.collect()['results'][0]
        self.assertEqual(result['run']['status'],'limited')
        self.assertEqual(result['run']['truncated'],1)

    def test_failure_empty_does_not_manufacture_zero_metric(self):
        self.transport.media_error = GraphError('permission')
        self.start()
        h = self.ig.status()['hashtags'][0]
        self.assertEqual(h['daily'],[])
        self.assertIsNone(h['first_observation_at'])

    def test_invalid_media_is_not_miscounted_as_duplicate(self):
        self.transport.media_pages=[{'data':[MEDIA,{'id':'invalid'}]}]
        result=self.start()
        self.assertEqual(result['error_type'],'invalid_response')
        self.assertEqual(result['run']['returned_count'],2)
        self.assertEqual(result['run']['new_count'],1)
        self.assertEqual(result['run']['duplicate_count'],0)

    def test_leases_cross_process_and_crash_recovery(self):
        self.start()
        owner = self.ig.store.acquire()
        other = InstagramService(Storage.create(self.store.path),self.config,self.client)
        with self.assertRaises(GraphError) as raised:
            other.collect()
        self.assertEqual(raised.exception.kind,'busy')
        run = self.ig.store.start_run(self.ig.store.tags()[0]['id'])
        with self.store.connect() as db:
            db.execute("UPDATE instagram_leases SET expires_at='2000-01-01'")
        self.assertTrue(other.collect()['success'])
        self.assertEqual(other.store.run(run)['status'],'interrupted')
        self.ig.store.release(owner)

    def test_scheduler_due_pause_resume_and_no_duplicate_thread(self):
        self.start()
        config = replace(self.config,interval_minutes=.002)
        self.ig.config = config
        self.ig.start()
        thread = self.ig.thread
        self.ig.start()
        self.assertIs(self.ig.thread,thread)
        deadline = time.monotonic()+3
        while time.monotonic()<deadline:
            with self.store.connect() as db:
                count=db.execute('SELECT count(*) FROM instagram_runs').fetchone()[0]
            if count>=2:
                break
            time.sleep(.03)
        self.assertGreaterEqual(count,2)
        self.ig.stop_event.set();thread.join(2)
        tag = self.ig.store.tags()[0]
        self.ig.store.pause(tag['id'])
        self.assertEqual(self.ig.collect()['results'],[])
        self.assertTrue(self.start()['success'])

    def test_connection_token_check_and_exchange_never_replace_env(self):
        self.assertTrue(self.ig.check_connection()['success'])
        config = replace(self.config,app_id='synthetic-app',app_secret='synthetic-secret')
        self.ig.config = config;self.client.config=config
        self.assertEqual(self.ig.check_token()['status'],'valid')
        dest = Path(self.temp.name)/'exchanged.secret'
        args = SimpleNamespace(instagram_status=False,instagram_check=False,instagram_token_check=False,
                               instagram_start=None,instagram_collect=False,token_output=str(dest))
        output=io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(instagram_command(args,self.ig),0)
        self.assertEqual(dest.read_text().strip(),'synthetic-new-token')
        self.assertNotIn('synthetic-new-token',output.getvalue())
        calls=len(self.transport.calls)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(instagram_command(args,self.ig),1)
        self.assertEqual(len(self.transport.calls),calls)

    def test_connection_error_and_token_change_clear_stale_expiry(self):
        self.transport.error = GraphError('authentication',190,463)
        with self.assertRaises(GraphError):
            self.ig.check_connection()
        self.assertEqual(self.ig.status()['connection']['status'],'authentication')
        self.ig.store.state('token',{'status':'invalid','expires_at':'2000-01-01'})
        changed=InstagramService(self.store,replace(self.config,token='synthetic-replacement'))
        self.assertEqual(changed.status()['token']['status'],'unchecked')
        self.assertEqual(changed.status()['connection']['status'],'unverified')

    def test_token_expiry_warning_is_local_and_tracks_time(self):
        now = datetime.now(timezone.utc)
        for days, expected in [(60,'healthy'),(6,'expiring'),(-1,'expired')]:
            self.ig.store.state('token', {'status':'valid','expires_at':(now+timedelta(days=days)).isoformat()})
            token = self.ig.status()['token']
            self.assertEqual(token['expiry_status'],expected)
            self.assertEqual(bool(token['warning']),expected!='healthy')
            self.assertEqual(token['days_remaining'],max(0,days))
            self.assertEqual(self.ig.token_blocked(),days<0)
        self.ig.store.state('token', {'status':'invalid','expires_at':None})
        self.assertEqual(self.ig.status()['token']['expiry_status'],'invalid')
        self.assertTrue(self.ig.status()['token']['warning'])
        self.ig.store.state('token', {'status':'unchecked','expires_at':None})
        self.assertEqual(self.ig.status()['token']['expiry_status'],'unknown')
        self.assertEqual(self.transport.calls,[])

    def test_sensitive_requests_have_no_credentials_in_http_urls(self):
        config=replace(self.config,app_id='synthetic-app',app_secret='synthetic-secret')
        self.ig.config=config;self.client.config=config
        metadata=self.ig.check_token()
        self.assertTrue(metadata['success'])
        self.assertEqual(metadata['token_type'],'USER')
        self.assertEqual(metadata['lifetime'],'short_lived')
        self.assertTrue(metadata['app_matches'])
        self.assertEqual(self.transport.outbound_methods,['POST'])
        for url in self.transport.outbound_urls:
            self.assertNotIn(FAKE_TOKEN,url)
            self.assertNotIn(config.app_secret,url)
            self.assertNotIn('input_token',url)
        self.assertNotIn(config.app_secret,json.dumps(metadata))
        self.assertNotIn(FAKE_TOKEN,json.dumps(metadata))

    def test_token_mismatch_missing_permissions_and_expiry_block_exchange(self):
        config=replace(self.config,app_id='synthetic-app',app_secret='synthetic-secret')
        self.ig.config=config;self.client.config=config
        self.transport.token_app='another-app'
        self.transport.token_scopes=['instagram_basic']
        metadata=self.ig.check_token()
        self.assertFalse(metadata['success'])
        self.assertFalse(metadata['app_matches'])
        self.assertFalse(metadata['permissions']['pages_show_list'])
        dest=Path(self.temp.name)/'blocked.secret'
        args=SimpleNamespace(instagram_status=False,instagram_check=False,instagram_token_check=False,
                             instagram_start=None,instagram_collect=False,token_output=str(dest))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(instagram_command(args,self.ig),1)
        self.assertFalse(dest.exists())
        self.assertNotIn('oauth/access_token',[c[0] for c in self.transport.calls])
        self.ig.store.state('token',{'status':'invalid','expires_at':'2000-01-01'})
        before=len(self.transport.calls)
        result=self.start()
        self.assertEqual(result['error_type'],'authentication')
        self.assertEqual(len(self.transport.calls),before)

    def test_caption_migration_and_growth_rates(self):
        result=self.start()
        self.ig.collect()
        # Reopening the same schema preserves the caption and existing rows.
        again=InstagramService(self.store,self.config,self.client)
        self.assertEqual(again.status(include_media=True)['hashtags'][0]['media'][0]['caption'],MEDIA['caption'])
        now=datetime.now(timezone.utc)
        previous=(now-timedelta(hours=30)).isoformat()
        started=(now-timedelta(days=3)).isoformat()
        tag_id=result['hashtag_id']
        with self.store.connect() as db:
            db.execute('UPDATE instagram_runs SET finished_at=?',(started,))
            for identifier in ['2001','2002']:
                db.execute('INSERT INTO instagram_media(id,first_seen,last_seen) VALUES (?,?,?)',(identifier,previous,previous))
                db.execute('INSERT INTO instagram_hashtag_media VALUES (?,?,?,?)',(tag_id,identifier,previous,previous))
        summary=again.status()['hashtags'][0]
        self.assertEqual(summary['growth_pct_24h'],-50.0)
        self.assertEqual(summary['change_24h'],-1)
        self.assertIsNone(summary['growth_unavailable_reason'])
        with self.store.connect() as db:
            db.execute('UPDATE instagram_hashtag_media SET first_seen=? WHERE media_id IN (?,?)',(started,'2001','2002'))
        summary=again.status()['hashtags'][0]
        self.assertIsNone(summary['growth_pct_24h'])
        self.assertEqual(summary['growth_unavailable_reason'],'zero_baseline')

    def test_live_verifier_uses_one_target_and_no_calls_when_unconfigured(self):
        from food_trends.instagram_verify import verify
        report=verify(InstagramService(self.store,replace(self.config,token='')))
        self.assertEqual(report['status'],'blocked')
        self.assertEqual(report['api_requests'],0)
        report=verify(self.ig)
        self.assertEqual(report['status'],'passed')
        self.assertEqual(report['actual_hashtags'],1)
        self.assertEqual(report['actual_media'],1)
        self.assertEqual(report['checks']['database']['daily_sum'],1)
        self.assertEqual(report['checks']['frontend']['status'],'pending')
        self.assertNotIn(FAKE_TOKEN,json.dumps(report))
        self.assertNotIn(MEDIA['caption'],json.dumps(report))

    def test_metrics_gaps_and_zero_are_different_and_config_is_finite(self):
        self.start()
        self.transport.media_pages=[{'data':[]}]
        result=self.ig.collect()['results'][0]
        self.assertEqual(result['run']['new_count'],0)
        with self.store.connect() as db:
            tag_id=self.ig.store.tags()[0]['id']
            yesterday=(datetime.now(timezone.utc)-timedelta(days=2)).date().isoformat()
            db.execute('INSERT INTO instagram_metrics VALUES (?,?,?,?,1,1)',(tag_id,'day',yesterday,0))
        daily=self.ig.status()['hashtags'][0]['daily']
        self.assertEqual(len(daily),2)
        self.assertEqual(daily[0]['new_count'],0)
        with self.assertRaises(ValueError):
            replace(self.config,interval_minutes=float('nan'))

    def test_api_approval_and_validation_and_no_secret_status(self):
        server=make_server(self.rss,port=0,instagram=self.ig)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        def post(path,body,headers=None):
            return urlopen(Request(base+path,data=json.dumps(body).encode(),headers=headers or {'Content-Type':'application/json'}))
        try:
            for body in [{'name':'두쫀쿠','candidate_id':self.candidate}, {'name':'두쫀쿠','candidate_id':'1','approved':True}]:
                with self.assertRaises(HTTPError) as raised:
                    post('/api/instagram/start',body)
                self.assertEqual(raised.exception.code,400)
            self.assertEqual(self.transport.calls,[])
            with post('/api/instagram/start',{'name':'두쫀쿠','candidate_id':self.candidate,'approved':True}) as r:
                self.assertTrue(json.load(r)['success'])
            with urlopen(base+'/api/instagram?days=30') as r:
                data=r.read().decode();self.assertNotIn(FAKE_TOKEN,data)
                self.assertEqual(json.loads(data)['hashtags'][0]['total_observed'],1)
            with self.assertRaises(HTTPError) as raised:
                post('/api/instagram/collect',{}, {'Content-Type':'application/json','Origin':'https://evil.invalid'})
            self.assertEqual(raised.exception.code,403)
            with urlopen(base+'/instagram.js') as r:
                self.assertEqual(r.status,200)
        finally:
            server.shutdown();server.server_close();thread.join()


if __name__ == '__main__':
    unittest.main()
