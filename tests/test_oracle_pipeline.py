"""ORACLE-RUNTIME-2 full path: the Oracle runtime alone runs planning to food observations.

FOOD_ID -> TRACKING_TARGET -> QUERY PLANNER -> TRACKING SLOT -> OUTBOX/JOB -> QUOTA RESERVATION ->
WORKER/LEASE -> MOCK COLLECTOR -> INGESTION/DQ -> FOOD OBSERVATION. The SQLite path runs the same
mock inputs from the same B7-loaded state; every stage's tables are compared as logical values.
"""
import json
import unittest
from food_trends.execution_mock import MockTransport, mock_controller
from food_trends.oracle_execution import oracle_mock_controller
from food_trends.quota_ledger import import_legacy_usage
from food_trends.target_migration import integrity_checks, migrate
from food_trends.tracking_service import instant
import test_oracle_observations as s4
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema
from test_food_entities import snapshot

STAGES = ('planner_queries', 'planner_plans', 'planner_query_targets', 'planner_plan_targets', 'tracking_targets', 'ingest_runs',
          'source_items', 'item_snapshots', 'run_items', 'quality_results', 'item_query_matches', 'food_mentions', 'metric_observations')
RUN_SPECIFIC = dict(s4.RUN_SPECIFIC, execution_jobs=('job_id', 'job_key', 'run_id', 'created_at', 'started_at', 'finished_at'))


class OraclePipelineStandInTests(s4.OracleObservationStandInTests):
    """Reuses the S4 harness (comparisons, views); the cases below replace the inherited ones."""

    def test_oracle_runtime_runs_the_whole_path_alone(self):
        for source in ('youtube', 'instagram_graph', 'naver_datalab'):
            self.active(source)
        migrate(self.storage.path, self.repo, now=self.now)
        import_legacy_usage(self.storage.path, self.repo, now=self.now, scopes=[('instagram_graph', 'local-default')])
        sqlite_transport, oracle_transport = MockTransport(), MockTransport()
        sqlite = mock_controller(self.storage, self.planner, sqlite_transport)
        oracle = oracle_mock_controller(self.repo, self.tracking.accounts, lambda: self.now, oracle_transport)
        oracle.execution.require_verified_usage = True  # the live-mode budget rule, exercised with mocks
        start = self.now
        left = sqlite.execution.schedule()
        while sqlite.run_once() is not None:
            pass
        sqlite_after = snapshot(self.storage)
        self.now = start
        right = oracle.execution.schedule()
        while oracle.run_once() is not None:
            pass
        # Same jobs, slots, plans, staging and links on both paths; the same provider requests.
        project = lambda jobs: sorted((j['query_id'], j['due_slot'], j['status']) for j in jobs)
        self.assertEqual(project(right), project(left))
        self.assertEqual(project(self.repo.rows('execution_jobs')), project(self.sqlite_rows('execution_jobs')))
        self.assertEqual(len(oracle_transport.calls), len(sqlite_transport.calls))
        self.assertEqual(sorted((s['target_id'], s['generation'], instant(s['due_slot'])) for s in self.repo.rows('tracking_slots')),
                         sorted((s['target_id'], s['generation'], instant(s['due_slot'])) for s in self.sqlite_rows('tracking_slots')))
        self.assertEqual({s['status'] for s in self.repo.rows('tracking_slots')}, {'COMPLETED'})
        self.assertTablesEqual(STAGES)
        self.assertViewsEqual()
        # The Oracle run wrote nothing to SQLite.
        self.assertEqual(snapshot(self.storage), sqlite_after)
        # Policy blocks hold: NAVER nothing stored, YouTube no mention or metric.
        self.assertFalse([r for r in self.repo.rows('source_items') if r['source_code'] == 'naver_datalab'])
        self.assertFalse([m for m in self.repo.rows('food_mentions') if m['source_code'] == 'youtube'])
        self.assertFalse([m for m in self.repo.rows('metric_observations') if m['source_code'] in ('youtube', 'naver_datalab')])
        # FK 0, payload refs resolved, nothing expired kept, no duplicates.
        checks, _ = integrity_checks(self.repo, self.now)
        self.assertEqual({k: checks[k] for k in ('foreign_key_errors', 'unresolved_payload_refs', 'blocked_policy_rows', 'expired_rows_in_target')},
                         dict(foreign_key_errors=0, unresolved_payload_refs=0, blocked_policy_rows=0, expired_rows_in_target=0))
        for table, key in s4.NATURAL_KEYS.items():
            values = [tuple(r[c] for c in key) for r in self.repo.rows(table)]
            self.assertEqual(len(values), len(set(values)), table)
        # A rerun records the plans of the next, not yet due slot (as SQLite does) and starts nothing.
        self.assertEqual((oracle.execution.schedule(), sqlite.execution.schedule()), ([], []))
        self.assertTablesEqual(('planner_queries', 'planner_plans', 'planner_query_targets', 'planner_plan_targets'))
        # Re-running every stage again changes nothing but the command log.
        before = self.snapshot()
        self.assertEqual(oracle.execution.schedule(), [])
        self.assertIsNone(oracle.run_once())
        self.assertEqual(oracle.ingestion.ingest_pending()['ingested'], 0)
        oracle.observations.link_pending()
        after = self.snapshot()
        self.assertEqual({t for t in after if after[t] != before[t]}, {'planner_commands'})

    def sqlite_rows(self, table):
        with self.storage.reader() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM %s' % table)]
        for r in rows:
            r.setdefault('due_slot', r.get('due_slot'))
        return rows


# The inherited S4 cases already run in their own module.
for name in [n for n in dir(s4.OracleObservationStandInTests) if n.startswith('test_')]:
    setattr(OraclePipelineStandInTests, name, None)


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OraclePipelineTests(OraclePipelineStandInTests):
    """The full path on the real disposable Oracle test schema."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db
