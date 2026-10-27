"""Validate explicit migration on a disposable copy, never the source database."""
import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def digest_columns(db, table, columns):
    names = ','.join('"'+c.replace('"', '""')+'"' for c in columns)
    digest = hashlib.sha256()
    rows = db.execute(f'SELECT {names} FROM "{table}" ORDER BY rowid').fetchall()
    for row in rows:
        digest.update(repr(tuple(row)).encode('utf-8'))
    return {'rows': len(rows), 'sha256': digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', help='An existing read-only preservation snapshot')
    parser.add_argument('--output', default='reports/development-a1-a3-2026-10-27/copy-migration.json')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from food_trends.migrations import migrate
    from food_trends.storage import Storage
    source_path = Path(args.source).resolve()
    with tempfile.TemporaryDirectory() as directory:
        copy_path = Path(directory) / 'migration-copy.sqlite3'
        with closing(sqlite3.connect(source_path.as_uri()+'?mode=ro', uri=True)) as source:
            source.execute('PRAGMA query_only=ON')
            with closing(sqlite3.connect(copy_path)) as destination:
                source.backup(destination)
        with closing(sqlite3.connect(copy_path)) as db:
            columns = {r[0]: [c[1] for c in db.execute(f'PRAGMA table_info("{r[0]}")')]
                       for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            before = {t: digest_columns(db, t, names) for t, names in columns.items()}
        migration = migrate(copy_path)
        with closing(sqlite3.connect(copy_path)) as db:
            after = {t: digest_columns(db, t, names) for t, names in columns.items()}
            integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
            fk_errors = len(db.execute('PRAGMA foreign_key_check').fetchall())
            legacy = {t: db.execute(f"SELECT count(*) FROM {t} WHERE origin!='legacy_unknown'").fetchone()[0]
                      for t in ('runs', 'instagram_runs', 'instagram_media', 'naver_calls')}
            state_before = list(db.iterdump())
        # Read-only construction, source status and export on real saved rows.
        from food_trends.config import InstagramConfig, NaverConfig, YouTubeConfig
        from food_trends.instagram_service import InstagramService
        from food_trends.naver_service import NaverService
        from food_trends.youtube_service import YouTubeService
        storage = Storage(copy_path)
        storage.status(); storage.trends(); storage.export()
        InstagramService(storage, InstagramConfig()).status()
        NaverService(storage, NaverConfig()).status()
        YouTubeService(storage, YouTubeConfig()).status()
        with closing(sqlite3.connect(copy_path)) as db:
            read_unchanged = state_before == list(db.iterdump())
        second = migrate(copy_path)
        result = {'source': str(source_path.relative_to(root)), 'migration': migration,
                  'original_tables_compared': len(before), 'original_columns_and_rows_preserved': before == after,
                  'integrity': integrity, 'foreign_key_errors': fk_errors,
                  'legacy_rows_promoted': legacy, 'constructors_and_reads_unchanged': read_unchanged,
                  'second_migration_applied': second['applied'], 'source_database_written': False}
    target = root / args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'source'}))
    return 0 if before == after and read_unchanged and integrity == 'ok' and not fk_errors and not any(legacy.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
