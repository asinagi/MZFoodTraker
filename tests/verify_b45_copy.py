"""B4-B5 on a disposable copy of a preserved snapshot: migrate, stage existing rows, link, repeat.

The source snapshot is opened read-only and never written. No API call is made. Only counts,
outcome tallies and hashes are reported; no provider content leaves the temporary copy.
"""
import argparse
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root)); sys.path.insert(0, str(root / 'tests'))
    from verify_preserved_copy import digest_columns
    from food_trends.migrations import migrate
    from food_trends.storage import Storage
    from food_trends.ingestion import IngestionService
    from food_trends.food_observations import FoodObservationService
    source = Path(args.source).resolve()
    tables = ('ingest_runs', 'source_items', 'item_snapshots', 'run_items', 'quality_results',
              'item_query_matches', 'food_mentions', 'metric_observations')
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / 'b45-copy.sqlite3'
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src, closing(sqlite3.connect(copy)) as dst:
            src.execute('PRAGMA query_only=ON'); src.backup(dst)
        with closing(sqlite3.connect(copy)) as db:
            original = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            columns = {t: [c[1] for c in db.execute(f'PRAGMA table_info("{t}")')] for t in original}
        migration = migrate(copy)
        with closing(sqlite3.connect(copy)) as db:
            before = {t: digest_columns(db, t, columns[t]) for t in original}
        storage = Storage(copy); ingestion = IngestionService(storage); links = FoodObservationService(storage)

        def run():
            first = ingestion.ingest_pending(limit=100000)
            legacy = [ingestion.legacy_backfill(s) for s in ('youtube', 'instagram_graph')]
            linked = links.link_pending(limit=100000)
            return first, legacy, linked
        first, legacy, linked = run()
        with storage.reader() as db:
            counts = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in tables}
            quality = {f'{r[0]}:{r[1]}': r[2] for r in db.execute('SELECT rule_code,outcome,count(*) FROM quality_results GROUP BY 1,2 ORDER BY 1,2')}
            origins = dict(db.execute('SELECT first_origin,count(*) FROM source_items GROUP BY 1').fetchall())
            coverage = {f'{r[0]}:{r[1]}': r[2] for r in db.execute('SELECT source_code,coverage,count(*) FROM ingest_runs GROUP BY 1,2')}
            sources = dict(db.execute('SELECT source_code,count(*) FROM source_items GROUP BY 1').fetchall())
            blocked = db.execute("SELECT count(*) FROM food_mentions m JOIN source_items s USING(item_key) WHERE s.source_code='youtube'").fetchone()[0]
        second = run()
        with storage.reader() as db:
            again = {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in tables}
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
            fk = len(db.execute('PRAGMA foreign_key_check').fetchall())
        with closing(sqlite3.connect(copy)) as db:
            after = {t: digest_columns(db, t, columns[t]) for t in original}
        result = {'source': str(source.relative_to(root)), 'migration': migration, 'original_tables': len(original),
                  'original_tables_unchanged_by_ingestion': before == after, 'ingested_runs_first_pass': first['ingested'],
                  'legacy_logical_runs': [l['ingest_run_id'] for l in legacy], 'linked_runs': linked['linked_runs'],
                  'counts': counts, 'counts_after_second_pass': again, 'idempotent': counts == again and second[0]['ingested'] == 0,
                  'source_items_by_source': sources, 'source_items_by_first_origin': origins, 'runs_by_source_coverage': coverage,
                  'quality_by_rule_outcome': quality, 'youtube_mentions': blocked, 'integrity': integrity, 'foreign_key_errors': fk,
                  'http_requests': 0, 'source_database_written': False}
    target = root / args.output; target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('migration', 'original_tables_unchanged_by_ingestion', 'idempotent', 'counts', 'integrity', 'foreign_key_errors')}, ensure_ascii=False))
    return 0 if result['idempotent'] and before == after and integrity == 'ok' and not fk and not blocked else 1


if __name__ == '__main__':
    raise SystemExit(main())
