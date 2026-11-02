"""B7 shadow: deliver committed SQLite changes to the target idempotently, compare, recover.

SQLite stays the system of record. Change capture (v11 triggers) records (table, key) in the same
SQLite transaction as the write; the deliverer re-reads the current committed row and upserts or
deletes it in the target. A redelivered event converges to the same state, so it never adds rows.
No collector runs and no provider API is called. Disabled until an operator enables it.
"""
import json
from contextlib import closing
from datetime import datetime, timedelta, timezone
from .target_db import TargetError, iso, utc
from .target_migration import LOAD, derive, open_source, reconcile, repair


class ShadowService:
    def __init__(self, storage, repo, clock=None, fault=None):
        self.storage, self.repo = storage, repo
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fault = fault or (lambda stage, event: None)

    def now(self):
        return utc(self.clock())

    def enable(self, enabled=True):
        """Explicit operator switch; capture starts with the next committed write."""
        with self.storage.connect() as db:
            db.execute('UPDATE shadow_settings SET enabled=?,updated_at=? WHERE singleton=1', (int(enabled), iso(self.now())))
        return self.status()

    def status(self):
        with self.storage.reader() as db:
            enabled = db.execute('SELECT enabled FROM shadow_settings WHERE singleton=1').fetchone()[0]
            counts = db.execute('''SELECT count(*),sum(delivered_at IS NULL),sum(delivered_at IS NULL AND last_error_code IS NOT NULL)
                FROM shadow_outbox''').fetchone()
        return {'enabled': bool(enabled), 'events': counts[0], 'pending': counts[1] or 0, 'failing': counts[2] or 0}

    def deliver(self, limit=500):
        now = self.now()
        with self.storage.reader() as db:
            events = [dict(r) for r in db.execute('''SELECT * FROM shadow_outbox WHERE delivered_at IS NULL
                AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY event_id LIMIT ?''', (iso(now), limit))]
        delivered = failed = 0
        with closing(open_source(self.storage.path)) as src:
            for event in events:
                try:
                    entries = derive(src, event['entity_table'], json.loads(event['entity_key']), now)
                    with self.repo.db.transaction() as tx:
                        for table, key_row, row, disposition in entries:
                            if row is not None and disposition == LOAD:
                                self.repo.upsert(tx, table, row)
                            elif row is not None and disposition == 'REJECTED_INVALID':
                                raise TargetError('shadow_row_invalid')
                            elif None not in key_row.values():
                                self.repo.delete(tx, table, key_row)  # deleted, expired or policy-excluded upstream
                        self.repo.upsert(tx, 'shadow_applied', dict(event_id=event['event_id'], entity_table=event['entity_table'],
                                         entity_key=event['entity_key'], operation=event['operation'], applied_at=iso(now)))
                    self.fault('after_target_commit', event)
                    with self.storage.connect() as db:
                        db.execute('UPDATE shadow_outbox SET delivered_at=?,attempts=attempts+1,last_error_code=NULL WHERE event_id=?',
                                   (iso(now), event['event_id']))
                    delivered += 1
                except TargetError as exc:
                    # Retryable: a missing parent, a busy or unreachable target, an invalid row awaiting review.
                    backoff = timedelta(seconds=min(3600, 5 * 2 ** min(event['attempts'], 10)))
                    with self.storage.connect() as db:
                        db.execute('UPDATE shadow_outbox SET attempts=attempts+1,next_attempt_at=?,last_error_code=? WHERE event_id=?',
                                   (iso(now + backoff), exc.code, event['event_id']))
                    failed += 1
        return dict(self.status(), delivered=delivered, failed=failed, http_requests=0)

    def compare(self):
        return reconcile(self.storage.path, self.repo, now=self.now(), source_storage=self.storage)

    def repair(self):
        return repair(self.storage.path, self.repo, now=self.now())
