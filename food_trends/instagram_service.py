import re
import hashlib
import threading
import time
import unicodedata
from .entity_types import can_track_legacy
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit
from .storage import utcnow
from .instagram_storage import InstagramStorage
from .instagram_graph import GraphClient, GraphError, MESSAGES


class InstagramService:
    def __init__(self, storage, config, client=None):
        self.storage, self.config = storage, config
        self.store = InstagramStorage(storage, config.user_id, config.caption_retention_days)
        self.client = client or GraphClient(config)
        self.stop_event = threading.Event()
        self.thread = None

    def credential_changed(self):
        saved = self.store.state('credential_fingerprint')
        return bool(self.config.configured and saved is not None and
                    saved != hashlib.sha256(self.config.token.encode()).hexdigest())

    def prepare_credentials(self):
        """Write preparation for collection/management, never for constructor or GET."""
        if self.config.configured:
            fingerprint = hashlib.sha256(self.config.token.encode()).hexdigest()
            if self.store.state('credential_fingerprint') != fingerprint:
                self.store.state('credential_fingerprint', fingerprint)
                self.store.state('token', {'status': 'unchecked', 'expires_at': None})
                self.store.state('connection', {'status': 'unverified'})

    def maintenance(self):
        """Local maintenance only; does not fetch or reprocess captions."""
        self.prepare_credentials()
        with self.storage.connect() as db:
            return self.store.analysis.purge_expired(db)

    @contextmanager
    def operation(self):
        from .execution_ownership import legacy_allowed
        if not legacy_allowed(self.storage,'instagram_graph','ig:'+self.config.user_id):
            raise GraphError('busy')
        if not self.config.configured:
            raise GraphError('not_configured')
        owner = self.store.acquire()
        if not owner:
            raise GraphError('busy')
        def renew():
            if not self.store.renew(owner):
                raise GraphError('busy')
        self.client.before_request = renew
        try:
            self.prepare_credentials()
            yield
        except GraphError as exc:
            if exc.kind in ('authentication', 'permission', 'rate_limit', 'network', 'upstream'):
                self.store.state('connection', {'status': exc.kind, 'checked_at': utcnow()})
            raise
        finally:
            self.client.before_request = lambda: None
            self.store.release(owner)

    def budget(self):
        usage = self.store.usage()
        local = {r['instagram_id'] or 'name:'+r['name'] for r in usage}
        remote = self.store.state('search_snapshot')
        fresh = bool(remote and remote['checked_at'] > (datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat())
        remote_ids = set(remote['ids']) if fresh else set()
        used = len(local | remote_ids)
        return {'local_unique_7d': len(local), 'server_unique_7d': len(remote['ids']) if remote else None,
                'server_checked_at': remote['checked_at'] if remote else None,
                'server_fresh': fresh, 'estimated_used': used, 'limit': 30,
                'soft_limit': self.config.search_soft_limit,
                'remaining_before_soft_limit': max(0, self.config.search_soft_limit-used),
                'reference_only': True,
                'message': '로컬 기록은 참고용입니다. 새 추적 시작 전 Meta 최근 검색 목록을 교차 확인합니다.'}

    def sync_budget(self):
        try:
            ids = self.client.recent_searches()
            self.store.state('search_snapshot', {'ids': sorted(ids), 'checked_at': utcnow()})
        except GraphError as exc:
            self.store.state('budget_sync_error', exc.kind)
            raise
        self.store.state('budget_sync_error', '')

    def reserve(self, tag):
        usage = self.store.usage()
        if any(r['name'] == tag['name'] for r in usage):
            return
        snapshot = self.store.state('search_snapshot')
        fresh = snapshot and snapshot['checked_at'] > (datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
        if not fresh:
            self.sync_budget()
            snapshot = self.store.state('search_snapshot')
        already_remote = tag['instagram_id'] and tag['instagram_id'] in snapshot['ids']
        if not already_remote and self.budget()['estimated_used'] >= self.config.search_soft_limit:
            raise GraphError('budget')
        # Conservative reservation before any hashtag request, including failed requests.
        self.store.record_usage(tag)

    @staticmethod
    def normalize(name):
        if not isinstance(name, str):
            raise GraphError('validation')
        name = unicodedata.normalize('NFC', name.strip().lstrip('#')).casefold()
        if not re.fullmatch(r'\w{1,100}', name, flags=re.UNICODE) or name.isdecimal():
            raise GraphError('validation')
        return name

    def start_tracking(self, name, candidate_id=None):
        if not self.config.enabled:
            raise GraphError('disabled')
        name = self.normalize(name)
        if candidate_id is not None:
            if type(candidate_id) is not int:
                raise GraphError('not_found')
            candidates = self.storage.trends(item_id=candidate_id)
            if not candidates:
                raise GraphError('not_found')
            if not can_track_legacy(candidates[0]['entity_type']):
                raise GraphError('validation')
        with self.operation():
            existing = next((h for h in self.store.tags() if h['name'] == name), None)
            tag = self.store.register(name, candidate_id)
            if existing and existing['active'] and existing['instagram_id']:
                return {'success': True, 'already_tracking': True, 'hashtag_id': tag['id']}
            # Authorize target explicitly, then resolve and collect under the same lease.
            self.store.approve(tag['id'])
            result = self._collect_tag(self.store.tag(tag['id']))
            return dict(result, hashtag_id=tag['id'])

    @staticmethod
    def clean_media(row):
        identifier = row.get('id')
        if not isinstance(identifier, str) or not identifier.isdigit():
            raise GraphError('invalid_response')
        published = None
        if row.get('timestamp'):
            try:
                stamp = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
                if stamp.tzinfo is None:
                    raise ValueError()
                published = stamp.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, AttributeError):
                raise GraphError('invalid_response') from None
        permalink = row.get('permalink')
        if permalink:
            if not isinstance(permalink, str):
                raise GraphError('invalid_response')
            url = urlsplit(permalink)
            if url.scheme != 'https' or url.hostname not in ('instagram.com', 'www.instagram.com') or url.username or url.password:
                permalink = None
        kind = row.get('media_type')
        if kind not in ('IMAGE', 'VIDEO', 'CAROUSEL_ALBUM'):
            kind = None
        caption = row.get('caption')
        if caption is not None and not isinstance(caption, str):
            raise GraphError('invalid_response')
        return {'id': identifier, 'published_at': published, 'permalink': permalink, 'media_type': kind, 'caption': caption,
                'provided_fields': [field for field, upstream in (('id', 'id'), ('published_at', 'timestamp'),
                    ('permalink', 'permalink'), ('media_type', 'media_type'), ('caption', 'caption')) if upstream in row]}

    def _collect_tag(self, tag):
        origin = getattr(self.client, 'origin', 'legacy_unknown')
        run_id = self.store.start_run(tag['id'], origin=origin,
                                     request={'hashtag_id': tag.get('instagram_id'), 'hashtag': tag['name'],
                                              'max_pages': self.config.max_pages, 'limit': 50,
                                              'api_version': self.config.version,
                                              'fields': ['id', 'caption', 'timestamp', 'media_type', 'permalink']})
        before = self.client.request_count
        media, returned, validated, truncated, error = {}, 0, 0, False, None
        try:
            if self.token_blocked():
                raise GraphError('authentication')
            self.reserve(tag)
            if not tag['instagram_id']:
                identifier = self.client.search(tag['name'])
                self.store.approve(tag['id'], identifier)
                self.store.searched(tag['id'])
                tag = self.store.tag(tag['id'])
                self.store.resolve_usage(tag)
            for rows, more in self.client.recent(tag['instagram_id']):
                returned += len(rows)
                for row in rows:
                    item = self.clean_media(row)
                    validated += 1
                    media[item['id']] = item
                truncated = more
            self.store.state('connection', {'status': 'connected', 'checked_at': utcnow()})
        except GraphError as exc:
            error = exc
        except Exception:
            error = GraphError('internal')
        if error:
            self.store.state('connection', {'status': error.kind, 'checked_at': utcnow()})
        run = self.store.finish_run(run_id, media, returned, self.client.request_count-before, error, truncated, validated)
        result = {'accepted': True, 'success': error is None, 'run': run}
        if error:
            result.update(error=str(error), error_type=error.kind)
        return result

    def collect(self, hashtag_id=None, due_only=False):
        if not self.config.enabled:
            raise GraphError('disabled')
        with self.operation():
            tags = self.store.tags(active_only=True)
            if hashtag_id is not None:
                tags = [t for t in tags if t['id'] == hashtag_id]
                if not tags:
                    raise GraphError('not_found')
            results = []
            for tag in tags:
                if due_only and not self.due(tag):
                    continue
                result = self._collect_tag(tag)
                results.append(result)
                if result.get('error_type') in ('authentication', 'rate_limit', 'permission', 'network', 'upstream'):
                    break
            return {'accepted': True, 'success': all(r['success'] for r in results), 'results': results}

    def due(self, tag):
        with self.storage.reader() as db:
            row = db.execute('SELECT finished_at FROM instagram_runs WHERE hashtag_id=? ORDER BY id DESC LIMIT 1', (tag['id'],)).fetchone()
        cutoff = (datetime.now(timezone.utc)-timedelta(minutes=self.config.interval_minutes)).isoformat()
        return not row or not row[0] or row[0] <= cutoff

    def status(self, candidate_id=None, days=7, hashtag_id=None, include_media=False):
        connection = ({'status': 'unverified'} if self.credential_changed() else
                      self.store.state('connection') or {'status': 'unverified'})
        if not self.config.configured:
            connection = {'status': 'not_configured'}
        elif not self.config.enabled:
            connection = {'status': 'disabled'}
        token = self.token_status()
        if self.config.configured and token and (token.get('status') == 'invalid' or token.get('expires_at') and token['expires_at'] <= utcnow()):
            connection = {'status': 'authentication'}
        with self.storage.reader() as db:
            lease = db.execute('SELECT expires_at FROM instagram_leases WHERE user_id=?', (self.config.user_id,)).fetchone()
        return {'configured': self.config.configured, 'enabled': self.config.enabled,
                'connection': dict(connection, message=MESSAGES.get(connection['status'], connection['status'])),
                'collecting': bool(lease and lease[0] > utcnow()),
                'interval_minutes': self.config.interval_minutes, 'budget': self.budget(),
                'token': token or {'status': 'unchecked', 'expires_at': None},
                'hashtags': [self.store.summary(t, days, include_media) for t in self.store.tags(candidate_id) if hashtag_id is None or t['id'] == hashtag_id],
                'limitations': '공개 API가 반환한 게시물의 관측치입니다. 전체 게시물 수가 아니며 응답 범위·페이지 제한·수집 중단에 따른 누락이 있을 수 있습니다. 빈 날짜는 미관측입니다. 시간·일 집계는 KST, 신규 기준은 최초 관측 시각입니다.'}

    def token_status(self):
        """Calculate expiry warnings locally; status polling never calls Meta."""
        token = dict({'status': 'unchecked', 'expires_at': None} if self.credential_changed() else
                     self.store.state('token') or {'status': 'unchecked', 'expires_at': None})
        token.update(expiry_status='unknown', warning=None, days_remaining=None, warning_days=7)
        if token.get('status') == 'invalid':
            token.update(expiry_status='invalid', warning='Instagram 토큰이 유효하지 않습니다. 새 토큰을 설정하고 재검사하세요.')
        elif token.get('expires_at'):
            try:
                expiry = datetime.fromisoformat(token['expires_at'].replace('Z', '+00:00'))
                if expiry.tzinfo is None:
                    raise ValueError()
                seconds = (expiry-datetime.now(timezone.utc)).total_seconds()
                token['days_remaining'] = max(0, -int(-seconds//86400))
                if seconds <= 0:
                    token.update(expiry_status='expired', warning='Instagram 토큰이 만료되어 자동 수집이 중단되었습니다. 새 토큰을 설정하고 재검사하세요.')
                elif seconds <= 7*86400:
                    token.update(expiry_status='expiring', warning='Instagram 토큰 만료가 7일 이내입니다. 만료 전에 새 토큰을 준비하고 승인 후 적용하세요.')
                else:
                    token['expiry_status'] = 'healthy'
            except (ValueError, TypeError, AttributeError):
                token['warning'] = 'Instagram 토큰 만료 시각을 확인할 수 없습니다. 토큰 상태를 재검사하세요.'
        return token

    def check_connection(self):
        with self.operation():
            pages = set()
            linked = False
            for rows, _ in self.client.pages('me/accounts', {'fields': 'id,name,instagram_business_account', 'limit': 100}, max_pages=5):
                for page in rows:
                    if page.get('id') == self.config.page_id:
                        pages.add(page['id'])
                        linked = page.get('instagram_business_account', {}).get('id') == self.config.user_id
            self.sync_budget()
            self.store.state('connection', {'status': 'connected' if linked else 'permission', 'checked_at': utcnow()})
            return {'success': linked, 'page_found': bool(pages), 'instagram_account_linked': linked, 'budget': self.budget()}

    def check_token(self, token=None, persist=True):
        if not self.config.app_id or not self.config.app_secret:
            raise GraphError('not_configured')
        with self.operation():
            payload = self.client.get('debug_token', {'input_token': token or self.config.token},
                                      bearer=f'{self.config.app_id}|{self.config.app_secret}').get('data', {})
            if not isinstance(payload, dict):
                raise GraphError('invalid_response')
            expiry = payload.get('expires_at')
            data_expiry = payload.get('data_access_expires_at')
            expiries = [v for v in (expiry, data_expiry) if isinstance(v, int) and v > 0]
            now = datetime.now(timezone.utc)
            valid = bool(payload.get('is_valid')) and (not expiries or min(expiries) > now.timestamp())
            scopes = payload.get('scopes') or []
            required = ['instagram_basic', 'pages_show_list', 'pages_read_engagement']
            permissions = {scope: scope in scopes for scope in required}
            token_type = payload.get('type')
            if token_type not in ('USER', 'PAGE', 'APP', 'SYSTEM_USER'):
                token_type = 'UNKNOWN'
            issued = payload.get('issued_at')
            lifetime = expiry-issued if type(expiry) is int and type(issued) is int and 0 < issued < expiry else None
            app_matches = str(payload.get('app_id', '')) == self.config.app_id
            result = {'success': valid and app_matches and all(permissions.values()),
                      'status': 'valid' if valid else 'invalid', 'checked_at': utcnow(),
                      'expires_at': datetime.fromtimestamp(min(expiries), timezone.utc).isoformat() if expiries else None,
                      'token_type': token_type, 'app_matches': app_matches, 'permissions': permissions,
                      'lifetime': 'unknown' if lifetime is None else 'short_lived' if lifetime <= 86400 else 'long_lived',
                      'lifetime_basis': 'issued_at와expires_at의 간격으로 추정. 영구 갱신을 보장하지 않습니다.'}
            if not persist:
                return result
            self.store.state('token', result)
            if result['status'] != 'valid':
                self.store.state('connection', {'status': 'authentication', 'checked_at': utcnow()})
            elif not result['success']:
                self.store.state('connection', {'status': 'permission', 'checked_at': utcnow()})
            elif (self.store.state('connection') or {}).get('status') == 'authentication':
                self.store.state('connection', {'status': 'unverified', 'checked_at': utcnow()})
            return result

    def token_blocked(self):
        if self.credential_changed():
            return False
        token = self.store.state('token') or {}
        return bool(token.get('status') == 'invalid' or token.get('expires_at') and token['expires_at'] <= utcnow()
                    or (self.store.state('connection') or {}).get('status') == 'authentication')

    def start(self, automatic=True):
        if self.thread and self.thread.is_alive():
            return
        def loop():
            maintenance_due = 0
            while not self.stop_event.is_set():
                try:
                    if time.monotonic() >= maintenance_due:
                        self.maintenance()
                        maintenance_due = time.monotonic()+3600
                    if automatic and self.config.enabled and self.config.configured and not self.token_blocked():
                        self.collect(due_only=True)
                except GraphError:
                    pass
                except Exception:
                    # Keep RSS and subsequent Instagram scheduling alive; no raw exception output.
                    self.store.state('scheduler_error', 'internal')
                self.stop_event.wait(min(60, self.config.interval_minutes*60))
        self.thread = threading.Thread(target=loop, name='instagram-scheduler', daemon=True)
        self.thread.start()
