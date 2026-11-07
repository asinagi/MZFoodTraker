"""ORACLE-LIVE-VERIFICATION-READY-1 Phases A, C and D: the writer cycle of a live pilot rehearsed on an isolated v11 copy of the
operational database and the disposable Oracle test schema (MZ_TEST), through the documented commands.

The operational file is only read (mode=ro, query_only): rehearse_cutover.guarded refuses any other connection to it and every
non-loopback socket. `--isolate-database` copies it into a temporary folder (deleted afterwards) and prepares the copy; every
later command names the copy with `--isolated-database`, so nothing needs the operational switch. On MZ_TEST (re-verified
before each reset): B7 load and legacy usage import, the live readiness check, the writer transfer, one pilot round through
the admin API under the pilot's request caps (the existing clients answered by a recording fake: no request leaves the
process; one real RSS item of the copy is observed again), a release while a request is on the wire (DRAINING) and the other
releases, conflicts while draining, the usage handover, the unchanged legacy budget code over the copy, reverse recovery and
the resume check, then every command again (each must change nothing). The credentials are synthetic and .env is not read.
sqlite3 connections are counted per step (the live round opens none). The report holds counts, codes, hashes and timings.
"""
import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('google_trends_rss', 'instagram_graph', 'youtube', 'naver_datalab')
API_SOURCES = SOURCES[1:]
# The pilot's request caps (LIVE_VERIFICATION_REQUEST.md) and the settings that make the budgets enforce them.
PLANNED = {'google_trends_rss': 1, 'instagram_graph': 5, 'youtube': 2, 'naver_datalab': 1}
PILOT_CAPS = {'INSTAGRAM_MAX_PAGES': '1', 'INSTAGRAM_SEARCH_SOFT_LIMIT': '1', 'YOUTUBE_DAILY_SEARCH_LIMIT': '1',
              'YOUTUBE_DAILY_VIDEO_LIMIT': '1', 'YOUTUBE_MAX_PAGES': '1', 'NAVER_DATALAB_DAILY_LIMIT': '1'}
HOSTS = (('trends.google', 'google_trends_rss'), ('facebook.com', 'instagram_graph'), ('instagram.com', 'instagram_graph'),
         ('googleapis.com', 'youtube'), ('ntruss.com', 'naver_datalab'), ('naver.com', 'naver_datalab'))


class Cleanups:
    """The addCleanup half of a TestCase, for the test helpers this script reuses."""

    def __init__(self):
        self.callbacks = []

    def addCleanup(self, function, *args):
        self.callbacks.append((function, args))

    def close(self):
        while self.callbacks:
            function, args = self.callbacks.pop()
            function(*args)


def wire_class():
    from test_oracle_live import LiveWire

    class PilotWire(LiveWire):
        """LiveWire counting requests per provider, with hooks that run while a given request is on the wire."""

        def __init__(self):
            super().__init__()
            self.by_source, self.hooks = Counter(), {}

        def open(self, request, timeout=None):
            url = request.full_url if hasattr(request, 'full_url') else request
            source = next((s for host, s in HOSTS if host in url), 'other')
            self.by_source[source] += 1
            hook = self.hooks.pop((source, urlsplit(url).path.rsplit('/', 1)[-1]), None)
            if hook:
                hook()
            return super().open(request, timeout)
    return PilotWire


class Connections:
    """sqlite3.connect calls per step and file role, counted above the guard (which still refuses what it refuses)."""

    def __init__(self, production):
        self.production, self.copy, self.step, self.counts, self.original = production, None, 'setup', {}, None

    def role(self, path):
        text = str(path)
        if text == ':memory:':
            return 'memory'
        if text.startswith('file:'):
            text = unquote(urlsplit(text).path)
            if len(text) > 2 and text[0] == '/' and text[2] == ':':
                text = text[1:]
        resolved = Path(text).resolve()
        if resolved == self.production:
            return 'operational_read_only'
        return 'isolated_copy' if self.copy and resolved == self.copy else 'other'

    def __enter__(self):
        self.original = sqlite3.connect

        def counted(path, *args, **kwargs):
            self.counts.setdefault(self.step, Counter())[self.role(path)] += 1
            return self.original(path, *args, **kwargs)
        sqlite3.connect = counted
        return self

    def __exit__(self, *exc):
        sqlite3.connect = self.original

    @contextmanager
    def during(self, step):
        previous, self.step = self.step, step
        try:
            yield
        finally:
            self.step = previous

    def report(self):
        return {step: dict(counts) for step, counts in self.counts.items()}


class Rehearsal:
    def __init__(self, production, work, connections):
        from test_admin_oracle import AdminClient
        self.production, self.work, self.connections = production, work, connections
        self.copy = work / 'isolated' / 'rehearsal.sqlite3'
        self.registry = str(work / 'registry.json')
        self.names = {str(self.copy): '<isolated>/' + self.copy.name, self.registry: '<registry>'}
        self.AdminClient, self.cleanups, self.out = AdminClient, Cleanups(), {}

    # The CLI as the operator runs it (in process, so the guards and the counters apply; .env is not read)

    def cli(self, *argv):
        out, err, code = io.StringIO(), io.StringIO(), 0
        from food_trends.__main__ import main
        with patch('sys.argv', ['food_trends', *argv]), patch('food_trends.__main__.load_env'), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                main()
            except SystemExit as stop:
                code = stop.code
        lines = [line for line in out.getvalue().splitlines() if line.startswith('{')]
        error = err.getvalue().strip().splitlines()
        return {'argv': ' '.join(self.names.get(a, a) for a in argv), 'exit': code, 'result': json.loads(lines[-1]) if lines else None,
                'error': error[-1][:300] if error and code else None}

    # Phase A

    def isolate(self):
        from food_trends import cutover
        self.connections.copy = self.copy.resolve()
        with self.connections.during('A_isolate'):
            created = self.cli('--isolate-database', str(self.copy))
        again = self.cli('--isolate-database', str(self.copy))
        now = datetime.now(timezone.utc)
        with self.connections.during('A_checks'), closing(cutover.readonly(self.copy)) as db:
            lineage = cutover.lineage(self.copy)
            dispositions = cutover.table_dispositions(self.copy)
            q = lambda sql, *a: db.execute(sql, a).fetchone()[0]
            retention = {
                'captions_past_window': q('SELECT count(*) FROM instagram_media WHERE caption IS NOT NULL AND coalesce(caption_captured_at,first_seen)<?',
                                          (now - timedelta(days=cutover.CAPTION_DAYS)).isoformat()),
                'youtube_videos_past_expiry': q('SELECT count(*) FROM youtube_videos WHERE expires_at<=?', now.isoformat()),
                'food_entities': q('SELECT count(*) FROM food_entities'), 'food_aliases': q('SELECT count(*) FROM food_aliases'),
                'origins': {r[0]: r[1] for r in db.execute('SELECT first_origin,count(*) FROM source_items GROUP BY first_origin')},
                'application_id': q('PRAGMA application_id')}
        self.out['A'] = {'isolate': created, 'isolate_again': {k: again[k] for k in ('exit', 'error')},
                         'lineage_mismatches': {k: v.get('mismatches', {}) for k, v in lineage.items() if isinstance(v, dict)},
                         'lineage': lineage, 'unclassified_tables': [t for t, v in dispositions.items() if v['disposition'] == 'UNCLASSIFIED'],
                         'dispositions': Counter(v['disposition'] for v in dispositions.values()), 'copy_after_prepare': retention}
        return created['exit'] == 0

    # Phase C, steps 4-7: registry, fresh usage ledger, readiness, transfer

    def prepare_target(self):
        from oracle_support import oracle_target, reset_target_schema
        db = oracle_target()
        try:
            reset_target_schema(db)  # re-verifies the disposable test account and its objects before dropping anything
        finally:
            db.close()
        Path(self.registry).write_text(json.dumps([dict(source_id=s, account_scope=self.scope(s), permission_verified=True, policy_allowed=True,
                                                        approved_by='rehearsal-operator', evidence='isolated rehearsal grant, not a real approval')
                                                   for s in SOURCES]), encoding='utf-8')
        steps = self.out.setdefault('C', {})
        steps['target_migrate'] = self.cli('--target-migrate', 'oracle')
        steps['target_status'] = self.cli('--target-status', 'oracle')
        with self.connections.during('C_target_load'):
            steps['target_load'] = self.cli('--target-load', 'oracle', '--isolated-database', str(self.copy))
        steps['readiness_before_transfer'] = self.readiness()
        with patch.dict(os.environ, {'INSTAGRAM_COLLECTION_ENABLED': 'true', 'YOUTUBE_COLLECTION_ENABLED': 'true'}):
            steps['readiness_legacy_scheduler_on'] = self.readiness()
        steps['transfer'] = {s: self.cli('--backend', 'oracle-test', '--writer-transfer', s, '--execution-registry', self.registry) for s in SOURCES}
        steps['transfer_again'] = self.cli('--backend', 'oracle-test', '--writer-transfer', 'youtube', '--execution-registry', self.registry)
        steps['status_after_transfer'] = self.status()
        steps['readiness_after_transfer'] = self.readiness()

    @staticmethod
    def scope(source):
        from test_oracle_live import SCOPES
        return SCOPES[source]

    def readiness(self):
        result = self.cli('--backend', 'oracle-test', '--execution-readiness', self.registry)
        r = result['result'] or {}
        return {'exit': result['exit'], 'ready': r.get('ready'), 'blockers': r.get('blockers'),
                'sources': {s: v['blockers'] for s, v in (r.get('sources') or {}).items()}}

    def status(self):
        result = self.cli('--backend', 'oracle-test', '--writer-status')
        sources = (result['result'] or {}).get('sources') or {}
        return {'exit': result['exit'], 'writers': {s: v.get('writer') for s, v in sources.items()},
                'active_live_jobs': {s: v.get('active_live_jobs') for s, v in sources.items()},
                'live_leases': {s: v.get('live_leases') for s, v in sources.items()},
                'cycles': {s: v.get('cycle') for s, v in sources.items()}, 'raw': result['result']}

    # Phase C, steps 8-11 and Phase D-3: the pilot round on the live assembly, with a release while YouTube's last request is out

    def pilot_round(self):
        from food_trends.__main__ import oracle_admin_server
        from food_trends.oracle_live import oracle_live_backend
        from food_trends.target_repository import TargetRepository
        from oracle_support import oracle_target
        from test_admin_oracle import TOKEN
        from test_oracle_rss import BAGEL, COOKIE, feed
        steps, wire = self.out['C'], wire_class()()
        wire.install(self.cleanups)
        wire.rss = self.feed_with_a_known_item(feed(COOKIE, BAGEL))
        drained = {}

        def release_in_flight():  # runs while the YouTube videos request is on the wire (the operator's commands, not the live path)
            with self.connections.during('C_operator_commands_while_draining'):
                drained['release'] = self.cli('--backend', 'oracle-test', '--writer-release', 'youtube', '--execution-registry', self.registry)
                drained['transfer_while_draining'] = self.cli('--backend', 'oracle-test', '--writer-transfer', 'youtube',
                                                              '--execution-registry', self.registry)
                drained['handover_while_draining'] = self.cli('--writer-handover', 'youtube', '--recovery-target', 'oracle',
                                                              '--isolated-database', str(self.copy))
                drained['resume_check_while_draining'] = self.cli('--legacy-resume-check', 'youtube', '--recovery-target', 'oracle',
                                                                  '--isolated-database', str(self.copy))
                drained['status_while_draining'] = self.status()
        wire.hooks[('youtube', 'videos')] = release_in_flight
        repo = TargetRepository(oracle_target())
        backend = oracle_live_backend(repo, self.registry)
        server, backend = oracle_admin_server(repo, 0, TOKEN, backend=backend)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        admin = self.AdminClient(server)

        def ok(method, path, body=None):
            status, data = admin.call(method, path, body)
            if status != 200:
                raise RuntimeError('%s %s -> %s %s' % (method, path, status, json.dumps(data, ensure_ascii=False)[:300]))
            return data
        review = lambda body: dict(body, request_key=uuid.uuid4().hex, approved=True, actor_ref='rehearsal-reviewer', reason='격리 리허설 검토')
        track = lambda body: dict(body, request_key=uuid.uuid4().hex, actor_ref='rehearsal-reviewer', reason='격리 리허설 추적')
        round_ = {}
        try:
            base = {'rss_runs': repo.count('rss_runs'), 'jobs': {j['job_id'] for j in repo.rows('execution_jobs')}}
            with self.connections.during('C_pilot_round'):
                collected = ok('POST', '/api/collect')
                round_['rss'] = {k: collected.get(k) for k in ('success', 'item_count', 'new_count')}
                round_['rss']['candidates'] = collected['entity_resolution']['candidates']
                candidates = {c['surface_form']: c for c in ok('GET', '/api/food-candidates')['items']}
                cookie = candidates[COOKIE[0]]
                food = ok('POST', '/api/foods/review', review(dict(candidate_id=cookie['candidate_id'], expected_revision=cookie['revision'],
                                                                   action='create', canonical_name='두쫀쿠')))['resolution']
                ok('POST', '/api/foods/alias', review(dict(action='approve', food_id=food['food_id'], surface_form='두바이쫀득쿠키',
                                                           expected_revision=ok('GET', '/api/foods/' + food['food_id'])['revision'])))
                for source in API_SOURCES:
                    target = ok('POST', '/api/tracking/register', track(dict(food_id=food['food_id'], source_id=source, account_scope=self.scope(source))))
                    ok('POST', '/api/tracking/activate', track(dict(target_id=target['target_id'], expected_revision=target['revision'], approved=True)))
                ok('POST', '/api/planner/commit', {'request_key': uuid.uuid4().hex})
                jobs = ok('POST', '/api/execution/schedule', {'mode': 'live'})['items']
                round_['jobs_scheduled'] = Counter(j['source_id'] for j in jobs)
                results = []
                while True:
                    batch = ok('POST', '/api/execution/tick', {'mode': 'live', 'max_jobs': 50})['results']
                    results += batch
                    if not batch:
                        break
                round_['job_results'] = Counter('%s:%s:%s' % (r['source_id'], r['status'], r['error_kind']) for r in results)
                round_['requests_by_source'] = {s: wire.by_source[s] for s in SOURCES} | {'other': wire.by_source['other']}
                round_['requests_total'] = sum(wire.by_source.values())
                round_['endpoints'] = Counter(wire.calls)
                # Running the round again (tick schedules first) sends nothing more: the budgets hold the caps.
                again = [ok('POST', '/api/execution/tick', {'mode': 'live', 'max_jobs': 50})['results'] for _ in range(2)]
                round_['second_tick'] = {'results': Counter('%s:%s:%s' % (r['source_id'], r['status'], r['error_kind']) for r in again[0] + again[1]),
                                         'requests_total': sum(wire.by_source.values())}
                status = ok('GET', '/api/execution/status')
                round_['admin_status_counts'] = status.get('counts')
            round_['recorded_requests'] = self.recorded(repo, base)
            round_['within_caps'] = all(round_['requests_by_source'][s] <= PLANNED[s] for s in SOURCES) and \
                round_['requests_by_source']['other'] == 0 and round_['second_tick']['requests_total'] == round_['requests_total']
            round_['drained'] = drained
            round_['food_id_assigned'] = bool(food['food_id'])
            self.food_id = food['food_id']
        finally:
            backend.execution.release()
            server.shutdown()
            server.server_close()
            repo.db.close()
            self.cleanups.close()
        steps['pilot_round'] = round_
        steps['release_youtube_after_round'] = self.cli('--backend', 'oracle-test', '--writer-release', 'youtube', '--execution-registry', self.registry)
        steps['status_after_youtube_release'] = self.status()

    def feed_with_a_known_item(self, synthetic):
        """The fake feed carrying, first, the newest RSS item of the copy as the provider returned it (raw XML, in memory only)."""
        from food_trends import cutover
        with closing(cutover.readonly(self.copy)) as db:
            row = db.execute('SELECT payload FROM items ORDER BY id DESC LIMIT 1').fetchone()
        raw = json.loads(row[0]).get('raw_xml') if row else None
        self.out.setdefault('C', {})['known_rss_item_reobserved'] = bool(raw)
        if not raw:
            return synthetic
        text = synthetic.decode('utf-8')
        cut = text.index('<channel>') + len('<channel>')
        return (text[:cut] + raw + text[cut:]).encode('utf-8')

    @staticmethod
    def recorded(repo, base):
        """What the target recorded of the round: one execution call per provider request (by source and status) and one RSS run
        per feed fetch, counted over the round's own jobs and runs only (the B7 load brought the copy's RSS runs)."""
        jobs = {j['job_id']: j['source_id'] for j in repo.rows('execution_jobs') if j['job_id'] not in base['jobs']}
        calls = [c for c in repo.rows('execution_calls') if c['job_id'] in jobs]
        by_source = Counter(jobs[c['job_id']] for c in calls)
        by_source['google_trends_rss'] = repo.count('rss_runs') - base['rss_runs']
        return {'requests_by_source': {s: by_source[s] for s in SOURCES}, 'calls_by_status': Counter('%s:%s' % (jobs[c['job_id']], c['status']) for c in calls),
                'origins': sorted({r['origin'] for r in repo.rows('execution_runs') if r['job_id'] in jobs})}

    # Phase C, steps 10-15: releases, handover, legacy budgets, recovery, resume, re-runs

    def wind_down(self):
        steps = self.out['C']
        isolated = ('--isolated-database', str(self.copy))
        steps['release'] = {s: self.cli('--backend', 'oracle-test', '--writer-release', s, '--execution-registry', self.registry)
                            for s in ('instagram_graph', 'naver_datalab', 'google_trends_rss')}
        steps['release_again'] = {s: self.cli('--backend', 'oracle-test', '--writer-release', s, '--execution-registry', self.registry) for s in SOURCES}
        steps['status_after_release'] = self.status()
        steps['resume_before_handover'] = {s: self.cli('--legacy-resume-check', s, '--recovery-target', 'oracle', *isolated) for s in SOURCES}
        with self.connections.during('C_handover'):
            steps['handover'] = {s: self.cli('--writer-handover', s, '--recovery-target', 'oracle', *isolated) for s in SOURCES}
        steps['handover_again'] = {s: self.cli('--writer-handover', s, '--recovery-target', 'oracle', *isolated) for s in SOURCES}
        with self.connections.during('C_recovery'):
            steps['recovery_plan'] = self.cli('--recovery', 'plan', '--recovery-target', 'oracle', *isolated)
            steps['recovery_apply'] = self.cli('--recovery', 'apply', '--recovery-target', 'oracle', '--apply-updates', *isolated)
        steps['recovery_again'] = self.cli('--recovery', 'apply', '--recovery-target', 'oracle', '--apply-updates', *isolated)
        with self.connections.during('C_legacy_budgets'):
            steps['legacy_budgets'] = self.legacy_budgets()
        with self.connections.during('C_resume_check'):
            steps['resume_check'] = {s: self.cli('--legacy-resume-check', s, '--recovery-target', 'oracle', *isolated) for s in SOURCES}
        steps['status_final'] = self.status()
        from food_trends import cutover
        facts = cutover.inspect(self.copy)
        steps['copy_final'] = {k: facts[k] for k in ('schema_version', 'integrity', 'foreign_key_errors')}
        steps['target_integrity'] = self.target_integrity()

    def legacy_budgets(self):
        """The unchanged legacy budget code over the copy after the handover (the pilot caps in force): each refuses before a request."""
        from food_trends.config import InstagramConfig, NaverConfig, YouTubeConfig
        from food_trends.instagram_graph import GraphError
        from food_trends.instagram_service import InstagramService
        from food_trends.naver_service import NaverService
        from food_trends.storage import Storage
        from food_trends.youtube import YouTubeError
        from food_trends.youtube_storage import YouTubeStore
        out = {}
        store = YouTubeStore(Storage(self.copy))
        with store.operation() as owner:
            try:
                store.reserve('search', None, owner, YouTubeConfig.from_env())
                out['youtube_search'] = 'reserved'
            except YouTubeError as error:
                out['youtube_search'] = 'refused:' + error.kind
        out['naver_daily_requests'] = NaverService(Storage(self.copy), NaverConfig.from_env()).status()['daily_requests']
        service = InstagramService(Storage(self.copy), InstagramConfig.from_env())
        service.store.state('search_snapshot', {'ids': [], 'checked_at': datetime.now(timezone.utc).isoformat()})  # no provider check
        out['instagram_local_unique_7d'] = service.budget()['local_unique_7d']
        try:
            service.reserve({'name': '리허설새태그', 'instagram_id': None})
            out['instagram_new_tag'] = 'reserved'
        except GraphError as error:
            out['instagram_new_tag'] = 'refused:' + error.kind
        return out

    @staticmethod
    def target_integrity():
        from food_trends.target_migration import integrity_checks
        from food_trends.target_repository import TargetRepository
        from oracle_support import oracle_target
        repo = TargetRepository(oracle_target())
        try:
            checks, _ = integrity_checks(repo, datetime.now(timezone.utc))
            return {k: checks[k] for k in ('foreign_key_errors', 'unresolved_payload_refs', 'blocked_policy_rows', 'expired_rows_in_target')}
        finally:
            repo.db.close()

    def reset_target(self):
        from oracle_support import oracle_target, reset_target_schema
        db = oracle_target()
        try:
            reset_target_schema(db)
            with db.transaction() as tx:
                return tx.scalar('SELECT count(*) FROM user_tables')
        finally:
            db.close()


def summary(report):
    """The pass/fail facts the report states (each one a reason code when false)."""
    a, c = report.get('A', {}), report.get('C', {})
    created = (a.get('isolate') or {}).get('result') or {}
    round_ = c.get('pilot_round', {})
    drained = round_.get('drained', {})
    code = lambda step: (step or {}).get('exit')
    res = lambda step: (step or {}).get('result') or {}
    checks = {
        'A_source_unchanged': all((created.get('source_unchanged') or {False: False}).values()),
        'A_copy_v11_integrity_fk': (created.get('copy') or {}).get('schema_version') == 11 and created['copy']['integrity'] == 'ok'
        and created['copy']['foreign_key_errors'] == 0,
        'A_original_rows_and_columns_preserved': (created.get('prepare') or {}).get('original_rows_and_columns_preserved') is True,
        'A_no_lineage_mismatch': not any(a.get('lineage_mismatches', {}).values()) and not a.get('unclassified_tables'),
        'A_no_expired_content_or_new_food': a.get('copy_after_prepare', {}).get('captions_past_window') == 0
        and a['copy_after_prepare'].get('youtube_videos_past_expiry') == 0 and a['copy_after_prepare'].get('food_entities') == 0,
        'A_rerun_refused_existing_copy': code(a.get('isolate_again')) == 2,
        'C_load_consistent': res(c.get('target_load')).get('consistent') is True,
        'C_not_ready_before_transfer': c.get('readiness_before_transfer', {}).get('ready') is False,
        'C_legacy_scheduler_blocks_live': 'LEGACY_SCHEDULER_ENABLED' in c.get('readiness_legacy_scheduler_on', {}).get('sources', {}).get('instagram_graph', []),
        'C_transferred_and_ready': set(c.get('status_after_transfer', {}).get('writers', {}).values()) == {'ORACLE'}
        and c.get('readiness_after_transfer', {}).get('ready') is True,
        'C_pilot_within_caps': round_.get('within_caps') is True,
        'C_requests_recorded': round_.get('recorded_requests', {}).get('requests_by_source') == {s: round_.get('requests_by_source', {}).get(s) for s in SOURCES},
        'C_draining_while_in_flight': (res(drained.get('release')).get('status') == 'DRAINING') and code(drained.get('transfer_while_draining')) == 2
        and code(drained.get('handover_while_draining')) == 2 and res(drained.get('resume_check_while_draining')).get('ready') is False,
        'C_other_writers_kept': {k: v for k, v in c.get('status_after_youtube_release', {}).get('writers', {}).items() if k != 'youtube'}
        == {'google_trends_rss': 'ORACLE', 'instagram_graph': 'ORACLE', 'naver_datalab': 'ORACLE'},
        'C_released': set(c.get('status_after_release', {}).get('writers', {}).values()) == {'RELEASED'},
        'C_no_live_job_or_lease_left': not any(c.get('status_after_release', {}).get('active_live_jobs', {}).values())
        and not any(c.get('status_after_release', {}).get('live_leases', {}).values()),
        'C_resume_refused_before_handover': all(res(r).get('ready') is False for s, r in c.get('resume_before_handover', {}).items() if s != 'google_trends_rss'),
        'C_handover_rerun_changes_nothing': all(res(r).get('handed_over') == 0 for r in c.get('handover_again', {}).values()),
        'C_recovery_applied_every_update': res(c.get('recovery_apply')).get('updates_applied') is True
        and (res(c.get('recovery_apply')).get('remaining') or {}).get('mutable_updates') == 0,
        'C_recovery_converged_rerun_writes_nothing': res(c.get('recovery_apply')).get('converged') is True
        and res(c.get('recovery_again')).get('writes') == {'inserted': 0, 'content_rows': 0, 'updated': 0, 'ignored': 0},
        'C_legacy_budget_code_counts_handover': c.get('legacy_budgets', {}).get('youtube_search') == 'refused:budget'
        and c.get('legacy_budgets', {}).get('instagram_new_tag') == 'refused:budget',
        'C_resume_ready_after_handover_and_recovery': all(res(r).get('ready') is True for r in c.get('resume_check', {}).values()),
        'C_release_rerun_unchanged': all(res(r).get('status') == 'RELEASED' for r in c.get('release_again', {}).values()),
        'D_live_round_opened_no_sqlite': not report.get('sqlite_connections', {}).get('C_pilot_round'),
        'G_no_external_or_write_attempt': not report.get('guard_attempts'),
        'G_operational_unchanged': all(report.get('operational_unchanged', {False: False}).values()),
        'G_temporary_copy_deleted': report.get('temporary_copy_deleted') is True,
        'G_target_reset': report.get('target_objects_after_reset') == 0,
    }
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', default='data/trends.sqlite3', help='the operational SQLite database (only read)')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / 'tests'))
    import rehearse_cutover
    from food_trends import cutover
    from oracle_support import ORACLE_READY, SKIP_REASON
    from test_oracle_live import LIVE_ENV
    if not ORACLE_READY:
        raise SystemExit(SKIP_REASON)
    production = (ROOT / 'data' / 'trends.sqlite3').resolve()
    source = (ROOT / args.source).resolve()
    env = dict(LIVE_ENV, **PILOT_CAPS, DATABASE_PATH=str(source), ORACLE_ENVIRONMENT='test',
               **{'ORACLE_' + k: os.environ['ORACLE_TEST_' + k] for k in ('DSN', 'USER', 'PASSWORD')})  # both prefixes name MZ_TEST
    started = time.perf_counter()
    report = {'task': 'ORACLE-LIVE-VERIFICATION-READY-1 phases A, C, D-3', 'rehearsed_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
              'source': source.name, 'target': 'oracle MZ_TEST (ORACLE_TEST_*)', 'credentials': 'synthetic; .env not read',
              'pilot_caps': PILOT_CAPS, 'planned_requests': PLANNED}
    before = cutover.inspect(source)
    with patch.dict(os.environ, env), rehearse_cutover.guarded(production) as attempts, Connections(production) as connections:
        with tempfile.TemporaryDirectory(prefix='mz-live-rehearsal-') as directory:
            rehearsal = Rehearsal(production, Path(directory), connections)
            try:
                if rehearsal.isolate():
                    rehearsal.prepare_target()
                    rehearsal.pilot_round()
                    rehearsal.wind_down()
            except Exception as error:  # the report keeps the failure's type and message; nothing secret is in either
                report['failed'] = '%s: %s' % (type(error).__name__, str(error)[:500])
            finally:
                report.update(rehearsal.out)
                report['target_objects_after_reset'] = rehearsal.reset_target()
            temporary = Path(directory)
        report['temporary_copy_deleted'] = not temporary.exists()
        report['sqlite_connections'] = connections.report()
    after = cutover.inspect(source)
    report['operational_unchanged'] = {k: before[k] == after[k] for k in ('sha256', 'bytes', 'modified_at', 'wal_bytes', 'schema_version', 'integrity', 'tables')}
    report['guard_attempts'] = list(attempts)
    report['seconds'] = round(time.perf_counter() - started, 3)
    report['checks'] = summary(report)
    report['passed'] = 'failed' not in report and all(report['checks'].values())
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps({k: report.get(k) for k in ('passed', 'failed', 'seconds')} | {'failed_checks': [k for k, v in report['checks'].items() if not v]},
                     ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
