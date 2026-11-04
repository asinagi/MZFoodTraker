"""ORACLE-RUNTIME-2 R3: exclusive write ownership of one target database (Shadow vs the Oracle runtime).

The target records one owning mode (IDLE, SHADOW, RUNTIME_TEST; RUNTIME_PRODUCTION is never granted)
with a mode epoch, and one lease row per participating process. Exclusion is enforced by the database,
not by process memory:

- Every Shadow or runtime write transaction first takes LOCK TABLE runtime_ownership IN SHARE MODE and
  verifies its mode, epoch and unexpired lease (guard). Share locks coexist, so runtime workers still run
  in parallel; the stand-in serializes all writers with BEGIN IMMEDIATE.
- A mode switch takes the table in EXCLUSIVE mode, so it waits for every in-flight guarded transaction,
  and later writers wait for the switch and then see the new mode and refuse. A switch is refused while
  another mode has a live lease, and away from a runtime mode while runtime jobs are unfinished.
- A process killed mid-run simply stops renewing; its lease expires and is never renewed afterwards, so its
  late writes are refused (a restarted process joins with a new lease). Job leases and fencing still decide
  every job.

Reads, status and comparisons need no ownership. Lease time is wall-clock process liveness, independent
of the domain clocks the services use.
"""
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from .target_db import TargetError
from .target_schema import TARGET_SCHEMA_VERSION

MODES = ('IDLE', 'SHADOW', 'RUNTIME_TEST', 'RUNTIME_PRODUCTION')
RUNTIME_MODES = ('RUNTIME_TEST', 'RUNTIME_PRODUCTION')
SCOPE = 'TARGET'


class OwnershipRefused(TargetError):
    pass


@dataclass(frozen=True)
class Lease:
    participant_id: str
    mode: str
    epoch: int


def _tx(cursor):
    from .oracle_execution import _Tx
    return cursor if hasattr(cursor, 'insert_absent') else _Tx(cursor)


class RuntimeOwnership:
    def __init__(self, repo, clock=None, ttl_seconds=120, fault=None):
        if not 10 <= ttl_seconds <= 3600:
            raise ValueError('Invalid ownership lease')
        self.repo, self.db = repo, repo.db
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.ttl = timedelta(seconds=ttl_seconds)
        self.fault = fault or (lambda stage: None)

    def now(self):
        return self.clock().astimezone(timezone.utc)

    def environment(self):
        settings = getattr(self.db, 'settings', None)
        return settings.environment if settings is not None else 'standin'

    # Checks and SQL

    def _allowed(self, mode):
        if mode not in MODES or mode == 'IDLE':
            raise OwnershipRefused('ownership_mode_invalid')
        if mode == 'RUNTIME_PRODUCTION':
            raise OwnershipRefused('runtime_production_disabled')
        environment = self.environment()
        if environment == 'production':
            raise OwnershipRefused('production_target_requires_approval')
        if mode == 'RUNTIME_TEST' and environment not in ('test', 'standin'):
            raise OwnershipRefused('runtime_test_requires_test_environment')
        if self.repo.schema_version() != TARGET_SCHEMA_VERSION:
            raise OwnershipRefused('target_schema_version_mismatch')

    @staticmethod
    def _lock(t, mode):
        if t.oracle:
            t.run('LOCK TABLE runtime_ownership IN %s MODE' % mode)

    @staticmethod
    def _row(t):
        return t.one('SELECT * FROM runtime_ownership WHERE owner_scope=:b_s', 'runtime_ownership', b_s=SCOPE)

    def _live(self, t, mode, epoch, exclude=None):
        rows = t.rows('''SELECT participant_id FROM runtime_participants WHERE owner_mode=:b_m AND mode_epoch=:b_e AND released_at IS NULL
            AND expires_at>:b_now''', b_m=mode, b_e=epoch, b_now=self.now())
        return [r['participant_id'] for r in rows if r['participant_id'] != exclude]

    @staticmethod
    def in_flight(t):
        """Unfinished jobs created by the Oracle runtime (only it writes execution_mappings)."""
        return int(t.scalar('''SELECT count(*) AS c FROM execution_jobs j WHERE j.status IN ('READY','RETRY_WAIT','CLAIMED','RUNNING')
            AND EXISTS (SELECT 1 FROM execution_mappings m WHERE m.job_id=j.job_id)'''))

    def _join(self, t, lease):
        now = self.now()
        values = dict(owner_mode=lease.mode, mode_epoch=lease.epoch, environment=self.environment(), renewed_at=now,
                      expires_at=now + self.ttl, released_at=None)
        if not t.update('runtime_participants', {'participant_id': lease.participant_id}, **values):
            t.insert('runtime_participants', dict(participant_id=lease.participant_id, joined_at=now, **values))
        return lease

    # Operations

    def acquire(self, mode, participant_id=None):
        """Join the current mode, or switch an idle/abandoned target to it. Returns a Lease or raises OwnershipRefused."""
        self._allowed(mode)
        pid = participant_id or 'OWNER_' + uuid.uuid4().hex
        with self.db.transaction() as cursor:  # fast path: the mode is already ours to join
            t = _tx(cursor)
            self._lock(t, 'SHARE')
            row = self._row(t)
            if row and row['owner_mode'] == mode:
                if mode == 'SHADOW' and self._live(t, mode, row['mode_epoch'], exclude=pid):
                    raise OwnershipRefused('shadow_already_active')
                return self._join(t, Lease(pid, mode, row['mode_epoch']))
        with self.db.transaction() as cursor:  # switch: waits for every in-flight guarded writer
            t = _tx(cursor)
            self._lock(t, 'EXCLUSIVE')
            row = self._row(t)
            if row is None:
                row = dict(owner_scope=SCOPE, owner_mode='IDLE', mode_epoch=0)
                t.insert('runtime_ownership', dict(row, changed_at=self.now(), changed_by=pid))
            if row['owner_mode'] == mode:  # another process switched first
                if mode == 'SHADOW' and self._live(t, mode, row['mode_epoch'], exclude=pid):
                    raise OwnershipRefused('shadow_already_active')
                return self._join(t, Lease(pid, mode, row['mode_epoch']))
            if row['owner_mode'] != 'IDLE' and self._live(t, row['owner_mode'], row['mode_epoch']):
                raise OwnershipRefused('ownership_held_by_' + row['owner_mode'])
            if row['owner_mode'] in RUNTIME_MODES and self.in_flight(t):
                raise OwnershipRefused('runtime_jobs_in_flight')
            epoch = row['mode_epoch'] + 1
            t.update('runtime_ownership', {'owner_scope': SCOPE}, owner_mode=mode, mode_epoch=epoch, changed_at=self.now(), changed_by=pid)
            lease = self._join(t, Lease(pid, mode, epoch))
            self.fault('before_switch_commit')
            return lease

    def guard(self, cursor, lease):
        """Inside a write transaction: the mode, epoch and lease must still be this writer's."""
        t = _tx(cursor)
        self._lock(t, 'SHARE')
        row = self._row(t)
        if row is None or row['owner_mode'] != lease.mode or row['mode_epoch'] != lease.epoch:
            raise OwnershipRefused('ownership_mode_changed')
        part = t.one('SELECT * FROM runtime_participants WHERE participant_id=:b_p', 'runtime_participants', b_p=lease.participant_id)
        if part is None or part['released_at'] is not None or part['mode_epoch'] != lease.epoch:
            raise OwnershipRefused('ownership_released')
        from .tracking_service import instant
        if instant(part['expires_at']) <= self.now():
            raise OwnershipRefused('ownership_expired')

    def renew(self, lease):
        """Extend an unexpired lease within the same mode epoch (never across a mode change or after expiry)."""
        with self.db.transaction() as cursor:
            t = _tx(cursor)
            self._lock(t, 'SHARE')
            row = self._row(t)
            if row is None or row['owner_mode'] != lease.mode or row['mode_epoch'] != lease.epoch:
                raise OwnershipRefused('ownership_mode_changed')
            part = t.one('SELECT * FROM runtime_participants WHERE participant_id=:b_p', 'runtime_participants', b_p=lease.participant_id)
            if part is None or part['released_at'] is not None:
                raise OwnershipRefused('ownership_released')
            from .tracking_service import instant
            if instant(part['expires_at']) <= self.now():
                raise OwnershipRefused('ownership_expired')
            self._join(t, lease)

    def release(self, lease):
        """Leave; the last participant returns the target to IDLE unless runtime jobs are unfinished."""
        with self.db.transaction() as cursor:
            t = _tx(cursor)
            self._lock(t, 'EXCLUSIVE')
            t.update('runtime_participants', {'participant_id': lease.participant_id}, released_at=self.now())
            row = self._row(t)
            if (row and row['owner_mode'] == lease.mode and row['mode_epoch'] == lease.epoch and not self._live(t, lease.mode, lease.epoch)
                    and not (lease.mode in RUNTIME_MODES and self.in_flight(t))):
                t.update('runtime_ownership', {'owner_scope': SCOPE}, owner_mode='IDLE', mode_epoch=lease.epoch + 1, changed_at=self.now(),
                         changed_by=lease.participant_id)

    @contextmanager
    def held(self, mode, participant_id=None):
        lease = self.acquire(mode, participant_id)
        try:
            yield lease
        finally:
            self.release(lease)

    def status(self):
        """Read-only: current mode, epoch, live leases and unfinished runtime jobs."""
        with self.db.transaction() as cursor:
            t = _tx(cursor)
            row = self._row(t) or dict(owner_mode='IDLE', mode_epoch=0)
            return {'mode': row['owner_mode'], 'mode_epoch': row['mode_epoch'], 'live_participants': len(self._live(t, row['owner_mode'], row['mode_epoch'])),
                    'runtime_jobs_in_flight': self.in_flight(t), 'environment': self.environment(), 'database_writes': 0}
