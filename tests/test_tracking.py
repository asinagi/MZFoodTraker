"""B1 target state, stale mappings, serial writes and the read-only HTTP boundary."""
import json
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen
from food_trends.api import make_server
from food_trends.food_service import FoodService, FoodError
from food_trends.storage import Storage
from food_trends.service import CollectionService
from food_trends.tracking_access import Account, mock_accounts
from food_trends.tracking_service import TrackingService, instant, stamp
from test_food_entities import original_rows, snapshot


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage.create(Path(self.temp.name)/'tracking.sqlite3')
        self.foods = FoodService(self.storage)
        self.now = datetime(2026, 10, 27, 0, tzinfo=timezone.utc)
        self.accounts = mock_accounts()
        self.tracking = TrackingService(self.storage, self.accounts, lambda: self.now)
        self.food = self.create_food('두쫀쿠', ['두바이 쫀득 쿠키', '두바이쫀득쿠키'])

    def tearDown(self):
        self.temp.cleanup()

    def create_food(self, name, aliases=None, scope='food:ko'):
        c = self.foods.command('submit', dict(request_key=uuid.uuid4().hex, name=name, aliases=aliases or [], scope=scope))
        return self.foods.command('review',dict(request_key=uuid.uuid4().hex,candidate_id=c['candidate_id'],
            expected_revision=c['revision'],action='create',approved=True,actor_ref='reviewer',reason='음식과 별칭 확인'))['resolution']

    def cmd(self, action, **data):
        return self.tracking.command(action, dict(request_key=data.pop('request_key', uuid.uuid4().hex),
            actor_ref='fixture-reviewer', reason='추적 관리 검토', **data))

    def register(self, source='youtube', **data):
        return self.cmd('register', food_id=self.food['food_id'], source_id=source, **data)

    def active(self, source='youtube'):
        target = self.register(source)
        return self.cmd('activate', target_id=target['target_id'], expected_revision=target['revision'], approved=True)

    def change(self, action, target, **data):
        return self.cmd(action, target_id=target['target_id'], expected_revision=target['revision'], **data)

    def test_approved_food_target_in_empty_rss_and_aliases_reuse_target(self):
        before=original_rows(self.storage)
        self.assertEqual(self.storage.status()['food_count'], 0)
        targets=[self.register(s) for s in ('instagram_graph','youtube','naver_datalab')]
        for t in targets:
            self.assertEqual(t['status'],'PENDING');self.assertEqual(t['origin'],'user')
            self.assertIsNone(t['last_success_at'])
        for alias in ('두쫀쿠','두바이 쫀득 쿠키','두바이쫀득쿠키'):
            food_id=self.foods.resolve_name(alias)['resolution']['food_id']
            t=self.cmd('register',food_id=food_id,source_id='youtube')
            self.assertEqual(t['target_id'],targets[1]['target_id'])
        self.assertEqual(len(self.tracking.list()),3)
        self.assertEqual(original_rows(self.storage),before)

    def test_duplicate_registration_and_replay_key_collision(self):
        key=uuid.uuid4().hex
        t=self.register(request_key=key)
        self.assertEqual(self.register(request_key=key),t)
        with self.assertRaises(FoodError):self.register('naver_datalab',request_key=key)
        self.assertEqual(len(self.tracking.history(t['target_id'])),1)

    def test_pending_activate_pause_resume_preserves_history(self):
        active=self.active()
        self.assertEqual(active['status'],'ACTIVE');self.assertEqual(len(self.tracking.due()),1)
        paused=self.change('pause',active)
        self.assertEqual(paused['status'],'PAUSED');self.assertGreater(paused['generation'],active['generation'])
        self.assertEqual(self.tracking.due(),[])
        resumed=self.change('resume',paused,approved=True)
        self.assertEqual(resumed['status'],'ACTIVE')
        self.assertEqual([r['action'] for r in self.tracking.history(active['target_id'])],['REGISTERED','ACTIVATE','PAUSE','RESUME'])

    def test_stopped_cannot_resume_or_automatically_reregister(self):
        t=self.change('stop',self.active())
        for action in ('activate','resume'):
            with self.assertRaises(FoodError):self.change(action,t,approved=True)
        self.assertEqual(self.register()['status'],'STOPPED')
        pending=self.register(reregister=True,expected_revision=t['revision'])
        self.assertEqual(pending['target_id'],t['target_id']);self.assertEqual(pending['status'],'PENDING')
        self.assertEqual(self.tracking.due(),[])

    def test_server_policy_permission_gate_cannot_be_overridden_by_approval(self):
        account=replace(self.accounts[1],policy_allowed=False)
        self.tracking.accounts[(account.source_id,account.account_scope)]=account
        t=self.register()
        blocked=self.change('activate',t,approved=True)
        self.assertEqual(blocked['status'],'BLOCKED');self.assertIn('POLICY_UNCONFIRMED',blocked['blockers'])
        again=self.change('activate',blocked,approved=True)
        self.assertEqual(again['status'],'BLOCKED');self.assertEqual(self.tracking.due(),[])
        self.tracking.accounts[(account.source_id,account.account_scope)]=replace(account,policy_allowed=True)
        ready=self.change('activate',again,approved=True)
        self.assertEqual(ready['status'],'ACTIVE')

    def test_changed_account_rights_immediately_block_reads_without_writes(self):
        t=self.active();account=self.accounts[1]
        self.tracking.accounts[(account.source_id,account.account_scope)]=replace(account,permission_verified=False)
        before=snapshot(self.storage);current=self.tracking.target(t['target_id'])
        self.assertEqual(current['effective_status'],'BLOCKED');self.assertEqual(self.tracking.due(),[])
        self.assertEqual(snapshot(self.storage),before)

    def test_interval_recalculates_next_due_and_invalidates_old_generation(self):
        t=self.active();changed=self.change('interval',t,interval_seconds=43200)
        self.assertEqual(instant(changed['next_due_at']),self.now+timedelta(seconds=43200))
        with self.storage.reader() as db:
            self.assertFalse(self.tracking.valid_mapping(db,t['target_id'],t['generation'],t['food_revision']))
        self.assertEqual(self.tracking.due(),[])

    def test_minimum_provider_intervals_and_unknown_source_are_rejected(self):
        for source,seconds in [('youtube',21599),('naver_datalab',86399),('instagram_graph',0)]:
            with self.assertRaises(FoodError):self.register(source,interval_seconds=seconds)
        with self.assertRaises(FoodError):self.register('google_trends_rss')
        self.assertEqual(self.tracking.list(),[])

    def test_alias_change_requires_explicit_refresh_and_new_generation(self):
        t=self.active()
        f=self.foods.food(self.food['food_id'])
        self.foods.command('alias',dict(request_key=uuid.uuid4().hex,food_id=f['food_id'],expected_revision=f['revision'],
            action='approve',surface_form='추가 승인 별칭',approved=True,actor_ref='reviewer',reason='정확 명칭 확인'))
        before=snapshot(self.storage);read=self.tracking.target(t['target_id'])
        self.assertIn('STALE_FOOD_REVISION',read['blockers']);self.assertEqual(self.tracking.due(),[])
        self.assertEqual(snapshot(self.storage),before)
        refreshed=self.change('refresh',t)
        self.assertGreater(refreshed['generation'],t['generation']);self.assertTrue(refreshed['planning_eligible'])

    def test_pause_and_stop_invalidate_generation_for_future_executor(self):
        t=self.active();paused=self.change('pause',t)
        with self.storage.reader() as db:
            self.assertFalse(self.tracking.valid_mapping(db,t['target_id'],t['generation'],t['food_revision']))
        resumed=self.change('resume',paused,approved=True);self.change('stop',resumed)
        with self.storage.reader() as db:
            self.assertFalse(self.tracking.valid_mapping(db,resumed['target_id'],resumed['generation'],resumed['food_revision']))

    def test_schedule_slot_is_unique_advances_due_and_creates_no_http_run(self):
        t=self.active();before=original_rows(self.storage)
        slot=self.change('schedule',t,request_key='schedule-once')
        self.assertFalse(slot['http_job_created']);self.assertEqual(self.tracking.due(),[])
        self.assertEqual(self.change('schedule',t,request_key='schedule-once'),slot)
        with self.assertRaises(FoodError):self.change('schedule',t)
        with self.storage.reader() as db:self.assertEqual(db.execute('SELECT count(*) FROM tracking_slots').fetchone()[0],1)
        self.assertEqual(original_rows(self.storage),before)

    def test_overdue_slots_skip_backlog_and_next_due_is_after_now(self):
        t=self.active();self.now+=timedelta(days=10,seconds=1)
        slot=self.change('schedule',t)
        self.assertGreater(instant(slot['target']['next_due_at']),self.now)
        self.assertLessEqual(instant(slot['target']['next_due_at']),self.now+timedelta(seconds=t['interval_seconds']))

    def test_concurrent_duplicate_register_creates_one_target(self):
        with ThreadPoolExecutor(max_workers=4) as workers:results=list(workers.map(lambda _:self.register(),range(4)))
        self.assertEqual(len({r['target_id'] for r in results}),1)

    def test_concurrent_revision_updates_prevent_lost_updates(self):
        t=self.active()
        def write(action):
            try:return self.change(action,t,**({'interval_seconds':43200} if action=='interval' else {}))['status']
            except FoodError as exc:return exc.status
        with ThreadPoolExecutor(max_workers=2) as workers:results=list(workers.map(write,['pause','interval']))
        self.assertEqual(results.count(409),1)

    def test_separate_account_scopes_have_distinct_targets(self):
        extra=replace(self.accounts[1],account_scope='account-two');self.tracking.accounts[(extra.source_id,extra.account_scope)]=extra
        a,b=self.register(),self.register(account_scope='account-two')
        self.assertNotEqual(a['target_id'],b['target_id'])

    def test_unapproved_unknown_food_brand_store_and_unresolved_candidate_blocked(self):
        for kind in ('BRAND','STORE'):
            c=self.foods.command('submit',dict(request_key=uuid.uuid4().hex,name=kind,entity_kind=kind))
            with self.assertRaises(FoodError):self.cmd('register',food_id=c['candidate_id'],source_id='youtube')
            with self.assertRaises(FoodError):self.register(candidate_id=c['candidate_id'])
        with self.assertRaises(FoodError):self.cmd('register',food_id='FOOD_missing',source_id='youtube')

    def test_utc_instants_and_kst_equivalence(self):
        self.assertEqual(stamp('2026-10-27T09:00:00+09:00'),stamp(self.now))
        with self.assertRaises(FoodError):instant('2026-10-27T09:00:00')

    def test_http_get_targets_due_accounts_and_history_use_only_readers(self):
        t=self.active();rss=CollectionService(self.storage,fetcher=lambda _:(_ for _ in ()).throw(AssertionError('No HTTP')))
        server=make_server(rss,port=0,tracking=self.tracking);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        before=snapshot(self.storage);connect=self.storage.connect
        def reader_only(*args,**kwargs):
            if not kwargs.get('readonly'):raise AssertionError('GET write')
            return connect(*args,**kwargs)
        try:
            with patch.object(self.storage,'connect',side_effect=reader_only):
                for route in ('accounts','targets','due','events?target_id='+t['target_id']):
                    with urlopen(f'http://127.0.0.1:{server.server_port}/api/tracking/'+route) as response:self.assertEqual(response.status,200)
        finally:server.shutdown();server.server_close();thread.join()
        self.assertEqual(snapshot(self.storage),before)
