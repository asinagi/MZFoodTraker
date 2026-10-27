"""Explicit, atomic, versioned SQLite migrations. No service constructors run DDL."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from .migration_definitions import BASE_SQL, LEGACY_ADDITIONS

LATEST_VERSION = 2


class MigrationRequired(RuntimeError):
    pass


def _add_columns(db, table, columns):
    existing = {r[1] for r in db.execute(f'PRAGMA table_info({table})')}
    for name, declaration in columns.items():
        if name not in existing:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')


def _legacy_schema(db):
    for statement in BASE_SQL.split(';'):
        if statement.strip():
            db.execute(statement)
    for table, columns in LEGACY_ADDITIONS.items():
        _add_columns(db, table, columns)


def _contracts(db):
    for table in ('runs', 'instagram_runs', 'instagram_media', 'naver_calls'):
        _add_columns(db, table, {
            'origin': "TEXT NOT NULL DEFAULT 'legacy_unknown'",
            'contract_json': 'TEXT CHECK(contract_json IS NULL OR json_valid(contract_json))',
        })
    for table in ('observations', 'instagram_analysis_observations', 'youtube_videos'):
        _add_columns(db, table, {
            'contract_json': 'TEXT CHECK(contract_json IS NULL OR json_valid(contract_json))',
        })


MIGRATIONS = ((1, 'legacy_additive_schema', _legacy_schema),
              (2, 'source_contract_metadata', _contracts))


def schema_version(path):
    path = Path(path).resolve()
    if not path.is_file():
        return 0
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchone():
            return 0
        versions = [r[0] for r in db.execute('SELECT version FROM schema_migrations ORDER BY version')]
        if versions != list(range(1, max(versions, default=0) + 1)):
            raise MigrationRequired('Non-contiguous migration history; restore or inspect the database.')
        return max(versions, default=0)


def require_current(path):
    if schema_version(path) != LATEST_VERSION:
        raise MigrationRequired('Database migration required. Back up first, then run python -m food_trends --migrate.')


def migrate(path):
    """Caller explicitly authorizes DDL. Existing source rows are never reprocessed."""
    path = Path(path)
    version = schema_version(path)
    if version > LATEST_VERSION:
        raise MigrationRequired('Database is newer than this application; refusing downgrade.')
    if version == LATEST_VERSION:
        return {'version': version, 'applied': []}
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)''')
        # Re-read after acquiring the database write lock, including concurrent migrators.
        current = max((r[0] for r in db.execute('SELECT version FROM schema_migrations')), default=0)
        if current > LATEST_VERSION:
            raise MigrationRequired('Database is newer than this application.')
        applied = []
        for number, name, change in MIGRATIONS:
            if number > current:
                change(db)
                db.execute('INSERT INTO schema_migrations VALUES (?,?,?)',
                           (number, name, datetime.now(timezone.utc).isoformat()))
                applied.append(number)
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise MigrationRequired('Foreign key validation failed; migration rolled back.')
        db.commit()
        return {'version': LATEST_VERSION, 'applied': applied}
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
