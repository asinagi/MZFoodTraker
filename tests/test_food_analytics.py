"""B8 read-time analytics over real B4/B5 rows (disposable DBs, fixed clocks, no network)."""
import json
import tempfile
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from food_trends.analytics_reader import SQLiteAnalyticsStore, TargetAnalyticsStore
from food_trends.analytics_service import FoodAnalytics, FoodNotFound, SourceNotRankable
from food_trends.food_observations import FoodObservationService
from food_trends.food_service import FoodService
from food_trends.observation_writer import ObservationWriter, snapshot
from food_trends.storage import Storage
from food_trends.target_db import SQLiteTarget
from food_trends.target_migration import migrate
from food_trends.target_repository import TargetRepository

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 3, 3, tzinfo=timezone.utc)  # 2026-11-03 12:00 KST; default end_date 2026-11-02
END = date(2026, 11, 2)


def kst(day, hour=12, minute=0, second=0):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=KST)


def back(days):
    return END - timedelta(days=days)


class AnalyticsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage.create(Path(self.temp.name) / 'b8.sqlite3')
        self.writer = ObservationWriter(self.storage, at=NOW - timedelta(days=120))
        self.now = NOW
        self.writer.food('FOOD_A', '두쫀쿠', ['두바이쫀득쿠키', '두바이 쫀득 쿠키'], 'PRODUCT')
        self.writer.food('FOOD_B', '마라탕', [], 'DISH')
        self.writer.query('Q_A1', '두쫀쿠')
        self.writer.query('Q_A2', '두바이쫀득쿠키')
        self.writer.query('Q_B', '마라탕')

    def analytics(self, origin='mock'):
        return FoodAnalytics(SQLiteAnalyticsStore(self.storage), clock=lambda: self.now, origin=origin)

    def rss_days(self, first, last, entries=lambda day: [], failed=(), origin='mock'):
        for n in range(first, last - 1, -1):
            day = back(n)
            self.writer.rss_run(kst(day, 9), entries(day), success=day not in failed, origin=origin)

    def series(self, result, source):
        return next(s for s in result['series'] if s['source_code'] == source)

    def metric(self, result, source):
        return next(s for s in result['sources'] if s['source_code'] == source)['metrics'][0]

    def values(self, result, source):
        return [(p['value'], p['value_state']) for p in self.series(result, source)['points']]


class CountingRules(AnalyticsCase):
    # 5, 6: an item observed on several days counts on each day, once per bucket; weeks and periods recompute the set
    def test_daily_values_repeat_and_week_and_period_recompute_the_set(self):
        same = {'keyword': '두쫀쿠', 'published_at': kst(back(13), 8).isoformat()}
        self.rss_days(13, 0, lambda day: [same] + ([{'keyword': '두쫀쿠 %s' % day}] if day.day % 2 else []))
        result = self.analytics().timeline('FOOD_A', 14, END)
        daily = [v for v, _ in self.values(result, 'google_trends_rss')]
        self.assertEqual(daily, [2 if back(n).day % 2 else 1 for n in range(13, -1, -1)])
        weeks = self.analytics().timeline('FOOD_A', 14, END, granularity='week')['series'][0]['points']
        self.assertEqual([(p['start_date'], p['days']) for p in weeks], [('2026-10-20', 6), ('2026-10-26', 7), ('2026-11-02', 1)])
        for week in weeks:
            days = [d for d in (date.fromisoformat(week['start_date']) + timedelta(days=k) for k in range(week['days']))]
            self.assertEqual(week['value'], 1 + sum(d.day % 2 for d in days))  # the repeated item once, not once per day
            self.assertLess(week['value'], sum(daily[(d - back(13)).days] for d in days) + (week['days'] == 1))
        self.assertEqual([p['complete'] for p in weeks], [False, True, False])  # clipped weeks are flagged
        current = self.metric(self.analytics().metrics('FOOD_A', 7, END), 'google_trends_rss')['current']
        self.assertEqual(current['value'], 1 + sum(back(n).day % 2 for n in range(7)))

    # 5: KST calendar days (UTC 14:59:59 is 23:59:59 the same KST day; 15:00:00 is the next KST day)
    def test_kst_day_boundaries(self):
        day = back(1)
        self.writer.rss_run(datetime(day.year, day.month, day.day, 14, 59, 59, tzinfo=timezone.utc) - timedelta(days=1),
                            [{'keyword': '두쫀쿠'}])
        self.writer.rss_run(datetime(day.year, day.month, day.day, 15, 0, 0, tzinfo=timezone.utc) - timedelta(days=1),
                            [{'keyword': '두바이쫀득쿠키'}])
        points = self.series(self.analytics().timeline('FOOD_A', 7, END), 'google_trends_rss')['points']
        by_day = {p['start_date']: (p['value'], p['value_state'], p['runs']['successful']) for p in points}
        self.assertEqual(by_day[back(2).isoformat()], (1, 'OBSERVED', 1))
        self.assertEqual(by_day[back(1).isoformat()], (1, 'OBSERVED', 1))
        self.assertEqual(by_day[back(0).isoformat()], (None, 'NOT_OBSERVED', 0))

    # 7, ITEM_QUERY_MATCH: one post with two aliases from two alias queries counts once; a search hit without the tag never counts
    def test_multi_alias_post_once_and_search_only_hits_excluded(self):
        for n in range(6, -1, -1):
            both = {'id': '91%02d' % n, 'tags': ['두쫀쿠', '두바이쫀득쿠키']}
            other = {'id': '92%02d' % n, 'tags': ['디저트'], 'caption': '#디저트 맛집 (태그에 음식 별칭 없음)'}
            self.writer.instagram_run(kst(back(n), 10), 'Q_A1', ['FOOD_A'], [both, other])
            self.writer.instagram_run(kst(back(n), 11), 'Q_A2', ['FOOD_A'], [both])
        result = self.analytics().timeline('FOOD_A', 7, END, source='instagram_graph')
        self.assertEqual(self.values(result, 'instagram_graph'), [(1, 'OBSERVED')] * 7)
        self.assertEqual(self.metric(self.analytics().metrics('FOOD_A', 7, END), 'instagram_graph')['current']['value'], 7)
        with self.storage.reader() as db:  # the search evidence exists but is not a mention
            self.assertEqual(db.execute("SELECT count(DISTINCT item_key) FROM item_query_matches").fetchone()[0], 14)
            self.assertEqual(db.execute("SELECT count(*) FROM food_mentions").fetchone()[0], 14)

    def test_unapproved_names_and_variants_are_not_counted_for_the_food(self):
        self.writer.food('FOOD_V', '짭쫀쿠', [], 'PRODUCT')
        self.writer.relate('FOOD_V', 'FOOD_A')
        self.rss_days(6, 0, lambda day: [{'keyword': '짭쫀쿠'}, {'keyword': '두쫀쿠런', 'news': ['쿠키런 신작']}])
        values = self.values(self.analytics().timeline('FOOD_A', 7, END), 'google_trends_rss')
        self.assertEqual(values, [(0, 'OBSERVED')] * 7)  # neither the variant nor a non-alias word is this food
        variant = self.analytics().timeline('FOOD_V', 7, END)
        self.assertEqual(self.values(variant, 'google_trends_rss'), [(1, 'OBSERVED')] * 7)
        detail = self.analytics().food('FOOD_A')
        self.assertEqual(detail['relations'], [{'relation_type': 'HAS_VARIANT',
                                                'food': {'food_id': 'FOOD_V', 'canonical_name': '짭쫀쿠', 'entity_kind': 'PRODUCT'}}])


class ObservationStates(AnalyticsCase):
    # 8: no successful run is NOT_OBSERVED (null); a successful run with no items is an observed 0
    def test_unobserved_is_null_and_observed_zero_is_zero(self):
        for n in range(6, -1, -1):
            if n == 3:
                self.writer.instagram_run(kst(back(n), 10), 'Q_B', ['FOOD_B'], coverage='NONE')
            elif n != 4:
                self.writer.instagram_run(kst(back(n), 10), 'Q_B', ['FOOD_B'], [{'id': '81%02d' % n, 'tags': ['마라탕']}] if n % 2 else [])
        points = self.series(self.analytics().timeline('FOOD_B', 7, END), 'instagram_graph')['points']
        self.assertEqual([(p['value'], p['value_state'], p['coverage']) for p in points],
                         [(0, 'OBSERVED', 'COMPLETE'), (1, 'OBSERVED', 'COMPLETE'), (None, 'NOT_OBSERVED', 'NONE'),
                          (None, 'NOT_OBSERVED', 'NONE'), (0, 'OBSERVED', 'COMPLETE'), (1, 'OBSERVED', 'COMPLETE'), (0, 'OBSERVED', 'COMPLETE')])
        self.assertEqual([p['runs'] for p in points][2:4], [{'successful': 0, 'failed': 0}, {'successful': 0, 'failed': 1}])
        current = self.metric(self.analytics().metrics('FOOD_B', 7, END), 'instagram_graph')['current']
        self.assertEqual((current['value'], current['value_state'], current['day_counts']),
                         (None, 'INSUFFICIENT_COVERAGE', {'observed': 5, 'not_observed': 2, 'evidence_expired': 0}))
        untracked = self.metric(self.analytics().metrics('FOOD_A', 7, END), 'instagram_graph')['current']
        self.assertEqual((untracked['value'], untracked['value_state']), (None, 'NOT_OBSERVED'))

    # 9: PARTIAL/LIMITED days are labelled and block growth; a failed alias query next to a successful one is PARTIAL
    def test_partial_and_limited_coverage_are_labelled(self):
        for n in range(13, -1, -1):
            post = [{'id': '93%02d' % n, 'tags': ['두쫀쿠']}]
            self.writer.instagram_run(kst(back(n), 10), 'Q_A1', ['FOOD_A'], post, coverage='LIMITED' if n == 2 else 'COMPLETE')
            if n == 6:  # a second run of the same query that day returns the same post
                self.writer.instagram_run(kst(back(n), 10, 30), 'Q_A1', ['FOOD_A'], post)
            self.writer.instagram_run(kst(back(n), 11), 'Q_A2', ['FOOD_A'], [], coverage='NONE' if n == 4 else 'COMPLETE')
        points = self.series(self.analytics().timeline('FOOD_A', 7, END), 'instagram_graph')['points']
        self.assertEqual([p['coverage'] for p in points], ['COMPLETE', 'COMPLETE', 'PARTIAL', 'COMPLETE', 'LIMITED', 'COMPLETE', 'COMPLETE'])
        self.assertEqual([p['value'] for p in points], [1] * 7)
        self.assertEqual((points[0]['runs'], points[2]['runs']), ({'successful': 3, 'failed': 0}, {'successful': 1, 'failed': 1}))
        metric = self.metric(self.analytics().metrics('FOOD_A', 7, END), 'instagram_graph')
        self.assertEqual((metric['current']['value'], metric['current']['coverage']), (7, 'PARTIAL'))
        self.assertEqual((metric['previous']['value'], metric['previous']['coverage']), (7, 'COMPLETE'))
        self.assertEqual(metric['change'], {'state': 'NOT_COMPARABLE', 'reason': 'COVERAGE_LIMITED', 'absolute': None, 'ratio': None})
        # The best run of a query that day decides its coverage.
        self.writer.instagram_run(kst(back(2), 12), 'Q_A1', ['FOOD_A'], [{'id': '9302', 'tags': ['두쫀쿠']}])
        self.assertEqual(self.series(self.analytics().timeline('FOOD_A', 7, END), 'instagram_graph')['points'][4]['coverage'], 'COMPLETE')

    # 9: legacy_unknown / coverage UNKNOWN observations are shown beside the values, never inside them
    def test_legacy_unknown_is_reported_separately(self):
        for n in range(6, -1, -1):
            self.writer.instagram_run(kst(back(n), 10), 'Q_B', ['FOOD_B'], [{'id': '84%02d' % n, 'tags': ['마라탕']}])
        self.writer.legacy_instagram(NOW - timedelta(hours=1), [{'id': '8500', 'tags': ['마라탕'], 'seen_at': kst(back(3), 9)},
                                                                {'id': '8403', 'tags': ['마라탕'], 'seen_at': kst(back(3), 9)}])
        points = self.series(self.analytics().timeline('FOOD_B', 7, END), 'instagram_graph')['points']
        self.assertEqual([(p['value'], p['legacy_unknown_value']) for p in points][3], (1, 2))
        self.assertTrue(all(p['legacy_unknown_value'] is None for k, p in enumerate(points) if k != 3))
        metrics = next(s for s in self.analytics().metrics('FOOD_B', 7, END)['sources'] if s['source_code'] == 'instagram_graph')
        self.assertEqual(metrics['metrics'][0]['current']['value'], 7)
        self.assertEqual(metrics['legacy_unknown'], {'value': 2, 'coverage': 'UNKNOWN', 'origin': 'legacy_unknown'})
        status = next(s for s in self.analytics().sources_status(7)['sources'] if s['source_code'] == 'instagram_graph')
        self.assertEqual(status['legacy_unknown'], {'runs': 1, 'items': 2, 'coverage': 'UNKNOWN'})

    def test_expired_evidence_day_is_not_recounted(self):
        for n in range(40, -1, -1):
            self.writer.instagram_run(kst(back(n), 10), 'Q_A1', ['FOOD_A'], [{'id': '95%02d' % n, 'tags': ['두쫀쿠']}])
        self.writer.purge(NOW)
        points = self.series(self.analytics().timeline('FOOD_A', 90, END, source='instagram_graph'), 'instagram_graph')['points']
        states = [p['value_state'] for p in points]
        self.assertEqual(states[:49], ['NOT_OBSERVED'] * 49)
        expired = [p['start_date'] for p in points if p['value_state'] == 'EVIDENCE_EXPIRED']
        # Captions captured at 01:00 UTC expire 30 days later; at 03:00 UTC on 11-03 that covers 10-04 and earlier.
        self.assertEqual(expired, [back(n).isoformat() for n in range(40, 28, -1)])
        self.assertTrue(all(p['value'] == 1 for p in points if p['value_state'] == 'OBSERVED'))
        current = self.metric(self.analytics().metrics('FOOD_A', 30, END), 'instagram_graph')['current']
        self.assertEqual(current['value_state'], 'INSUFFICIENT_COVERAGE')
        self.assertEqual(self.metric(self.analytics().metrics('FOOD_A', 7, END), 'instagram_graph')['current']['value'], 7)

    def test_period_including_today_is_insufficient_but_timeline_shows_today(self):
        self.rss_days(6, -1, lambda day: [{'keyword': '두쫀쿠'}])
        timeline = self.series(self.analytics().timeline('FOOD_A', 7, END + timedelta(days=1)), 'google_trends_rss')['points']
        self.assertEqual((timeline[-1]['value'], timeline[-1]['complete']), (1, False))
        current = self.metric(self.analytics().metrics('FOOD_A', 7, END + timedelta(days=1)), 'google_trends_rss')['current']
        self.assertEqual((current['value'], current['value_state'], current['includes_incomplete_day']), (None, 'INSUFFICIENT_COVERAGE', True))

    # 12: a live server never counts mock-origin runs; legacy stays separate in every mode
    def test_origin_filter_keeps_mock_rows_out_of_live_values(self):
        self.rss_days(6, 0, lambda day: [{'keyword': '두쫀쿠'}], origin='mock')
        live = self.analytics('live_api')
        self.assertEqual(self.values(live.timeline('FOOD_A', 7, END), 'google_trends_rss'), [(None, 'NOT_OBSERVED')] * 7)
        self.assertEqual(self.values(self.analytics('mock').timeline('FOOD_A', 7, END), 'google_trends_rss'), [(1, 'OBSERVED')] * 7)
        self.assertEqual(self.metric(self.analytics('mock').metrics('FOOD_A', 7, END), 'google_trends_rss')['origin'], 'mock')
        self.assertIsNone(next(s for s in live.sources_status(7)['sources'] if s['source_code'] == 'google_trends_rss')['last_run_at'])


class PolicyAndComparisons(AnalyticsCase):
    # 10: YouTube and NAVER carry availability only, never numbers, and cannot be ranked
    def test_blocked_sources_have_no_values(self):
        analytics = self.analytics()
        for result, key in ((analytics.timeline('FOOD_A', 7, END), 'series'), (analytics.metrics('FOOD_A', 7, END), 'sources')):
            blocked = {s['source_code']: s for s in result[key] if s['source_code'] in ('youtube', 'naver_datalab')}
            self.assertEqual({k: v['availability'] for k, v in blocked.items()}, {'youtube': 'POLICY_BLOCKED', 'naver_datalab': 'NOT_STORED'})
            self.assertTrue(all(v['availability_reason'] for v in blocked.values()))
            self.assertTrue(all(not v.get('points') and not v.get('metrics') for v in blocked.values()))
        for source, reason in (('youtube', 'policy_blocked'), ('naver_datalab', 'not_stored')):
            with self.assertRaises(SourceNotRankable) as caught:
                analytics.rankings(source, 7, END)
            self.assertEqual(caught.exception.reason, reason)
        status = {s['source_code']: s for s in analytics.sources_status(7)['sources']}
        self.assertEqual(status['naver_datalab']['policy']['retention_days'], 0)
        self.assertIsNone(status['youtube']['window_stats'])

    # 11: rankings compare one source, one metric and one period; there is no composite score anywhere
    def test_rankings_are_single_source_with_competition_ranks(self):
        self.writer.food('FOOD_C', '탕후루', [], 'DISH')
        self.rss_days(13, 0, lambda day: [{'keyword': '두쫀쿠 %d' % k} for k in range(3)] + [{'keyword': '마라탕'}, {'keyword': '탕후루'}])
        for n in range(6, -1, -1):
            self.writer.instagram_run(kst(back(n), 10), 'Q_B', ['FOOD_B'], [{'id': '86%02d' % n, 'tags': ['마라탕']}])
        rss = self.analytics().rankings('google_trends_rss', 7, END)
        self.assertEqual([(e['rank'], e['food']['food_id'], e['current']['value']) for e in rss['items']],
                         [(1, 'FOOD_A', 21), (2, 'FOOD_B', 7), (2, 'FOOD_C', 7)])
        self.assertEqual(rss['basis']['scope'], 'SINGLE_SOURCE')
        self.assertEqual(rss['items'][0]['change'], {'state': 'COMPARABLE', 'reason': None, 'absolute': 0, 'ratio': 0.0})
        instagram = self.analytics().rankings('instagram_graph', 7, END)
        self.assertEqual([e['food']['food_id'] for e in instagram['items']], ['FOOD_B'])
        self.assertEqual(instagram['excluded'], {'insufficient_coverage': 0, 'not_observed': 2, 'zero_value': 0})
        text = json.dumps([rss, instagram, self.analytics().metrics('FOOD_A', 7, END)])
        self.assertFalse(any(word in text for word in ('"score"', '"total"', '"composite', '"overall')))
        self.assertEqual({e['food']['food_id'] for e in self.analytics().rankings('google_trends_rss', 7, END, kind='PRODUCT')['items']}, {'FOOD_A'})

    def test_growth_needs_two_complete_periods(self):
        self.rss_days(13, 0, lambda day: [{'keyword': '두쫀쿠 %d' % k} for k in range(2 if day <= back(7) else 3)] +
                      ([{'keyword': '마라탕'}] if day > back(7) else []))
        metrics = self.analytics().metrics('FOOD_A', 7, END)
        self.assertEqual(self.metric(metrics, 'google_trends_rss')['change'], {'state': 'COMPARABLE', 'reason': None, 'absolute': 7, 'ratio': 0.5})
        new = self.metric(self.analytics().metrics('FOOD_B', 7, END), 'google_trends_rss')['change']
        self.assertEqual(new, {'state': 'COMPARABLE', 'reason': 'PREVIOUS_ZERO', 'absolute': 7, 'ratio': None})
        earlier = self.metric(self.analytics().metrics('FOOD_A', 7, back(7)), 'google_trends_rss')['change']
        self.assertEqual(earlier['reason'], 'PREVIOUS_PERIOD_INSUFFICIENT')  # the week before was never collected: not a zero


class IdentityAndSafety(AnalyticsCase):
    # 3, 4: search over approved names, stable FOOD_ID, merge redirect keeps the merged food's observations
    def test_search_detail_and_merge_redirect(self):
        self.writer.food('FOOD_OLD', '두바이 쿠키', ['두바이쿠키'], 'PRODUCT')
        self.rss_days(6, 0, lambda day: [{'keyword': '두바이쿠키'}, {'keyword': '두쫀쿠'}])
        self.writer.merge('FOOD_OLD', 'FOOD_A', at=NOW - timedelta(hours=2))
        analytics = self.analytics()
        self.assertEqual([f['food_id'] for f in analytics.foods()], ['FOOD_A', 'FOOD_B'])  # merged foods are not listed
        found = analytics.foods(q='두바이쿠키', sort='relevance')
        self.assertEqual([(f['food_id'], f['match']) for f in found], [('FOOD_A', {'field': 'alias', 'text': '두바이쿠키', 'kind': 'exact'})])
        self.assertEqual(analytics.foods(q=' 두바이  쫀득 ')[0]['match'], {'field': 'alias', 'text': '두바이 쫀득 쿠키', 'kind': 'prefix'})
        self.assertEqual(analytics.foods(q='쿠키')[0]['match']['kind'], 'contains')
        self.assertEqual(analytics.foods(q='없는음식'), [])
        detail = analytics.food('FOOD_OLD')
        self.assertEqual((detail['food_id'], detail['redirect'], detail['merged_food_ids']),
                         ('FOOD_A', {'requested_food_id': 'FOOD_OLD', 'reason': 'MERGED'}, ['FOOD_OLD']))
        self.assertIn('두바이쿠키', [a['text'] for a in detail['aliases']])
        self.assertEqual(self.values(analytics.timeline('FOOD_OLD', 7, END), 'google_trends_rss'), [(2, 'OBSERVED')] * 7)
        with self.assertRaises(FoodNotFound):
            analytics.food('FOOD_NOPE')
        ordered = analytics.foods(sort='-last_observed_at')
        self.assertEqual([f['food_id'] for f in ordered], ['FOOD_A', 'FOOD_B'])
        self.assertIsNone(ordered[1]['last_observed_at'])

    def test_pending_entity_change_is_flagged_until_reprocessed(self):
        foods = FoodService(self.storage)
        candidate = foods.command('submit', dict(request_key=uuid.uuid4().hex, name='버터떡', aliases=[]))
        food = foods.command('review', dict(request_key=uuid.uuid4().hex, candidate_id=candidate['candidate_id'], expected_revision=candidate['revision'],
                                            action='create', approved=True, actor_ref='reviewer', reason='B8 검증'))['resolution']
        self.rss_days(2, 0, lambda day: [{'keyword': '버터떡'}])
        self.assertTrue(self.analytics().food(food['food_id'])['freshness']['reprocessing_pending'])
        FoodObservationService(self.storage, lambda: self.now).apply_entity_changes()
        detail = self.analytics().food(food['food_id'])
        self.assertFalse(detail['freshness']['reprocessing_pending'])
        self.assertEqual(self.analytics().sources_status(7)['aggregation']['pending_entity_changes'], 0)

    # 14: every read model leaves the database byte-for-byte unchanged (no consumed invalidations either)
    def test_reads_do_not_change_the_database(self):
        self.rss_days(9, 0, lambda day: [{'keyword': '두쫀쿠'}], failed=(back(4),))
        for n in range(9, -1, -1):
            self.writer.instagram_run(kst(back(n), 10), 'Q_A1', ['FOOD_A'], [{'id': '97%02d' % n, 'tags': ['두쫀쿠']}])
        before, raw = snapshot(self.storage.path), Path(self.storage.path).read_bytes()
        analytics = self.analytics()
        for days in (7, 30, 90):
            analytics.timeline('FOOD_A', days, END)
            analytics.timeline('FOOD_A', days, END, granularity='week')
            analytics.metrics('FOOD_A', days, END)
            analytics.rankings('instagram_graph', days, END, include_zero=True)
            analytics.sources_status(days)
        analytics.foods(q='쿠키', sort='relevance')
        analytics.food('FOOD_A')
        self.assertEqual(snapshot(self.storage.path), before)
        self.assertEqual(Path(self.storage.path).read_bytes(), raw)

    # DBMS independence: the same SQL over the B6 target schema (SQLite stand-in after B7 migration) gives the same answers
    def test_target_schema_store_matches_sqlite_store(self):
        self.rss_days(13, 0, lambda day: [{'keyword': '두쫀쿠', 'traffic': '1,000+'}, {'keyword': '아이돌', 'news': ['두바이 쫀득 쿠키 화제']}],
                      failed=(back(9),))
        for n in range(13, -1, -1):
            self.writer.instagram_run(kst(back(n), 10), 'Q_A1', ['FOOD_A'], [{'id': '98%02d' % n, 'tags': ['두쫀쿠']}],
                                      coverage='PARTIAL' if n == 5 else 'COMPLETE')
            self.writer.instagram_run(kst(back(n), 11), 'Q_B', ['FOOD_B'], [] if n % 3 else [{'id': '99%02d' % n, 'tags': ['마라탕']}])
        self.writer.legacy_instagram(NOW - timedelta(hours=1), [{'id': '9900', 'tags': ['마라탕'], 'seen_at': kst(back(2), 9)}])
        self.writer.purge(NOW)
        repo = TargetRepository(SQLiteTarget(Path(self.temp.name) / 'target.sqlite3'), clock=lambda: NOW)
        repo.migrate()
        report = migrate(self.storage.path, repo, now=NOW)
        self.assertFalse([t for t, v in report['tables'].items() if v['dispositions']])
        sqlite, target = self.analytics(), FoodAnalytics(TargetAnalyticsStore(repo), clock=lambda: self.now, origin='mock')
        for name, args, kw in (('foods', (), {'q': '쿠키'}), ('foods', (), {'sort': '-last_observed_at'}), ('food', ('FOOD_A',), {}),
                               ('timeline', ('FOOD_A', 30, END), {}), ('timeline', ('FOOD_B', 90, END), {'granularity': 'week'}),
                               ('metrics', ('FOOD_A', 7, END), {}), ('metrics', ('FOOD_B', 30, END), {}),
                               ('rankings', ('instagram_graph', 7, END), {'include_zero': True}), ('rankings', ('google_trends_rss', 7, END), {}),
                               ('sources_status', (30,), {})):
            with self.subTest(name=name, args=args):
                self.assertEqual(getattr(target, name)(*args, **kw), getattr(sqlite, name)(*args, **kw))


if __name__ == '__main__':
    unittest.main()
