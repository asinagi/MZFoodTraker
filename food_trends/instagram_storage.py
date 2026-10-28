"""Additive SQLite schema and observation metrics; no changes to RSS tables."""
import json
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from .storage import utcnow
from .instagram_analysis import HashtagAnalysis
from .contracts import envelope, encode

KST = timezone(timedelta(hours=9))


class InstagramStorage:
    def __init__(self, storage, user_id, caption_retention_days=30):
        self.storage, self.user_id = storage, user_id
        self.analysis = HashtagAnalysis(storage, user_id, caption_retention_days)

    def state(self, key, value=None):
        with (self.storage.connect() if value is not None else self.storage.reader()) as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO instagram_state VALUES (?,?,?)',
                           (self.user_id, key, json.dumps(value)))
                return value
            row = db.execute('SELECT value FROM instagram_state WHERE user_id=? AND key=?', (self.user_id, key)).fetchone()
            return json.loads(row[0]) if row else None

    def acquire(self):
        owner, now = uuid.uuid4().hex, utcnow()
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            lease = db.execute('SELECT * FROM instagram_leases WHERE user_id=?', (self.user_id,)).fetchone()
            if lease and lease['expires_at'] > now:
                return None
            db.execute('INSERT OR REPLACE INTO instagram_leases VALUES (?,?,?)', (self.user_id, owner, expiry))
            db.execute("UPDATE instagram_runs SET status='interrupted',finished_at=?,error_type='interrupted' "
                       "WHERE status='running' AND hashtag_id IN (SELECT id FROM instagram_hashtags WHERE user_id=?)",
                       (now, self.user_id))
        return owner

    def renew(self, owner):
        expiry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with self.storage.connect() as db:
            return db.execute('UPDATE instagram_leases SET expires_at=? WHERE user_id=? AND owner=?',
                              (expiry, self.user_id, owner)).rowcount == 1

    def release(self, owner):
        with self.storage.connect() as db:
            db.execute('DELETE FROM instagram_leases WHERE user_id=? AND owner=?', (self.user_id, owner))

    def tags(self, candidate_id=None, active_only=False):
        sql = 'SELECT h.* FROM instagram_hashtags h'
        args = []
        if candidate_id is not None:
            sql += ' JOIN instagram_candidate_hashtags c ON c.hashtag_id=h.id AND c.candidate_id=?'
            args.append(candidate_id)
        sql += ' WHERE h.user_id=?'
        args.append(self.user_id)
        if active_only:
            sql += ' AND h.active=1'
        with self.storage.reader() as db:
            return [dict(r) for r in db.execute(sql + ' ORDER BY h.id', args)]

    def tag(self, tag_id):
        return next((h for h in self.tags() if h['id'] == tag_id), None)

    def register(self, name, candidate_id=None):
        with self.storage.connect() as db:
            db.execute('INSERT OR IGNORE INTO instagram_hashtags(user_id,name,created_at) VALUES (?,?,?)',
                       (self.user_id, name, utcnow()))
            tag = dict(db.execute('SELECT * FROM instagram_hashtags WHERE user_id=? AND name=?', (self.user_id, name)).fetchone())
            if candidate_id is not None:
                db.execute('INSERT OR IGNORE INTO instagram_candidate_hashtags VALUES (?,?)', (candidate_id, tag['id']))
        return tag

    def approve(self, tag_id, instagram_id=None):
        now = utcnow()
        with self.storage.connect() as db:
            db.execute('UPDATE instagram_hashtags SET instagram_id=coalesce(?,instagram_id),active=1,approved_at=coalesce(approved_at,?) WHERE id=? AND user_id=?',
                       (instagram_id, now, tag_id, self.user_id))

    def pause(self, tag_id):
        with self.storage.connect() as db:
            return db.execute('UPDATE instagram_hashtags SET active=0 WHERE id=? AND user_id=?', (tag_id, self.user_id)).rowcount

    def searched(self, tag_id):
        with self.storage.connect() as db:
            db.execute('UPDATE instagram_hashtags SET last_searched_at=? WHERE id=? AND user_id=?', (utcnow(), tag_id, self.user_id))

    def usage(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        with self.storage.reader() as db:
            return [dict(r) for r in db.execute('SELECT name,instagram_id,searched_at FROM instagram_searches WHERE user_id=? AND searched_at>?', (self.user_id, cutoff))]

    def record_usage(self, tag):
        # A repeated request inside its original 7-day window does not reset that window.
        if any(r['name'] == tag['name'] for r in self.usage()):
            return
        with self.storage.connect() as db:
            db.execute('INSERT INTO instagram_searches(user_id,name,instagram_id,searched_at) VALUES (?,?,?,?)',
                       (self.user_id, tag['name'], tag['instagram_id'], utcnow()))

    def resolve_usage(self, tag):
        with self.storage.connect() as db:
            db.execute('UPDATE instagram_searches SET instagram_id=? WHERE user_id=? AND name=?',
                       (tag['instagram_id'], self.user_id, tag['name']))

    def start_run(self, tag_id, origin='legacy_unknown', request=None):
        contract = envelope('instagram_graph', origin, utcnow(), request=request,
                            retention_days=self.analysis.retention_days)
        with self.storage.connect() as db:
            return db.execute('INSERT INTO instagram_runs(hashtag_id,started_at,origin,contract_json) VALUES (?,?,?,?)',
                              (tag_id, contract['collected_at'], origin, encode(contract))).lastrowid

    def finish_run(self, run_id, media, returned, requests, error=None, truncated=False, validated=None, *, connection=None, observed_at=None):
        seen = observed_at or utcnow()
        new = 0
        with (nullcontext(connection) if connection is not None else self.storage.connect()) as db:
            run = db.execute('SELECT * FROM instagram_runs WHERE id=?', (run_id,)).fetchone()
            tag_id = run['hashtag_id']
            for item in media.values():
                contract = envelope('instagram_graph', run['origin'], seen, published_at=item.get('published_at'),
                                    fields={k: v for k, v in item.items() if k in item.get('provided_fields', item)},
                                    request=json.loads(run['contract_json'])['request_conditions'] if run['contract_json'] else {},
                                    observation={'run_id': run_id, 'hashtag_id': tag_id},
                                    retention_days=self.analysis.retention_days)
                db.execute('''INSERT INTO instagram_media(id,published_at,first_seen,last_seen,media_type,permalink,caption,caption_captured_at)
                              VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen,
                              published_at=coalesce(excluded.published_at,instagram_media.published_at),
                              media_type=coalesce(excluded.media_type,instagram_media.media_type),
                              permalink=coalesce(excluded.permalink,instagram_media.permalink),
                              caption=coalesce(excluded.caption,instagram_media.caption),
                              caption_captured_at=CASE WHEN excluded.caption IS NOT NULL AND
                              (instagram_media.caption IS NULL OR instagram_media.caption!=excluded.caption)
                              THEN excluded.caption_captured_at ELSE instagram_media.caption_captured_at END,
                              caption_purged_at=CASE WHEN excluded.caption IS NOT NULL THEN NULL ELSE instagram_media.caption_purged_at END''',
                           (item['id'], item.get('published_at'), seen, seen, item.get('media_type'), item.get('permalink'), item.get('caption'),
                            seen if item.get('caption') is not None else None))
                new += db.execute('INSERT OR IGNORE INTO instagram_hashtag_media VALUES (?,?,?,?)',
                                  (tag_id, item['id'], seen, seen)).rowcount
                db.execute('UPDATE instagram_hashtag_media SET last_seen=? WHERE hashtag_id=? AND media_id=?', (seen, tag_id, item['id']))
                self.analysis.analyze_latest(db, item['id'], item.get('caption'), seen, seen)
                self.analysis.observe(db, tag_id, item['id'], item.get('caption'), seen, run_id)
                db.execute('UPDATE instagram_media SET origin=?,contract_json=? WHERE id=?',
                           (run['origin'], encode(contract), item['id']))
                db.execute('UPDATE instagram_analysis_observations SET contract_json=? WHERE run_id=? AND media_id=?',
                           (encode(contract), run_id, item['id']))
            status = ('partial' if media else 'failed') if error else ('limited' if truncated else 'success')
            db.execute('''UPDATE instagram_runs SET finished_at=?,status=?,request_count=?,returned_count=?,
                          new_count=?,duplicate_count=?,error_type=?,error_code=?,error_subcode=?,truncated=? WHERE id=?''',
                       (seen, status, requests, returned, new, max(0, (returned if validated is None else validated)-new),
                        error.kind if error else None, error.code if error else None,
                        error.subcode if error else None, int(truncated), run_id))
            # A failed empty run is unknown coverage, not a measured zero.
            if not error or media:
                local = datetime.fromisoformat(seen).astimezone(KST)
                for grain, bucket in [('day', local.strftime('%Y-%m-%d')), ('hour', local.strftime('%Y-%m-%dT%H:00:00+09:00'))]:
                    db.execute('''INSERT INTO instagram_metrics VALUES (?,?,?,?,1,?)
                                  ON CONFLICT(hashtag_id,grain,bucket) DO UPDATE SET
                                  new_count=new_count+excluded.new_count,run_count=run_count+1,
                                  successful_runs=successful_runs+excluded.successful_runs''',
                               (tag_id, grain, bucket, new, int(not error and not truncated)))
            self.analysis.purge_expired(db)
            if connection is not None:
                return dict(db.execute('SELECT * FROM instagram_runs WHERE id=?', (run_id,)).fetchone())
        return self.run(run_id)

    def run(self, run_id):
        with self.storage.reader() as db:
            row = db.execute('SELECT * FROM instagram_runs WHERE id=?', (run_id,)).fetchone()
            return dict(row) if row else None

    def summary(self, tag, days=7, include_media=False):
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(hours=24)).isoformat()
        prior = (now - timedelta(hours=48)).isoformat()
        start_day = (now.astimezone(KST).date() - timedelta(days=days-1)).isoformat()
        hour_start = (now - timedelta(days=days)).astimezone(KST).isoformat()
        with self.storage.reader() as db:
            total = db.execute('SELECT count(*) FROM instagram_hashtag_media WHERE hashtag_id=?', (tag['id'],)).fetchone()[0]
            recent = db.execute('SELECT count(*) FROM instagram_hashtag_media WHERE hashtag_id=? AND first_seen>=?', (tag['id'], cutoff)).fetchone()[0]
            previous = db.execute('SELECT count(*) FROM instagram_hashtag_media WHERE hashtag_id=? AND first_seen>=? AND first_seen<?', (tag['id'], prior, cutoff)).fetchone()[0]
            last = db.execute('SELECT * FROM instagram_runs WHERE hashtag_id=? ORDER BY id DESC LIMIT 1', (tag['id'],)).fetchone()
            last_success = db.execute("SELECT finished_at FROM instagram_runs WHERE hashtag_id=? AND status IN ('success','limited') ORDER BY id DESC LIMIT 1", (tag['id'],)).fetchone()
            first = db.execute('SELECT min(finished_at) FROM instagram_runs WHERE hashtag_id=? AND (status IN (\'success\',\'limited\') OR new_count>0)', (tag['id'],)).fetchone()[0]
            media = [dict(r) for r in db.execute('''SELECT m.*,hm.first_seen AS hashtag_first_seen,hm.last_seen AS hashtag_last_seen
                     FROM instagram_media m JOIN instagram_hashtag_media hm ON hm.media_id=m.id
                     WHERE hm.hashtag_id=? ORDER BY hm.first_seen DESC,m.id LIMIT 20''', (tag['id'],))] if include_media else []
            daily = [dict(r) for r in db.execute("SELECT bucket,new_count,run_count,successful_runs FROM instagram_metrics WHERE hashtag_id=? AND grain='day' AND bucket>=? ORDER BY bucket", (tag['id'], start_day))]
            hourly = [dict(r) for r in db.execute("SELECT bucket,new_count,run_count,successful_runs FROM instagram_metrics WHERE hashtag_id=? AND grain='hour' AND bucket>=? ORDER BY bucket", (tag['id'], hour_start))]
        comparison_ready = bool(first and first <= prior)
        result = dict(tag, total_observed=total, new_24h=recent,
                    change_24h=recent-previous if comparison_ready else None,
                    growth_pct_24h=round((recent-previous)/previous*100, 2) if comparison_ready and previous else None,
                    growth_unavailable_reason=None if comparison_ready and previous else 'zero_baseline' if comparison_ready else 'insufficient_history',
                    first_observation_at=first, latest_run=dict(last) if last else None,
                    last_success_at=last_success[0] if last_success else None,
                    daily=daily, hourly=hourly, analysis=self.analysis.summary(tag, days))
        if include_media:
            cutoff = (now-timedelta(days=self.analysis.retention_days)).isoformat()
            for item in media:
                if item['caption'] is not None and (item['caption_captured_at'] or item['first_seen']) <= cutoff:
                    item['caption'] = None
                    item['caption_retention_state'] = 'expired_pending_maintenance'
            result['media'] = media
        return result
