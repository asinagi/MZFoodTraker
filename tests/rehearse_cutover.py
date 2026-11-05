"""Oracle cut-over rehearsal on an isolated copy of the operational SQLite database (ORACLE-CUTOVER-PREP-1 Phase 2).

The operational file is opened only read-only (mode=ro, query_only) and copied with the SQLite backup API into a
temporary directory that is deleted afterwards. The copy is migrated v0 -> v11, staged and purged locally, loaded
into the target (the disposable ORACLE_TEST_* schema, re-verified before reset, or the SQLite stand-in), compared,
and loaded again to prove the re-run changes nothing. No food is approved and no FOOD_ID assigned; non-loopback
sockets are refused (no provider API) and so is any connection to the operational file that is not read-only.
The report holds counts, reason codes, hashes and timings only.
"""
import argparse
import json
import socket
import sqlite3
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

LOOPBACK = ('127.0.0.1', '::1', 'localhost', b'127.0.0.1', None)


@contextmanager
def guarded(production):
    """Refuse provider network access and any write-capable connection to the operational database; yields the refusals."""
    attempts = []
    original_connect, original_resolve, original_db = socket.socket.connect, socket.getaddrinfo, sqlite3.connect

    def allow(host):
        if host not in LOOPBACK:
            attempts.append('external_socket')
            raise RuntimeError('External network disabled for the cut-over rehearsal')

    def connect(sock, address):
        if isinstance(address, tuple):
            allow(address[0])
        return original_connect(sock, address)

    def resolve(host, *args, **kwargs):
        allow(host)
        return original_resolve(host, *args, **kwargs)

    def database(path, *args, **kwargs):
        text, readonly = str(path), False
        if text.startswith('file:'):
            parts = urlsplit(text)
            text, readonly = unquote(parts.path), 'mode=ro' in parts.query.split('&')
            if len(text) > 2 and text[0] == '/' and text[2] == ':':
                text = text[1:]
        if text != ':memory:' and Path(text).resolve() == production and not readonly:
            attempts.append('operational_database_write_connection')
            raise RuntimeError('The operational database may only be opened read-only')
        return original_db(path, *args, **kwargs)
    socket.socket.connect, socket.getaddrinfo, sqlite3.connect = connect, resolve, database
    try:
        yield attempts
    finally:
        socket.socket.connect, socket.getaddrinfo, sqlite3.connect = original_connect, original_resolve, original_db


def oracle_server(repo):
    with repo.db.transaction() as tx:
        return {'version': tx.scalar("SELECT version_full FROM product_component_version WHERE product LIKE 'Oracle%'"),
                'container': tx.scalar("SELECT sys_context('USERENV','CON_NAME') FROM dual"),
                'session_time_zone': tx.scalar('SELECT sessiontimezone FROM dual'),
                'charset': tx.scalar("SELECT value FROM nls_database_parameters WHERE parameter='NLS_CHARACTERSET'")}


def open_target(kind, path, now):
    from food_trends.target_db import SQLiteTarget
    from food_trends.target_repository import TargetRepository
    if kind == 'oracle':
        from oracle_support import oracle_target, reset_target_schema
        db = oracle_target()
        reset_target_schema(db)  # re-verifies the disposable test account and its objects before dropping anything
    else:
        db = SQLiteTarget(path)
    return TargetRepository(db, clock=lambda: now)


def load_and_compare(copy, repo, now, storage):
    from food_trends import cutover
    from food_trends.quota_ledger import import_legacy_usage
    from food_trends.target_migration import migrate, reconcile
    schema = repo.migrate()
    started = time.perf_counter()
    load = migrate(copy, repo, now=now)
    load_seconds = time.perf_counter() - started
    usage = import_legacy_usage(copy, repo, now=now)
    started = time.perf_counter()
    result = reconcile(copy, repo, now=now, source_storage=storage)
    reconcile_seconds = time.perf_counter() - started
    tables = {t: {k: v[k] for k in ('candidates', 'loaded_rows', 'excluded_rows', 'rejected_rows', 'dispositions', 'tombstones', 'settled_by_load')}
              for t, v in load['tables'].items()}
    return {'target_schema': schema, 'tables': tables, 'loaded_rows': sum(v['loaded_rows'] for v in load['tables'].values()),
            'legacy_usage': usage, 'load_seconds': round(load_seconds, 3), 'reconcile_seconds': round(reconcile_seconds, 3),
            'reconciliation': {'consistent': result['consistent'], 'summary': result['summary'], 'checks': result['checks'],
                               'tables': {t: {k: v[k] for k in ('eligible', 'target', 'missing', 'extra', 'changed', 'excluded', 'rejected')}
                                          for t, v in result['tables'].items()},
                               'hash_equal_tables': sum(v['source_hash'] == v['target_hash'] for v in result['tables'].values()),
                               'compared_tables': len(result['tables'])},
            'target_categories': cutover.target_categories(repo), 'keys': cutover.key_checks(repo)}


def rerun(copy, repo, now, storage):
    """A completed batch writes nothing; a second full pass after clearing the batch bookkeeping changes nothing either."""
    from food_trends.quota_ledger import import_legacy_usage
    from food_trends.target_migration import migrate, reconcile
    from food_trends.target_schema import TABLES
    # Ownership leases (R3) record which process wrote, not domain data: every writer adds one.
    counts = {t.name: repo.count(t.name) for t in TABLES if t.name not in ('runtime_ownership', 'runtime_participants')}
    completed = migrate(copy, repo, now=now)
    with repo.db.transaction() as tx:
        tx.execute('DELETE FROM migration_batches')
    full = migrate(copy, repo, now=now)
    usage = import_legacy_usage(copy, repo, now=now)
    after = {t: repo.count(t) for t in counts}
    writes = lambda report: sum(v['writes']['inserted'] + v['writes']['updated'] for v in report['tables'].values())
    return {'completed_batch_writes': writes(completed), 'full_pass_writes': writes(full),
            'full_pass_unchanged_rows': sum(v['writes']['unchanged'] for v in full['tables'].values()),
            'legacy_usage_writes': usage['writes'], 'row_counts_unchanged': counts == after,
            'changed_tables': sorted(t for t in counts if counts[t] != after[t]),
            'consistent_after': reconcile(copy, repo, now=now, source_storage=storage)['consistent']}


def rehearse(source, target, as_of, production, attempts):
    """The whole rehearsal on one source file; returns the report (counts, codes, hashes and timings only)."""
    from food_trends import cutover
    from food_trends.storage import Storage
    source = Path(source).resolve()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    started = time.perf_counter()
    report = {'task': 'ORACLE-CUTOVER-PREP-1 phase 2', 'rehearsed_at': now.isoformat(), 'source': source.name,
              'operational_source': source == production, 'target': target, 'http_requests': 0, 'foods_approved': 0}
    before = cutover.inspect(source)
    report['source_before'] = before
    with tempfile.TemporaryDirectory(prefix='mz-cutover-') as directory:
        copy = Path(directory) / 'copy.sqlite3'
        copied = cutover.backup(source, copy)
        report['copy'] = {'method': 'sqlite3 backup API from a mode=ro, query_only connection', 'integrity': copied['integrity'],
                          'schema_version': copied['schema_version'], 'tables_and_rows_equal_source': copied['tables'] == before['tables']}
        report['prepare'] = cutover.prepare(copy, now)
        report['copy_after_prepare'] = {k: v for k, v in cutover.inspect(copy).items() if k in ('schema_version', 'integrity', 'foreign_key_errors')}
        report['table_dispositions'] = cutover.table_dispositions(copy)
        report['lineage'] = cutover.lineage(copy)
        report['source_categories'] = cutover.source_categories(copy)
        storage = Storage(copy)
        repo = open_target(target, Path(directory) / 'target.sqlite3', now)
        try:
            if target == 'oracle':
                report['oracle_server'] = oracle_server(repo)
            report['load'] = load_and_compare(copy, repo, now, storage)
            started_api = time.perf_counter()
            report['api_parity'] = cutover.api_parity(storage, repo, lambda: now)
            report['api_parity_seconds'] = round(time.perf_counter() - started_api, 3)
            report['rerun'] = rerun(copy, repo, now, storage)
        finally:
            if hasattr(repo.db, 'close'):
                repo.db.close()
        if as_of:
            later = open_target(target, Path(directory) / 'target-as-of.sqlite3', as_of)
            try:
                result = load_and_compare(copy, later, as_of, storage)
                report['as_of'] = {'instant': as_of.isoformat(), 'tables': {t: v for t, v in result['tables'].items() if v['dispositions'] or v['tombstones']},
                                   'loaded_rows': result['loaded_rows'], 'reconciliation': {k: result['reconciliation'][k] for k in ('consistent', 'summary')},
                                   'checks': result['reconciliation']['checks'], 'target_categories': result['target_categories']}
            finally:
                if hasattr(later.db, 'close'):
                    later.db.close()
        report['copy_bytes'] = copy.stat().st_size
        temporary = Path(directory)
    report['temporary_copy_deleted'] = not temporary.exists()
    after = cutover.inspect(source)
    keys = ('sha256', 'bytes', 'modified_at', 'wal_bytes', 'schema_version', 'integrity', 'tables')
    report['source_unchanged'] = {k: before[k] == after[k] for k in keys}
    report['guard_attempts'] = list(attempts)
    report['seconds'] = round(time.perf_counter() - started, 3)
    load, again = report['load'], report['rerun']
    report['passed'] = bool(
        report['copy']['tables_and_rows_equal_source'] and report['prepare']['original_rows_and_columns_preserved']
        and not [t for t, v in report['table_dispositions'].items() if v['disposition'] == 'UNCLASSIFIED']
        and load['reconciliation']['consistent'] and load['keys']['clean']
        and not any(v['mismatches'] for v in report['api_parity'].values())
        and again['completed_batch_writes'] == 0 and again['full_pass_writes'] == 0 and again['row_counts_unchanged'] and again['consistent_after']
        and all(report['source_unchanged'].values()) and report['temporary_copy_deleted'] and not attempts
        and (not as_of or report['as_of']['reconciliation']['consistent']))
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='data/trends.sqlite3', help='the operational SQLite database (opened read-only)')
    parser.add_argument('--output', required=True)
    parser.add_argument('--target', choices=('sqlite', 'oracle'), default='sqlite',
                        help='oracle: the disposable ORACLE_TEST_* schema, re-verified before every reset')
    parser.add_argument('--as-of', help='also load the same prepared copy as of this later instant (retention scenario)')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / 'tests'))
    production = (root / 'data' / 'trends.sqlite3').resolve()
    with guarded(production) as attempts:
        report = rehearse(args.source, args.target, datetime.fromisoformat(args.as_of) if args.as_of else None, production, attempts)
    output = root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({k: report[k] for k in ('passed', 'seconds', 'source_unchanged', 'temporary_copy_deleted', 'guard_attempts')}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
