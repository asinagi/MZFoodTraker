"""ORACLE-CUTOVER-PREP-1 phase 4: Oracle -> SQLite reverse recovery, the rollback path after a cut-over.

Brings the allowed data and change history that exist only on the target back into a SQLite v11 database (a
restored backup or the frozen pre-cut-over file), so the SQLite system can resume without losing the Oracle period.

- Starts only from a quiet target (ownership IDLE, no unfinished runtime job) and holds the target's exclusive SHADOW
  lease for the whole run: no runtime worker, Shadow delivery or B7 load can write meanwhile. The only target write is
  this run's own progress row in migration_batches.
- Compares by the keys both stores share (FOOD_ID, alias, candidate, event, target and slot ids; ITEM_/SNAP_/INGEST_/
  METRIC_/MATCH_ keys, mention keys, plan and query ids, request keys). Target-only rows are inserted parent-first. A
  row already in SQLite is never overwritten unless its difference is confined to columns the target specification
  declares mutable, the destination is still the file a completed B7 load read (or the run resumes a recovery that
  started from such a file), and the operator asked for updates. Every other difference is a reported conflict,
  left alone; SQLite-only rows are never deleted.
- Retention holds: YouTube content past its expiry and Instagram captions past their window do not come back; NAVER
  has nothing stored. Links keep the target's state, so EXPIRED tombstones stay tombstones.
- Idempotent and resumable: insert-if-absent, updates set the target's values, chunks commit separately. A re-run
  after any failure converges and its plan shows nothing left. Target state SQLite cannot represent is listed as a
  loss with its reason. No provider API is called; reports carry counts and codes, never values.
"""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .contracts import SOURCES
from .ingestion import item_key
from .target_db import logical, utc
from .target_migration import Plan, fingerprint, open_source, writer_for
from .target_schema import BY_NAME

CAPTION_DAYS = SOURCES['instagram_graph']['policy']['retention_days']
CONTENT = 'source_contents'
# Parent-first (SQLite enforces foreign keys at once); content goes to the collector stores before staging refers to it.
ORDER = ('food_commands', 'food_entities', 'food_aliases', 'food_relations', 'food_candidates', 'food_events', 'tracking_targets',
         'tracking_commands', 'tracking_events', 'planner_queries', 'planner_plans', 'planner_query_targets', 'planner_plan_targets',
         'planner_commands', 'quota_reservations', 'quota_charges', 'execution_jobs', 'execution_runs', 'execution_calls', 'execution_outbox',
         'execution_results', 'execution_mappings', 'execution_leases', 'execution_checkpoints', 'execution_item_receipts', 'execution_blocks',
         'tracking_slots', CONTENT, 'ingest_runs', 'source_items', 'item_snapshots', 'run_items', 'quality_results', 'item_query_matches',
         'food_mentions', 'metric_observations', 'entity_change_effects', 'aggregate_invalidations')
# Runtime-owned target tables with a SQLite table of the same name; these target-only columns are dropped on the way back.
RUNTIME_DROPPED = {'tracking_commands': (), 'tracking_events': ('event_sequence',), 'planner_plan_targets': (), 'planner_commands': (),
                   'execution_mappings': (), 'execution_leases': (), 'execution_checkpoints': (), 'execution_item_receipts': (),
                   'execution_blocks': (), 'quota_charges': (),
                   'tracking_slots': ('plan_id', 'job_id', 'status', 'claimed_at', 'completed_at', 'cancellation_reason')}
RENAMED = {'quota_charges': {'resource_code': 'resource'}}
# Loaded tables' target columns derived on load (rebuilt from SQLite, never written back as such).
DERIVED = {'food_mentions': ('source_code',), 'food_events': ('event_sequence',), 'food_candidates': ('source_item_key',)}
LOAD_BOOKKEEPING = 'target-only load bookkeeping'
NOT_RECOVERED = {
    'runtime_ownership': 'write ownership of the target itself (bookkeeping without a SQLite counterpart)',
    'runtime_participants': 'process leases of the target writers (bookkeeping without a SQLite counterpart)',
    'quota_usage_ledger': "a copy of SQLite's own legacy call tables; the Oracle period's usage returns as quota_reservations/quota_charges",
    'quota_accounts': 'usage verification stamps of the Oracle runtime (no SQLite counterpart)',
    'legacy_key_map': LOAD_BOOKKEEPING, 'migration_batches': LOAD_BOOKKEEPING, 'migration_rejections': LOAD_BOOKKEEPING,
    'shadow_applied': 'target-only Shadow bookkeeping'}
RSS_ONLY_ON_SQLITE = ('rss_classifications', 'rss_classification_history')
LOSSES = {'tracking_slots': 'slot lifecycle (status, claim/completion times, cancellation reason, plan and job) has no SQLite column',
          'instagram_caption_tags': 'original hashtag spellings: the target keeps normalized names only',
          'instagram_caption_captured_at': 'derived from the caption window end, so the expiry instant is unchanged',
          'youtube_payload_text': 'payload re-serialized canonically; its original SHA-256 stays in item_snapshots.payload_hash'}


class RecoveryRefused(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _operational(path):
    """The operational file by name, or the configured DATABASE_PATH (as the observation writer's refusal decides it)."""
    import os
    from .paths import DEFAULT_DATABASE, database_path
    if path.name == DEFAULT_DATABASE.name:
        return True
    for configured in {DEFAULT_DATABASE.resolve(), Path(database_path(os.environ)).resolve()}:
        if path == configured or (path.exists() and configured.exists() and os.path.samefile(path, configured)):
            return True
    return False


def _key(spec, row):
    return json.dumps([row[k] for k in spec.key], ensure_ascii=False, default=str)


def _value(row, name):
    return json.dumps(row.get(name), ensure_ascii=False, sort_keys=True, default=str)


class Recovery:
    def __init__(self, destination, repo, now=None, ownership=None, allow_operational=False, chunk=500, fault=None):
        from .runtime_ownership import RuntimeOwnership
        self.destination = Path(destination).resolve()
        if not allow_operational and _operational(self.destination):
            raise RecoveryRefused('operational_database_requires_approval')  # the real rollback needs the separately approved switch
        self.repo, self.now = repo, utc(now or datetime.now(timezone.utc))
        self.ownership = ownership or RuntimeOwnership(repo)
        self.chunk, self.fault = chunk, fault or (lambda stage, table, n: None)

    # Preconditions and authorization

    def preconditions(self):
        from .migrations import LATEST_VERSION, schema_version
        status = self.ownership.status()
        reasons = [] if status['mode'] == 'IDLE' else ['ownership_not_idle:' + status['mode']]
        reasons += ['runtime_jobs_in_flight'] if status['runtime_jobs_in_flight'] else []
        reasons += [] if schema_version(self.destination) == LATEST_VERSION else ['destination_schema_not_current']
        return {'mode': status['mode'], 'runtime_jobs_in_flight': status['runtime_jobs_in_flight'], 'ready': not reasons, 'reasons': reasons}

    def baseline(self):
        """Updates are authorized only on the file a completed B7 load read, or when resuming a recovery that started from one."""
        wal = Path(str(self.destination) + '-wal')
        current = fingerprint(self.destination)
        batches = self.repo.rows('migration_batches')
        loaded = {b['source_fingerprint'] for b in batches if b['status'] == 'COMPLETED' and b['batch_key'].startswith('B7_')}
        resumed = [b for b in batches if b['batch_key'].startswith('R7_') and b['status'] == 'RUNNING']
        wal_empty = not wal.exists() or wal.stat().st_size == 0
        return {'fingerprint': current, 'wal_empty': wal_empty, 'matches_completed_load': current in loaded,
                'resumes_recovery': bool(resumed), 'verified': wal_empty and (current in loaded or bool(resumed))}

    # Classification shared by plan() and run()

    def _sqlite_rows(self, src, plan, table):
        """SQLite rows of one table in the target's logical shape (B7's own mapping for loaded tables), by key."""
        if table == CONTENT:
            return {r['payload_ref']: logical(CONTENT, r) for r in plan._source_rows(CONTENT)}
        spec = BY_NAME[table]
        if spec.source_table:
            return {_key(spec, r): logical(table, r) for r in plan._source_rows(table)}
        columns, rename = {r[1] for r in src.execute('PRAGMA table_info(%s)' % table)}, RENAMED.get(table, {})
        out = {}
        for raw in src.execute('SELECT * FROM %s' % table):
            raw = dict(raw)
            row = logical(table, {c.name: raw.get(rename.get(c.name, c.name)) for c in spec.columns if rename.get(c.name, c.name) in columns})
            out[_key(spec, row)] = row
        return out

    def _classify(self, table, mine, theirs):
        spec = BY_NAME[table]
        dropped = set(RUNTIME_DROPPED.get(table, ())) | set(DERIVED.get(table, ()))
        result = {'only': [], 'same': 0, 'updates': [], 'conflicts': [], 'sqlite_only': [k for k in mine if k not in theirs], 'skipped': {}}
        for key, row in theirs.items():
            if key not in mine:
                result['only'].append(row)
                continue
            changed = sorted(c.name for c in spec.columns if c.name not in dropped and _value(mine[key], c.name) != _value(row, c.name))
            if table == CONTENT and changed and mine[key]['refreshed_at'] == row['refreshed_at']:
                changed = []  # same observation: caption or content purges are each store's own retention, not a change
            if not changed:
                result['same'] += 1
            elif set(changed) <= set(spec.mutable):
                result['updates'].append((row, changed))
            else:
                result['conflicts'].append((key, changed))
        if table == CONTENT:
            keep = []
            for row in result['only']:
                reason = ('rss_content_written_on_target' if row['payload_ref'].startswith('items:') else
                          'expired_content' if row['expires_at'] and utc(row['expires_at']) <= self.now else None)
                if reason:
                    result['skipped'][reason] = result['skipped'].get(reason, 0) + 1
                else:
                    keep.append(row)
            result['only'] = keep
        return result

    def _work(self):
        with closing(open_source(self.destination)) as src:
            plan = Plan(src, self.now)
            self._items = {item_key('google_trends_rss', r['source_id']): r['id'] for r in src.execute('SELECT id,source_id FROM items')}
            work = {}
            for table in ORDER:
                theirs = {(r['payload_ref'] if table == CONTENT else _key(BY_NAME[table], r)): r for r in self.repo.rows(table)}
                work[table] = self._classify(table, self._sqlite_rows(src, plan, table), theirs)
                work[table]['target'] = len(theirs)
            for table in RSS_ONLY_ON_SQLITE:
                mine = self._sqlite_rows(src, plan, table)
                work[table] = {'only': [r for r in self.repo.rows(table) if _key(BY_NAME[table], r) not in mine]}
        return work

    def plan(self, work=None):
        """Classify every recoverable row and list what cannot come back; writes nothing to either store."""
        work = work or self._work()
        out = {'tables': {}, 'conflicts': {}, 'losses': {}, 'not_recovered': {}, 'contents_not_recovered': work[CONTENT]['skipped']}
        for table in ORDER:
            w = work[table]
            out['tables'][table] = {'target': w['target'], 'target_only': len(w['only']), 'same': w['same'], 'mutable_updates': len(w['updates']),
                                    'conflicts': len(w['conflicts']), 'sqlite_only': len(w['sqlite_only'])}
            if w['conflicts']:
                out['conflicts'][table] = sorted({c for _, cols in w['conflicts'] for c in cols})
        held = [r for r in work[CONTENT]['only'] if r['source_code'] == 'instagram_graph' and self._caption(r) is not None]
        youtube = [r for r in work[CONTENT]['only'] if r['source_code'] == 'youtube']
        for name, rows in (('tracking_slots', work['tracking_slots']['only']), ('instagram_caption_tags', held),
                           ('instagram_caption_captured_at', held), ('youtube_payload_text', youtube)):
            if rows:
                out['losses'][name] = {'rows': len(rows), 'reason': LOSSES[name]}
        for table in RSS_ONLY_ON_SQLITE:
            if work[table]['only']:
                out['losses'][table] = {'rows': len(work[table]['only']), 'reason': 'RSS discovery rows written on the target have no SQLite store'}
        for table, reason in NOT_RECOVERED.items():
            out['not_recovered'][table] = {'rows': self.repo.count(table), 'reason': reason}
        totals = out['tables'].values()
        out['summary'] = {k: sum(t[k] for t in totals) for k in ('target_only', 'mutable_updates', 'conflicts', 'sqlite_only')}
        return out

    # Apply

    def run(self, apply_updates=False):
        """Plan, then write target-only rows (and authorized mutable updates) into SQLite; returns the plans and the writes."""
        ready = self.preconditions()
        if not ready['ready']:
            raise RecoveryRefused(ready['reasons'][0])
        with self.ownership.held('SHADOW') as lease:
            self._checkpoint()  # committed WAL frames into the file, so the fingerprint is the database's state
            base = self.baseline()
            authorized = apply_updates and base['verified']
            writer = writer_for(self.repo, self.ownership, lease)
            if authorized and not base['resumes_recovery']:
                with writer() as tx:  # the starting file, so a resumed run keeps its authorization after partial writes
                    self.repo.upsert(tx, 'migration_batches', dict(batch_key='R7_' + base['fingerprint'][:24], table_name='*', source_fingerprint=base['fingerprint'],
                        status='RUNNING', last_key=None, loaded_rows=0, excluded_rows=0, rejected_rows=0, started_at=self.now.isoformat(), finished_at=None))
            work = self._work()
            before = self.plan(work)
            writes = self._write(work, authorized)
            after = self.plan()
            if authorized:
                running = [b for b in self.repo.rows('migration_batches') if b['batch_key'].startswith('R7_') and b['status'] == 'RUNNING']
                with writer() as tx:  # read first: the stand-in serializes every transaction
                    for batch in running:
                        self.repo.upsert(tx, 'migration_batches', dict(batch, status='COMPLETED', finished_at=self.now.isoformat(),
                                                                       loaded_rows=writes['inserted'] + writes['content_rows'] + writes['updated']))
        left = after['summary']['target_only'] - writes['ignored']
        return {'preconditions': ready, 'baseline': base, 'updates_requested': apply_updates, 'updates_applied': authorized,
                'plan_before': before, 'writes': writes, 'plan_after': after, 'http_requests': 0,
                'converged': left == 0 and (not authorized or after['summary']['mutable_updates'] == 0)}

    def _checkpoint(self):
        with closing(sqlite3.connect(self.destination, timeout=30)) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')

    def _write(self, work, authorized):
        counts = {'inserted': 0, 'content_rows': 0, 'updated': 0, 'ignored': 0}
        for table in ORDER:
            rows, updates = self._ordered(table, work[table]['only']), work[table]['updates'] if authorized else []
            for start in range(0, len(rows), self.chunk):
                with closing(sqlite3.connect(self.destination, timeout=30)) as db:
                    db.execute('PRAGMA foreign_keys=ON')
                    with db:
                        for n, row in enumerate(rows[start:start + self.chunk]):
                            self.fault('row', table, start + n)
                            written = self._insert_content(db, row) if table == CONTENT else self._insert(db, table, row)
                            counts['content_rows' if table == CONTENT else 'inserted'] += written
                            counts['ignored'] += not written  # a SQLite unique key already held by another row: left for review
                self.fault('after_commit', table, start + self.chunk)
            if updates:
                with closing(sqlite3.connect(self.destination, timeout=30)) as db:
                    db.execute('PRAGMA foreign_keys=ON')
                    with db:
                        for row, changed in updates:
                            counts['updated'] += self._update_content(db, row) if table == CONTENT else self._update(db, table, row, changed)
        return counts

    @staticmethod
    def _ordered(table, rows):
        """Rows a SQLite foreign key inside the same table needs first: merge targets, reversed events, append order."""
        if table in ('food_events', 'tracking_events'):
            return sorted(rows, key=lambda r: (r['event_sequence'] is None, r['event_sequence'] or 0, r['occurred_at'], r['event_id']))
        if table != 'food_entities':
            return rows
        done, pending, out, known = set(), list(rows), [], {r['food_id'] for r in rows}
        while pending:
            ready = [r for r in pending if r['merged_into'] is None or r['merged_into'] in done or r['merged_into'] not in known] or pending
            out += ready
            done |= {r['food_id'] for r in ready}
            pending = [r for r in pending if r['food_id'] not in done]
        return out

    def _sqlite_row(self, table, row):
        spec = BY_NAME[table]
        if spec.source_table:
            out = {c.src: row[c.name] for c in spec.columns if c.name not in DERIVED.get(table, ())}
            if table == 'food_candidates':
                out['source_item_id'] = self._items.get(row['source_item_key']) if row['source_item_key'] else None
            return out
        rename = RENAMED.get(table, {})
        return {rename.get(c.name, c.name): row[c.name] for c in spec.columns if c.name not in RUNTIME_DROPPED[table]}

    def _insert(self, db, table, row):
        values = self._sqlite_row(table, row)
        return db.execute('INSERT OR IGNORE INTO %s (%s) VALUES (%s)' % (table, ','.join(values), ','.join('?' * len(values))),
                          tuple(values.values())).rowcount

    def _update(self, db, table, row, changed):
        spec, values = BY_NAME[table], self._sqlite_row(table, row)
        name = {c.name: (c.src if spec.source_table else RENAMED.get(table, {}).get(c.name, c.name)) for c in spec.columns}
        sets, keys = [name[c] for c in changed], [name[k] for k in spec.key]
        return db.execute('UPDATE %s SET %s WHERE %s' % (table, ','.join('%s=?' % s for s in sets), ' AND '.join('%s=?' % k for k in keys)),
                          tuple(values[s] for s in sets) + tuple(values[k] for k in keys)).rowcount

    def _caption(self, content):
        """The caption the destination may hold now, or None (purged, window closed or never held)."""
        if content['field_purged_at'] or not content['field_expires_at'] or utc(content['field_expires_at']) <= self.now:
            return None
        return json.loads(content['content_json']).get('caption')

    def _instagram(self, content):
        body, caption = json.loads(content['content_json']), self._caption(content)
        window = utc(content['field_expires_at']) if content['field_expires_at'] else None
        captured = (window - timedelta(days=CAPTION_DAYS)).isoformat() if caption is not None and window else None
        purged = content['field_purged_at'] or (content['field_expires_at'] if caption is None and window else None)
        return body, caption, captured, purged

    def _tags(self, db, external, content, body, caption):
        db.execute('DELETE FROM instagram_caption_tags WHERE media_id=?', (external,))
        for tag in (body.get('caption_tags') or []) if caption is not None else ():
            db.execute('INSERT INTO instagram_caption_tags VALUES (?,?,?,?,?,1)',
                       (external, tag, json.dumps([tag], ensure_ascii=False), content['captured_at'], content['refreshed_at']))

    def _insert_content(self, db, content):
        kind, _, external = content['payload_ref'].partition(':')
        if kind == 'youtube_videos':
            n = db.execute('INSERT OR IGNORE INTO youtube_videos (video_id,payload,collected_at,refreshed_at,expires_at) VALUES (?,?,?,?,?)',
                           (external, content['content_json'], content['captured_at'], content['refreshed_at'], content['expires_at'])).rowcount
            db.execute('INSERT OR IGNORE INTO youtube_video_provenance (video_id,origin,verified_at,run_id) VALUES (?,?,?,NULL)',
                       (external, content['origin'], content['refreshed_at']))
            return n
        body, caption, captured, purged = self._instagram(content)
        n = db.execute('''INSERT OR IGNORE INTO instagram_media (id,published_at,first_seen,last_seen,media_type,permalink,source,caption,
            caption_captured_at,caption_purged_at,origin) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                       (external, body.get('published_at'), content['captured_at'], content['refreshed_at'], body.get('media_type'),
                        body.get('permalink'), body.get('source', 'instagram_graph'), caption, captured, purged, content['origin'])).rowcount
        if n:
            self._tags(db, external, content, body, caption)
        return n

    def _update_content(self, db, content):
        """A refresh the runtime observed after the cut-over; UPDATE in place (a REPLACE would cascade-delete search links)."""
        kind, _, external = content['payload_ref'].partition(':')
        if kind == 'youtube_videos':
            n = db.execute('UPDATE youtube_videos SET payload=?,refreshed_at=?,expires_at=? WHERE video_id=?',
                           (content['content_json'], content['refreshed_at'], content['expires_at'], external)).rowcount
            db.execute('UPDATE youtube_video_provenance SET origin=?,verified_at=? WHERE video_id=?', (content['origin'], content['refreshed_at'], external))
            return n
        body, caption, captured, purged = self._instagram(content)
        n = db.execute('''UPDATE instagram_media SET published_at=?,last_seen=?,media_type=?,permalink=?,caption=?,caption_captured_at=?,
            caption_purged_at=?,origin=? WHERE id=?''', (body.get('published_at'), content['refreshed_at'], body.get('media_type'), body.get('permalink'),
                                                        caption, captured, purged, content['origin'], external)).rowcount
        self._tags(db, external, content, body, caption)
        return n


def recover(destination, repo, now=None, apply_updates=False, **options):
    return Recovery(destination, repo, now, **options).run(apply_updates)


def plan(destination, repo, now=None, **options):
    """Dry run: the classification, the losses and whether a run could start and update (no write anywhere)."""
    recovery = Recovery(destination, repo, now, **options)
    return dict(recovery.plan(), preconditions=recovery.preconditions(), baseline=recovery.baseline())
