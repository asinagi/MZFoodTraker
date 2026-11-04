"""ORACLE-RUNTIME-2 R3: Shadow and the Oracle runtime never write the same target at the same time.

Ownership lives in the target (runtime_ownership + runtime_participants) and is checked inside every
write transaction. Lease expiry uses an injected wall clock so process death is simulated exactly.
"""
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from food_trends.oracle_execution import OracleExecution, oracle_mock_controller
from food_trends.execution_mock import MockTransport
from food_trends.runtime_ownership import OwnershipRefused, RuntimeOwnership
from food_trends.shadow import ShadowService
from food_trends.target_db import SQLiteTarget, TransientError
from food_trends.target_migration import migrate
from food_trends.target_repository import TargetRepository
import test_oracle_execution as s2
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema


class Boom(Exception):
    pass


class OwnershipStandInTests(unittest.TestCase):
    setUp_base = s2.OracleExecutionStandInTests.setUp_base
    tearDown = s2.OracleExecutionStandInTests.tearDown
    make_target = s2.OracleExecutionStandInTests.make_target
    create_food = s2.OracleExecutionStandInTests.create_food
    cmd = s2.OracleExecutionStandInTests.cmd
    register = s2.OracleExecutionStandInTests.register
    active = s2.OracleExecutionStandInTests.active
    change = s2.OracleExecutionStandInTests.change
    advance = s2.OracleExecutionStandInTests.advance

    def setUp(self):
        s2.OracleExecutionStandInTests.setUp(self)
        self.wall = datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc)
        self.target = self.active('youtube')
        migrate(self.storage.path, self.repo, now=self.now, ownership=self.owner())

    def owner(self, fault=None):
        return RuntimeOwnership(self.repo, clock=lambda: self.wall, ttl_seconds=60, fault=fault)

    def runtime(self):
        controller = oracle_mock_controller(self.repo, self.tracking.accounts, lambda: self.now, MockTransport())
        controller.execution.ownership = self.owner()
        return controller

    def shadow(self):
        return ShadowService(self.storage, self.repo, clock=lambda: self.now, ownership=self.owner())

    def mode(self):
        return self.owner().status()

    def shadow_event(self):
        """Capture one SQLite change for Shadow to deliver."""
        self.shadow().enable()
        self.change('interval', self.tracking.target(self.target['target_id']), interval_seconds=43200)

    # 12: Shadow active -> runtime writes refused; reads allowed.

    def test_runtime_write_refused_while_shadow_owns_the_target(self):
        lease = self.owner().acquire('SHADOW')
        runtime = self.runtime()
        with self.assertRaises(OwnershipRefused) as refused:
            runtime.execution.schedule()
        self.assertEqual(refused.exception.code, 'ownership_held_by_SHADOW')
        self.assertEqual(self.repo.count('planner_plans') + self.repo.count('execution_jobs') + self.repo.count('tracking_slots'), 0)
        self.assertTrue(runtime.execution.planner.preview()['plans'])  # reads need no ownership
        self.assertEqual(runtime.execution.status()['items'], [])
        self.owner().release(lease)
        self.assertEqual(self.mode()['mode'], 'IDLE')
        self.assertTrue(runtime.execution.schedule())
        self.assertEqual(self.mode()['mode'], 'RUNTIME_TEST')

    # 13: runtime active -> Shadow delivery, B7 load and repair refused; compare allowed.

    def test_shadow_refused_while_runtime_owns_the_target(self):
        self.shadow_event()
        runtime = self.runtime()
        runtime.execution.schedule()
        shadow = self.shadow()
        with self.assertRaises(OwnershipRefused) as refused:
            shadow.deliver()
        self.assertEqual(refused.exception.code, 'ownership_held_by_RUNTIME_TEST')
        status = shadow.status()
        self.assertEqual((status['pending'], status['failing']), (status['events'], 0))  # events untouched, not counted as failures
        for write in (lambda: migrate(self.storage.path, self.repo, now=self.now, ownership=self.owner()), shadow.repair):
            with self.assertRaises(OwnershipRefused):
                write()
        self.assertIn('consistent', shadow.compare())  # a read-only comparison is allowed

    # 14: a killed runtime process: its lease expires, unfinished jobs keep Shadow out, a new process resumes.

    def test_killed_runtime_recovers_without_handing_unfinished_jobs_to_shadow(self):
        killed = self.runtime()
        jobs = killed.execution.schedule()
        self.wall += timedelta(seconds=61)  # the process is gone: no renewal
        with self.assertRaises(OwnershipRefused) as refused:
            self.owner().acquire('SHADOW')
        self.assertEqual(refused.exception.code, 'runtime_jobs_in_flight')
        resumed = self.runtime()
        for _ in jobs:
            resumed.run_once()
        self.assertEqual({j['status'] for j in self.repo.rows('execution_jobs')}, {'SUCCEEDED'})
        with self.assertRaises(OwnershipRefused) as late:  # the dead process's lease expired
            killed.execution.enqueue([jobs[0]['plan_id']])
        self.assertEqual(late.exception.code, 'ownership_expired')
        resumed.execution.release()
        self.assertEqual(self.mode()['mode'], 'IDLE')
        lease = self.owner().acquire('SHADOW')
        with self.assertRaises(OwnershipRefused) as stale:
            killed.execution.ownership.renew(killed.execution.lease)  # never across a mode change
        self.assertEqual(stale.exception.code, 'ownership_mode_changed')
        self.owner().release(lease)

    def test_abandoned_runtime_without_jobs_can_be_taken_over(self):
        idle = self.runtime()
        idle.execution.planner.commit('plans-only')
        self.wall += timedelta(seconds=61)
        lease = self.owner().acquire('SHADOW')
        with self.assertRaises(OwnershipRefused) as late:
            idle.execution.schedule()
        self.assertIn(late.exception.code, ('ownership_held_by_SHADOW', 'ownership_mode_changed', 'ownership_expired'))
        self.assertEqual(self.repo.count('execution_jobs'), 0)
        self.owner().release(lease)

    # 15: a late worker cannot commit after another worker took the job over.

    def test_late_worker_result_is_refused(self):
        runtime = self.runtime()
        job = runtime.execution.schedule()[0]
        first = runtime.execution.claim('late-worker', job['job_id'])
        runtime.execution.begin(first)
        self.advance()  # the job lease expires
        self.wall += timedelta(seconds=30)
        other = self.runtime()
        other.run_once(job['job_id'])
        with self.assertRaises(Exception) as refused:
            runtime.execution.finish(first, 'SUCCEEDED')
        self.assertEqual(type(refused.exception).__name__, 'LostLease')
        self.assertEqual(self.repo.count('source_contents'), 1)
        self.assertEqual(self.repo.count('execution_item_receipts'), 1)

    # 16: a failure during a mode change leaves the previous mode in place.

    def test_failed_mode_switch_changes_nothing(self):
        def fault(stage):
            if stage == 'before_switch_commit':
                raise Boom()
        with self.assertRaises(Boom):
            self.owner(fault).acquire('RUNTIME_TEST')
        self.assertEqual((self.mode()['mode'], self.mode()['live_participants']), ('IDLE', 0))

        class Down(SQLiteTarget):
            def transaction(self):
                raise TransientError('connection_or_timeout')
        with self.assertRaises(TransientError):
            RuntimeOwnership(TargetRepository(Down(Path(self.temp.name) / 'down.sqlite3'))).acquire('SHADOW')
        self.assertEqual(self.mode()['mode'], 'IDLE')
        lease = self.owner().acquire('RUNTIME_TEST')
        self.assertEqual(self.mode()['mode'], 'RUNTIME_TEST')
        self.owner().release(lease)

    def test_refusals(self):
        owner = self.owner()
        with self.assertRaises(OwnershipRefused) as production:
            owner.acquire('RUNTIME_PRODUCTION')
        self.assertEqual(production.exception.code, 'runtime_production_disabled')
        with self.assertRaises(OwnershipRefused):
            owner.acquire('IDLE')
        first = owner.acquire('SHADOW')
        with self.assertRaises(OwnershipRefused) as second:
            owner.acquire('SHADOW')
        self.assertEqual(second.exception.code, 'shadow_already_active')
        owner.release(first)
        with self.repo.db.transaction() as tx:
            tx.execute('UPDATE target_schema_migrations SET version=version+100')
        with self.assertRaises(OwnershipRefused) as schema:
            owner.acquire('RUNTIME_TEST')
        self.assertEqual(schema.exception.code, 'target_schema_version_mismatch')
        with self.repo.db.transaction() as tx:
            tx.execute('UPDATE target_schema_migrations SET version=version-100')

    # A mode switch waits for in-flight runtime transactions, then refuses.

    def test_switch_waits_for_an_open_runtime_transaction(self):
        runtime = self.runtime()
        job = runtime.execution.schedule()[0]
        self.wall += timedelta(seconds=61)  # even with an expired lease the open transaction is protected
        held, done, timing = threading.Event(), threading.Event(), {}

        def hold(job_id):
            held.set()
            time.sleep(1.5)
        runtime.execution.claim_locked = hold
        runtime.execution.ownership = RuntimeOwnership(self.repo, ttl_seconds=60)  # real clock: the claim is allowed

        def claim():
            timing['claim'] = runtime.execution.claim('W', job['job_id'])
            timing['claim_done'] = time.monotonic()

        def switch():
            held.wait(10)
            try:
                RuntimeOwnership(self.repo, ttl_seconds=60).acquire('SHADOW')
                timing['switch'] = 'granted'
            except OwnershipRefused as exc:
                timing['switch'] = exc.code
            timing['switch_done'] = time.monotonic()
        threads = [threading.Thread(target=claim), threading.Thread(target=switch)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertIsNotNone(timing['claim'])
        self.assertIn(timing['switch'], ('ownership_held_by_RUNTIME_TEST', 'runtime_jobs_in_flight'))
        self.assertGreaterEqual(timing['switch_done'], timing['claim_done'])


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OwnershipOracleTests(OwnershipStandInTests):
    """Every case above on the real disposable Oracle test schema (LOCK TABLE share/exclusive)."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db
