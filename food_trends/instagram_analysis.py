"""Caption-only hashtag analysis. This module never calls an external API."""
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from .classifier import normalize
from .storage import utcnow

VERSION = 'caption-hashtags-v1'
KST = timezone(timedelta(hours=9))


def normalize_tag(name):
    # Reuse the project's NFC/whitespace normalization. No food alias guessing.
    return normalize(name).casefold()


def extract_hashtags(caption):
    """Return one canonical entry per post, retaining every original spelling."""
    if not isinstance(caption, str):
        return []
    found = defaultdict(set)
    url_spans = [m.span() for m in re.finditer(r'[A-Za-z][A-Za-z0-9+.-]*://\S+', caption)]
    index = 0
    while index < len(caption):
        if caption[index] not in '#＃':
            index += 1
            continue
        url_end = next((end for start, end in url_spans if start <= index < end), None)
        if url_end is not None:
            index = url_end
            continue
        start = index + 1
        end = start
        while end < len(caption):
            char = caption[end]
            category = unicodedata.category(char)
            if category[0] not in ('L', 'N', 'M') and category != 'Pc':
                break
            end += 1
        original = caption[start:end]
        # A combining mark cannot start a tag; numbers and underscores can.
        if original and unicodedata.category(original[0])[0] != 'M':
            found[normalize_tag(original)].add(original)
        index = max(end, start)
    return [{'normalized_name': name, 'original_names': sorted(originals)}
            for name, originals in sorted(found.items())]


def caption_state(caption, tags):
    return 'missing' if caption is None else 'empty' if not caption.strip() else 'parsed' if tags else 'no_tags'


class HashtagAnalysis:
    def __init__(self, storage, user_id, retention_days=30):
        self.storage, self.user_id, self.retention_days = storage, user_id, retention_days

    @staticmethod
    def digest(caption):
        return hashlib.sha256(caption.encode()).hexdigest() if caption is not None else None

    def analyze_latest(self, db, media_id, caption, first_seen, last_seen):
        previous = db.execute('SELECT * FROM instagram_caption_analysis WHERE media_id=?', (media_id,)).fetchone()
        # A missing API field does not erase a previously confirmed caption analysis.
        if caption is None and previous:
            return
        tags = extract_hashtags(caption)
        db.execute('''INSERT INTO instagram_caption_analysis VALUES (?,?,?,?,?,?)
                      ON CONFLICT(media_id) DO UPDATE SET caption_state=excluded.caption_state,
                      caption_hash=excluded.caption_hash,extractor_version=excluded.extractor_version,
                      analyzed_at=excluded.analyzed_at,hashtag_count=excluded.hashtag_count''',
                   (media_id, caption_state(caption, tags), self.digest(caption), VERSION, utcnow(), len(tags)))
        db.execute('UPDATE instagram_caption_tags SET active=0 WHERE media_id=?', (media_id,))
        for tag in tags:
            db.execute('''INSERT INTO instagram_caption_tags VALUES (?,?,?,?,?,1)
                          ON CONFLICT(media_id,normalized_name) DO UPDATE SET original_names=excluded.original_names,
                          last_seen=max(last_seen,excluded.last_seen),active=1''',
                       (media_id, tag['normalized_name'], json.dumps(tag['original_names'], ensure_ascii=False), first_seen, last_seen))

    def observe(self, db, hashtag_id, media_id, caption, observed_at, run_id=None, evidence_kind='api'):
        tags = extract_hashtags(caption)
        state, digest = caption_state(caption, tags), self.digest(caption)
        db.execute('''INSERT OR IGNORE INTO instagram_analysis_observations
                      (hashtag_id,media_id,run_id,observed_at,caption_state,caption_hash,hashtag_count,extractor_version,evidence_kind)
                      VALUES (?,?,?,?,?,?,?,?,?)''',
                   (hashtag_id, media_id, run_id, observed_at, state, digest, len(tags), VERSION, evidence_kind))
        observation = db.execute('SELECT id,caption_hash FROM instagram_analysis_observations WHERE hashtag_id=? AND media_id=? AND observed_at=?',
                                 (hashtag_id, media_id, observed_at)).fetchone()
        if observation['caption_hash'] != digest:
            return
        db.execute('UPDATE instagram_observation_tags SET active=0 WHERE observation_id=?', (observation['id'],))
        for tag in tags:
            db.execute('''INSERT INTO instagram_observation_tags VALUES (?,?,?,1)
                          ON CONFLICT(observation_id,normalized_name) DO UPDATE SET original_names=excluded.original_names,active=1''',
                       (observation['id'], tag['normalized_name'], json.dumps(tag['original_names'], ensure_ascii=False)))
        db.execute('UPDATE instagram_analysis_observations SET caption_state=?,hashtag_count=?,extractor_version=? WHERE id=?',
                   (state, len(tags), VERSION, observation['id']))

    def reprocess(self):
        count = 0
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''UPDATE instagram_media SET caption_captured_at=first_seen
                WHERE caption IS NOT NULL AND caption_captured_at IS NULL''')
            rows = db.execute('''SELECT DISTINCT m.* FROM instagram_media m JOIN instagram_hashtag_media hm ON hm.media_id=m.id
                                 JOIN instagram_hashtags h ON h.id=hm.hashtag_id WHERE h.user_id=?''', (self.user_id,)).fetchall()
            for row in rows:
                previous = db.execute('SELECT * FROM instagram_caption_analysis WHERE media_id=?', (row['id'],)).fetchone()
                if row['caption'] is not None or previous is None:
                    self.analyze_latest(db, row['id'], row['caption'], row['caption_captured_at'] or row['first_seen'], row['last_seen'])
                    count += 1
                # Legacy rows have no per-run membership or historical caption versions.
                # Seed only their last known snapshot; never invent earlier daily co-tags.
                links = db.execute('''SELECT hm.* FROM instagram_hashtag_media hm JOIN instagram_hashtags h ON h.id=hm.hashtag_id
                                      WHERE hm.media_id=? AND h.user_id=?''', (row['id'], self.user_id)).fetchall()
                for link in links:
                    known = db.execute('SELECT 1 FROM instagram_analysis_observations WHERE hashtag_id=? AND media_id=? LIMIT 1',
                                       (link['hashtag_id'], row['id'])).fetchone()
                    if not known:
                        self.observe(db, link['hashtag_id'], row['id'], row['caption'], link['last_seen'], evidence_kind='legacy_snapshot')
            purged = self.purge_expired(db)
        return {'processed': count, 'raw_captions_purged': purged, 'extractor_version': VERSION}

    def purge_expired(self, db):
        cutoff = (datetime.now(timezone.utc)-timedelta(days=self.retention_days)).isoformat()
        return db.execute('''UPDATE instagram_media SET caption=NULL,caption_purged_at=?
                             WHERE caption IS NOT NULL AND coalesce(caption_captured_at,first_seen)<?''',
                          (utcnow(), cutoff)).rowcount

    def summary(self, tag, days=7, limit=30):
        now = datetime.now(timezone.utc)
        start_date = now.astimezone(KST).date()-timedelta(days=days-1)
        start = datetime.combine(start_date, datetime.min.time(), KST).astimezone(timezone.utc).isoformat()
        end = now.isoformat()
        where = 'o.hashtag_id=? AND o.observed_at>=? AND o.observed_at<=?'
        args = (tag['id'], start, end)
        base = normalize_tag(tag['name'])
        with self.storage.reader() as db:
            quality = db.execute(f'''SELECT count(*) AS total,
                coalesce(sum(available),0) AS available,coalesce(sum(extractable),0) AS extractable,
                coalesce(sum(base_confirmed),0) AS base_confirmed FROM (
                SELECT o.media_id,max(o.caption_state!='missing') AS available,
                max(o.hashtag_count>0) AS extractable,
                max(EXISTS(SELECT 1 FROM instagram_observation_tags t WHERE t.observation_id=o.id AND t.normalized_name=? AND t.active=1)) AS base_confirmed
                FROM instagram_analysis_observations o WHERE {where} GROUP BY o.media_id)''', (base, *args)).fetchone()
            rows = db.execute(f'''SELECT t.normalized_name,count(DISTINCT o.media_id) AS post_count,
                min(o.observed_at) AS first_seen,max(o.observed_at) AS last_seen
                FROM instagram_analysis_observations o JOIN instagram_observation_tags t ON t.observation_id=o.id AND t.active=1
                WHERE {where} AND t.normalized_name!=? GROUP BY t.normalized_name
                ORDER BY post_count DESC,t.normalized_name''', (*args, base)).fetchall()
            coverage = {r['day']: dict(r) for r in db.execute(f'''SELECT substr(datetime(o.observed_at,'+9 hours'),1,10) AS day,
                count(DISTINCT o.media_id) AS observed_posts,
                count(DISTINCT CASE WHEN o.caption_state!='missing' THEN o.media_id END) AS caption_available
                FROM instagram_analysis_observations o WHERE {where} GROUP BY day ORDER BY day''', args)}
            runs = db.execute("""SELECT substr(datetime(finished_at,'+9 hours'),1,10) AS day,count(*) AS runs
                FROM instagram_runs WHERE hashtag_id=? AND finished_at>=? AND finished_at<=? AND status IN ('success','limited') GROUP BY day""", args).fetchall()
            for row in runs:
                coverage.setdefault(row['day'], {'day': row['day'], 'observed_posts': 0, 'caption_available': 0})['runs'] = row['runs']
            ranking = []
            for row in rows[:limit]:
                item = dict(row)
                first_last = db.execute('''SELECT min(o.observed_at),max(o.observed_at) FROM instagram_analysis_observations o
                    JOIN instagram_observation_tags t ON t.observation_id=o.id AND t.active=1
                    WHERE o.hashtag_id=? AND t.normalized_name=?''', (tag['id'], item['normalized_name'])).fetchone()
                item['period_first_seen'], item['period_last_seen'] = item['first_seen'], item['last_seen']
                item['first_seen'], item['last_seen'] = first_last[0], first_last[1]
                item['rate_pct'] = round(item['post_count']/quality['total']*100, 2) if quality['total'] else None
                changes = {r['day']: r['post_count'] for r in db.execute(f'''SELECT substr(datetime(o.observed_at,'+9 hours'),1,10) AS day,
                    count(DISTINCT o.media_id) AS post_count FROM instagram_analysis_observations o
                    JOIN instagram_observation_tags t ON t.observation_id=o.id AND t.active=1
                    WHERE {where} AND t.normalized_name=? GROUP BY day''', (*args, item['normalized_name']))}
                item['daily'] = [{'day': day, 'post_count': changes.get(day, 0), 'observed_posts': info['observed_posts'],
                                  'rate_pct': round(changes.get(day, 0)/info['observed_posts']*100, 2) if info['observed_posts'] else None}
                                 for day, info in sorted(coverage.items())]
                originals = set()
                for form in db.execute(f'''SELECT DISTINCT t.original_names FROM instagram_analysis_observations o
                    JOIN instagram_observation_tags t ON t.observation_id=o.id AND t.active=1
                    WHERE {where} AND t.normalized_name=?''', (*args, item['normalized_name'])):
                    originals.update(json.loads(form[0]))
                item['original_names'] = sorted(originals)
                ranking.append(item)
            retention_cutoff = (now-timedelta(days=self.retention_days)).isoformat()
            raw = db.execute(f'''SELECT count(DISTINCT CASE WHEN m.caption IS NOT NULL
                AND coalesce(m.caption_captured_at,m.first_seen)>? THEN m.id END),
                count(DISTINCT CASE WHEN m.caption IS NULL AND m.caption_purged_at IS NOT NULL THEN m.id END)
                FROM instagram_analysis_observations o JOIN instagram_media m ON m.id=o.media_id WHERE {where}''',
                (retention_cutoff, *args)).fetchone()
            missing_observations = db.execute(f"SELECT count(*) FROM instagram_analysis_observations o WHERE {where} AND caption_state='missing'", args).fetchone()[0]
            legacy = db.execute(f"SELECT count(*) FROM instagram_analysis_observations o WHERE {where} AND evidence_kind='legacy_snapshot'", args).fetchone()[0]
        return {'days': days, 'period_start': start, 'period_end': end, 'time_basis': 'observation_time_kst',
                'period_unique_posts': quality['total'], 'caption_available_posts': quality['available'],
                'caption_missing_posts': quality['total']-quality['available'], 'hashtag_extractable_posts': quality['extractable'],
                'caption_without_tags_posts': quality['available']-quality['extractable'], 'base_tag_confirmed_posts': quality['base_confirmed'],
                'missing_caption_observations': missing_observations, 'raw_captions_retained': raw[0], 'raw_captions_purged': raw[1],
                'retention_days': self.retention_days, 'unique_related_tags': len(rows), 'limit': limit,
                'ranking': ranking, 'daily_coverage': [coverage[d] for d in sorted(coverage)],
                'legacy_snapshot_count': legacy, 'extractor_version': VERSION,
                'rate_definition': '선택 기간에 기준 태그 API로 실제 관측한 고유 게시물 전체 중 공동 태그를 확인한 게시물의 비율입니다. 캡션 누락도 분모에 포함됩니다.',
                'history_note': '기존 자료는 마지막 관측의 캡션 스냅샷만 재처리했습니다. 과거 회차별 태그 분포를 소급 생성하지 않습니다. 일별 고유 건수의 합은 기간 고유 건수와 다를 수 있습니다.'}
