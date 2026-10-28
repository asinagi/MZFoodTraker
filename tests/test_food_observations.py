"""B5 search evidence, confirmed mentions and metric observations around FOOD_ID (disposable DBs, Mock only)."""
import json
import sqlite3
import unittest
import uuid
from datetime import timedelta
from urllib.error import URLError
from urllib.parse import urlsplit
from xml.sax.saxutils import escape
from food_trends.execution_mock import MockTransport, MockResponse, mock_controller
from food_trends.food_observations import FoodObservationService, alias_pattern
from food_trends.ingestion import PolicyBlocked
from food_trends.service import CollectionService
from food_trends.tracking_service import stamp
import test_execution as fixtures


def rss_xml(*entries):
    """entries: (keyword, [news titles])."""
    items = ''.join('<item><title>%s</title><pubDate>Tue, 27 Oct 2026 10:00:00 +0900</pubDate><ht:approx_traffic>%s</ht:approx_traffic>%s</item>'
                    % (escape(k), '1000+' if i == 0 else '', ''.join('<ht:news_item><ht:news_item_title>%s</ht:news_item_title></ht:news_item>' % escape(t) for t in news))
                    for i, (k, news) in enumerate(entries))
    return ('<rss xmlns:ht="https://trends.google.com/trending/rss"><channel>%s</channel></rss>' % items).encode()


class FoodObservationTests(unittest.TestCase):
    setUp = fixtures.ExecutionTests.setUp
    tearDown = fixtures.ExecutionTests.tearDown
    create_food = fixtures.ExecutionTests.create_food
    cmd = fixtures.ExecutionTests.cmd
    register = fixtures.ExecutionTests.register
    active = fixtures.ExecutionTests.active
    job = fixtures.ExecutionTests.job
    advance = fixtures.ExecutionTests.advance

    def obs(self):
        return FoodObservationService(self.storage, lambda: self.now)

    def rss(self, *entries):
        return CollectionService(self.storage, fetcher=lambda _: (rss_xml(*entries), 200)).collect()

    def run_source(self, source, transport=None):
        self.active(source)
        controller = mock_controller(self.storage, self.planner, transport or MockTransport())
        return [controller.run_once(j['job_id']) for j in controller.execution.schedule()]

    def q(self, sql, *args):
        with self.storage.reader() as db:
            return [tuple(r) for r in db.execute(sql, args)]

    def review(self, operation, **body):
        if 'expected_revision' not in body:  # food-level commands use the subject food's current revision
            body['expected_revision'] = self.foods.food(body['from_food_id'] if operation == 'relate' else body['food_id'])['revision']
        return self.foods.command(operation, dict(request_key=uuid.uuid4().hex, approved=True, actor_ref='reviewer', reason='개체 검토', **body))

    def mentions(self, status='ACTIVE'):
        return self.q('SELECT food_id,field_code,verification_status FROM food_mentions WHERE status=? ORDER BY food_id,field_code', status)

    # 21, 26: aliases resolve to one FOOD_ID and per-food item counts are distinct
    def test_two_aliases_one_food_and_one_item_counted_once(self):
        food = self.food['food_id']
        self.rss(('두쫀쿠', ['두바이 쫀득 쿠키 열풍, 두쫀쿠 품절']), ('두바이쫀득쿠키', []))
        self.assertEqual({m[0] for m in self.mentions()}, {food})
        self.assertEqual(len(self.mentions()), 4)  # keyword x2, and two aliases inside one news title
        view = self.obs().food_view(food)['sources']['google_trends_rss']
        self.assertEqual((view['confirmed_items'], view['mentions']), (2, 4))

    # 22, 23, 30: search evidence is not a mention; YouTube derived analysis stays off
    def test_same_video_two_queries_one_item_and_no_youtube_mention_or_metric(self):
        results = self.run_source('youtube')
        self.assertTrue(all(r['linking']['matches'] == 1 for r in results))
        view = self.obs().food_view(self.food['food_id'])['sources']['youtube']
        self.assertEqual((view['search_matches'], view['search_matched_items'], view['confirmed_items']), (3, 1, 0))
        self.assertEqual((view['mention_policy'], view['metric_storage']), ('POLICY_BLOCKED', 'POLICY_BLOCKED'))
        self.assertEqual(self.q('SELECT count(*) FROM food_mentions'), [(0,)])
        self.assertEqual(self.q('SELECT count(*) FROM metric_observations'), [(0,)])
        self.assertEqual(self.q('SELECT DISTINCT verification_status FROM item_query_matches'), [('SEARCH_ONLY',)])

    def test_instagram_search_hit_needs_the_caption_tag_for_a_mention(self):
        class Captions(MockTransport):
            def __call__(self, request, timeout=None):
                raw = super().__call__(request, timeout)
                if urlsplit(request.full_url).path.endswith('recent_media') and 'after' not in request.full_url:
                    data = json.loads(raw.getvalue()); data['data'].append(dict(data['data'][0], id='900002', caption='#디저트 맛집'))
                    return MockResponse(json.dumps(data, ensure_ascii=False).encode())
                return raw
        self.run_source('instagram_graph', Captions())
        view = self.obs().food_view(self.food['food_id'])['sources']['instagram_graph']
        self.assertEqual((view['search_matched_items'], view['confirmed_items']), (2, 1))
        self.assertEqual(self.mentions(), [(self.food['food_id'], 'caption_hashtag', 'EXACT_ALIAS_HASHTAG')])

    # 24, 25: several foods in one item; a derived food keeps its own FOOD_ID
    def test_multiple_foods_and_variant_food_are_separate(self):
        mala = self.create_food('마라탕')
        variant = self.create_food('짭쫀쿠')
        self.review('relate', from_food_id=variant['food_id'], to_food_id=self.food['food_id'], relation_type='VARIANT_OF')
        self.rss(('짭쫀쿠', ['두쫀쿠와 마라탕 조합', '두쫀쿠런 이벤트']))
        foods = sorted({m[0] for m in self.mentions()})
        self.assertEqual(foods, sorted([self.food['food_id'], mala['food_id'], variant['food_id']]))
        self.assertEqual(self.q("SELECT count(*) FROM food_mentions WHERE food_id=? AND field_code='keyword'", variant['food_id']), [(1,)])
        self.assertEqual(self.q("SELECT count(*) FROM food_mentions WHERE food_id=? AND field_code='keyword'", self.food['food_id']), [(0,)])
        self.assertEqual(self.q("SELECT count(*) FROM food_mentions WHERE field_code='news_title:1'"), [(0,)])  # '두쫀쿠런' is not '두쫀쿠'
        self.assertIsNone(alias_pattern('쿠키').search('쿠키런'))
        self.assertIsNotNone(alias_pattern('두쫀쿠').search('요즘 두쫀쿠가 인기'))

    # 27, 28, 29, 31: units and contexts kept apart; NAVER never stored; failure is NULL, zero is zero
    def test_units_contexts_failure_and_zero_are_distinct(self):
        self.rss(('두쫀쿠', []))
        self.run_source('instagram_graph')
        self.run_source('naver_datalab')
        view = self.obs().food_view(self.food['food_id'])['sources']
        self.assertEqual({m['unit'] for m in view['google_trends_rss']['metrics']}, {'approximate_search_traffic_text'})
        self.assertEqual({m['unit'] for m in view['instagram_graph']['metrics']}, {'unique_observed_post'})
        self.assertEqual(view['naver_datalab']['metric_storage'], 'TRANSIENT_ONLY')
        self.assertEqual(self.q("SELECT count(*) FROM metric_observations WHERE source_code IN ('naver_datalab','youtube')"), [(0,)])
        self.assertNotIn('total', json.dumps(view))
        with self.storage.connect() as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute('''INSERT INTO metric_observations VALUES ('m','k',(SELECT ingest_run_id FROM ingest_runs LIMIT 1),'naver_datalab',
                'naver.relative_search_index','relative_search_index_0_100','FOOD','x',?,NULL,'2026-10-27T00:00:00+00:00',NULL,NULL,NULL,
                '{}','h','OBSERVED',NULL,50.0,NULL,'COMPLETE','mock','v',NULL,'ACTIVE')''', (self.food['food_id'],))
        with self.storage.connect() as db, self.assertRaises(PolicyBlocked):
            run = db.execute("SELECT * FROM ingest_runs WHERE source_code='naver_datalab'").fetchone()
            self.obs()._insert_metric(db, run, dict(metric_code='naver.relative_search_index', subject_key='x', value_state='OBSERVED',
                                                    decimal_value=50.0, observed_at=stamp(self.now), comparison_context={}))

    def test_failed_day_is_not_observed_and_empty_success_is_zero(self):
        class Down(MockTransport):
            def __call__(self, request, timeout=None):
                raise URLError('fixture outage')

        class Empty(MockTransport):
            def __call__(self, request, timeout=None):
                if urlsplit(request.full_url).path.endswith('recent_media'):
                    self.calls.append('recent_media'); return MockResponse(b'{"data": []}')
                return super().__call__(request, timeout)
        target = self.active('instagram_graph')
        failing = mock_controller(self.storage, self.planner, Down())
        jobs = failing.execution.schedule()
        for _ in range(3):
            for j in jobs:
                failing.run_once(j['job_id'])
            self.advance(600)
        self.assertEqual({r[0] for r in self.q("SELECT value_state FROM metric_observations")}, {'NOT_OBSERVED'})
        self.assertEqual({r[0] for r in self.q("SELECT count_value FROM metric_observations")}, {None})
        self.now += timedelta(seconds=target['interval_seconds'] + 1)
        empty = mock_controller(self.storage, self.planner, Empty())
        for j in empty.execution.schedule():
            empty.run_once(j['job_id'])
        observed = self.q("SELECT count_value,coverage FROM metric_observations WHERE value_state='OBSERVED'")
        self.assertTrue(observed and all(r == (0, 'COMPLETE') for r in observed))

    # 32, 33: alias changes, merge and split reprocess allowed evidence and mark aggregates dirty
    def test_alias_approval_and_revocation_reprocess_mentions(self):
        self.rss(('두쫀 디저트', ['두쫀 신메뉴']))
        self.assertEqual(self.mentions(), [])
        alias = self.review('alias', food_id=self.food['food_id'], surface_form='두쫀', action='approve')
        effects = self.obs().apply_entity_changes()
        self.assertEqual(sum(e['created_mentions'] for e in effects), 2)
        self.review('alias', alias_id=alias['alias_id'], action='revoke', expected_revision=alias['revision'])
        effect = self.obs().apply_entity_changes()[-1]
        self.assertEqual(effect['invalidated_mentions'], 2)
        self.assertEqual(len(self.mentions('INVALIDATED')), 2)
        self.assertEqual(self.obs().apply_entity_changes(), [])  # each decision is processed once
        self.assertTrue(self.q('SELECT count(*) FROM aggregate_invalidations WHERE food_id=?', self.food['food_id'])[0][0] >= 2)

    def test_merge_and_split_move_mentions_and_list_affected_foods(self):
        other = self.create_food('두바이 디저트')
        self.rss(('두바이 디저트', []))
        self.obs().apply_entity_changes()
        before = self.mentions()
        self.assertEqual(before[0][0], other['food_id'])
        self.review('merge', food_id=other['food_id'], target_food_id=self.food['food_id'],
                    expected_target_revision=self.foods.food(self.food['food_id'])['revision'])
        affected = {f for e in self.obs().apply_entity_changes() for f in e['affected_food_ids']}
        self.assertEqual(affected, {other['food_id'], self.food['food_id']})
        self.assertEqual([m[0] for m in self.mentions()], [self.food['food_id']])
        self.assertEqual([m[0] for m in self.mentions('INVALIDATED')], [other['food_id']])
        redirected = self.obs().food_view(other['food_id'])
        self.assertTrue(redirected['redirected'])
        self.assertEqual(redirected['sources']['google_trends_rss']['confirmed_items'], 1)
        # Split: reassign the alias to a separately approved food.
        third = self.create_food('두바이 디저트 세트')
        alias = next(a for a in self.foods.food(self.food['food_id'])['approved_aliases'] if a['surface_form'] == '두바이 디저트')
        self.review('alias', alias_id=alias['alias_id'], action='reassign', food_id=third['food_id'], expected_revision=alias['revision'])
        self.obs().apply_entity_changes()
        self.assertEqual([m[0] for m in self.mentions()], [third['food_id']])
        self.assertTrue(self.q('SELECT count(*) FROM aggregate_invalidations WHERE food_id=?', third['food_id'])[0][0] >= 1)

    # 34: expired evidence is not regenerated
    def test_expired_caption_evidence_is_not_regenerated(self):
        self.run_source('instagram_graph')
        self.assertEqual(len(self.mentions()), 1)
        with self.storage.connect() as db:
            db.execute('UPDATE instagram_media SET caption=NULL,caption_purged_at=?', (stamp(self.now),))
        self.review('alias', food_id=self.food['food_id'], surface_form='디저트', action='approve')
        effect = self.obs().apply_entity_changes()[-1]
        self.assertEqual((effect['created_mentions'], effect['not_reprocessable_items']), (0, 1))
        self.now += timedelta(days=31)
        self.assertEqual(self.obs().purge()['expired_mentions'], 1)
        self.assertEqual(self.obs().link_pending()['linked_runs'] >= 1, True)
        self.assertEqual(self.mentions(), [])

    def test_linking_is_idempotent(self):
        self.rss(('두쫀쿠', ['두쫀쿠 리뷰']))
        self.run_source('youtube')
        tables = ('item_query_matches', 'food_mentions', 'metric_observations')
        before = {t: self.q(f'SELECT * FROM {t} ORDER BY 1') for t in tables}
        self.obs().link_pending()
        self.assertEqual({t: self.q(f'SELECT * FROM {t} ORDER BY 1') for t in tables}, before)
