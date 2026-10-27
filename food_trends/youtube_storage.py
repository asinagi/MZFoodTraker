"""Additive SQLite storage; API entities expire independently of RSS and FOOD data."""
import json
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .naver_service import keyword_group
from .storage import utcnow
from .youtube import YouTubeError
from .contracts import envelope, encode


def pacific_day():
    now = datetime.now(timezone.utc)
    try:
        return now.astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
    except ZoneInfoNotFoundError:
        # Windows without tzdata: US Pacific DST since 2007, UTC transition instants.
        march, november = datetime(now.year, 3, 8, 10, tzinfo=timezone.utc), datetime(now.year, 11, 1, 9, tzinfo=timezone.utc)
        start = march + timedelta(days=(6-march.weekday()) % 7)
        end = november + timedelta(days=(6-november.weekday()) % 7)
        return (now-timedelta(hours=7 if start <= now < end else 8)).date().isoformat()


class YouTubeStore:
    def __init__(self, storage):
        self.storage = storage

    def state(self, key, default=None):
        with self.storage.reader() as db:
            row = db.execute('SELECT value FROM youtube_state WHERE key=?', (key,)).fetchone()
            return row[0] if row else default

    def set_state(self, key, value):
        with self.storage.connect() as db:
            db.execute('INSERT OR REPLACE INTO youtube_state VALUES (?,?)', (key, str(value)))

    @contextmanager
    def operation(self):
        import uuid
        owner = uuid.uuid4().hex
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM youtube_state WHERE key='lease_until'").fetchone()
            if row and float(row[0]) > time.time():
                raise YouTubeError('busy')
            db.executemany('INSERT OR REPLACE INTO youtube_state VALUES (?,?)',
                           [('lease_owner', owner), ('lease_until', str(time.time()+180))])
        try:
            yield owner
        finally:
            with self.storage.connect() as db:
                row = db.execute("SELECT value FROM youtube_state WHERE key='lease_owner'").fetchone()
                if row and row[0] == owner:
                    db.execute("DELETE FROM youtube_state WHERE key IN ('lease_owner','lease_until')")

    def sync_queries(self, config):
        with self.storage.connect() as db:
            db.execute('UPDATE youtube_queries SET active=0')
            db.execute('DELETE FROM youtube_candidate_foods')
            rows = db.execute("SELECT item_id,normalized_name FROM candidates WHERE classification='FOOD'").fetchall()
            for row in rows:
                group = keyword_group(row['normalized_name'])
                db.execute('INSERT OR IGNORE INTO youtube_foods(name) VALUES (?)', (group['groupName'],))
                food_id = db.execute('SELECT id FROM youtube_foods WHERE name=?', (group['groupName'],)).fetchone()[0]
                db.execute('INSERT INTO youtube_candidate_foods VALUES (?,?)', (row['item_id'], food_id))
                for query in group['keywords']:
                    db.execute('''INSERT INTO youtube_queries(query,mode,food_id) VALUES (?,'tracking',?)
                        ON CONFLICT(query,mode) DO UPDATE SET active=1,food_id=excluded.food_id''', (query, food_id))
            for query in config.discovery_queries:
                db.execute('''INSERT INTO youtube_queries(query,mode) VALUES (?,'discovery')
                    ON CONFLICT(query,mode) DO UPDATE SET active=1''', (query.strip(),))
            return [dict(r) for r in db.execute('SELECT * FROM youtube_queries WHERE active=1 ORDER BY coalesce(last_attempt,\'\'),id')]

    def reserve(self, endpoint, query_id, owner, config, origin='legacy_unknown', run_id=None):
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM youtube_state WHERE key='lease_owner'").fetchone()
            if not row or row[0] != owner:
                raise YouTubeError('busy')
            db.execute("UPDATE youtube_state SET value=? WHERE key='lease_until'", (str(time.time()+180),))
            states = dict(db.execute('SELECT key,value FROM youtube_state'))
            if states.get('quota_'+endpoint) == pacific_day():
                raise YouTubeError('quota')
            if float(states.get('cooldown_until', '0')) > time.time():
                raise YouTubeError('cooldown')
            count = db.execute('SELECT count(*) FROM youtube_calls WHERE day=? AND endpoint=?', (pacific_day(), endpoint)).fetchone()[0]
            if count >= (config.daily_search_limit if endpoint == 'search' else config.daily_video_limit):
                raise YouTubeError('budget')
            return db.execute('''INSERT INTO youtube_calls(day,endpoint,query_id,started_at,status,origin,run_id)
                VALUES (?,?,?,?,'running',?,?)''', (pacific_day(), endpoint, query_id, utcnow(), origin, run_id)).lastrowid

    def finish(self, ticket, state, status, result_count=None):
        with self.storage.connect() as db:
            db.execute('UPDATE youtube_calls SET finished_at=?,status=?,http_status=?,result_count=? WHERE id=?',
                       (utcnow(), state, status, result_count, ticket))
            endpoint = db.execute('SELECT endpoint FROM youtube_calls WHERE id=?', (ticket,)).fetchone()[0]
            if state == 'quota':
                db.execute('INSERT OR REPLACE INTO youtube_state VALUES (?,?)', ('quota_'+endpoint, pacific_day()))
            if state in ('rate_limit', 'authentication', 'permission'):
                delay = 60 if state == 'rate_limit' else 21600 if state in ('authentication', 'permission') else 5
                db.execute("INSERT OR REPLACE INTO youtube_state VALUES ('cooldown_until',?)", (str(time.time()+delay),))

    def save_videos(self, requested, videos, origin='legacy_unknown', run_id=None, request_context=None):
        stamp = utcnow()
        expiry = (datetime.now(timezone.utc)+timedelta(days=29)).isoformat()
        public = {v['video_id']: v for v in videos if v['privacy_status'] == 'public' and v.get('upload_status') not in ('deleted', 'failed', 'rejected')}
        with self.storage.connect() as db:
            db.execute('PRAGMA secure_delete=ON')
            for identity in requested:
                if identity not in public:
                    db.execute('DELETE FROM youtube_videos WHERE video_id=?', (identity,))
            for identity, video in public.items():
                fields = {k: v for k, v in video.items()
                          if k in video.get('provided_fields', video)}
                contract = envelope('youtube', origin, stamp, published_at=video['published_at'], fields=fields,
                                    request=request_context or {'endpoint': 'videos',
                                        'part': 'snippet,statistics,contentDetails,status', 'id': ','.join(requested)},
                                    observation={'run_id': run_id, 'item_key': identity, 'snapshot_only': True})
                db.execute('''INSERT INTO youtube_videos(video_id,payload,collected_at,refreshed_at,expires_at,contract_json)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET
                    payload=excluded.payload,refreshed_at=excluded.refreshed_at,expires_at=excluded.expires_at,
                    contract_json=excluded.contract_json''',
                           (identity, json.dumps(video, ensure_ascii=False), stamp, stamp, expiry, encode(contract)))
                db.execute('INSERT OR REPLACE INTO youtube_video_provenance VALUES (?,?,?,?)', (identity, origin, stamp, run_id))
        return set(public)

    def match(self, query_id, identities):
        with self.storage.connect() as db:
            for identity in identities:
                db.execute('INSERT OR REPLACE INTO youtube_matches VALUES (?,?,?)', (query_id, identity, utcnow()))

    def purge(self):
        with self.storage.connect() as db:
            db.execute('PRAGMA secure_delete=ON')
            deleted = db.execute('DELETE FROM youtube_videos WHERE expires_at<=?', (utcnow(),)).rowcount
            # Match provenance is API data too; refreshing video metadata does not refresh old matches.
            db.execute('DELETE FROM youtube_matches WHERE matched_at<=?', ((datetime.now(timezone.utc)-timedelta(days=29)).isoformat(),))
            db.execute('''UPDATE youtube_queries SET page_token=NULL,before_at=NULL,token_at=NULL
                          WHERE token_at<=?''', ((datetime.now(timezone.utc)-timedelta(days=7)).isoformat(),))
        return deleted

    def videos(self, limit=25):
        with self.storage.reader() as db:
            result = []
            for row in db.execute('SELECT * FROM youtube_videos WHERE expires_at>? ORDER BY refreshed_at DESC,video_id LIMIT ?', (utcnow(), limit)):
                video = dict(json.loads(row['payload']), collected_at=row['collected_at'], refreshed_at=row['refreshed_at'], expires_at=row['expires_at'])
                provenance = db.execute('SELECT origin,verified_at,run_id FROM youtube_video_provenance WHERE video_id=?', (row['video_id'],)).fetchone()
                video['provenance'] = dict(provenance) if provenance else {'origin': 'legacy_unknown', 'verified_at': None, 'run_id': None}
                video['matches'] = [dict(r) for r in db.execute('''SELECT q.query,q.mode,q.food_id,f.name AS food_name,
                    'search_match_unverified' AS verification FROM youtube_matches m
                    JOIN youtube_queries q ON q.id=m.query_id LEFT JOIN youtube_foods f ON f.id=q.food_id
                    WHERE m.video_id=? AND q.active=1 AND m.matched_at>?''',
                    (row['video_id'], (datetime.now(timezone.utc)-timedelta(days=29)).isoformat()))]
                video['contract'] = json.loads(row['contract_json']) if row['contract_json'] else None
                result.append(video)
            return result
