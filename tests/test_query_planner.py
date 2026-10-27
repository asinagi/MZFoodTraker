"""B2 planning, multi-target mappings, time windows and atomic local budgets."""
import json
import sqlite3
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from zoneinfo import ZoneInfoNotFoundError
from food_trends.api import make_server
from food_trends.food_service import FoodError
from food_trends.service import CollectionService
from food_trends.query_planner import QueryPlanner
from food_trends.quota import daily_window
from food_trends.tracking_service import instant
import test_tracking as tracking_fixtures
from test_food_entities import original_rows, snapshot
from test_mvp import fixture


class PlannerTests(unittest.TestCase):
    create_food=tracking_fixtures.TrackingTests.create_food
    cmd=tracking_fixtures.TrackingTests.cmd
    register=tracking_fixtures.TrackingTests.register
    active=tracking_fixtures.TrackingTests.active
    change=tracking_fixtures.TrackingTests.change
    tearDown=tracking_fixtures.TrackingTests.tearDown

    def setUp(self):
        tracking_fixtures.TrackingTests.setUp(self)
        self.planner=QueryPlanner(self.storage,self.tracking)

    def plans(self, source=None, **options):
        rows=self.planner.preview(options=options)['plans']
        return [p for p in rows if source is None or p['source_id']==source]

    def persisted(self, source='youtube', q=None):
        self.active(source)
        result=self.planner.commit(uuid.uuid4().hex)
        return next(p for p in result['plans'] if p['source_id']==source and (q is None or p['canonical_query']['parameters'].get('q')==q))

    def set_account(self, source, **options):
        old=self.tracking.account(source,'local-default')
        new=replace(old,options=dict(old.options,**options))
        self.tracking.accounts[(source,'local-default')]=new

    def test_approved_aliases_generate_source_queries_without_space_removal_or_or(self):
        for source in ('instagram_graph','youtube','naver_datalab'):self.active(source)
        before=original_rows(self.storage);preview=self.planner.preview()
        ig=[p for p in preview['plans'] if p['source_id']=='instagram_graph']
        yt=[p for p in preview['plans'] if p['source_id']=='youtube']
        nav=[p for p in preview['plans'] if p['source_id']=='naver_datalab']
        self.assertEqual({p['canonical_query']['parameters']['q'] for p in ig},{'두쫀쿠','두바이쫀득쿠키'})
        self.assertEqual(len(yt),3);self.assertTrue(all('|' not in p['canonical_query']['parameters']['q'] for p in yt))
        groups=nav[0]['canonical_query']['parameters']['keywordGroups'];self.assertEqual(len(groups),1)
        self.assertEqual(set(groups[0]['keywords']),{'두쫀쿠','두바이 쫀득 쿠키','두바이쫀득쿠키'})
        self.assertEqual(groups[0]['groupName'],self.food['food_id'])
        self.assertEqual(original_rows(self.storage),before)

    def test_unapproved_alias_proposals_and_related_foods_are_not_search_terms(self):
        self.foods.command('submit',dict(request_key='proposal',name='짭쫀쿠',aliases=['비승인 별칭']))
        self.active('youtube')
        words={p['canonical_query']['parameters']['q'] for p in self.plans('youtube')}
        self.assertNotIn('짭쫀쿠',words);self.assertNotIn('비승인 별칭',words)

    def test_canonical_serialization_and_repeated_plan_commit_are_idempotent(self):
        t=self.active();a=self.planner.commit('plan-a',[t['target_id']],{'regionCode':'KR','order':'date'})
        b=self.planner.commit('plan-b',[t['target_id'],t['target_id']],{'order':'date','regionCode':'KR'})
        self.assertEqual(a['stored_plan_ids'],b['stored_plan_ids'])
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM planner_queries').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT count(*) FROM planner_plans').fetchone()[0],3)
        before=snapshot(self.storage);self.assertEqual(self.planner.commit('plan-a',[t['target_id']],{'regionCode':'KR','order':'date'}),a)
        self.assertEqual(snapshot(self.storage),before)
        with self.assertRaises(FoodError):self.planner.commit('plan-a',[],{})

    def test_query_shared_between_two_foods_and_pause_keeps_other_mapping(self):
        one=self.active()
        second=self.create_food('다른 음식',['두쫀쿠'],scope='food:second')
        t=self.cmd('register',food_id=second['food_id'],source_id='youtube')
        two=self.change('activate',t,approved=True)
        saved=self.planner.commit('shared')
        shared=next(p for p in saved['plans'] if p['canonical_query']['parameters']['q']=='두쫀쿠')
        self.assertEqual({m['food_id'] for m in shared['targets']},{self.food['food_id'],second['food_id']})
        r=self.planner.quota.reserve(shared['plan_id'],'shared-attempt');self.planner.quota.mark_attempted(r['reservation_id'])
        self.change('pause',one)
        self.assertEqual([m['target_id'] for m in self.planner.valid_result_targets(r['reservation_id'])],[two['target_id']])
        self.assertTrue(self.planner.plan(shared['plan_id'])['current_validation']['valid'])
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM planner_query_targets WHERE query_id=?',(shared['query_id'],)).fetchone()[0],2)

    def test_account_scope_and_period_region_are_part_of_spec_hash(self):
        self.active()
        base=self.plans('youtube')
        changed=self.plans('youtube',regionCode='US',youtube_days=14)
        self.assertTrue({p['spec_hash'] for p in base}.isdisjoint(p['spec_hash'] for p in changed))
        account=replace(self.tracking.account('youtube','local-default'),account_scope='second')
        self.tracking.accounts[('youtube','second')]=account
        t=self.register(account_scope='second');self.change('activate',t,approved=True)
        rows=self.plans('youtube');same=[p for p in rows if p['canonical_query']['parameters']['q']=='두쫀쿠']
        self.assertEqual(len({p['query_id'] for p in same}),2)

    def test_youtube_search_and_video_costs_and_dependent_template_are_separate(self):
        self.set_account('youtube',max_pages=2);self.active()
        p=self.plans('youtube')[0]
        lines={c['resource']:c for c in p['budget']['lines']}
        self.assertEqual(lines['youtube.search_calls']['cost'],2)
        self.assertEqual(lines['youtube.search_calls']['provider_limit'],100)
        self.assertEqual(lines['youtube.video_calls']['cost'],2)
        self.assertEqual(lines['youtube.video_calls']['provider_limit'],10000)
        self.assertIsNone(lines['youtube.search_calls']['provider_observed_usage'])
        self.assertTrue(p['canonical_query']['dependent_requests'][0]['requires_runtime_materialization'])
        self.assertEqual(p['mapping_semantics'],'search_provenance_only; not verified food mentions')

    def test_youtube_discovery_preserved_and_identical_transport_can_share_tracking(self):
        self.set_account('youtube',discovery_queries=['두쫀쿠','광역 발굴'])
        self.active();rows=self.plans('youtube',include_discovery=True)
        shared=next(p for p in rows if p['canonical_query']['parameters']['q']=='두쫀쿠')
        self.assertEqual(shared['query_kinds'],['DISCOVERY','TRACKING'])
        self.assertEqual(len(shared['targets']),1)
        self.assertIn('광역 발굴',{p['canonical_query']['parameters']['q'] for p in rows})

    def test_pt_dst_calendar_windows_and_fallback_are_correct(self):
        cases=[('2026-03-08T08:00:00+00:00',23,'2026-03-08'),('2026-11-01T07:00:00+00:00',25,'2026-11-01'),
               ('2026-03-08T07:59:59+00:00',24,'2026-03-07')]
        for at,hours,day in cases:
            window=daily_window(at,'America/Los_Angeles')
            self.assertEqual((instant(window['end'])-instant(window['start'])).total_seconds(),hours*3600)
            self.assertEqual(window['day'],day)
            with patch('food_trends.quota.ZoneInfo',side_effect=ZoneInfoNotFoundError):
                self.assertEqual(daily_window(at,'America/Los_Angeles'),window)

    def test_naver_kst_date_boundary_and_context_separation(self):
        self.active('naver_datalab')
        self.now=instant('2026-10-27T14:59:59+00:00');before=self.plans('naver_datalab')[0]
        self.now=instant('2026-10-27T15:00:00+00:00');after=self.plans('naver_datalab')[0]
        self.assertEqual(before['canonical_query']['parameters']['endDate'],'2026-10-26')
        self.assertEqual(after['canonical_query']['parameters']['endDate'],'2026-10-27')
        self.assertNotEqual(before['effective_window']['context_hash'],after['effective_window']['context_hash'])
        filtered=self.plans('naver_datalab',filters={'device':'mo','ages':['3','2','2']})[0]
        self.assertEqual(filtered['canonical_query']['parameters']['ages'],['2','3'])
        self.assertNotEqual(after['spec_hash'],filtered['spec_hash'])
        self.assertEqual(daily_window(self.now,'Asia/Seoul')['day'],'2026-10-28')

    def test_naver_5_group_batches_and_no_duplicate_terms_inside_group(self):
        self.active('naver_datalab')
        for n in range(5):
            f=self.create_food('음식 '+str(n))
            t=self.cmd('register',food_id=f['food_id'],source_id='naver_datalab');self.change('activate',t,approved=True)
        plans=self.plans('naver_datalab');self.assertEqual(sorted(len(p['targets']) for p in plans),[1,5])
        for p in plans:
            for group in p['canonical_query']['parameters']['keywordGroups']:
                self.assertEqual(len(group['keywords']),len(set(group['keywords'])))
        context1=plans[0]['effective_window']['context_hash'];new=self.planner.preview([plans[0]['targets'][0]['target_id']])['plans'][0]
        if len(plans[0]['targets'])>1:self.assertNotEqual(context1,new['effective_window']['context_hash'])

    def test_naver_cross_group_overlap_is_blocked_without_silent_assignment(self):
        self.active('naver_datalab')
        second=self.create_food('다른 상품',['두쫀쿠'],scope='food:second')
        t=self.cmd('register',food_id=second['food_id'],source_id='naver_datalab');self.change('activate',t,approved=True)
        preview=self.planner.preview();self.assertFalse([p for p in preview['plans'] if p['source_id']=='naver_datalab'])
        self.assertEqual(sum('OVERLAPPING_NAVER_GROUPS' in r['reasons'] for r in preview['rejections']),2)

    def test_more_than_five_naver_aliases_is_blocked_not_truncated(self):
        f=self.create_food('큰 그룹',['별칭'+str(n) for n in range(5)])
        t=self.cmd('register',food_id=f['food_id'],source_id='naver_datalab');self.change('activate',t,approved=True)
        preview=self.planner.preview();self.assertEqual(preview['plans'],[])
        self.assertEqual(preview['rejections'][0]['reasons'],['NAVER_5_KEYWORD_LIMIT'])

    def test_instagram_soft_limit_counts_unique_tags_and_distinguishes_page_calls(self):
        self.set_account('instagram_graph',soft_limit=1,max_pages=3);self.active('instagram_graph')
        rows=self.plans('instagram_graph')
        self.assertEqual(sum(p['budget_status']=='READY_LOCAL' for p in rows),1)
        self.assertEqual(sum(p['budget_status']=='DEFERRED' for p in rows),1)
        lines={l['resource']:l for l in rows[0]['budget']['lines']}
        self.assertEqual(lines['instagram.unique_tags']['provider_limit'],30)
        self.assertEqual(lines['instagram.media_calls']['cost'],3)
        self.assertEqual(rows[0]['canonical_query']['pagination']['on_page_cap'],'PARTIAL_LIMITED')

    def test_instagram_repeating_tag_does_not_extend_first_seven_day_window(self):
        self.active('instagram_graph');p=self.planner.commit('first')['plans'][0]
        tag=p['canonical_query']['parameters']['q'];r=self.planner.quota.reserve(p['plan_id'],'first-use');self.planner.quota.mark_attempted(r['reservation_id'])
        self.now+=timedelta(days=6)
        p2=next(p for p in self.planner.commit('repeat')['plans'] if p['canonical_query']['parameters']['q']==tag)
        cost=next(l for l in p2['budget']['lines'] if l['resource']=='instagram.unique_tags');self.assertEqual(cost['cost'],0)
        r2=self.planner.quota.reserve(p2['plan_id'],'repeat-use');self.planner.quota.mark_attempted(r2['reservation_id'])
        self.now+=timedelta(days=1,seconds=1)
        p3=next(p for p in self.plans('instagram_graph') if p['canonical_query']['parameters']['q']==tag)
        self.assertEqual(next(l['cost'] for l in p3['budget']['lines'] if l['resource']=='instagram.unique_tags'),1)

    def test_instagram_legacy_rolling_usage_keeps_existing_tag_and_defers_new_tag(self):
        account=replace(self.tracking.account('instagram_graph','local-default'),account_scope='ig:12345')
        self.tracking.accounts[('instagram_graph','ig:12345')]=account
        t=self.register('instagram_graph',account_scope='ig:12345');self.change('activate',t,approved=True)
        with self.storage.connect() as db:
            for i in range(28):
                db.execute('INSERT INTO instagram_searches(user_id,name,instagram_id,searched_at) VALUES (?,?,?,?)',
                    ('12345','두쫀쿠' if i==0 else 'legacy'+str(i),str(i),(self.now-timedelta(days=6)).isoformat()))
            db.execute('INSERT INTO instagram_searches(user_id,name,searched_at) VALUES (?,?,?)',('12345','expired',(self.now-timedelta(days=8)).isoformat()))
        rows=self.plans('instagram_graph')
        existing=next(p for p in rows if p['canonical_query']['parameters']['q']=='두쫀쿠')
        new=next(p for p in rows if p['canonical_query']['parameters']['q']=='두바이쫀득쿠키')
        self.assertEqual(existing['budget_status'],'READY_LOCAL');self.assertEqual(new['budget_status'],'DEFERRED')
        self.assertEqual(existing['budget']['lines'][0]['observed_usage'],28)

    def test_released_shared_tag_reservation_is_rechecked_before_attempt(self):
        old=self.tracking.account('instagram_graph','local-default')
        self.tracking.accounts[('instagram_graph','local-default')]=replace(old,interval_seconds=1,options={'soft_limit':1})
        self.active('instagram_graph')
        first=self.planner.commit('initial-tags')['plans'][0];tag=first['canonical_query']['parameters']['q']
        r1=self.planner.quota.reserve(first['plan_id'],'owner-one')
        self.now+=timedelta(seconds=1)
        newer=self.planner.commit('second-slot')['plans']
        same=next(p for p in newer if p['canonical_query']['parameters']['q']==tag)
        other=next(p for p in newer if p['canonical_query']['parameters']['q']!=tag)
        r2=self.planner.quota.reserve(same['plan_id'],'owner-two')
        self.planner.quota.release(r1['reservation_id'])
        self.planner.quota.reserve(other['plan_id'],'other-tag')
        with self.assertRaises(FoodError):self.planner.quota.mark_attempted(r2['reservation_id'])

    def test_cost_or_adapter_version_change_invalidates_old_plan_and_changes_spec(self):
        p=self.persisted()
        with patch('food_trends.query_planner.COST_VERSION','fixture-next-cost'),patch('food_trends.quota.COST_VERSION','fixture-next-cost'):
            self.assertFalse(self.planner.plan(p['plan_id'])['current_validation']['valid'])
            self.assertNotIn(p['query_id'],{row['query_id'] for row in self.plans('youtube')})

    def test_mutating_preview_cannot_change_server_limits(self):
        self.active();preview=self.plans('youtube')[0]
        preview['account_options']['daily_search_limit']=100
        self.assertNotIn('daily_search_limit',self.tracking.account('youtube','local-default').options)
        self.assertEqual(self.plans('youtube')[0]['budget']['lines'][0]['local_limit'],40)

    def test_reservation_racing_pause_cannot_authorize_stale_attempt(self):
        p=self.persisted();target=self.tracking.target(p['targets'][0]['target_id'])
        def reserve():
            try:return self.planner.quota.reserve(p['plan_id'],'racing-pause')
            except FoodError as exc:return exc.status
        with ThreadPoolExecutor(max_workers=2) as workers:
            future=workers.submit(reserve);pause=workers.submit(self.change,'pause',target);result=future.result();pause.result()
        self.assertFalse(self.planner.plan(p['plan_id'])['current_validation']['valid'])
        if isinstance(result,dict):
            with self.assertRaises(FoodError):self.planner.quota.mark_attempted(result['reservation_id'])
        else:self.assertEqual(result,409)

    def test_existing_legacy_calls_consume_same_local_daily_budget(self):
        self.set_account('youtube',daily_search_limit=1);self.active()
        with self.storage.connect() as db:
            db.execute("INSERT INTO youtube_calls(day,endpoint,started_at,status) VALUES (?,'search',?,'failed')",('2026-10-26',self.now.isoformat()))
        self.assertTrue(all(p['budget_status']=='DEFERRED' for p in self.plans('youtube')))

    def test_preview_never_creates_usage_reservations_or_writes(self):
        self.active();before=snapshot(self.storage)
        for _ in range(4):self.planner.preview()
        self.assertEqual(snapshot(self.storage),before)
        stored=self.planner.commit('store-only')
        with self.storage.reader() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM quota_reservations').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM youtube_calls').fetchone()[0],0)
        self.assertTrue(all(not p['execution_enabled'] for p in stored['plans']))

    def test_aggregate_preview_defers_excess_requests_in_same_batch(self):
        self.set_account('youtube',daily_search_limit=1);self.active()
        rows=self.plans('youtube');self.assertEqual(sum(p['decision']=='READY_FOR_B3_RECHECK' for p in rows),1)
        self.assertEqual(sum(p['decision']=='DEFERRED_BUDGET' for p in rows),2)

    def test_future_due_is_previewed_without_executable_reservation(self):
        t=self.change('interval',self.active(),interval_seconds=43200)
        p=self.planner.commit('future')['plans'][0];self.assertEqual(p['decision'],'NOT_DUE')
        with self.assertRaises(FoodError):self.planner.quota.reserve(p['plan_id'],'too-early')

    def test_persisted_specs_and_old_plan_versions_are_immutable(self):
        p=self.persisted();before=self.planner.plan(p['plan_id'])['plan']
        for table in ('planner_queries','planner_plans','planner_plan_targets','planner_query_targets'):
            with self.assertRaises(sqlite3.IntegrityError),self.storage.connect() as db:db.execute('DELETE FROM '+table)
        self.assertEqual(self.planner.plan(p['plan_id'])['plan'],before)

    def test_alias_revoke_refresh_creates_new_mapping_version_preserving_old_plan(self):
        t=self.active();saved=self.planner.commit('old')
        old=next(p for p in saved['plans'] if p['canonical_query']['parameters']['q']=='두바이쫀득쿠키')
        f=self.foods.food(self.food['food_id']);alias=next(a for a in f['approved_aliases'] if a['surface_form']=='두바이쫀득쿠키')
        self.foods.command('alias',dict(request_key='revoke',alias_id=alias['alias_id'],expected_revision=alias['revision'],action='revoke',approved=True,actor_ref='reviewer',reason='잘못된 별칭'))
        self.assertFalse(self.planner.plan(old['plan_id'])['current_validation']['valid'])
        self.change('refresh',t);new=self.planner.commit('new')
        self.assertNotIn('두바이쫀득쿠키',{p['canonical_query']['parameters']['q'] for p in new['plans']})
        self.assertTrue(set(saved['stored_plan_ids']).isdisjoint(new['stored_plan_ids']))
        self.assertEqual(self.planner.plan(old['plan_id'])['plan'],old)

    def test_policy_blocked_operations_and_user_override_options_are_rejected(self):
        self.active()
        with self.assertRaises(FoodError):self.planner.preview(options={'operation':'food_extraction'})
        with self.assertRaises(FoodError):self.planner.preview(options={'policy_allowed':True})
        account=self.tracking.account('youtube','local-default')
        self.tracking.accounts[('youtube','local-default')]=replace(account,policy_allowed=False)
        p=self.planner.preview();self.assertEqual(p['plans'],[]);self.assertEqual(p['rejections'][0]['policy_status'],'BLOCKED')

    def test_rss_feed_is_one_shared_discovery_without_food_targets_or_fake_foods(self):
        self.active();self.active('instagram_graph')
        before=original_rows(self.storage)
        rows=self.planner.preview(options={'include_discovery':True})['plans'];rss=[p for p in rows if p['source_id']=='google_trends_rss']
        self.assertEqual(len(rss),1);self.assertEqual(rss[0]['targets'],[])
        self.assertEqual(rss[0]['canonical_query']['parameters'],{'geo':'KR'})
        self.assertEqual(rss[0]['canonical_query']['interval_seconds'],1800)
        self.assertEqual(original_rows(self.storage),before)

    def test_rss_collection_bridges_new_food_candidates_through_exact_resolution(self):
        food=self.create_food('쿠키')
        rss=CollectionService(self.storage,fetcher=lambda _:(fixture('쿠키'),200))
        result=rss.collect();self.assertTrue(result['success']);self.assertEqual(result['entity_resolution']['state'],'completed')
        candidate=next(c for c in self.foods.candidates() if c['source_code']=='google_trends_rss')
        self.assertEqual(candidate['resolved_food_id'],food['food_id']);self.assertEqual(candidate['origin'],'mock')
        target=self.cmd('register',food_id=food['food_id'],source_id='youtube',candidate_id=candidate['candidate_id'])
        self.assertEqual(target['registration_method'],'DISCOVERY');self.assertEqual(target['origin'],'mock')
        self.assertEqual(self.planner.preview()['plans'],[]) # approval still required

    def test_rss_zero_food_is_success_without_fake_common_candidates(self):
        before=len(self.foods.candidates());rss=CollectionService(self.storage,fetcher=lambda _:(fixture('광주송정역'),200))
        result=rss.collect();self.assertTrue(result['success']);self.assertEqual(self.storage.status()['food_count'],0)
        self.assertEqual(result['entity_resolution']['candidates'],0);self.assertEqual(len(self.foods.candidates()),before)

    def test_rss_resolution_failure_does_not_rewrite_successful_source_run(self):
        rss=CollectionService(self.storage,fetcher=lambda _:(fixture('쿠키'),200))
        with patch('food_trends.food_service.FoodService.import_rss_run',side_effect=RuntimeError('synthetic local failure')):
            result=rss.collect()
        self.assertTrue(result['success']);self.assertEqual(result['entity_resolution']['state'],'pending_retry')
        with self.storage.reader() as db:self.assertEqual(db.execute('SELECT success FROM runs WHERE id=?',(result['run_id'],)).fetchone()[0],1)
        self.assertEqual(self.foods.import_rss_run(result['run_id'])['state'],'completed')

    def test_atomic_reservation_idempotence_and_double_reservation_prevention(self):
        p=self.persisted();q=self.planner.quota
        r=q.reserve(p['plan_id'],'attempt-one')
        self.assertEqual(q.reserve(p['plan_id'],'attempt-one'),r)
        with self.assertRaises(FoodError):q.reserve(p['plan_id'],'other-key',attempt_no=1)
        q.release(r['reservation_id']);self.assertEqual(q.reserve(p['plan_id'],'retry',attempt_no=2)['state'],'RESERVED')

    def test_concurrent_reservation_budget_race_prevents_overspending(self):
        self.set_account('youtube',daily_search_limit=1);self.active();plans=self.planner.commit('budget-race')['plans']
        def reserve(p):
            try:return self.planner.quota.reserve(p['plan_id'],uuid.uuid4().hex)['state']
            except FoodError as exc:return exc.status
        with ThreadPoolExecutor(max_workers=3) as workers:results=list(workers.map(reserve,plans))
        self.assertEqual(results.count('RESERVED'),1);self.assertEqual(results.count(409),2)

    def test_concurrent_same_physical_query_reservation_creates_one_attempt(self):
        p=self.persisted()
        def reserve(_):
            try:return self.planner.quota.reserve(p['plan_id'],uuid.uuid4().hex)['state']
            except FoodError as exc:return exc.status
        with ThreadPoolExecutor(max_workers=4) as workers:results=list(workers.map(reserve,range(4)))
        self.assertEqual(results.count('RESERVED'),1)

    def test_pause_after_reservation_blocks_attempt_and_late_result_attachment(self):
        p=self.persisted();r=self.planner.quota.reserve(p['plan_id'],'pending')
        target=self.tracking.target(p['targets'][0]['target_id']);self.change('pause',target)
        with self.assertRaises(FoodError):self.planner.quota.mark_attempted(r['reservation_id'])
        self.assertEqual(self.planner.valid_result_targets(r['reservation_id']),[])

    def test_attempts_keep_usage_even_on_unknown_remote_debit_and_no_refund(self):
        p=self.persisted();r=self.planner.quota.reserve(p['plan_id'],'attempt');self.planner.quota.mark_attempted(r['reservation_id'])
        with self.assertRaises(FoodError):self.planner.quota.release(r['reservation_id'])
        budget=self.planner.plan(p['plan_id'])['current_budget']
        self.assertFalse(budget['remote_debit_confirmed']);self.assertEqual(budget['lines'][0]['observed_usage'],1)

    def test_expiry_and_daily_reset_require_fresh_reservation(self):
        p=self.persisted();r=self.planner.quota.reserve(p['plan_id'],'expire',ttl_seconds=1)
        self.now+=timedelta(seconds=2)
        with self.assertRaises(FoodError):self.planner.quota.mark_attempted(r['reservation_id'])
        self.assertEqual(self.planner.plan(p['plan_id'])['current_budget']['lines'][0]['reserved_cost'],0)
        self.assertEqual(self.planner.quota.reserve(p['plan_id'],'new',attempt_no=2)['state'],'RESERVED')

    def test_reserved_snapshot_excludes_target_not_due_at_attempt(self):
        one=self.active();second=self.create_food('다른 음식',['두쫀쿠'],scope='food:second')
        t=self.cmd('register',food_id=second['food_id'],source_id='youtube');t=self.change('activate',t,approved=True)
        self.change('interval',t,interval_seconds=43200)
        p=next(p for p in self.planner.commit('future-shared')['plans'] if p['canonical_query']['parameters']['q']=='두쫀쿠')
        r=self.planner.quota.reserve(p['plan_id'],'only-due');self.planner.quota.mark_attempted(r['reservation_id'])
        self.now+=timedelta(seconds=43200)
        self.assertEqual([m['target_id'] for m in self.planner.valid_result_targets(r['reservation_id'])],[one['target_id']])

    def test_http_preview_and_history_open_only_read_connections(self):
        p=self.persisted();rss=CollectionService(self.storage,fetcher=lambda _:(_ for _ in ()).throw(AssertionError('No HTTP')))
        server=make_server(rss,port=0,tracking=self.tracking,planner=self.planner);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        before=snapshot(self.storage);connect=self.storage.connect
        def reader_only(*args,**kwargs):
            if not kwargs.get('readonly'):raise AssertionError('Preview write')
            return connect(*args,**kwargs)
        try:
            with patch.object(self.storage,'connect',side_effect=reader_only):
                for route in ('preview','history','plans/'+p['plan_id']):
                    with urlopen(f'http://127.0.0.1:{server.server_port}/api/planner/'+route) as response:self.assertEqual(response.status,200)
                req=Request(f'http://127.0.0.1:{server.server_port}/api/planner/preview',data=b'{}',headers={'Content-Type':'application/json'})
                with urlopen(req) as response:self.assertEqual(response.status,200)
        finally:server.shutdown();server.server_close();thread.join()
        self.assertEqual(snapshot(self.storage),before)

    def test_http_policy_extraction_and_permission_override_options_are_rejected(self):
        self.active();rss=CollectionService(self.storage,fetcher=lambda _:None)
        server=make_server(rss,port=0,tracking=self.tracking,planner=self.planner);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        before=snapshot(self.storage)
        try:
            for query,status in [('operation=food_extraction',403),('policy_allowed=true',400)]:
                with self.assertRaises(HTTPError) as error:urlopen(f'http://127.0.0.1:{server.server_port}/api/planner/preview?'+query)
                self.assertEqual(error.exception.code,status)
        finally:server.shutdown();server.server_close();thread.join()
        self.assertEqual(snapshot(self.storage),before)

    def test_naver_response_storage_policy_and_metadata_only_tables_remain(self):
        self.active('naver_datalab');before=original_rows(self.storage);self.planner.commit('metadata-only')
        with self.storage.reader() as db:
            values=' '.join(r[0] for r in db.execute('SELECT payload_json FROM planner_plans'))
            self.assertNotIn('"ratio"',values);self.assertFalse(self.plans('naver_datalab')[0]['contract']['policy']['response_storage'])
        self.assertEqual(original_rows(self.storage),before)
