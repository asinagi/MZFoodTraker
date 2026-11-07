"""ORACLE-PRODUCTION-CUTOVER-1: the approved live pilot on the production Oracle schema, with hard request caps.

Runs only against the approved production schema (runtime_storage.oracle_production_repository) with a reviewed registry.
In order: a fresh legacy usage import from the migrated SQLite copy, the writer transfer of every granted source, the live
readiness check, then one round through the admin API of the live assembly: RSS once, the owner-approved food (submitted
manually, no extra alias), tracking on Instagram, YouTube and NAVER, planning, and each scheduled job run once. The B8 app
API then reads the same schema through its own connection. Every provider request goes through the existing clients; a
counting opener below them records host and HTTP status only and refuses, before sending, any request beyond the pilot
cap of its source. sqlite3 connections are counted (the live round opens none). The writers stay with the Oracle runtime
afterwards (it is the system of record); nothing schedules further requests. Output: counts, statuses and codes only.
"""
import argparse
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ('google_trends_rss', 'instagram_graph', 'youtube', 'naver_datalab')
CAPS = {'google_trends_rss': 1, 'instagram_graph': 5, 'youtube': 2, 'naver_datalab': 1}
PILOT_ENV = {'B3_LIVE_ENABLED': 'true', 'B3_EXCLUSIVE_SOURCES': ','.join(SOURCES), 'B3_AUTOMATIC_ENABLED': 'false',
             'INSTAGRAM_COLLECTION_ENABLED': 'false', 'YOUTUBE_COLLECTION_ENABLED': 'false', 'NAVER_DATALAB_ENABLED': 'false',
             'INSTAGRAM_MAX_PAGES': '1', 'INSTAGRAM_SEARCH_SOFT_LIMIT': '1', 'YOUTUBE_DAILY_SEARCH_LIMIT': '1',
             'YOUTUBE_DAILY_VIDEO_LIMIT': '1', 'YOUTUBE_MAX_PAGES': '1', 'NAVER_DATALAB_DAILY_LIMIT': '1'}
HOSTS = (('trends.google', 'google_trends_rss'), ('facebook.com', 'instagram_graph'), ('instagram.com', 'instagram_graph'),
         ('googleapis.com', 'youtube'), ('ntruss.com', 'naver_datalab'), ('naver.com', 'naver_datalab'))
LOOPBACK = build_opener(ProxyHandler({}))


class Wire:
    """Below the existing clients: counts every provider request by source and status and enforces the pilot caps."""

    def __init__(self):
        self.sent, self.statuses, self.refused = Counter(), Counter(), Counter()
        self.lock = threading.Lock()

    def source(self, url):
        return next((s for host, s in HOSTS if host in urlsplit(url).netloc), 'other')

    def admit(self, url):
        source = self.source(url)
        with self.lock:
            if source == 'other' or self.sent[source] >= CAPS[source]:
                self.refused[source] += 1
                raise URLError('pilot request cap: %s not sent' % source)
            self.sent[source] += 1
        return source

    def record(self, source, call):
        try:
            response = call()
        except HTTPError as error:
            self.statuses['%s:%s' % (source, error.code)] += 1
            raise
        except Exception as error:
            self.statuses['%s:%s' % (source, type(error).__name__)] += 1
            raise
        self.statuses['%s:%s' % (source, getattr(response, 'status', 200))] += 1
        return response

    def opener(self, *handlers):
        real = build_opener(*handlers)
        wire = self

        class Counted:
            def open(self, request, timeout=None):
                url = request.full_url if hasattr(request, 'full_url') else request
                source = wire.admit(url)
                return wire.record(source, lambda: real.open(request, timeout=timeout))
        return Counted()

    def urlopen(self, request, timeout=None, **kwargs):
        url = request.full_url if hasattr(request, 'full_url') else request
        source = self.admit(url)
        return self.record(source, lambda: urlopen(request, timeout=timeout, **kwargs))

    def install(self):
        patches = [patch('food_trends.instagram_graph.build_opener', self.opener), patch('food_trends.youtube.build_opener', self.opener),
                   patch('food_trends.naver_datalab.build_opener', self.opener), patch('food_trends.collector.urlopen', self.urlopen)]
        for p in patches:
            p.start()
        return patches


class SqliteCounter:
    def __init__(self):
        self.count, self.original = 0, sqlite3.connect

    def __enter__(self):
        def counted(*args, **kwargs):
            self.count += 1
            return self.original(*args, **kwargs)
        sqlite3.connect = counted
        return self

    def __exit__(self, *exc):
        sqlite3.connect = self.original


def call(base, method, path, body=None, token=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {'Content-Type': 'application/json'} if data is not None else {}
    if token:
        headers['X-MZ-Admin-Token'] = token
    try:
        with LOOPBACK.open(Request(base + path, data=data, headers=headers, method=method), timeout=180) as response:
            return response.status, json.loads(response.read() or b'null')
    except HTTPError as error:
        return error.code, json.loads(error.read() or b'null')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--registry', required=True)
    parser.add_argument('--source-copy', required=True, help='the migrated SQLite v11 copy (usage import, read-only)')
    parser.add_argument('--food', default='두쫀쿠', help='the owner-approved food (submitted manually, no alias)')
    parser.add_argument('--approved-by', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from food_trends import app_contract as contract
    from food_trends.__main__ import oracle_admin_server, target_load
    from food_trends.analytics_reader import TargetAnalyticsStore
    from food_trends.analytics_service import FoodAnalytics
    from food_trends.app_api import AppApi, make_app_server
    from food_trends.config import load_env
    from food_trends.cutover import isolated
    from food_trends.oracle_live import oracle_live_backend, oracle_live_readiness, transfer_writer, writer_status
    from food_trends.runtime_storage import oracle_maintenance, select
    load_env()  # the real credentials (process variables first); never printed
    os.environ.update(PILOT_ENV)
    report = {'task': 'ORACLE-PRODUCTION-CUTOVER-1 live pilot', 'started_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
              'schema': None, 'caps': CAPS, 'pilot_settings': PILOT_ENV}
    repo = select('oracle-production')  # the approved production schema or a refusal
    report['schema'] = repo.db.settings.user.upper()
    copy = str(isolated(args.source_copy))
    report['usage_import'] = {k: v for k, v in target_load(repo, copy).items() if k in ('consistent', 'legacy_usage_rows', 'legacy_usage_accounts')}
    report['transfer'] = {s: transfer_writer(repo, s, args.registry)['usage_status'] for s in SOURCES}
    readiness = oracle_live_readiness(repo, args.registry)
    report['readiness'] = {'ready': readiness['ready'], 'blockers': readiness['blockers'], 'sources': {s: v['blockers'] for s, v in readiness['sources'].items()}}
    if not readiness['ready']:
        raise SystemExit(json.dumps(report, ensure_ascii=False))
    wire = Wire()
    patches = wire.install()
    token = uuid.uuid4().hex
    backend = oracle_live_backend(repo, args.registry)
    server, backend = oracle_admin_server(repo, 0, token, backend=backend)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    admin = 'http://127.0.0.1:%d' % server.server_address[1]
    reader = select('oracle-production')
    app = AppApi(FoodAnalytics(TargetAnalyticsStore(reader), origin='live_api'), data_mode='database')
    b8 = make_app_server(app, port=0, quiet=True)
    threading.Thread(target=b8.serve_forever, daemon=True).start()
    b8_base = 'http://127.0.0.1:%d' % b8.server_address[1]
    doc, steps = contract.openapi(), {}
    ok = lambda method, path, body=None: call(admin, method, path, body, token)
    review = lambda body: dict(body, request_key=uuid.uuid4().hex, approved=True, actor_ref=args.approved_by,
                               reason='ORACLE-PRODUCTION-CUTOVER-1 운영 검증 음식(소유자 승인)')
    try:
        with SqliteCounter() as opened:
            status, rss = ok('POST', '/api/collect')
            steps['rss'] = {'http': status, 'success': rss.get('success'), 'item_count': rss.get('item_count'), 'new_count': rss.get('new_count'),
                            'candidates': (rss.get('entity_resolution') or {}).get('candidates'), 'error': rss.get('error')}
            status, candidate = ok('POST', '/api/foods/submit', dict(request_key=uuid.uuid4().hex, name=args.food, aliases=[]))
            status, reviewed = ok('POST', '/api/foods/review', review(dict(candidate_id=candidate['candidate_id'], expected_revision=candidate['revision'],
                                                                           action='create', canonical_name=args.food)))
            food = reviewed['resolution']
            steps['food'] = {'http': status, 'food_id': food['food_id']}
            accounts = {a.source_id: a.account_scope for a in backend.tracking.accounts.values() if a.mode == 'live'}
            tracked = {}
            for source in ('instagram_graph', 'youtube', 'naver_datalab'):
                track = lambda body: dict(body, request_key=uuid.uuid4().hex, actor_ref=args.approved_by, reason='ORACLE-PRODUCTION-CUTOVER-1 운영 검증 추적')
                _, target = ok('POST', '/api/tracking/register', track(dict(food_id=food['food_id'], source_id=source, account_scope=accounts[source])))
                _, active = ok('POST', '/api/tracking/activate', track(dict(target_id=target['target_id'], expected_revision=target['revision'], approved=True)))
                tracked[source] = active.get('effective_status')
            steps['tracking'] = tracked
            status, committed = ok('POST', '/api/planner/commit', {'request_key': uuid.uuid4().hex})
            steps['plans'] = len(committed.get('plans') or [])
            _, scheduled = ok('POST', '/api/execution/schedule', {'mode': 'live'})
            jobs = scheduled.get('items') or []
            steps['jobs_scheduled'] = Counter(j['source_id'] for j in jobs)
            results = []
            for job in jobs:
                status, ran = ok('POST', '/api/execution/run', {'job_id': job['job_id']})
                result = (ran or {}).get('result') or {}
                results.append('%s:%s:%s' % (job['source_id'], result.get('status'), result.get('error_kind')))
            steps['job_results'] = Counter(results)
            calls = Counter()
            for job in jobs:
                _, detail = ok('GET', '/api/execution/jobs/' + job['job_id'])
                for c in ((detail or {}).get('items') or [{}])[0].get('calls') or []:
                    calls['%s:%s:%s' % (job['source_id'], c.get('status'), c.get('http_status'))] += 1
            steps['recorded_calls'] = calls
            steps['maintenance'] = {k: v for k, v in oracle_maintenance(backend).items() if k in ('entity_changes_reprocessed', 'api_requests')}
            reads = {}
            for path in ('/api/v1/health', '/api/v1/foods', '/api/v1/foods/' + food['food_id'], '/api/v1/sources/status',
                         '/api/v1/foods/%s/timeline?period=7d' % food['food_id'], '/api/v1/foods/%s/metrics?period=7d' % food['food_id']):
                status, body = call(b8_base, 'GET', path)
                schema = contract.response_schema(doc, contract.operation_for(doc, urlsplit(path).path)[1], status)
                reads[urlsplit(path).path.replace(food['food_id'], '{food_id}')] = {'http': status, 'contract_errors': len(contract.check(doc, body, schema))}
                if path == '/api/v1/foods':
                    reads['food_ids'] = [f['food_id'] == food['food_id'] for f in (body.get('data') or {}).get('items', [])]
            steps['b8'] = reads
        steps['sqlite_connections_in_live_round'] = opened.count
    finally:
        backend.execution.release()
        server.shutdown()
        server.server_close()
        b8.shutdown()
        b8.server_close()
        for p in patches:
            p.stop()
    report['steps'] = steps
    report['provider_requests'] = {'sent': {s: wire.sent[s] for s in SOURCES}, 'total': sum(wire.sent.values()),
                                   'by_status': dict(wire.statuses), 'refused_by_cap': dict(wire.refused)}
    status = writer_status(repo)
    report['writers_after'] = {s: v.get('writer') for s, v in status['sources'].items()}
    report['finished_at'] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    repo.db.close()
    reader.db.close()
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({'provider_requests': report['provider_requests'], 'jobs': steps.get('job_results'), 'b8': {k: v for k, v in steps.get('b8', {}).items() if k != 'food_ids'}},
                     ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
