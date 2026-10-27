"""Scheduled search with query provenance, not content classification or trend scores."""
import logging
import threading
import time
import json
import uuid
from datetime import datetime, timedelta, timezone
from .storage import utcnow
from .youtube import YouTubeClient, YouTubeError, items, parse_video, video_id
from .youtube_storage import YouTubeStore, pacific_day
from .contracts import envelope


class YouTubeService:
    def __init__(self, storage, config, client=None):
        self.storage, self.config = storage, config
        self.store = YouTubeStore(storage)
        self.client = client or YouTubeClient(config)
        self.stop_event = threading.Event()
        self.automatic_scheduling = False
        self.origin = getattr(self.client, 'origin', 'legacy_unknown')
        self.run_id = None
        self.metrics = None

    def _begin_metrics(self):
        self.metrics = {'search_result_rows': 0, 'detail_result_rows': 0,
                        'http_statuses': {'search': {}, 'videos': {}}}
        self.search_ids, self.saved_ids, self.new_ids, self.updated_ids, self.food_links = set(), set(), set(), set(), set()

    def _request(self, endpoint, params, owner, query_id=None):
        def finish(ticket, state, status, result_count=None):
            self.store.finish(ticket, state, status, result_count)
            if self.metrics is not None:
                bucket = self.metrics['http_statuses'][endpoint]
                key = str(status) if status is not None else 'no_http_response'
                bucket[key] = bucket.get(key, 0)+1
        return self.client.get(endpoint, params,
                               lambda ep: self.store.reserve(ep, query_id, owner, self.config, self.origin, self.run_id), finish)

    def _details(self, identities, owner, query_id=None, request_context=None):
        identities = list(dict.fromkeys(identities))
        valid = set()
        for offset in range(0, len(identities), 50):
            batch = identities[offset:offset+50]
            payload = self._request('videos', {'part': 'snippet,statistics,contentDetails,status', 'id': ','.join(batch)}, owner, query_id)
            parsed = [parse_video(item) for item in items(payload)]
            if any(v['video_id'] not in batch for v in parsed) or len({v['video_id'] for v in parsed}) != len(parsed):
                raise YouTubeError('invalid_response')
            with self.storage.connect() as db:
                existing = {r[0] for r in db.execute('SELECT video_id FROM youtube_videos WHERE video_id IN ('+','.join('?' for _ in batch)+')', batch)}
            saved = self.store.save_videos(batch, parsed, self.origin, self.run_id, request_context)
            valid.update(saved)
            if self.metrics is not None:
                self.metrics['detail_result_rows'] += len(parsed)
                self.new_ids.update(saved-existing)
                self.updated_ids.update((saved & existing)-self.new_ids)
                self.saved_ids.update(saved)
        return valid

    def _search(self, query, owner, max_pages, *, max_results=50, checkpoint=True):
        now = datetime.now(timezone.utc)
        after = query['after_at'] or (now-timedelta(days=7)).isoformat()
        before = query['before_at'] or now.isoformat()
        token, seen_tokens, identities = query['page_token'], set(), set()
        for _ in range(max_pages):
            params = dict(part='snippet', type='video', regionCode='KR', relevanceLanguage='ko', order='date',
                          publishedAfter=after, publishedBefore=before, maxResults=max_results, q=query['query'])
            if token:
                if token in seen_tokens:
                    raise YouTubeError('invalid_response')
                seen_tokens.add(token)
                params['pageToken'] = token
            try:
                payload = self._request('search', params, owner, query['id'])
            except YouTubeError as exc:
                if checkpoint and token and exc.kind == 'validation':
                    with self.storage.connect() as db:
                        db.execute('UPDATE youtube_queries SET page_token=NULL,token_at=NULL WHERE id=?', (query['id'],))
                raise
            page_ids = []
            try:
                for item in items(payload):
                    page_ids.append(video_id(item['id']['videoId']))
                    if not isinstance(item['snippet'], dict):
                        raise ValueError()
                next_token = payload.get('nextPageToken')
                if next_token is not None and (not isinstance(next_token, str) or not next_token or len(next_token) > 4096):
                    raise ValueError()
                if next_token and (next_token == token or next_token in seen_tokens):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise YouTubeError('invalid_response') from None
            if self.metrics is not None:
                self.metrics['search_result_rows'] += len(page_ids)
                self.search_ids.update(page_ids)
            # Detail response supplies the full snippet and availability. Missing/private videos are removed.
            context = {k: v for k, v in params.items() if k != 'pageToken'}
            valid = self._details(page_ids, owner, query['id'], request_context=dict(context, endpoint='search'))
            identities.update(valid)
            self.store.match(query['id'], valid)
            if self.metrics is not None and query['food_id'] is not None:
                self.food_links.update((identity, query['food_id']) for identity in valid)
            token = next_token
            if not checkpoint:
                break
            with self.storage.connect() as db:
                if token:
                    db.execute('''UPDATE youtube_queries SET after_at=?,before_at=?,page_token=?,token_at=?
                                  WHERE id=?''', (after, before, token, utcnow(), query['id']))
                else:
                    # Fixed upper bound prevents losing new uploads while resuming paginated windows.
                    watermark = (datetime.fromisoformat(before)-timedelta(minutes=10)).isoformat()
                    db.execute('''UPDATE youtube_queries SET after_at=?,before_at=NULL,page_token=NULL,token_at=NULL
                                  WHERE id=?''', (watermark, query['id']))
            if not token:
                break
        return identities

    def _refresh(self, owner, force=False):
        cutoff = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        with self.storage.connect() as db:
            identities = [r[0] for r in db.execute('SELECT video_id FROM youtube_videos WHERE refreshed_at<? ORDER BY refreshed_at LIMIT 500',
                                                   (utcnow() if force else cutoff,))]
        return self._details(identities, owner) if identities else set()

    def collect(self, automatic=False, small=False, refresh_only=False, verification=False):
        self.store.purge()
        if not self.config.configured:
            return {'success': False, 'error_type': 'not_configured', 'requests': 0}
        if automatic and not self.config.enabled:
            return {'success': False, 'error_type': 'disabled', 'requests': 0}
        try:
            with self.store.operation() as owner:
                if automatic and float(self.store.state('next_run_epoch', '0')) > time.time():
                    return {'success': True, 'skipped': True, 'requests': 0}
                before_calls = self._call_count()
                self.run_id = uuid.uuid4().hex
                self._begin_metrics()
                with self.storage.connect() as db:
                    db.execute('INSERT INTO youtube_runs(id,started_at,trigger,origin,status) VALUES (?,?,?,?,?)',
                               (self.run_id, utcnow(), 'verification' if verification else 'refresh' if refresh_only else 'automatic' if automatic else 'manual', self.origin, 'running'))
                found, completed = set(), 0
                state = 'success'
                try:
                    if refresh_only:
                        found = self._refresh(owner, force=True)
                    else:
                        queries = self.store.sync_queries(self.config)
                        discovery = [q for q in queries if q['mode'] == 'discovery']
                        tracking = [q for q in queries if q['mode'] == 'tracking']
                        limit = 1 if verification else 2 if small else self.config.queries_per_run
                        # Reserve slots for both modes; rotate within each mode by persisted attempts.
                        discovery_slots = min(len(discovery), max(1, limit-2)) if tracking else limit
                        chosen = discovery[:discovery_slots]+tracking[:max(0, limit-discovery_slots)]
                        if verification:
                            # Verify one recent search independently of normal pagination/watermarks.
                            chosen = [dict((tracking or discovery)[0], after_at=None, before_at=None, page_token=None)]
                        if not small and not verification:
                            self._refresh(owner)
                        for query in chosen:
                            if not verification:
                                with self.storage.connect() as db:
                                    db.execute('UPDATE youtube_queries SET last_attempt=? WHERE id=?', (utcnow(), query['id']))
                            found.update(self._search(query, owner, 1 if small or verification else self.config.max_pages,
                                                      max_results=5 if verification else 50, checkpoint=not verification))
                            completed += 1
                except YouTubeError as exc:
                    state = exc.kind
                except Exception:
                    state = 'internal'
                    logging.error('YouTube collection failed; upstream text omitted')
                finally:
                    if not verification:
                        self.store.set_state('next_run_epoch', time.time()+self.config.interval_seconds)
                    if state == 'success' and self._call_count() == before_calls:
                        state = 'no_request'
                    self.store.set_state('last_run_at', utcnow())
                    self.store.set_state('last_status', state)
                    if state in ('network', 'upstream', 'invalid_response', 'internal'):
                        self.store.set_state('cooldown_until', time.time()+60)
                    self.store.purge()
                result = {'success': state in ('success', 'no_request'), 'status': state, 'error_type': None if state in ('success', 'no_request') else state,
                        'requests': self._call_count()-before_calls, 'completed_queries': completed,
                        'videos_received': len(self.saved_ids), 'source': 'youtube', 'origin': self.origin, 'run_id': self.run_id,
                        **self.metrics, 'unique_search_videos': len(self.search_ids),
                        'duplicate_search_rows': self.metrics['search_result_rows']-len(self.search_ids),
                        'saved_videos': len(self.saved_ids), 'new_videos': len(self.new_ids), 'updated_existing_videos': len(self.updated_ids),
                        'food_connections': len(self.food_links),
                        'food_extraction': 'disabled_pending_policy_approval',
                        'mapping': 'search_query_provenance_only'}
                result['contract'] = envelope('youtube', self.origin, utcnow(),
                    observation={'mapping': 'search_query_provenance_only', 'snapshot_only': True})
                result['live_connection_verified'] = self.origin == 'live_api' and any('200' in b for b in self.metrics['http_statuses'].values())
                with self.storage.connect() as db:
                    db.execute('UPDATE youtube_runs SET finished_at=?,status=?,result_json=? WHERE id=?',
                               (utcnow(), state, json.dumps(result, ensure_ascii=False), self.run_id))
                return result
        except YouTubeError as exc:
            return {'success': False, 'error_type': exc.kind, 'requests': 0}

    def _call_count(self):
        with self.storage.reader() as db:
            return db.execute('SELECT count(*) FROM youtube_calls').fetchone()[0]

    def delete_data(self):
        with self.store.operation():
            with self.storage.connect() as db:
                db.execute('PRAGMA secure_delete=ON')
                count = db.execute('DELETE FROM youtube_videos').rowcount
                db.execute('UPDATE youtube_queries SET page_token=NULL,token_at=NULL,before_at=NULL')
            return {'success': True, 'deleted_videos': count, 'notice': '로컬 저장 데이터만 삭제했습니다. YouTube 원본은 삭제되지 않습니다.'}

    def status(self):
        with self.storage.reader() as db:
            daily = dict(db.execute('SELECT endpoint,count(*) FROM youtube_calls WHERE day=? GROUP BY endpoint', (pacific_day(),)))
            queries = [dict(r) for r in db.execute('''SELECT q.id,q.query,q.mode,q.food_id,f.name AS food_name,q.last_attempt,
                q.page_token IS NOT NULL AS has_more,count(c.id) AS daily_requests
                FROM youtube_queries q LEFT JOIN youtube_foods f ON q.food_id=f.id
                LEFT JOIN youtube_calls c ON c.query_id=q.id AND c.endpoint='search' AND c.day=?
                WHERE q.active=1 GROUP BY q.id ORDER BY q.id''', (pacific_day(),))]
            video_count = db.execute('SELECT count(*) FROM youtube_videos WHERE expires_at>?', (utcnow(),)).fetchone()[0]
            latest = db.execute('SELECT result_json FROM youtube_runs WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1').fetchone()
            latest_run = json.loads(latest[0]) if latest and latest[0] else None
            last_live = db.execute("SELECT max(finished_at) FROM youtube_calls WHERE origin='live_api' AND status='success' AND http_status=200").fetchone()[0]
            legacy_success = db.execute("SELECT count(*) FROM youtube_calls WHERE origin='legacy_unknown' AND http_status=200 AND status='success'").fetchone()[0]
            breakdown = [dict(r) for r in db.execute('''SELECT endpoint,origin,status,http_status,count(*) AS attempts,
                sum(result_count) AS response_items FROM youtube_calls WHERE day=? GROUP BY endpoint,origin,status,http_status''', (pacific_day(),))]
            origins = [dict(r) for r in db.execute('''SELECT coalesce(p.origin,'legacy_unknown') AS origin,count(*) AS videos
                FROM youtube_videos v LEFT JOIN youtube_video_provenance p ON v.video_id=p.video_id
                WHERE v.expires_at>? GROUP BY coalesce(p.origin,'legacy_unknown')''', (utcnow(),))]
        return {'source': 'youtube', 'configured': self.config.configured,
                'status_version': 2, 'latest_run': latest_run, 'daily_call_breakdown': breakdown,
                'video_origins': origins, 'last_live_http_success_at': last_live,
                'connection_status': 'live_verified' if last_live else 'legacy_http_success' if legacy_success else 'mock_only' if latest_run and latest_run['origin'] == 'mock' else 'unverified',
                'status': self.store.state('last_status', 'unverified') if self.config.configured else 'not_configured',
                'last_run_at': self.store.state('last_run_at'), 'collecting': float(self.store.state('lease_until', '0')) > time.time(),
                'automatic_enabled': self.config.enabled and self.config.configured,
                'collection_scheduler_running': self.automatic_scheduling and bool(getattr(self, 'thread', None) and self.thread.is_alive()),
                'scheduler_running': bool(getattr(self, 'thread', None) and self.thread.is_alive()),
                'interval_seconds': self.config.interval_seconds, 'quota_day_pt': pacific_day(),
                'search_requests': daily.get('search', 0), 'video_requests': daily.get('videos', 0),
                'search_budget': self.config.daily_search_limit, 'video_budget': self.config.daily_video_limit,
                'search_remaining_estimate': max(0, self.config.daily_search_limit-daily.get('search', 0)),
                'video_remaining_estimate': max(0, self.config.daily_video_limit-daily.get('videos', 0)),
                'quota_notice': '로컬 요청 시도(재시도·HTTP 응답 없는 실패 포함) 기준의 보수적 예산입니다. Google 실제 차감량·다른 앱 사용량은 알 수 없습니다. 기본 search 100회/일, 기타 API 10,000유닛/일, PT 자정 초기화.',
                'queries': queries, 'stored_videos': video_count, 'videos': self.store.videos(),
                'policy': {'food_extraction_enabled': False, 'content_classification_enabled': False,
                           'derived_metrics_enabled': False, 'retention_days': 29,
                           'mapping': 'search_match_unverified'},
                'notice': 'KR/ko는 한국 제작을 보장하지 않습니다. 검색 표본은 전체 발화량이 아닙니다. FOOD 연결은 앱 검색어 기준이며 영상 내용 검증 결과가 아닙니다.'}

    def start(self, automatic=True):
        if getattr(self, 'thread', None) and self.thread.is_alive():
            return
        self.automatic_scheduling = automatic
        def loop():
            while not self.stop_event.is_set():
                try:
                    self.store.purge()  # Retention runs even if the key or automatic collection is disabled.
                    if automatic and self.config.configured and self.config.enabled:
                        self.collect(automatic=True)
                except Exception:
                    logging.error('YouTube scheduler failed; next tick will retry')
                self.stop_event.wait(60)
        self.thread = threading.Thread(target=loop, name='youtube-scheduler', daemon=True)
        self.thread.start()
