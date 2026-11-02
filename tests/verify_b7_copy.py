"""B7 on a disposable copy of a preserved snapshot: v0->v10, B4/B5 backfill, migrate to the target, reconcile.

The snapshot is opened read-only. A single test-only food ('두쫀쿠', clearly labelled) is approved inside
the temporary copy so real mentions exist to migrate. The target is the SQLite stand-in unless
--oracle is given (needs ORACLE_TEST_* and an isolated schema). Only counts, codes, hashes and timings
are written to the report; no provider content leaves the temporary directory.
"""
import argparse
import json
import sqlite3
import sys
import tempfile
import time
import tracemalloc
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from food_trends.food_observations import FoodObservationService
    from food_trends.food_service import FoodService
    from food_trends.ingestion import IngestionService
    from food_trends.migrations import migrate as migrate_sqlite
    from food_trends.storage import Storage
    from food_trends.target_db import SQLiteTarget
    from food_trends.target_migration import fingerprint, migrate, reconcile
    from food_trends.target_repository import TargetRepository
    from food_trends.youtube_storage import YouTubeStore
    source = Path(args.source).resolve()
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / 'source.sqlite3'
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(copy)) as dst:
            src.execute('PRAGMA query_only=ON'); src.backup(dst)
        sqlite_migration = migrate_sqlite(copy)
        storage = Storage(copy)
        ingestion, links = IngestionService(storage), FoodObservationService(storage)
        ingestion.ingest_pending(limit=100000)
        for s in ('youtube', 'instagram_graph'):
            ingestion.legacy_backfill(s)
        links.link_pending(limit=100000)
        foods = FoodService(storage)
        candidate = foods.command('submit', dict(request_key='b7-test-food', name='두쫀쿠', aliases=['두바이 쫀득 쿠키', '두바이쫀득쿠키']))
        foods.command('review', dict(request_key='b7-test-approval', candidate_id=candidate['candidate_id'], expected_revision=candidate['revision'],
                                     action='create', approved=True, actor_ref='b7-verification-copy-only', reason='B7 격리 복제본 검증용 승인'))
        links.apply_entity_changes()
        YouTubeStore(storage).purge(); ingestion.purge(); links.purge()  # the source's own retention policy first
        with closing(sqlite3.connect(copy)) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            source_counts = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in
                             ('items', 'youtube_videos', 'instagram_media', 'source_items', 'item_query_matches', 'food_mentions', 'metric_observations', 'food_events')}
        target_path = Path(directory) / 'target.sqlite3'
        repo = TargetRepository(SQLiteTarget(target_path), clock=lambda: now)
        schema = repo.migrate()
        tracemalloc.start()
        started = time.perf_counter()
        first = migrate(copy, repo, now=now)
        load_seconds = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        started = time.perf_counter()
        result = reconcile(copy, repo, now=now, source_storage=Storage(copy))
        reconcile_seconds = time.perf_counter() - started
        counts_before = {t: repo.count(t) for t in first['tables']}
        with repo.db.transaction() as tx:
            tx.execute('DELETE FROM migration_batches')
        second = migrate(copy, repo, now=now)
        rerun_writes = sum(v['writes']['inserted'] + v['writes']['updated'] for v in second['tables'].values())
        counts_after = {t: repo.count(t) for t in first['tables']}
        # Micro-measurements on the stand-in (development hardware; not an Oracle or production figure).
        food_id = repo.rows('food_entities')[0]['food_id']
        timings = {}
        def measure(name, fn, n=50):
            started = time.perf_counter()
            for _ in range(n):
                fn()
            timings[name + '_ms'] = round((time.perf_counter() - started) * 1000 / n, 3)
        measure('food_view', lambda: repo.food_view(food_id))
        measure('alias_lookup', lambda: repo.resolve_alias('두쫀쿠', 'food:ko'))
        def single_upsert():
            with repo.db.transaction() as tx:
                repo.upsert(tx, 'food_commands', dict(request_key='perf-' + uuid.uuid4().hex, operation='perf', payload_hash='0' * 64,
                                                      result_json='{}', created_at=now.isoformat()))
        measure('single_upsert_transaction', single_upsert, 100)
        loaded = sum(v['loaded_rows'] for v in first['tables'].values())
        report = {
            'source': str(source.relative_to(root)), 'source_sha256': fingerprint(source), 'verified_at': now.isoformat(),
            'target': 'sqlite_stand_in', 'oracle_executed': False, 'sqlite_migration': sqlite_migration, 'target_schema': schema,
            'test_only_food_approved_in_copy': True, 'source_counts_after_backfill': source_counts,
            'tables': {t: {k: v[k] for k in ('candidates', 'loaded_rows', 'excluded_rows', 'rejected_rows', 'dispositions')}
                       for t, v in first['tables'].items()},
            'reconciliation': {'consistent': result['consistent'], 'summary': result['summary'], 'checks': result['checks'],
                               'hashes': {t: v['source_hash'] == v['target_hash'] for t, v in result['tables'].items()}},
            'rerun': {'writes': rerun_writes, 'counts_unchanged': counts_before == counts_after},
            'performance_dev_only': dict(timings, loaded_rows=loaded, load_seconds=round(load_seconds, 3),
                                         rows_per_second=round(loaded / load_seconds, 1), reconcile_seconds=round(reconcile_seconds, 3),
                                         python_peak_memory_mb=round(peak / 2 ** 20, 2), target_bytes=target_path.stat().st_size,
                                         source_bytes=copy.stat().st_size, connections_open_after=repo.db.connections),
            'http_requests': 0, 'source_database_written': False}
    target = root / args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('reconciliation', 'rerun')}, ensure_ascii=False)[:1500])
    return 0 if result['consistent'] and rerun_writes == 0 and counts_before == counts_after else 1


if __name__ == '__main__':
    raise SystemExit(main())
