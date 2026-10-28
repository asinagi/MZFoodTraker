"""Provider transports are injected. All source/result tests use disposable databases."""
import io
import json
import sqlite3
import threading
import unittest
import uuid
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request,urlopen
from food_trends.api import make_server
from food_trends.execution import ExecutionWorker, ExecutionError
from food_trends.execution_mock import mock_controller, MockTransport, MockResponse
from food_trends.execution_sources import YouTubeExecutor
from food_trends.service import CollectionService
from food_trends.youtube import YouTubeClient
from food_trends.config import YouTubeConfig,InstagramConfig,NaverConfig
from food_trends.instagram_graph import GraphClient
from food_trends.naver_datalab import NaverClient
from food_trends.execution_ownership import legacy_allowed
from food_trends.contracts import require_operation
import test_execution as fixtures
from test_food_entities import snapshot


# Avoid duplicate baseline/infra counts: install just the setup and helper methods.
class SourceTests(unittest.TestCase):
    setUp=fixtures.ExecutionTests.setUp
    tearDown=fixtures.ExecutionTests.tearDown
    create_food=fixtures.ExecutionTests.create_food
    cmd=fixtures.ExecutionTests.cmd
    register=fixtures.ExecutionTests.register
    active=fixtures.ExecutionTests.active
    change=fixtures.ExecutionTests.change
    job=fixtures.ExecutionTests.job
    advance=fixtures.ExecutionTests.advance

    def execute(self, source, transport=None, fault=None):
        job=self.job(source)
        controller=mock_controller(self.storage,self.planner,transport)
        if fault:controller.worker.fault=fault
        return job,controller,controller.run_once(job['job_id'])

    def test_three_existing_clients_execute_and_persist_only_allowed_results(self):
        results={}
        for source in ('youtube','instagram_graph','naver_datalab'):
            job,controller,result=self.execute(source)
            self.assertEqual(result['status'],'SUCCEEDED',result)
            self.assertEqual(result['run']['origin'],'mock')
            results[source]=result
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM youtube_videos').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM instagram_media').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM youtube_calls').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM naver_calls').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM instagram_searches').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0],0)
        self.assertIsNone(results['naver_datalab']['run']['result_contract']['allowed_result_reference'])
        self.assertEqual([c['endpoint'] for c in results['youtube']['calls']],['search','videos'])

    def test_naver_ratio_body_and_series_never_persist(self):
        class Ratios(MockTransport):
            def __call__(self,request,timeout=None):
                result=super().__call__(request,timeout)
                for group in result['results']:
                    body=json.loads(request.data)
                    group['data']=[{'period':body['startDate'],'ratio':73.125987}]
                return result
        job,controller,result=self.execute('naver_datalab',Ratios())
        self.assertEqual(result['status'],'SUCCEEDED')
        dump=snapshot(self.storage)
        self.assertNotIn('73.125987',dump)
        self.assertNotIn('"ratio"',dump)
        self.assertNotIn('"data":',dump)

    def test_youtube_public_snapshot_ttl_and_unverified_query_evidence(self):
        job,controller,result=self.execute('youtube')
        with self.storage.reader() as db:
            row=db.execute('SELECT * FROM youtube_videos').fetchone()
            from food_trends.tracking_service import instant
            self.assertEqual((instant(row['expires_at'])-instant(row['refreshed_at'])).days,29)
            self.assertEqual(json.loads(row['contract_json'])['origin'],'mock')
            self.assertEqual(db.execute('SELECT count(*) FROM youtube_matches').fetchone()[0],1)
        self.assertEqual(result['run']['result_contract']['mapping_semantics'],'search_provenance_only')
        self.assertEqual(result['run']['returned_count'],1)
        with self.assertRaises(ValueError):require_operation('youtube','food_extraction')

    def test_limited_page_preserves_results_and_checkpoints(self):
        class Pages(MockTransport):
            def __call__(self,request,timeout=None):
                raw=super().__call__(request,timeout)
                if urlsplit(request.full_url).path.endswith('/search'):
                    payload=json.loads(raw.getvalue());payload['nextPageToken']='next-page'
                    return MockResponse(json.dumps(payload).encode())
                return raw
        job,controller,result=self.execute('youtube',Pages())
        self.assertEqual(result['status'],'LIMITED')
        self.assertEqual(result['run']['accepted_count'],1)
        self.assertIsNone(self.tracking.target(self.target['target_id'])['last_success_at'])

    def test_instagram_page_cap_and_no_related_tag_tracking(self):
        old=self.tracking.account('instagram_graph','local-default')
        self.tracking.accounts[('instagram_graph','local-default')]=replace(old,options={'max_pages':1})
        class Pages(MockTransport):
            def __call__(self,request,timeout=None):
                raw=super().__call__(request,timeout)
                if urlsplit(request.full_url).path.endswith('recent_media'):
                    payload=json.loads(raw.getvalue());payload['paging']={'next':'https://graph.facebook.com/no-follow','cursors':{'after':'next'}}
                    return MockResponse(json.dumps(payload).encode())
                return raw
        job,controller,result=self.execute('instagram_graph',Pages())
        self.assertEqual(result['status'],'LIMITED')
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM instagram_hashtags').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT active FROM instagram_hashtags').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM tracking_targets').fetchone()[0],1)

    def test_duplicate_video_pages_have_one_item_and_one_job_receipt(self):
        old=self.tracking.account('youtube','local-default')
        self.tracking.accounts[('youtube','local-default')]=replace(old,options={'max_pages':2})
        class Pages(MockTransport):
            def __call__(self,request,timeout=None):
                raw=super().__call__(request,timeout)
                if urlsplit(request.full_url).path.endswith('/search') and 'pageToken=' not in request.full_url:
                    payload=json.loads(raw.getvalue());payload['nextPageToken']='second-page'
                    return MockResponse(json.dumps(payload).encode())
                return raw
        job,controller,result=self.execute('youtube',Pages())
        self.assertEqual(result['status'],'SUCCEEDED')
        self.assertEqual(result['run']['accepted_count'],1)
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM youtube_videos').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM execution_item_receipts').fetchone()[0],1)

    def test_instagram_duplicate_items_have_one_analysis_observation_per_job(self):
        old=self.tracking.account('instagram_graph','local-default')
        self.tracking.accounts[('instagram_graph','local-default')]=replace(old,options={'max_pages':2})
        class Pages(MockTransport):
            def __call__(self,request,timeout=None):
                raw=super().__call__(request,timeout)
                if urlsplit(request.full_url).path.endswith('recent_media') and 'after=' not in request.full_url:
                    payload=json.loads(raw.getvalue());payload['paging']={'next':'https://graph.facebook.com/fixed','cursors':{'after':'second'}}
                    return MockResponse(json.dumps(payload).encode())
                return raw
        job,controller,result=self.execute('instagram_graph',Pages())
        self.assertEqual(result['status'],'SUCCEEDED')
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM instagram_analysis_observations').fetchone()[0],1)

    def test_transport_error_text_and_credentials_are_not_in_execution_state(self):
        class Failure(MockTransport):
            def __call__(self,request,timeout=None):
                raise URLError('Bearer synthetic-credential-must-never-persist')
        job,controller,result=self.execute('youtube',Failure())
        self.assertEqual(result['status'],'RETRY_WAIT')
        self.assertNotIn('synthetic-credential-must-never-persist',snapshot(self.storage))

    def test_invalid_provider_schema_is_failed_without_source_rows(self):
        class Invalid(MockTransport):
            def __call__(self,request,timeout=None):return MockResponse(b'{"items": "bad"}')
        job,controller,result=self.execute('youtube',Invalid())
        self.assertEqual(result['status'],'FAILED')
        self.assertEqual(result['error_kind'],'invalid_response')
        with self.storage.reader() as db:self.assertEqual(db.execute('SELECT count(*) FROM youtube_videos').fetchone()[0],0)

    def test_existing_clients_honor_retry_after_without_internal_b3_retry(self):
        def limited(request,timeout=None):
            raise HTTPError(request.full_url,429,'secret upstream error',{'Retry-After':'7200'},io.BytesIO(b'{}'))
        job,controller,result=self.execute('youtube',limited)
        self.assertEqual(len(result['calls']),1)
        self.assertEqual(result['status'],'RETRY_WAIT')
        from food_trends.tracking_service import instant
        self.assertGreaterEqual((instant(result['next_retry_at'])-self.now).total_seconds(),7200)
        for client,action in [
            (GraphClient(InstagramConfig(token='fixture-token'),limited),lambda c:c.get('ig_hashtag_search',single_attempt=True)),
            (NaverClient(NaverConfig(client_id='fixture-id',client_secret='fixture-secret'),limited),lambda c:c.search({},single_attempt=True))]:
            with self.assertRaises(Exception) as error:action(client)
            self.assertEqual(error.exception.retry_after,7200)

    def test_expired_checkpoint_does_not_restore_api_data(self):
        job=self.job();controller=mock_controller(self.storage,self.planner)
        controller.run_once(job['job_id'])
        with self.storage.connect() as db:
            db.execute("UPDATE execution_jobs SET status='READY',attempt_count=0 WHERE job_id=?",(job['job_id'],))
            db.execute("UPDATE execution_checkpoints SET expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?",(job['job_id'],))
            db.execute('DELETE FROM youtube_videos')
            db.execute('UPDATE execution_leases SET expires_at=?',(self.now.isoformat(),))
        result=controller.run_once(job['job_id'])
        self.assertEqual(result['status'],'FAILED');self.assertEqual(result['error_kind'],'checkpoint_expired')
        with self.storage.reader() as db:self.assertEqual(db.execute('SELECT count(*) FROM youtube_videos').fetchone()[0],0)

    def test_quota_shortage_sends_no_http_and_waits_for_reset(self):
        old=self.tracking.account('youtube','local-default')
        self.tracking.accounts[('youtube','local-default')]=replace(old,options={'daily_search_limit':1})
        target=self.active();plans=self.planner.commit('quota')['plans']
        controller=mock_controller(self.storage,self.planner)
        jobs=controller.execution.enqueue([p['plan_id'] for p in plans])
        first=controller.run_once(jobs[0]['job_id']);second=controller.run_once(jobs[1]['job_id'])
        self.assertEqual(first['status'],'SUCCEEDED');self.assertEqual(second['status'],'RETRY_WAIT')
        self.assertEqual(second['error_kind'],'budget');self.assertEqual(second['calls'],[])

    def test_execution_http_reads_use_only_reader_and_do_not_reserve(self):
        job=self.job();controller=mock_controller(self.storage,self.planner)
        server=make_server(CollectionService(self.storage,fetcher=lambda _:None),port=0,tracking=self.tracking,planner=self.planner,execution=controller)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        before=snapshot(self.storage);connect=self.storage.connect
        def readonly(*args,**kwargs):
            self.assertTrue(kwargs.get('readonly'));return connect(*args,**kwargs)
        try:
            with patch.object(self.storage,'connect',side_effect=readonly):
                for route in ('/api/execution/status','/api/execution/jobs/'+job['job_id']):
                    with urlopen('http://127.0.0.1:'+str(server.server_port)+route) as response:
                        self.assertTrue(json.load(response)['read_only'])
        finally:server.shutdown();server.server_close();thread.join()
        self.assertEqual(snapshot(self.storage),before)

    def test_http_cannot_grant_live_or_policy_permissions(self):
        job=self.job();controller=mock_controller(self.storage,self.planner)
        server=make_server(CollectionService(self.storage,fetcher=lambda _:None),port=0,tracking=self.tracking,planner=self.planner,execution=controller)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            request=Request('http://127.0.0.1:'+str(server.server_port)+'/api/execution/enqueue',
                data=json.dumps({'plan_ids':[job['plan_id']],'mode':'live','live_enabled':True}).encode(),headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as error:urlopen(request)
            self.assertEqual(error.exception.code,400);error.exception.close()
        finally:server.shutdown();server.server_close();thread.join()

    def test_ownership_is_read_only_and_default_legacy(self):
        before=snapshot(self.storage)
        self.assertTrue(legacy_allowed(self.storage,'youtube'))
        self.assertEqual(snapshot(self.storage),before)
        with self.storage.connect() as db:
            db.execute("INSERT INTO execution_source_ownership VALUES ('youtube','local-default','B3','fixture-operator','fixture approval',?)",(self.now.isoformat(),))
        self.assertFalse(legacy_allowed(self.storage,'youtube'))
        from food_trends.youtube_service import YouTubeService
        service=YouTubeService(self.storage,YouTubeConfig(api_key='fixture-key'),YouTubeClient(YouTubeConfig(api_key='fixture-key'),MockTransport()))
        self.assertEqual(service.collect()['requests'],0)

    def test_default_live_runtime_is_closed(self):
        from food_trends.execution_runtime import live_controller
        from food_trends.food_service import FoodError
        with self.assertRaises(FoodError):live_controller(self.storage,'nonexistent-registry.json',{})

    def test_late_source_response_cannot_commit_after_lease_takeover(self):
        job=self.job();transport=MockTransport();controller=mock_controller(self.storage,self.planner,transport)
        def takeover(stage):
            if stage=='after_http':
                self.advance()
                self.assertIsNotNone(self.execution.claim('replacement',job['job_id']))
        controller.worker.fault=takeover
        result=controller.run_once(job['job_id'])
        self.assertEqual(result['status'],'STALE_OWNER_REJECTED')
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM youtube_videos').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM execution_results').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM execution_calls WHERE status='UNKNOWN'").fetchone()[0],1)

    def test_response_crash_then_retry_has_one_durable_source_item(self):
        job=self.job();controller=mock_controller(self.storage,self.planner)
        count={'http':0}
        def crash(stage):
            if stage=='after_http':
                count['http']+=1
                if count['http']==2:raise fixtures.Crash()
        controller.worker.fault=crash
        with self.assertRaises(fixtures.Crash):controller.run_once(job['job_id'])
        self.advance();controller.worker.fault=lambda stage:None
        result=controller.run_once(job['job_id'])
        self.assertEqual(result['status'],'SUCCEEDED');self.assertTrue(result['remote_duplicate_possible'])
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM youtube_videos').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM execution_item_receipts').fetchone()[0],1)

    def test_partial_page_failure_recovers_from_committed_cursor_without_repeating_page(self):
        old=self.tracking.account('youtube','local-default')
        self.tracking.accounts[('youtube','local-default')]=replace(old,options={'max_pages':2})
        class Flaky(MockTransport):
            failed=False
            searches=0
            def __call__(self,request,timeout=None):
                if urlsplit(request.full_url).path.endswith('/search'):
                    self.searches+=1
                    if 'pageToken=' in request.full_url and not self.failed:
                        self.failed=True;raise URLError('fixture timeout')
                    raw=super().__call__(request,timeout)
                    if 'pageToken=' not in request.full_url:
                        data=json.loads(raw.getvalue());data['nextPageToken']='next';return MockResponse(json.dumps(data).encode())
                    return raw
                return super().__call__(request,timeout)
        transport=Flaky();job,controller,first=self.execute('youtube',transport)
        self.assertEqual(first['status'],'RETRY_WAIT');self.advance()
        result=controller.run_once(job['job_id'])
        self.assertEqual(result['status'],'SUCCEEDED');self.assertEqual(transport.searches,3)
        self.assertEqual(result['run']['accepted_count'],1)

    def test_partial_result_has_contract_when_retries_exhausted(self):
        old=self.tracking.account('youtube','local-default')
        self.tracking.accounts[('youtube','local-default')]=replace(old,options={'max_pages':2})
        class Flaky(MockTransport):
            def __call__(self,request,timeout=None):
                if 'pageToken=' in request.full_url:raise URLError('fixture timeout')
                raw=super().__call__(request,timeout)
                if urlsplit(request.full_url).path.endswith('/search'):
                    data=json.loads(raw.getvalue());data['nextPageToken']='next';return MockResponse(json.dumps(data).encode())
                return raw
        job,controller,result=self.execute('youtube',Flaky())
        for _ in range(2):self.advance();result=controller.run_once(job['job_id'])
        self.assertEqual(result['status'],'PARTIAL')
        self.assertEqual(result['run']['result_contract']['execution_status'],'PARTIAL')
        self.assertEqual(result['run']['accepted_count'],1)

    def test_mode_specific_tick_does_not_claim_other_mode_job(self):
        job=self.job()
        self.assertIsNone(self.execution.claim('live-only',job['job_id'],mode='live'))
        self.assertEqual(self.execution.status(job['job_id'])['items'][0]['attempt_count'],0)

    def test_stopped_then_resumed_target_uses_new_generation_and_preserves_old_job(self):
        job=self.job();target=self.tracking.target(self.target['target_id'])
        paused=self.change('pause',target);self.change('resume',paused,approved=True)
        plans=self.planner.commit('resumed')['plans']
        plan=next(p for p in plans if p['query_id']==job['query_id'])
        new=self.execution.enqueue([plan['plan_id']])[0]
        self.assertNotEqual(new['job_id'],job['job_id'])
        controller=mock_controller(self.storage,self.planner)
        self.assertEqual(controller.run_once(job['job_id'])['status'],'CANCELLED')
        self.assertEqual(controller.run_once(new['job_id'])['status'],'SUCCEEDED')

    def test_normal_zero_results_succeeds_and_creates_no_video(self):
        class Empty(MockTransport):
            def __call__(self,request,timeout=None):return MockResponse(b'{"items":[]}')
        job,controller,result=self.execute('youtube',Empty())
        self.assertEqual(result['status'],'SUCCEEDED');self.assertEqual(result['run']['accepted_count'],0)
        self.assertEqual(len(result['calls']),1)
        with self.storage.reader() as db:self.assertEqual(db.execute('SELECT count(*) FROM youtube_videos').fetchone()[0],0)

    def test_explicit_expiry_maintenance_removes_ids_and_keeps_nonreplayable_tombstone(self):
        job,controller,result=self.execute('youtube')
        from food_trends.execution_maintenance import purge
        expired=self.now+timedelta(days=31)
        result=purge(self.storage,expired)
        self.assertEqual(result['expired_item_receipts'],1);self.assertEqual(result['expired_checkpoints'],1)
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT state_json FROM execution_checkpoints').fetchone()[0],'{"expired":true}')

    def test_rss_identity_bridge_failure_is_recoverable_without_fetch_or_original_changes(self):
        from test_mvp import fixture
        from food_trends.food_service import FoodService
        from test_food_entities import original_rows
        rss=CollectionService(self.storage,fetcher=lambda _: (fixture('두쫀쿠'),200))
        with patch.object(FoodService,'import_rss_run',side_effect=RuntimeError('fixture bridge failure')):
            result=rss.collect()
        self.assertTrue(result['success']);self.assertEqual(result['entity_resolution']['state'],'pending_retry')
        original=original_rows(self.storage)
        controller=mock_controller(self.storage,self.planner)
        self.assertEqual(controller.recover_rss()['results'][0]['state'],'completed')
        self.assertEqual(controller.recover_rss()['results'],[])
        self.assertEqual(original_rows(self.storage),original)

    def test_shared_actual_query_searches_once_and_filters_stopped_target(self):
        one=self.active();otherfood=self.create_food('다른 음식',['두쫀쿠'],scope='food:second')
        other=self.cmd('register',food_id=otherfood['food_id'],source_id='youtube');other=self.change('activate',other,approved=True)
        plan=next(p for p in self.planner.commit('shared-source')['plans'] if p['canonical_query']['parameters']['q']=='두쫀쿠')
        job=self.execution.enqueue([plan['plan_id']])[0]
        class StopOne(MockTransport):
            def __call__(inner,request,timeout=None):
                if urlsplit(request.full_url).path.endswith('/videos'):
                    self.change('stop',self.tracking.target(one['target_id']))
                return super().__call__(request,timeout)
        transport=StopOne();controller=mock_controller(self.storage,self.planner,transport)
        result=controller.run_once(job['job_id'])
        self.assertEqual(result['status'],'SUCCEEDED')
        self.assertEqual(transport.calls.count('/youtube/v3/search'),1)
        self.assertEqual(result['run']['result_contract']['active_target_ids'],[other['target_id']])


