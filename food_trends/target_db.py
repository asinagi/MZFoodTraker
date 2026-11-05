"""B6 adapters for the target schema: Oracle (python-oracledb Thin mode) and a SQLite stand-in.

Conversion and validation are dialect independent, so the stand-in refuses what Oracle would
refuse. Timestamps are stored as UTC instants (TSTZ in Oracle), JSON canonically, '' only in
columns declared empty_as_null. Credentials come from the environment and are never logged,
echoed or placed in error text. The driver is imported lazily; without it Oracle is unavailable,
never silently replaced by SQLite.
"""
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from .target_schema import BY_NAME, KINDS

TEXT_KINDS = ('id', 'key', 'code', 'text', 'hash', 'clob')
TRANSIENT_ORACLE = ('DPY-1001', 'DPY-4011', 'DPY-4024', 'DPY-6005', 'ORA-03113', 'ORA-03114', 'ORA-03135', 'ORA-12170', 'ORA-12541')


class TargetError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class UniqueViolation(TargetError):
    pass


class IntegrityViolation(TargetError):
    pass


class TransientError(TargetError):
    pass


class ConversionError(TargetError, ValueError):
    pass


class PolicyRefused(TargetError):
    pass


class OracleUnavailable(TargetError):
    pass


def canonical_json(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ConversionError('invalid_json') from None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except ValueError:
        raise ConversionError('invalid_json') from None


def utc(value):
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise ConversionError('invalid_timestamp') from None
    else:
        raise ConversionError('invalid_timestamp')
    if moment.tzinfo is None:
        raise ConversionError('naive_timestamp')
    return moment.astimezone(timezone.utc)


def iso(moment):
    return utc(moment).isoformat(timespec='microseconds')


def convert(c, value, dialect):
    """One logical value -> bind value. Raises ConversionError(code) only."""
    if value == '':
        if c.empty_as_null:
            return None if dialect == 'oracle' else ''
        if c.kind in TEXT_KINDS or c.kind in ('json', 'ts'):
            raise ConversionError('empty_string_not_representable:' + c.name)
    if value is None:
        if not c.null and not c.empty_as_null:
            raise ConversionError('required_missing:' + c.name)
        return None
    if c.kind == 'ts':
        return utc(value) if dialect == 'oracle' else iso(value)
    if c.kind == 'json':
        return canonical_json(value)
    if c.kind == 'int':
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConversionError('not_integer:' + c.name)
        if abs(value) >= 10 ** 19:
            raise ConversionError('integer_out_of_range:' + c.name)
        return value
    if c.kind == 'real':
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ConversionError('not_finite_number:' + c.name)
        return float(value)
    if not isinstance(value, str):
        raise ConversionError('not_text:' + c.name)
    limit = KINDS[c.kind][2]
    if limit and len(value) > limit:
        raise ConversionError('length_exceeds_target:' + c.name)
    return value


def to_target(table, row, dialect):
    """Logical source row -> bind values for one dialect."""
    return {'b_' + c.name: convert(c, row.get(c.name), dialect) for c in BY_NAME[table].columns}


def logical(table, row):
    """Target or source row -> comparable logical values (UTC ISO instants, canonical JSON, '' restored)."""
    spec, out = BY_NAME[table], {}
    for c in spec.columns:
        value = row.get(c.name)
        if hasattr(value, 'read'):  # Oracle LOB
            value = value.read()
        if value is None and c.empty_as_null:
            value = ''
        elif value is not None and c.kind == 'ts':
            value = iso(value if isinstance(value, datetime) and value.tzinfo else
                        value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) else value)
        elif value is not None and c.kind == 'json':
            value = canonical_json(value)
        elif value is not None and c.kind == 'real':
            value = float(value)
        elif value is not None and c.kind == 'int':
            value = int(value)
        out[c.name] = value
    return out


@dataclass(frozen=True)
class OracleSettings:
    dsn: str
    user: str
    password: str = field(repr=False)
    environment: str = 'development'
    pool_min: int = 1
    pool_max: int = 4
    call_timeout_ms: int = 30000

    @classmethod
    def from_env(cls, environ=None, prefix='ORACLE_'):
        env = os.environ if environ is None else environ
        dsn, user, password = (env.get(prefix + k, '').strip() for k in ('DSN', 'USER', 'PASSWORD'))
        if not (dsn and user and password):
            raise OracleUnavailable('oracle_not_configured')
        environment = env.get(prefix + 'ENVIRONMENT', 'development').strip().lower()
        if environment not in ('development', 'test', 'production'):
            raise OracleUnavailable('oracle_environment_invalid')
        # A production target is opened only with an explicit operator switch (G-OPS-6).
        if environment == 'production' and env.get('ORACLE_ALLOW_PRODUCTION', 'false').lower() != 'true':
            raise OracleUnavailable('oracle_production_not_approved')
        return cls(dsn, user, password, environment, int(env.get(prefix + 'POOL_MIN', '1')),
                   int(env.get(prefix + 'POOL_MAX', '4')), int(env.get(prefix + 'CALL_TIMEOUT_MS', '30000')))


class SQLiteTarget:
    """Stand-in target on SQLite for development and tests; same DDL spec, conversion and policy."""
    dialect = 'sqlite'

    def __init__(self, path):
        self.path = str(path)
        self.connections = 0

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        self.connections += 1
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield _Cursor(conn, self.dialect)
                conn.execute('COMMIT')
            except BaseException:
                if conn.in_transaction:
                    conn.execute('ROLLBACK')
                raise
        except sqlite3.Error as exc:
            raise self.map_error(exc) from None
        finally:
            conn.close()
            self.connections -= 1

    @staticmethod
    def map_error(exc):
        text = str(exc)
        if isinstance(exc, sqlite3.IntegrityError):
            if 'UNIQUE' in text or 'PRIMARY KEY' in text:
                return UniqueViolation('unique_violation')
            if 'FOREIGN KEY' in text:
                return IntegrityViolation('foreign_key_violation')
            if 'NOT NULL' in text:
                return IntegrityViolation('not_null_violation')
            return IntegrityViolation('check_violation')
        if 'locked' in text or 'busy' in text:
            return TransientError('database_busy')
        return TargetError('database_error')

    def size_bytes(self):
        return os.path.getsize(self.path)


class OracleTarget:
    """python-oracledb Thin mode with a connection pool; one transaction per context."""
    dialect = 'oracle'

    def __init__(self, settings):
        self.settings = settings
        self._pool = None

    def _driver(self):
        try:
            import oracledb
        except ImportError:
            raise OracleUnavailable('python_oracledb_not_installed') from None
        return oracledb

    def pool(self):
        if self._pool is None:
            oracledb = self._driver()
            s = self.settings
            self._pool = oracledb.create_pool(user=s.user, password=s.password, dsn=s.dsn, min=s.pool_min,
                                              max=s.pool_max, increment=1)
        return self._pool

    @contextmanager
    def transaction(self):
        oracledb = self._driver()
        try:
            conn = self.pool().acquire()
        except oracledb.Error as exc:  # unreachable database or refused login: a TargetError code like any driver error
            raise self.map_error(exc) from None
        try:
            conn.call_timeout = self.settings.call_timeout_ms
            try:
                yield _Cursor(conn, self.dialect)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        except oracledb.Error as exc:
            raise self.map_error(exc) from None
        finally:
            self.pool().release(conn)

    @staticmethod
    def map_error(exc):
        error = exc.args[0] if exc.args else None
        code = getattr(error, 'full_code', '') or ''
        message = getattr(error, 'message', '') or ''
        if code == 'ORA-00001':
            return UniqueViolation('unique_violation')
        if code in ('ORA-02291', 'ORA-02292') or (code == 'ORA-02091' and 'ORA-02291' in message):
            return IntegrityViolation('foreign_key_violation')
        if code == 'ORA-01400':
            return IntegrityViolation('not_null_violation')
        if code in ('ORA-02290', 'ORA-02091', 'ORA-40441'):
            return IntegrityViolation('check_violation')
        if code in TRANSIENT_ORACLE:
            return TransientError('connection_or_timeout')
        return TargetError('database_error')

    def close(self):
        if self._pool is not None:
            self._pool.close()
            self._pool = None


class _Cursor:
    """Uniform execute/fetch with named binds and dict rows; TSTZ input sizes on Oracle."""

    def __init__(self, conn, dialect):
        self.conn, self.dialect = conn, dialect

    def execute(self, sql, binds=None, ts_binds=()):
        if self.dialect == 'sqlite':
            return self.conn.execute(sql, binds or {})
        cursor = self.conn.cursor()
        if ts_binds:
            import oracledb
            cursor.setinputsizes(**{name: oracledb.DB_TYPE_TIMESTAMP_TZ for name in ts_binds})
        cursor.execute(sql, binds or {})
        return cursor

    def rows(self, sql, binds=None):
        cursor = self.execute(sql, binds)
        if self.dialect == 'sqlite':
            return [dict(r) for r in cursor.fetchall()]
        names = [d[0].lower() for d in cursor.description]
        return [dict(zip(names, r)) for r in cursor.fetchall()]

    def scalar(self, sql, binds=None):
        rows = self.rows(sql, binds)
        return next(iter(rows[0].values())) if rows else None
