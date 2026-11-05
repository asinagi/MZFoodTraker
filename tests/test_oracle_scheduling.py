"""ORACLE-RUNTIME-2 R2: tracking slots and schedule advancement on the target schema.

The SQLite ExecutionService.schedule() and the Oracle runtime's schedule() run from the same B7-loaded
state and instant; targets (next_due_at, revision), slots and jobs are compared. Target lifecycle
decisions (pause, resume, interval, alias changes) are made on SQLite and delivered as Shadow would.
"""
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from food_trends.execution_mock import MockTransport, mock_controller
from food_trends.oracle_execution import OracleExecution, oracle_mock_controller
from food_trends.quota import daily_window
from food_trends.target_db import logical
from food_trends.target_migration import migrate
from food_trends.tracking_service import instant
import test_oracle_execution as s2
from oracle_support import ORACLE_READY, SKIP_REASON, oracle_target, reset_target_schema
from test_oracle_planner import sync


class Boom(Exception):
    pass


class OracleSchedulingStandInTests(unittest.TestCase):
    setUp_base = s2.OracleExecutionStandInTests.setUp_base
    setUp = s2.OracleExecutionStandInTests.setUp
    tearDown = s2.OracleExecutionStandInTests.tearDown
    make_target = s2.OracleExecutionStandInTests.make_target
    create_food = s2.OracleExecutionStandInTests.create_food
    cmd = s2.OracleExecutionStandInTests.cmd
    register = s2.OracleExecutionStandInTests.register
    active = s2.OracleExecutionStandInTests.active
    change = s2.OracleExecutionStandInTests.change
    advance = s2.OracleExecutionStandInTests.advance

    def load(self, *sources):
        self.targets = {s: self.active(s) for s in sources}
        migrate(self.storage.path, self.repo, now=self.now)

    def controller(self, transport=None):
        return oracle_mock_controller(self.repo, self.tracking.accounts, lambda: self.now, transport or MockTransport())

    def sqlite_controller(self):
        return mock_controller(self.storage, self.planner, MockTransport())

    def slots(self):
        return self.repo.rows('tracking_slots')

    def sqlite_rows(self, sql):
        with self.storage.reader() as db:
            return [dict(r) for r in db.execute(sql)]

    def slot_keys(self, rows):
        return sorted((r['target_id'], r['generation'], r['food_revision'], instant(r['due_slot'])) for r in rows)

    def targets_equal(self):
        with self.storage.reader() as db:
            left = sorted(json.dumps(logical('tracking_targets', dict(r)), sort_keys=True) for r in db.execute('SELECT * FROM tracking_targets'))
        right = sorted(json.dumps(r, sort_keys=True) for r in self.repo.rows('tracking_targets'))
        self.assertEqual(left, right)

    def jobs(self, rows):
        return sorted((r['query_id'], r['due_slot'], r.get('mode') or r.get('exec_mode'), r['status'], r['execution_generation'], r['error_kind'])
                      for r in rows)

    def drain(self, controller):
        while controller.run_once() is not None:
            pass

    # R2-6, 10: slots, next_due and jobs agree with SQLite; a second pass creates nothing.

    def test_schedule_matches_sqlite_and_is_not_repeated(self):
        self.load('youtube', 'instagram_graph', 'naver_datalab')
        sqlite, oracle = self.sqlite_controller(), self.controller()
        left, right = sqlite.execution.schedule(), oracle.execution.schedule()
        self.assertEqual(self.jobs(right), self.jobs(left))
        self.assertEqual(self.slot_keys(self.slots()), self.slot_keys(self.sqlite_rows('SELECT * FROM tracking_slots')))
        self.assertEqual({s['status'] for s in self.slots()}, {'SCHEDULED'})
        self.targets_equal()
        self.assertEqual(oracle.execution.schedule(), sqlite.execution.schedule())  # nothing due twice
        self.assertEqual(len(self.slots()), 3)
        mappings = sorted((r['plan_id'], r['target_id'], r['generation']) for r in self.repo.rows('execution_mappings'))
        self.assertEqual(mappings, sorted((r['plan_id'], r['target_id'], r['generation']) for r in self.sqlite_rows('SELECT * FROM execution_mappings')))
        self.drain(sqlite)
        self.drain(oracle)
        self.assertEqual(self.jobs(oracle.execution.status()['items']), self.jobs(sqlite.execution.status()['items']))
        self.targets_equal()  # last_success_at included
        jobs = {j['job_id']: j for j in self.repo.rows('execution_jobs')}
        for slot in self.slots():
            self.assertEqual((slot['status'], jobs[slot['job_id']]['status']), ('COMPLETED', 'SUCCEEDED'))
            self.assertIsNotNone(slot['claimed_at'])
            target = next(t for t in self.repo.rows('tracking_targets') if t['target_id'] == slot['target_id'])
            self.assertGreater(instant(target['next_due_at']), instant(slot['due_slot']))
        # The next interval schedules a new slot, never the old one again.
        self.now += timedelta(days=1, seconds=1)
        self.assertEqual(self.jobs(oracle.execution.schedule()), self.jobs(sqlite.execution.schedule()))
        self.assertEqual(self.slot_keys(self.slots()), self.slot_keys(self.sqlite_rows('SELECT * FROM tracking_slots')))
        self.targets_equal()

    # R2-7: concurrent schedulers and workers.

    def test_concurrent_schedulers_create_one_slot_and_job(self):
        self.load('youtube')
        results, errors, barrier = {}, [], threading.Barrier(2)

        def run(name):
            try:
                controller = self.controller()
                barrier.wait(10)
                results[name] = controller.execution.schedule()
            except Exception as exc:  # recorded and asserted below
                errors.append(repr(exc))
        threads = [threading.Thread(target=run, args=(n,)) for n in ('A', 'B')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(self.repo.count('execution_jobs'), 3)  # one per query (three approved aliases)
        self.assertEqual(len(self.slots()), 1)  # one target, one due slot
        # Either both saw the same due plans (and got the same jobs) or the later one saw nothing due.
        all_jobs = {j['job_id'] for j in self.repo.rows('execution_jobs')}
        self.assertTrue(all(not r or {j['job_id'] for j in r} == all_jobs for r in results.values()), results)
        target = self.repo.rows('tracking_targets')[0]
        self.assertEqual(instant(target['next_due_at']) - instant(self.slots()[0]['due_slot']), timedelta(seconds=target['interval_seconds']))
        # Two workers on one job: one claim, one claimed_at.
        job = self.slots()[0]['job_id']
        claims = []
        workers = [threading.Thread(target=lambda n=n: claims.append(OracleExecution(self.repo, self.tracking.accounts, lambda: self.now).claim(n, job)))
                   for n in ('W1', 'W2')]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        self.assertEqual(sum(c is not None for c in claims), 1)
        slot = self.slots()[0]
        self.assertEqual((slot['status'], slot['claimed_at'] is not None), ('CLAIMED', True))

    # R2-8: a failure inside the scheduling transaction leaves neither a job nor a moved due time.

    def test_failed_scheduling_transaction_rolls_back_everything(self):
        self.load('youtube')
        before = self.repo.rows('tracking_targets')
        for stage in ('after_schedule_slot', 'before_enqueue_commit'):
            with self.subTest(stage=stage):
                controller = self.controller()

                def fault(name, stage=stage):
                    if name == stage:
                        raise Boom(stage)
                controller.execution.fault = fault
                with self.assertRaises(Boom):
                    controller.execution.schedule()
                for table in ('execution_jobs', 'execution_outbox', 'execution_runs', 'execution_mappings', 'tracking_slots'):
                    self.assertEqual(self.repo.count(table), 0, table)
                self.assertEqual(self.repo.rows('tracking_targets'), before)
        jobs = self.controller().execution.schedule()
        self.assertEqual((len(jobs), len(self.slots())), (3, 1))

    # R2-9: pause and resume.

    def test_pause_cancels_open_slot_and_resume_schedules_a_new_generation(self):
        self.load('youtube')
        sqlite, oracle = self.sqlite_controller(), self.controller()
        sqlite.execution.schedule()
        oracle.execution.schedule()
        paused = self.change('pause', self.tracking.target(self.targets['youtube']['target_id']))
        sync(self.storage, self.repo, self.now)
        self.drain(sqlite)
        self.drain(oracle)
        self.assertEqual(self.jobs(oracle.execution.status()['items']), self.jobs(sqlite.execution.status()['items']))
        self.assertEqual({j['status'] for j in self.repo.rows('execution_jobs')}, {'CANCELLED'})
        self.assertEqual([(s['status'], s['cancellation_reason']) for s in self.slots()], [('CANCELLED', 'TARGET_PAUSED')])
        self.assertEqual(oracle.execution.schedule(), [])  # paused: nothing planned
        self.change('resume', paused, approved=True)
        sync(self.storage, self.repo, self.now)
        self.assertEqual(self.jobs(oracle.execution.schedule()), self.jobs(sqlite.execution.schedule()))
        self.assertEqual(self.slot_keys(self.slots()), self.slot_keys(self.sqlite_rows('SELECT * FROM tracking_slots')))
        generations = sorted(s['generation'] for s in self.slots())
        self.assertEqual(generations, [generations[0], generations[0] + 2])  # pause and resume each start a generation
        self.targets_equal()

    def test_alias_change_and_stale_generation_invalidate_existing_plans(self):
        self.load('youtube')
        oracle = self.controller()
        jobs = oracle.execution.schedule()
        alias = next(a for a in self.foods.food(self.food['food_id'])['approved_aliases'] if a['surface_form'] == '두바이쫀득쿠키')
        self.foods.command('alias', dict(request_key='revoke', alias_id=alias['alias_id'], expected_revision=alias['revision'], action='revoke',
                                         approved=True, actor_ref='reviewer', reason='잘못된 별칭'))
        sync(self.storage, self.repo, self.now)
        self.drain(oracle)
        self.assertEqual({(j['status'], j['error_kind']) for j in self.repo.rows('execution_jobs')}, {('CANCELLED', 'NO_ACTIVE_TARGETS')})
        self.assertEqual([(s['status'], s['cancellation_reason']) for s in self.slots()], [('CANCELLED', 'STALE_FOOD_REVISION')])
        # An old plan of an earlier generation is refused, not revived.
        self.change('interval', self.tracking.target(self.targets['youtube']['target_id']), interval_seconds=43200)
        sync(self.storage, self.repo, self.now)
        blocked = oracle.execution.enqueue([j['plan_id'] for j in jobs])
        self.assertEqual({b['status'] for b in blocked}, {'BLOCKED'})
        self.assertEqual(self.repo.count('execution_jobs'), len(jobs))

    # R2-11: UTC instants, KST budget days, Los Angeles DST days.

    def test_kst_midnight_and_los_angeles_dst_boundaries(self):
        # 2026-11-01 is the US DST end: the Los Angeles budget day is 25 hours. 14:59:59Z is 23:59:59 KST.
        self.now = datetime(2026, 11, 1, 14, 59, 59, tzinfo=timezone.utc)
        self.load('youtube', 'naver_datalab')
        sqlite, oracle = self.sqlite_controller(), self.controller()
        left, right = sqlite.execution.schedule(), oracle.execution.schedule()
        self.assertEqual(self.jobs(right), self.jobs(left))
        naver = next(j for j in right if j['source_id'] == 'naver_datalab')
        self.assertEqual(naver['due_slot'], '2026-10-31T15:00:00.000000+00:00')  # KST 2026-11-01 00:00
        self.drain(sqlite)
        self.drain(oracle)
        with self.storage.reader() as db:
            sqlite_windows = sorted((r['resource'], instant(r['window_start']), instant(r['window_end'])) for r in db.execute('SELECT * FROM quota_charges'))
        windows = sorted((r['resource_code'], instant(r['window_start']), instant(r['window_end'])) for r in self.repo.rows('quota_charges'))
        self.assertEqual(windows, sqlite_windows)
        youtube = [w for w in windows if w[0] == 'youtube.search_calls'][0]
        self.assertEqual((youtube[1].isoformat(), youtube[2] - youtube[1]), ('2026-11-01T07:00:00+00:00', timedelta(hours=25)))
        kst = [w for w in windows if w[0] == 'naver.calls'][0]
        self.assertEqual((kst[1].isoformat(), kst[2] - kst[1]), ('2026-10-31T15:00:00+00:00', timedelta(hours=24)))
        # One second later the KST day and the NAVER slot change; the spring-forward day is 23 hours.
        self.assertEqual(daily_window(datetime(2026, 11, 1, 15, tzinfo=timezone.utc), 'Asia/Seoul')['day'], '2026-11-02')
        spring = daily_window(datetime(2027, 3, 14, 20, tzinfo=timezone.utc), 'America/Los_Angeles')
        self.assertEqual(instant(spring['end']) - instant(spring['start']), timedelta(hours=23))
        # Due times advance by exact UTC seconds across the DST change.
        for slot in self.slots():
            target = next(t for t in self.repo.rows('tracking_targets') if t['target_id'] == slot['target_id'])
            self.assertEqual((instant(target['next_due_at']) - instant(slot['due_slot'])).total_seconds() % target['interval_seconds'], 0)
        self.targets_equal()


@unittest.skipUnless(ORACLE_READY, SKIP_REASON)
class OracleSchedulingTests(OracleSchedulingStandInTests):
    """Every case above on the real disposable Oracle test schema."""

    def make_target(self):
        db = oracle_target()
        self.addCleanup(db.close)
        reset_target_schema(db)
        return db
