"""B4-B5 acceptance on a disposable DB: user food -> approval -> targets -> planner -> B3 Mock execution
(with an injected worker crash) -> B4 ingestion/DQ -> B5 matches/mentions/metrics -> FOOD_ID view.
External sockets raise; the production database is never opened.
"""
import json
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from food_trends.storage import Storage
from food_trends.food_service import FoodService
from food_trends.tracking_service import TrackingService
from food_trends.tracking_access import mock_accounts
from food_trends.query_planner import QueryPlanner
from food_trends.execution_mock import mock_controller, MockTransport
from food_trends.food_observations import FoodObservationService
from food_trends.ingestion import IngestionService
from food_trends.service import CollectionService

LINKED = ('ingest_runs', 'source_items', 'item_snapshots', 'run_items', 'quality_results',
          'item_query_matches', 'food_mentions', 'metric_observations')


class Crash(BaseException):
    pass


def rss(keyword, news):
    return ('<rss xmlns:ht="https://trends.google.com/trending/rss"><channel><item><title>%s</title>'
            '<pubDate>Tue, 27 Oct 2026 10:00:00 +0900</pubDate><ht:approx_traffic>1000+</ht:approx_traffic>'
            '<ht:news_item><ht:news_item_title>%s</ht:news_item_title></ht:news_item></item></channel></rss>' % (keyword, news)).encode()


def counts(storage):
    with storage.reader() as db:
        return {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in LINKED}


def main():
    with tempfile.TemporaryDirectory() as directory, patch.object(socket.socket, 'connect', side_effect=AssertionError('External HTTP forbidden')):
        storage = Storage.create(Path(directory) / 'b45.sqlite3'); foods = FoodService(storage)
        candidate = foods.command('submit', dict(request_key='b45-input', name='두쫀쿠', aliases=['두바이 쫀득 쿠키', '두바이쫀득쿠키']))
        food = foods.command('review', dict(request_key='b45-approval', candidate_id=candidate['candidate_id'], expected_revision=candidate['revision'],
            action='create', canonical_name='두바이 쫀득 쿠키', approved=True, actor_ref='mock-reviewer', reason='B4-B5 격리 검증'))['resolution']
        tracking = TrackingService(storage, mock_accounts()); planner = QueryPlanner(storage, tracking)
        for source in ('instagram_graph', 'youtube', 'naver_datalab'):
            target = tracking.command('register', dict(request_key=source + '-register', food_id=food['food_id'], source_id=source,
                                                       actor_ref='mock-reviewer', reason='격리 계획 등록'))
            tracking.command('activate', dict(request_key=source + '-activate', target_id=target['target_id'], expected_revision=target['revision'],
                                              approved=True, actor_ref='mock-reviewer', reason='격리 계획 승인'))
        rss_result = CollectionService(storage, fetcher=lambda _: (rss('두쫀쿠', '두바이 쫀득 쿠키와 두쫀쿠 품절 행렬'), 200)).collect()
        transport = MockTransport(); controller = mock_controller(storage, planner, transport)
        jobs = controller.execution.schedule()
        # Worker dies right after the first committed page; the lease expires and the job resumes.
        crashed = {'done': False}
        def crash(stage):
            if stage == 'after_page_commit' and not crashed['done']:
                crashed['done'] = True; raise Crash()
        controller.worker.fault = crash
        try:
            controller.run_once(jobs[0]['job_id'])
        except Crash:
            pass
        controller.worker.fault = lambda stage: None
        clock = tracking.clock
        from datetime import datetime, timedelta, timezone
        later = datetime.now(timezone.utc) + timedelta(seconds=180)
        tracking.clock = lambda: later
        completed = [controller.run_once(j['job_id']) for j in jobs]
        tracking.clock = clock
        http = len(transport.calls)
        before = counts(storage)
        # Re-delivery and re-processing: no HTTP, no new rows.
        for job in jobs:
            assert controller.run_once(job['job_id']) is None
        IngestionService(storage).ingest_pending(); FoodObservationService(storage).link_pending()
        after = counts(storage)
        assert len(transport.calls) == http and after == before, (http, before, after)
        view = FoodObservationService(storage).food_view(food['food_id'])
        with storage.reader() as db:
            dump = '\n'.join(db.iterdump())
            quality = dict(db.execute('SELECT outcome,count(*) FROM quality_results GROUP BY outcome').fetchall())
            youtube_mentions = db.execute("SELECT count(*) FROM food_mentions m JOIN source_items s USING(item_key) WHERE s.source_code='youtube'").fetchone()[0]
            stored_blocked = db.execute("SELECT count(*) FROM metric_observations WHERE source_code IN ('naver_datalab','youtube')").fetchone()[0]
            coverage = dict(db.execute('SELECT source_code,group_concat(DISTINCT coverage) FROM ingest_runs GROUP BY source_code').fetchall())
            assert db.execute('PRAGMA foreign_key_check').fetchall() == [] and db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        sources = view['sources']
        assert all(c['status'] == 'SUCCEEDED' for c in completed if c), [c and c['status'] for c in completed]
        assert crashed['done'] and sources['youtube']['confirmed_items'] == 0 and youtube_mentions == 0 and stored_blocked == 0
        assert sources['youtube']['search_matches'] == 3 and sources['youtube']['search_matched_items'] == 1
        assert sources['instagram_graph']['confirmed_items'] == 1 and sources['google_trends_rss']['confirmed_items'] == 1
        assert '"ratio"' not in dump and storage.status()['food_count'] in (0, 1)
        result = {'successful': True, 'food_id': food['food_id'], 'jobs': len(jobs), 'mock_http_attempts': http, 'real_http_attempts': 0,
                  'worker_crash_injected': True, 'duplicate_http_after_redelivery': 0, 'row_changes_after_reprocessing': 0,
                  'rss_collection': {'success': rss_result['success'], 'ingested': rss_result['ingestion']['created']},
                  'staging_and_link_counts': after, 'quality_outcomes': quality, 'coverage_by_source': coverage,
                  'youtube_mentions': youtube_mentions, 'stored_naver_or_youtube_metrics': stored_blocked,
                  'food_view': {s: {k: v for k, v in d.items() if k != 'metrics'} | {'metric_units': sorted({m['unit'] for m in d['metrics']})}
                                for s, d in sources.items()},
                  'live_enabled': False, 'production_database_used': False}
    output = ROOT / 'reports/development-b4-b5-2026-10-28'; output.mkdir(parents=True, exist_ok=True)
    (output / 'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('successful', 'jobs', 'mock_http_attempts', 'real_http_attempts',
                                             'duplicate_http_after_redelivery', 'row_changes_after_reprocessing')}))


if __name__ == '__main__':
    main()
