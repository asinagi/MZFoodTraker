"""ORACLE-BACKEND-COMPLETION-1: Google Trends RSS discovery on the target schema (Oracle, or its SQLite stand-in).

The existing collector (fetch/parse) and classifier run unchanged, and the Storage/CollectionService contract
(start, finish, status, trends, export, collect) is kept, written to target tables inside the Oracle runtime's
ownership-guarded transactions (refused while Shadow owns the target):
  items              -> source_contents 'items:N' (N is the SQLite item id B7 loaded, or the next number)
  candidates/history -> rss_classifications / rss_classification_history (B7's keys)
  runs/observations  -> rss_runs / rss_observations (v8; a run id continues past every loaded B4 run_ref runs:N)
RSS writers are serialized by an exclusive lock on rss_runs (BEGIN IMMEDIATE on the stand-in). A finished run is
staged by B4 in the same transaction (the SQLite path stages right after its run commits), so no finished run is
left unstaged. FOOD/REVIEW items then become candidates through FoodService's import decisions (oracle_foods) and
B5 links the staged run; both are idempotent and retried locally (recover_rss, link_pending). Candidates are
proposals only: no food, alias or tracking target is created, BRAND/STORE/NON_FOOD stay behind the type gate and a
run with no FOOD item is a normal result. Opens no SQLite database. The raw response body is not kept on the target,
and live fetching is refused (only an injected mock fetcher runs) until Oracle live collection is separately approved.
"""
import hashlib
import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from . import collector
from .classifier import classify
from .contracts import encode, envelope
from .execution_runtime import ExecutionController
from .ingestion import digest, ingest_safely, item_key
from .oracle_execution import utc_value
from .oracle_ingestion import OracleIngestionService
from .target_db import canonical_json
from .target_repository import policy
from .tracking_service import stamp

SOURCE_CODE = 'google_trends_rss'
REQUEST = {'geo': 'KR', 'endpoint': 'trending/rss'}
# The SQLite observation contract's mapping from feed elements to envelope fields (Storage.finish).
FIELD_MAP = {'title': 'keyword', 'pubDate': 'published_raw', 'approx_traffic': 'traffic', 'news_item': 'news'}
LIVE_DISABLED = 'oracle_live_collection_disabled'


def _number(payload_ref):
    kind, _, number = payload_ref.partition(':')
    return int(number) if kind == 'items' and number.isdigit() else None


def _first(t, sql, table=None, **binds):
    return t.one(sql + (' FETCH FIRST 1 ROWS ONLY' if t.oracle else ' LIMIT 1'), table, **binds)


def _ts(value):
    """A timestamp read without a table mapping (Oracle: naive UTC datetime; stand-in: ISO text) as logical() writes it."""
    return None if value is None else stamp(utc_value(value))


class _Staging(OracleIngestionService):
    """B4 inside the RSS run's own transaction."""

    def __init__(self, execution, t):
        super().__init__(execution)
        self.t = t

    @contextmanager
    def _transaction(self):
        yield self.t


class OracleRssStore:
    """Storage's RSS contract on the target."""

    def __init__(self, execution):
        self.execution = execution

    def now(self):
        return stamp(self.execution.now())

    @staticmethod
    def _lock(t):
        if t.oracle:
            t.run('LOCK TABLE rss_runs IN EXCLUSIVE MODE')

    @staticmethod
    def _next_run(t):
        """Past every run on the target, including the SQLite runs B7 staged as runs:N (their B4 keys must not collide)."""
        refs = [r['run_ref'] for r in t.rows("SELECT run_ref FROM ingest_runs WHERE source_code='google_trends_rss' AND run_ref LIKE 'runs:%'")]
        loaded = [int(ref[5:]) for ref in refs if ref[5:].isdigit()]
        return max([int(t.scalar('SELECT coalesce(max(run_id),0) AS n FROM rss_runs'))] + loaded) + 1

    @staticmethod
    def _next_item(t):
        refs = [r['payload_ref'] for table in ('source_contents', 'source_items')
                for r in t.rows("SELECT payload_ref FROM %s WHERE source_code='google_trends_rss' AND payload_ref LIKE 'items:%%'" % table)]
        return max([n for n in map(_number, refs) if n is not None], default=0) + 1

    def _run(self, t, run_id):
        run = t.one('SELECT * FROM rss_runs WHERE run_id=:b_r', 'rss_runs', b_r=run_id)
        if run is None:
            raise ValueError('unknown RSS run')
        return run

    def start(self, origin='mock'):
        contract = envelope(SOURCE_CODE, origin, self.now(), request=REQUEST)
        with self.execution.transaction() as t:
            self._lock(t)
            run_id = self._next_run(t)
            t.insert('rss_runs', dict(run_id=run_id, started_at=contract['collected_at'], finished_at=None, success=None, error=None,
                                      http_status=None, item_count=0, new_count=0, warnings_json='[]', origin=origin, contract_json=encode(contract),
                                      food_count=None, review_count=None, new_food_count=None, new_review_count=None, classifier_version=None))
        return run_id

    def _classify(self, t, key, number, item, seen):
        """Storage._classify: the current result, and the previous one in history when it changed (same keys as B7)."""
        result = dict(classify(item), classified_at=seen)
        old = t.one('SELECT * FROM rss_classifications WHERE item_key=:b_k', 'rss_classifications', b_k=key)
        row = dict(item_key=key, normalized_name=result['normalized_name'], classification=result['classification'],
                   reasons_json=json.dumps(result['reasons'], ensure_ascii=False), classifier_version=result['classifier_version'],
                   entity_type=result['entity_type'], rule_score=result['rule_score'], score_basis=result['score_basis'],
                   evidence_news_json=json.dumps(result['evidence_news'], ensure_ascii=False), classified_at=seen)
        if old is None:
            t.insert('rss_classifications', row)
            return result
        previous = dict(item_id=number, normalized_name=old['normalized_name'], classification=old['classification'],
                        reasons=json.loads(old['reasons_json']), classifier_version=old['classifier_version'], entity_type=old['entity_type'],
                        rule_score=old['rule_score'], score_basis=old['score_basis'], evidence_news=json.loads(old['evidence_news_json']),
                        classified_at=old['classified_at'])
        comparison = {k: v for k, v in previous.items() if k not in ('item_id', 'classified_at')}
        if comparison != {k: v for k, v in result.items() if k != 'classified_at'}:
            text = json.dumps(previous, ensure_ascii=False)
            t.insert_absent('rss_classification_history', dict(history_key='CLSH_' + digest([key, seen, text])[:32], item_key=key,
                                                                recorded_at=seen, result_json=text))
        t.update('rss_classifications', {'item_key': key}, **{k: v for k, v in row.items() if k != 'item_key'})
        return result

    def _put_item(self, t, key, item, payload, origin, seen, allocate):
        """The items row: inserted once (first capture and origin kept), payload and last_seen refreshed."""
        old = t.one('SELECT payload_ref FROM source_contents WHERE item_key=:b_k', b_k=key)
        content = dict(content_json=payload, content_hash=hashlib.sha256(canonical_json(item).encode()).hexdigest(), refreshed_at=seen,
                       source_payload_hash=hashlib.sha256(payload.encode()).hexdigest())
        if old:
            t.update('source_contents', {'payload_ref': old['payload_ref']}, **content)
            return old['payload_ref'], 0
        row = dict(content, payload_ref='items:%d' % allocate(), item_key=key, source_code=SOURCE_CODE, origin=origin, captured_at=seen,
                   expires_at=None, field_expires_at=None, field_purged_at=None)
        policy('source_contents', row, self.execution.now())
        t.insert('source_contents', row)
        return row['payload_ref'], 1

    def finish(self, run_id, items=None, raw=None, http_status=None, error=None, warnings=None):
        """Storage.finish on the target, plus the run's B4 staging in the same transaction. The raw body is not kept."""
        new_count, counts, new_counts, unique, observed, c = 0, {'FOOD': 0, 'REVIEW': 0}, {'FOOD': 0, 'REVIEW': 0}, set(), [], None
        seen = self.now()
        with self.execution.transaction() as t:
            self._lock(t)
            run = self._run(t, run_id)
            numbers = []

            def allocate():
                numbers.append(numbers[-1] + 1 if numbers else self._next_item(t))
                return numbers[-1]

            for item in items or []:
                if item['source_id'] in unique:
                    continue
                unique.add(item['source_id'])
                payload, key = json.dumps(item, ensure_ascii=False), item_key(SOURCE_CODE, item['source_id'])
                ref, created = self._put_item(t, key, item, payload, run['origin'], seen, allocate)
                new_count += created
                number = _number(ref)
                c = self._classify(t, key, number, item, seen)
                supplied = {FIELD_MAP[k] for k in item.get('provided_fields', ()) if k in FIELD_MAP}
                if 'published_raw' in supplied:
                    supplied.add('published_at')
                if item.get('related_queries') is not None:
                    supplied.add('related_queries')
                contract = encode(envelope(SOURCE_CODE, run['origin'], seen, published_at=item['published_at'],
                                           fields={k: item[k] for k in supplied}, request=REQUEST,
                                           observation={'run_id': run_id, 'item_key': item['source_id']}))
                t.insert_absent('rss_observations', dict(run_id=run_id, item_key=key, payload_ref=ref,
                                                         classification_json=json.dumps(c, ensure_ascii=False), contract_json=contract, observed_at=seen))
                observed.append(dict(observed=payload, contract_json=contract, id=number, source_id=item['source_id'], published_at=item['published_at']))
                if c['classification'] in counts:
                    counts[c['classification']] += 1
                    new_counts[c['classification']] += created
            ok = error is None
            status = http_status if type(http_status) is int else None
            finished = dict(finished_at=seen, success=int(ok), error=None if ok else str(error)[:1000], http_status=status,
                            item_count=len(items or []), new_count=new_count, warnings_json=json.dumps(warnings or [], ensure_ascii=False),
                            food_count=counts['FOOD'] if ok else None, review_count=counts['REVIEW'] if ok else None,
                            new_food_count=new_counts['FOOD'] if ok else None, new_review_count=new_counts['REVIEW'] if ok else None,
                            classifier_version=c['classifier_version'] if items and ok else None)
            t.update('rss_runs', {'run_id': run_id}, **finished)
            # A failed run is staged as not observed, never as a measured zero (as CollectionService does).
            staged = _Staging(self.execution, t).rss_observed(dict(finished, id=run_id, origin=run['origin'], contract_json=run['contract_json']),
                                                              observed if ok else [])
        return {'new_count': new_count, 'food_count': counts['FOOD'] if ok else None, 'review_count': counts['REVIEW'] if ok else None,
                'ingestion': {k: v for k, v in staged.items() if k not in ('storable_metrics', 'transient_metrics')}}

    # Reads (one read transaction each, as Storage)

    @staticmethod
    def _items(t, item_id=None):
        """RSS items as the SQLite items rows (id, source_id, keyword, published_at, first_seen, last_seen, payload)."""
        where = 'payload_ref=:b_p' if item_id is not None else "payload_ref LIKE 'items:%'"
        binds = {'b_p': 'items:%d' % item_id} if item_id is not None else {}
        out = []
        for c in t.rows("SELECT * FROM source_contents WHERE source_code='google_trends_rss' AND " + where, 'source_contents', **binds):
            number = _number(c['payload_ref'])
            if number is None:
                continue
            body = json.loads(c['content_json'])
            out.append({'id': number, 'item_key': c['item_key'], 'source_id': body.get('source_id'), 'keyword': body.get('keyword'),
                        'published_at': body.get('published_at'), 'first_seen': c['captured_at'], 'last_seen': c['refreshed_at'], 'payload': body})
        return out

    @staticmethod
    def _keywords(t):
        """Every item's keyword and seen window (a JSON path read, so no content body is fetched)."""
        path = "JSON_VALUE(content_json,'$.keyword' RETURNING VARCHAR2(1000))" if t.oracle else "json_extract(content_json,'$.keyword')"
        return [dict(keyword=r['keyword'], first_seen=_ts(r['captured_at']), last_seen=_ts(r['refreshed_at'])) for r in t.rows(
            f"SELECT {path} AS keyword,captured_at,refreshed_at FROM source_contents WHERE source_code='google_trends_rss' AND payload_ref LIKE 'items:%'")]

    def status(self):
        with self.execution.reader() as t:
            run = _first(t, 'SELECT * FROM rss_runs WHERE finished_at IS NOT NULL ORDER BY run_id DESC', 'rss_runs')
            success = _first(t, 'SELECT finished_at FROM rss_runs WHERE success=1 ORDER BY run_id DESC', 'rss_runs')
            counts = {r['classification']: int(r['n']) for r in t.rows('SELECT classification,count(*) AS n FROM rss_classifications GROUP BY classification')}
            keywords = self._keywords(t)
            observations = int(t.scalar('SELECT count(*) AS n FROM rss_observations'))
            current, latest_publication = {}, None
            if run and run['success']:
                current = {r['classification']: int(r['n']) for r in t.rows('''SELECT c.classification,count(*) AS n FROM rss_observations o
                    JOIN rss_classifications c ON c.item_key=o.item_key WHERE o.run_id=:b_r GROUP BY c.classification''', b_r=run['run_id'])}
                values = [json.loads(r['contract_json']).get('published_at')
                          for r in t.rows('SELECT * FROM rss_observations WHERE run_id=:b_r', 'rss_observations', b_r=run['run_id'])]
                latest_publication = max((v for v in values if v), default=None)
        latest = None
        if run:
            latest = {k: run[k] for k in ('started_at', 'finished_at', 'success', 'error', 'http_status', 'item_count', 'new_count', 'food_count',
                                          'review_count', 'new_food_count', 'new_review_count', 'classifier_version', 'origin')}
            latest.update(id=run['run_id'], contract=json.loads(run['contract_json']), warnings=json.loads(run['warnings_json']))
            latest['discovery_state'] = ('NEW_ITEMS' if latest['new_count'] else 'NO_NEW_ITEMS') if latest['success'] else 'COLLECTION_FAILED'
            latest['current_food_count'] = current.get('FOOD', 0) if latest['success'] else None
            latest['current_review_count'] = current.get('REVIEW', 0) if latest['success'] else None
            latest['latest_publication_at'] = latest_publication
            age = (datetime.fromisoformat(latest['finished_at']) - datetime.fromisoformat(latest_publication)).total_seconds() if latest_publication else None
            latest['feed_age_seconds'] = max(0, age) if age is not None else None
        return {'latest_run': latest, 'last_success_at': success['finished_at'] if success else None,
                'total_keywords': len(keywords), 'food_count': counts.get('FOOD', 0),
                'review_count': counts.get('REVIEW', 0), 'non_food_count': counts.get('NON_FOOD', 0),
                'distinct_keywords': len({k['keyword'] for k in keywords if k['keyword'] is not None}),
                'last_discovery_at': max((k['first_seen'] for k in keywords), default=None), 'observation_count': observations,
                'storage': self.execution.storage_label()}

    def trends(self, category='ALL', query='', item_id=None):
        with self.execution.reader() as t:
            items = self._items(t, item_id)
            one = ' AND item_key=:b_k' if item_id is not None and items else ''
            binds = {'b_k': items[0]['item_key']} if one else {}
            labels = {r['item_key']: r for r in t.rows('SELECT * FROM rss_classifications WHERE 1=1' + one, 'rss_classifications', **binds)}
            contracts = {r['item_key']: r['contract_json'] for r in t.rows('''SELECT * FROM rss_observations o
                WHERE o.run_id=(SELECT max(x.run_id) FROM rss_observations x WHERE x.item_key=o.item_key)''' + one.replace('item_key', 'o.item_key'),
                'rss_observations', **binds)}
            keywords = self._keywords(t)
        aggregate = {}  # Storage: min(first_seen), max(last_seen), count(*) over every item with the same keyword
        for k in keywords:
            first, last, n = aggregate.get(k['keyword'], (k['first_seen'], k['last_seen'], 0))
            aggregate[k['keyword']] = (min(first, k['first_seen']), max(last, k['last_seen']), n + 1)
        results = []
        for item in items:
            label = labels.get(item['item_key'])
            if label is None:
                continue
            if category in ('FOOD', 'REVIEW', 'NON_FOOD') and label['classification'] != category:
                continue
            if query and query.lower() not in label['normalized_name'].lower():
                continue
            r = {k: item[k] for k in ('id', 'source_id', 'keyword', 'published_at', 'first_seen', 'last_seen')}
            r.update(normalized_name=label['normalized_name'], classification=label['classification'], reasons=json.loads(label['reasons_json']),
                     classifier_version=label['classifier_version'], entity_type=label['entity_type'], rule_score=label['rule_score'],
                     score_basis=label['score_basis'], evidence_news=json.loads(label['evidence_news_json']), classified_at=label['classified_at'])
            r['contract'] = json.loads(contracts[item['item_key']]) if item['item_key'] in contracts else None
            r.update(item['payload'])
            first, last, n = aggregate[item['keyword']]
            r.update(keyword_first_seen=first, keyword_last_seen=last, occurrence_count=n)
            if item_id is None:
                r.pop('raw_xml', None)
            results.append(r)
        # Storage orders by first_seen, published_at, id (all descending; a missing value sorts last).
        results.sort(key=lambda r: (r['first_seen'] or '', r['published_at'] or '', r['id']), reverse=True)
        return results

    def export(self):
        """RSS-only audit export in the SQLite shapes; the raw response is not retained on the target."""
        with self.execution.reader() as t:
            items = self._items(t)
            numbers = {i['item_key']: i['id'] for i in items}
            labels = t.rows('SELECT * FROM rss_classifications ORDER BY item_key', 'rss_classifications')
            observations = t.rows('SELECT * FROM rss_observations ORDER BY run_id,item_key', 'rss_observations')
            runs = t.rows('SELECT * FROM rss_runs ORDER BY run_id', 'rss_runs')
            history = t.rows('SELECT * FROM rss_classification_history ORDER BY recorded_at,history_key', 'rss_classification_history')
        result = {'exported_at': self.now(), 'format_version': 1, 'encoding': 'raw_response_base64 is original bytes',
                  'score_notice': 'Rule scores are not probabilities', 'storage': self.execution.storage_label(), 'raw_response_retained': False}
        result['items'] = [{k: i[k] for k in ('id', 'source_id', 'keyword', 'published_at', 'first_seen', 'last_seen', 'payload')}
                           for i in sorted(items, key=lambda i: i['id'])]
        result['candidates'] = [dict(item_id=numbers.get(r['item_key']), normalized_name=r['normalized_name'], classification=r['classification'],
                                     reasons=json.loads(r['reasons_json']), classifier_version=r['classifier_version'], entity_type=r['entity_type'],
                                     rule_score=r['rule_score'], score_basis=r['score_basis'], evidence_news=json.loads(r['evidence_news_json']),
                                     classified_at=r['classified_at']) for r in labels]
        result['observations'] = [dict(run_id=r['run_id'], item_id=numbers.get(r['item_key']), classification_json=json.loads(r['classification_json']),
                                       contract_json=r['contract_json'], observed_at=r['observed_at']) for r in observations]
        result['runs'] = [dict({k: v for k, v in r.items() if k not in ('run_id', 'warnings_json')}, id=r['run_id'],
                               warnings=json.loads(r['warnings_json']), raw_response_base64=None) for r in runs]
        result['classification_history'] = [dict(history_key=r['history_key'], item_id=numbers.get(r['item_key']), recorded_at=r['recorded_at'],
                                                  result_json=json.loads(r['result_json'])) for r in history]
        return result

    def unimported_runs(self, limit):
        """Successful runs with a FOOD/REVIEW item that has no candidate yet (the SQLite bridge's retry query)."""
        with self.execution.reader() as t:
            rows = t.rows("""SELECT DISTINCT o.run_id FROM rss_observations o JOIN rss_runs r ON r.run_id=o.run_id
                JOIN rss_classifications c ON c.item_key=o.item_key WHERE r.success=1 AND c.classification IN ('FOOD','REVIEW')
                AND NOT EXISTS (SELECT 1 FROM food_candidates f WHERE f.source_code='google_trends_rss' AND f.source_item_key=o.item_key)""")
        return sorted(int(r['run_id']) for r in rows)[:limit]


class OracleRssService:
    """CollectionService on the target: the same summary, lock and status; mock fetchers only."""

    def __init__(self, execution, foods, observations, interval=1800, timeout=20, fetcher=None):
        self.execution, self.storage = execution, OracleRssStore(execution)
        self.foods, self.observations = foods, observations
        self.interval, self.timeout, self.fetcher = interval, timeout, fetcher
        self.origin = 'mock'  # a live fetch is refused below, so every run collected here is a mock run
        self.lock = threading.Lock()

    def collect(self):
        if self.fetcher is None:
            return {'accepted': False, 'success': False, 'error': LIVE_DISABLED,
                    'message': 'Oracle 런타임의 실제 RSS 수집은 별도 승인 전까지 비활성입니다(Mock 응답만 수집).'}
        if not self.lock.acquire(blocking=False):
            return {'accepted': False, 'message': '이미 수집 중입니다'}
        run_id = raw = status = None
        try:
            run_id = self.storage.start(origin=self.origin)
            raw, status = self.fetcher(self.timeout)
            items, warnings = collector.parse(raw)
            summary = self.storage.finish(run_id, items, raw, status, warnings=warnings)
            # Local identity preparation only. A failed bridge must not rewrite a successful RSS run.
            try:
                summary['entity_resolution'] = self.foods.import_rss_run(run_id)
            except Exception:
                logging.warning('RSS entity resolution pending retry; source collection is preserved')
                summary['entity_resolution'] = {'state': 'pending_retry', 'run_id': run_id}
            self._link(summary['ingestion'])
            return {'accepted': True, 'success': True, 'run_id': run_id, 'item_count': len(items), **summary}
        except Exception as exc:
            logging.exception('Collection failed')
            error = f'{type(exc).__name__}: {exc}'
            if run_id is not None:
                try:
                    self._link(self.storage.finish(run_id, raw=raw, http_status=getattr(exc, 'code', status), error=error)['ingestion'])
                except Exception:
                    logging.exception('Could not persist collection error')
            return {'accepted': True, 'success': False, 'error': error, 'run_id': run_id}
        finally:
            self.lock.release()

    def _link(self, ingestion):
        if ingestion.get('ingest_run_id'):
            ingestion['linking'] = ingest_safely(self.observations.link_run, ingestion['ingest_run_id'])
        return ingestion

    def status(self):
        return dict(self.storage.status(), collecting=self.lock.locked(), interval_seconds=self.interval, source_url=collector.SOURCE,
                    live_enabled=False)


class OracleExecutionController(ExecutionController):
    """ExecutionController over the Oracle runtime: recover-rss retries the candidate bridge from the target's RSS rows."""

    def __init__(self, execution, adapters=None, ingestion=None, observations=None):
        super().__init__(execution, adapters, ingestion, observations)
        self.rss = None  # attached by runtime_storage.oracle_backend

    def _unimported_rss_runs(self, limit):
        return OracleRssStore(self.execution).unimported_runs(limit)

    def _rss_foods(self):
        from .oracle_foods import OracleFoodService
        return self.rss.foods if self.rss is not None else OracleFoodService(self.execution)
