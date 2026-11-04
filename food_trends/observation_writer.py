"""Deterministic B4/B5 rows for isolated mock and test databases (never the configured database).

Source-store rows (RSS runs/items/observations, Instagram media and caption tags) are written directly
with explicit timestamps; everything downstream runs through the real services: IngestionService
(staging, DQ) and FoodObservationService (search evidence, exact-alias mentions, metrics, purge) on a
clock this writer controls. Instagram tracking runs carry the per-food observed_posts metric B5 writes for
B3 jobs, so observation scope matches production rows without building execution jobs.
"""
import hashlib
import json
import os
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
from .classifier import normalize
from .contracts import SOURCES, envelope, encode
from .food_observations import FoodObservationService
from .food_service import NORMALIZATION_VERSION, dump
from .ingestion import IngestionService, digest
from .instagram_analysis import normalize_tag
from .paths import DEFAULT_DATABASE, database_path
from .storage import Storage
from .tracking_service import instant, stamp


class ScratchStorage(Storage):
    """A disposable, rebuildable database (mock datasets, fixtures): commits skip fsync. Never a real store."""

    def __init__(self, path):
        refuse_configured_database(path)
        super().__init__(path)

    @classmethod
    def create(cls, path):
        refuse_configured_database(path)  # before Storage.create runs any DDL
        return super().create(path)

    @contextmanager
    def connect(self, readonly=False):
        with super().connect(readonly) as db:
            if not readonly:
                db.execute('PRAGMA synchronous=OFF')
            yield db


def refuse_configured_database(path):
    target = Path(path).resolve()
    if target.name == DEFAULT_DATABASE.name:
        raise ValueError('관측 기록기는 운영 DB 파일(trends.sqlite3)에 쓸 수 없습니다.')
    for configured in {DEFAULT_DATABASE.resolve(), Path(database_path(os.environ)).resolve()}:
        if target == configured or (target.exists() and configured.exists() and os.path.samefile(target, configured)):
            raise ValueError('관측 기록기는 설정된 운영 DB에 쓸 수 없습니다.')


class ObservationWriter:
    def __init__(self, storage, at=None):
        refuse_configured_database(storage.path)
        self.storage, self.at = storage, instant(at) if at else None
        clock = lambda: self.at
        self.ingestion = IngestionService(storage, clock)
        self.observations = FoodObservationService(storage, clock)

    def _stamp(self, at):
        if at is not None:
            self.at = instant(at)
        return stamp(self.at)

    @contextmanager
    def _transaction(self):
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    def _write(self, sql_rows):
        with self._transaction() as db:
            return [db.execute(sql, args) for sql, args in sql_rows]

    # Reviewed foods with fixed IDs (FoodService assigns random ones).

    def food(self, food_id, name, aliases=(), kind='DISH', at=None, scope='food:ko'):
        created = self._stamp(at)
        rows = [('INSERT INTO food_entities VALUES (?,?,?,?,1,?,NULL,?,?)', (food_id, name, kind, 'ACTIVE', scope, created, created))]
        for n, surface in enumerate(dict.fromkeys((name,) + tuple(aliases))):
            text = normalize(surface)
            rows.append(('INSERT INTO food_aliases VALUES (?,?,?,?,?,?,?,1,?,?)',
                         ('ALIAS_%s_%02d' % (food_id, n), food_id, text, text, scope, NORMALIZATION_VERSION, 'ACTIVE', created, created)))
        self._write(rows)
        return food_id

    def relate(self, from_food_id, to_food_id, relation_type='VARIANT_OF', at=None):
        created = self._stamp(at)
        self._write([('INSERT INTO food_relations VALUES (?,?,?,?,?,1,?,?)',
                      ('REL_%s_%s' % (from_food_id, to_food_id), from_food_id, to_food_id, relation_type, 'ACTIVE', created, created))])

    def merge(self, food_id, into, at=None):
        """Merged identity as FoodService leaves it (aliases moved, redirect kept), without a reprocessing event."""
        updated = self._stamp(at)
        self._write([("UPDATE food_aliases SET food_id=?,revision=revision+1,updated_at=? WHERE food_id=? AND status='ACTIVE'", (into, updated, food_id)),
                     ("UPDATE food_entities SET status='MERGED',merged_into=?,revision=revision+1,updated_at=? WHERE food_id=?", (into, updated, food_id))])

    def query(self, query_id, hashtag, at=None):
        canonical = {'source_id': 'instagram_graph', 'hashtag': hashtag}
        self._write([('INSERT INTO planner_queries VALUES (?,?,?,?,?,?,?,?)', (query_id, 'instagram_graph', 'mock-scope', 'hashtag_recent_media',
                                                                             dump(canonical), digest(canonical), 'mock-planner-v1', self._stamp(at)))])
        return query_id

    # Google Trends RSS: the existing run/items/observations store, then B4 rss_run and B5 link_run.

    def rss_run(self, at, entries, success=True, origin='mock'):
        """entries: dicts with keyword, optional news (titles), traffic and published_at. Returns the ingest run id."""
        self.at = instant(at)
        seen = stamp(self.at)
        request = {'geo': 'KR', 'endpoint': 'trending/rss'}
        run_contract = encode(envelope('google_trends_rss', origin, seen, request=request))
        with self._transaction() as db:
            run_id = db.execute('''INSERT INTO runs(started_at,finished_at,success,error,http_status,item_count,new_count,warnings,origin,contract_json)
                VALUES (?,?,?,?,?,?,0,'[]',?,?)''', (seen, seen, int(success), None if success else 'MockOutage: fixture', 200 if success else 503,
                                                     len(entries) if success else 0, origin, run_contract)).lastrowid
            for entry in entries if success else ():
                published = entry.get('published_at', seen)
                source_id = hashlib.sha256(('%s|%s' % (entry['keyword'], published)).encode()).hexdigest()
                payload = json.dumps({'source_id': source_id, 'keyword': entry['keyword'], 'published_at': published,
                                      'traffic': entry.get('traffic'), 'news': [{'title': t} for t in entry.get('news', ())]}, ensure_ascii=False)
                db.execute('''INSERT INTO items(source_id,keyword,published_at,first_seen,last_seen,payload) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(source_id) DO UPDATE SET last_seen=excluded.last_seen,payload=excluded.payload''',
                           (source_id, entry['keyword'], published, seen, seen, payload))
                item_id = db.execute('SELECT id FROM items WHERE source_id=?', (source_id,)).fetchone()[0]
                fields = {'keyword': entry['keyword'], 'published_at': published, 'news': [{'title': t} for t in entry.get('news', ())]}
                if entry.get('traffic'):
                    fields['traffic'] = entry['traffic']
                contract = encode(envelope('google_trends_rss', origin, seen, published_at=published, fields=fields, request=request,
                                           observation={'run_id': run_id, 'item_key': source_id}))
                db.execute('INSERT INTO observations(run_id,item_id,payload,classification_json,contract_json) VALUES (?,?,?,?,?)',
                           (run_id, item_id, payload, None, contract))
        staged = self.ingestion.rss_run(run_id)
        self.observations.link_run(staged['ingest_run_id'])
        return staged['ingest_run_id']

    # Instagram: media and caption tags as the collector stores them, then B4 ingest and B5 link_run.

    def _media(self, db, post, seen, origin):
        tags = post.get('tags', ())
        caption = post.get('caption', ' '.join('#' + t for t in tags))
        db.execute('''INSERT INTO instagram_media(id,published_at,first_seen,last_seen,media_type,permalink,caption,caption_captured_at,origin)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET first_seen=min(first_seen,excluded.first_seen),
            last_seen=max(last_seen,excluded.last_seen)''',
                   (post['id'], post.get('published_at', seen), seen, seen, 'IMAGE', 'https://www.instagram.com/p/mock%s/' % post['id'],
                    caption, seen, origin))
        for tag in tags:
            db.execute('''INSERT INTO instagram_caption_tags VALUES (?,?,?,?,?,1)
                ON CONFLICT(media_id,normalized_name) DO UPDATE SET last_seen=excluded.last_seen''',
                       (post['id'], normalize_tag(tag), json.dumps([tag], ensure_ascii=False), seen, seen))
        row = db.execute('SELECT * FROM instagram_media WHERE id=?', (post['id'],)).fetchone()
        visible = {k: row[k] for k in ('id', 'published_at', 'media_type', 'permalink', 'caption')}
        return dict(external_key=row['id'], published_at=row['published_at'], origin=origin,
                    fields={'caption': row['caption'], 'media_type': row['media_type']},
                    field_states=envelope('instagram_graph', origin, seen, fields=visible)['field_states'],
                    payload_ref='instagram_media:' + row['id'], payload_hash=digest([row['id'], row['published_at'], row['media_type'],
                                                                                     row['permalink'], hashlib.sha256(row['caption'].encode()).hexdigest()]),
                    expires_at=None, first_seen=row['first_seen'], observed_at=seen)

    def instagram_run(self, at, query_id, food_ids, posts=(), coverage='COMPLETE', origin='mock'):
        """One hashtag query run. coverage NONE is a failed run: no items and a NOT_OBSERVED observation per food."""
        self.at = instant(at)
        seen = stamp(self.at)
        with self._transaction() as db:
            hashtag = json.loads(db.execute('SELECT canonical_json FROM planner_queries WHERE query_id=?', (query_id,)).fetchone()[0])['hashtag']
            contents = [self._media(db, p, seen, origin) for p in posts] if coverage != 'NONE' else []
        staged = self.ingestion.ingest(dict(source_code='instagram_graph', run_ref='mock-instagram:%s:%s' % (query_id, seen), origin=origin,
            query_id=query_id, execution_status='SUCCEEDED' if coverage != 'NONE' else 'FAILED', coverage=coverage,
            error_kind=None if coverage != 'NONE' else 'mock_outage', request_context={'hashtag': hashtag, 'api_version': 'v26.0'},
            observed_at=seen, returned_count=len(contents), accepted_count=len(contents),
            source_schema_version='graph-media-v1'), contents)
        run_id = staged['ingest_run_id']
        self.observations.link_run(run_id)
        with self._transaction() as db:
            run = self.observations._run(db, run_id)
            observed = coverage in ('COMPLETE', 'PARTIAL', 'LIMITED')
            for food_id in food_ids:
                # The per-food observation B5 records for every food a tracking job served (failure: NOT_OBSERVED, no value).
                self.observations._insert_metric(db, run, dict(metric_code='instagram.observed_posts', unit='unique_observed_post',
                    subject_key=food_id, food_id=food_id, observed_at=seen,
                    comparison_context={'query_id': query_id, 'basis': 'distinct_items_returned_by_this_query', 'window': 'provider_recent_media'},
                    value_state='OBSERVED' if observed else 'NOT_OBSERVED', count_value=len(contents) if observed else None,
                    decimal_value=None, raw_value=None))
        return run_id

    def legacy_instagram(self, at, posts):
        """Origin-unknown media as one labelled logical run (coverage UNKNOWN), as legacy_backfill stages it."""
        self.at = instant(at)
        seen = stamp(self.at)
        with self._transaction() as db:
            contents = []
            for post in posts:
                record = self._media(db, post, stamp(instant(post['seen_at'])), 'legacy_unknown')
                contents.append(record)
        staged = self.ingestion.ingest(dict(source_code='instagram_graph', run_ref='legacy:instagram_graph:origin-unknown', origin='legacy_unknown',
            execution_status=None, coverage='UNKNOWN', error_kind=None, request_context={}, observed_at=seen, returned_count=None,
            accepted_count=None, source_schema_version='graph-media-v1'), contents)
        self.observations.link_run(staged['ingest_run_id'])
        return staged['ingest_run_id']

    def purge(self, at):
        """Retention as production applies it: Instagram caption TTL in the source store, then B4 and B5 purges."""
        self.at = instant(at)
        cutoff = stamp(self.at - timedelta(days=SOURCES['instagram_graph']['policy']['retention_days']))
        with self._transaction() as db:
            db.execute('''UPDATE instagram_media SET caption=NULL,caption_purged_at=? WHERE caption IS NOT NULL
                AND coalesce(caption_captured_at,first_seen)<=?''', (stamp(self.at), cutoff))
        return {'ingestion': self.ingestion.purge(self.at), 'observations': self.observations.purge(self.at)}


def snapshot(path):
    """Full logical dump of a SQLite file, for proving read paths leave it unchanged."""
    import sqlite3
    with closing(sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)) as db:
        return list(db.iterdump())
