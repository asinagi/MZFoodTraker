"""Food-candidate mapping and daily collection; persist operational metadata only."""
import hashlib
import json
import logging
import threading
import time
import uuid
from .classifier import normalize
from .naver_datalab import NaverClient, NaverError, request_body, today
from .storage import utcnow
from .contracts import envelope, encode
from .entity_types import can_track_legacy

# Explicit, exact aliases only. No fuzzy matching or generated food associations.
ALIASES = {'두쫀쿠': ['두쫀쿠', '두바이 쫀득 쿠키', '두바이쫀득쿠키']}


def keyword_group(name):
    name = normalize(name)
    for title, aliases in ALIASES.items():
        if name in aliases:
            return {'groupName': title, 'keywords': list(aliases)}
    return {'groupName': name, 'keywords': [name]}


class NaverService:
    def __init__(self, storage, config, client=None):
        self.storage, self.config = storage, config
        self.client = client or NaverClient(config)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

    def targets(self):
        """Pure read projection; absent persisted target rows remain pending."""
        with self.storage.reader() as db:
            rows = db.execute('''SELECT c.item_id,c.normalized_name,c.entity_type,t.last_attempt_at,
                t.last_success_at,coalesce(t.status,'pending') AS status FROM candidates c
                LEFT JOIN naver_targets t ON t.candidate_id=c.item_id
                WHERE c.classification='FOOD' ORDER BY c.item_id''').fetchall()
        return [dict(candidate_id=r['item_id'], topic=keyword_group(r['normalized_name'])['groupName'],
                     keywords=keyword_group(r['normalized_name'])['keywords'], source='naver_datalab',
                     last_attempt_at=r['last_attempt_at'], last_success_at=r['last_success_at'], status=r['status'])
                for r in rows if can_track_legacy(r['entity_type'])]

    def sync_targets(self):
        """Explicit write preparation, called only by collection/query operations."""
        with self.storage.connect() as db:
            rows = db.execute("SELECT item_id,normalized_name,entity_type FROM candidates WHERE classification='FOOD' ORDER BY item_id").fetchall()
            for row in rows:
                if not can_track_legacy(row['entity_type']):
                    continue
                group = keyword_group(row['normalized_name'])
                db.execute('''INSERT INTO naver_targets(candidate_id,topic,keywords_json) VALUES (?,?,?)
                    ON CONFLICT(candidate_id) DO UPDATE SET topic=excluded.topic,keywords_json=excluded.keywords_json''',
                           (row['item_id'], group['groupName'], json.dumps(group['keywords'], ensure_ascii=False)))
        return self.targets()

    def status(self):
        targets = self.targets()
        with self.storage.reader() as db:
            counts = dict(db.execute('SELECT status,count(*) FROM naver_calls GROUP BY status'))
            daily = db.execute('SELECT count(*) FROM naver_calls WHERE day=?', (today().isoformat(),)).fetchone()[0]
            latest = db.execute('SELECT requested_at,finished_at,status,http_status FROM naver_calls ORDER BY id DESC LIMIT 1').fetchone()
        state = ('not_configured' if not self.config.configured else
                 'disabled' if not self.config.enabled else latest['status'] if latest else 'unverified')
        return {'source': 'naver_datalab', 'configured': self.config.configured,
                'automatic_enabled': self.config.configured and self.config.enabled,
                'scheduler_running': bool(getattr(self, 'thread', None) and self.thread.is_alive()),
                'status': state, 'collecting': self.lock.locked(), 'interval_seconds': self.config.interval_seconds,
                'daily_requests': daily, 'daily_limit': self.config.daily_limit,
                'total_requests': sum(counts.values()), 'counts': counts,
                'latest_request': dict(latest) if latest else None, 'targets': targets,
                'retention': 'no_response_storage_or_cache',
                'notice': 'NAVER 상대 검색 지수 (0~100). 요청이 다른 지수끼리 순위를 비교하지 않습니다.'}

    def query(self, candidate_ids, days=30):
        if not isinstance(candidate_ids, list) or not candidate_ids or any(type(i) is not int for i in candidate_ids):
            raise NaverError('validation')
        selected = [t for t in self.targets() if t['candidate_id'] in candidate_ids]
        if set(candidate_ids) != {t['candidate_id'] for t in selected}:
            raise NaverError('validation')
        self.sync_targets()
        groups = {t['topic']: {'groupName': t['topic'], 'keywords': t['keywords']} for t in selected}
        return self._query(selected, groups, days)

    def query_keywords(self, keywords, days=30):
        """Explicit user queries do not create RSS items or FOOD classifications."""
        if not isinstance(keywords, list) or not keywords or any(not isinstance(k, str) for k in keywords):
            raise NaverError('validation')
        groups = {}
        for keyword in keywords:
            group = keyword_group(keyword)
            groups[group['groupName']] = group
        return self._query([], groups, days)

    def _query(self, selected, groups, days):
        candidate_ids = [t['candidate_id'] for t in selected]
        body = request_body([groups[k] for k in sorted(groups)], days=days)
        contract = envelope('naver_datalab', getattr(self.client, 'origin', 'legacy_unknown'), utcnow(),
                            request=body, observation={'trigger': 'rss_food' if selected else 'user_keyword'})
        if not self.config.configured:
            raise NaverError('not_configured')
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        owner = uuid.uuid4().hex
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            blocked = db.execute('SELECT until_epoch FROM naver_guards WHERE fingerprint IN (?,?)', (fingerprint, '*')).fetchall()
            if any(r[0] > time.time() for r in blocked):
                raise NaverError('duplicate')
            db.execute('INSERT OR REPLACE INTO naver_guards VALUES (?,?,?)', (fingerprint, owner, time.time() + 600))
        state, retry_after = 'internal', 0
        try:
            def reserve():
                with self.storage.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    count = db.execute('SELECT count(*) FROM naver_calls WHERE day=?', (today().isoformat(),)).fetchone()[0]
                    if count >= self.config.daily_limit:
                        raise NaverError('budget')
                    ticket = db.execute('''INSERT INTO naver_calls(day,fingerprint,requested_at,status,request_json,candidate_ids,origin,contract_json)
                        VALUES (?,?,?,'running',?,?,?,?)''',
                        (today().isoformat(), fingerprint, utcnow(), json.dumps(body, ensure_ascii=False),
                         json.dumps(sorted(set(candidate_ids))), contract['origin'], encode(contract))).lastrowid
                    return ticket

            def finish(ticket, result, status):
                with self.storage.connect() as db:
                    db.execute('UPDATE naver_calls SET status=?,http_status=?,finished_at=? WHERE id=?',
                               (result, status, utcnow(), ticket))

            results = self.client.search(body, reserve, finish)
            state = 'success' if any(r['data'] for r in results) else 'no_data'
            return {'success': True, 'status': state, 'source': 'naver_datalab',
                    'metric': 'relative_search_index', 'range': [0, 100], 'request_id': owner,
                    'origin': 'rss_food' if selected else 'user_keyword',
                    'start_date': body['startDate'], 'end_date': body['endDate'], 'time_unit': body['timeUnit'],
                    'filters': {}, 'collected_at': utcnow(),
                    'contract': envelope('naver_datalab', contract['origin'], utcnow(),
                                         fields={k: results if k == 'results' else body[k]
                                                 for k in ('startDate', 'endDate', 'timeUnit', 'results')},
                                         request=body, observation=contract['observation_conditions']),
                    'results': [dict(r, candidate_ids=[t['candidate_id'] for t in selected if t['topic'] == r['title']])
                                for r in results]}
        except NaverError as exc:
            state, retry_after = exc.kind, exc.retry_after
            raise
        finally:
            stamp = utcnow()
            with self.storage.connect() as db:
                db.execute('UPDATE naver_guards SET until_epoch=? WHERE fingerprint=? AND owner=?',
                           (time.time() + max(self.config.cooldown_seconds, retry_after), fingerprint, owner))
                if state == 'rate_limit':
                    db.execute('INSERT OR REPLACE INTO naver_guards VALUES (?,?,?)',
                               ('*', owner, time.time() + max(60, retry_after)))
                for target in selected:
                    db.execute('''UPDATE naver_targets SET last_attempt_at=?,status=?,
                        last_success_at=CASE WHEN ? IN ('success','no_data') THEN ? ELSE last_success_at END
                        WHERE candidate_id=?''', (stamp, state, state, stamp, target['candidate_id']))

    def collect(self, automatic=False):
        if not self.config.configured:
            return {'success': False, 'error_type': 'not_configured', 'requests': 0}
        if automatic and not self.config.enabled:
            return {'success': False, 'error_type': 'disabled', 'requests': 0}
        if not self.lock.acquire(blocking=False):
            return {'success': False, 'error_type': 'duplicate', 'requests': 0}
        try:
            groups = {}
            for target in self.sync_targets():
                groups.setdefault(target['topic'], []).append(target)
            if automatic:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                groups = {k: v for k, v in groups.items() if not any(
                    t['last_attempt_at'] and (now - datetime.fromisoformat(t['last_attempt_at'])).total_seconds()
                    < self.config.interval_seconds for t in v)}
            # Oldest attempt first prevents starvation when targets exceed the daily budget.
            topics = sorted(groups, key=lambda k: max((t['last_attempt_at'] or '' for t in groups[k]), default=''))
            batches = []
            for offset in range(0, len(topics), 5):
                ids = [t['candidate_id'] for k in topics[offset:offset+5] for t in groups[k]]
                try:
                    result = self.query(ids)
                    # Automatic runs discard the response after this operation; no result cache.
                    if automatic:
                        result = {k: result[k] for k in ('success', 'status', 'collected_at')}
                    batches.append(result)
                except NaverError as exc:
                    batches.append({'success': False, 'error_type': exc.kind, 'error': str(exc)})
                    if exc.kind in ('budget', 'authentication', 'rate_limit', 'duplicate'):
                        break
            return {'success': all(r['success'] for r in batches), 'batches': batches, 'requests': len(batches)}
        finally:
            self.lock.release()

    def start(self):
        if not self.config.configured or not self.config.enabled or (getattr(self, 'thread', None) and self.thread.is_alive()):
            return
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.collect(automatic=True)
                except Exception:
                    logging.error('NAVER metadata collection failed; retry on next scheduler tick')
                self.stop_event.wait(60)
        self.thread = threading.Thread(target=loop, name='naver-scheduler', daemon=True)
        self.thread.start()
