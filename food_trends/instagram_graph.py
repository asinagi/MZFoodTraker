"""Facebook Login Graph client. Never persist raw responses or remote error messages."""
import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler


MESSAGES = {
    'not_configured': 'Instagram 액세스 토큰을 .env에 입력하고 서버를 재시작하세요.',
    'authentication': '토큰이 만료되었거나 유효하지 않습니다. 토큰을 확인하세요.',
    'permission': 'Instagram 권한 또는 Public Content Access 승인을 확인하세요.',
    'rate_limit': 'Meta API 사용 한도입니다. 백오프 후 다음 주기에 재시도합니다.',
    'network': 'Instagram 네트워크 요청에 실패했습니다. 다음 주기에 재시도합니다.',
    'upstream': 'Meta 서버 오류입니다. 다음 주기에 재시도합니다.',
    'invalid_response': 'Instagram API 응답 형식을 확인할 수 없습니다.',
    'api_error': 'Instagram API 요청이 거절되었습니다. API 버전과 요청 조건을 확인하세요.',
    'budget': '최근 7일 해시태그 검색 예산에 근접했습니다. 신규 탐색을 중단합니다.',
    'not_found': '해시태그 또는 승인할 음식 후보가 없습니다.',
    'busy': '다른 Instagram 작업이 실행 중입니다.',
    'disabled': 'Instagram 수집이 환경변수에서 비활성화되어 있습니다.',
    'validation': '해시태그는 #을 제외한 한글, 영문, 숫자, 밑줄 1~100자로 입력하세요.',
    'internal': 'Instagram 작업을 완료하지 못했습니다.',
}


class GraphError(Exception):
    def __init__(self, kind, code=None, subcode=None):
        self.kind, self.code, self.subcode = kind, code, subcode
        super().__init__(MESSAGES.get(kind, MESSAGES['internal']))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GraphClient:
    def __init__(self, config, transport=None, sleeper=time.sleep):
        self.config = config
        self.transport = transport or build_opener(NoRedirect()).open
        self.origin = 'live_api' if transport is None else 'mock'
        self.sleeper = sleeper
        self.request_count = 0
        self.before_request = lambda: None

    def get(self, path, params=None, *, bearer=None):
        token = self.config.token if bearer is None else bearer
        if not token:
            raise GraphError('not_configured')
        params = dict(params or {})
        private = bool({'input_token', 'fb_exchange_token', 'client_secret', 'access_token'} & params.keys())
        base = f'https://graph.facebook.com/{self.config.version}/'
        headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
        if private:
            # Meta's batch GET stays a read operation, carried in a POST body.
            # Sensitive parameters never appear in the HTTP request URL.
            url = base
            body = urlencode({'batch': json.dumps([{
                'method': 'GET', 'relative_url': path + '?' + urlencode(params)
            }])}).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            if path == 'oauth/access_token':
                # OAuth exchange accepts form POST directly; batch is for Graph reads.
                url, body = base + path, urlencode(params).encode()
        else:
            url, body = base + path + '?' + urlencode(params), None
        for attempt in range(3):
            self.before_request()
            self.request_count += 1
            retry_after = 0
            try:
                request = Request(url, data=body, headers=headers)
                with self.transport(request, timeout=self.config.timeout) as response:
                    payload = json.loads(response.read(8 * 1024 * 1024))
                if private and isinstance(payload, list):
                    if len(payload) != 1 or not isinstance(payload[0], dict):
                        raise GraphError('invalid_response')
                    envelope = payload[0]
                    payload = json.loads(envelope.get('body', 'null'))
                    if not isinstance(payload, dict):
                        raise GraphError('invalid_response')
                    if 'error' in payload or not 200 <= envelope.get('code', 0) < 300:
                        raise self.error(payload.get('error', {}), envelope.get('code'))
                if not isinstance(payload, dict):
                    raise GraphError('invalid_response')
                if 'error' in payload:
                    raise self.error(payload['error'])
                return payload
            except HTTPError as exc:
                try:
                    payload = json.loads(exc.read(65536))
                    error = self.error(payload.get('error', {}), exc.code)
                except (ValueError, TypeError, AttributeError):
                    error = self.error({}, exc.code)
                try:
                    retry_after = min(30, max(0, float(exc.headers.get('Retry-After', '0'))))
                except (ValueError, AttributeError):
                    pass
                exc.close()
            except (URLError, TimeoutError, socket.timeout, OSError):
                error = GraphError('network')
            except (ValueError, UnicodeError):
                error = GraphError('invalid_response')
            except GraphError as exc:
                error = exc
            if error.kind not in ('network', 'rate_limit', 'upstream') or attempt == 2:
                raise error from None
            self.sleeper(max(retry_after, 2 ** (attempt + 1)))

    @staticmethod
    def error(error, status=None):
        if not isinstance(error, dict):
            return GraphError('invalid_response')
        code, sub = error.get('code'), error.get('error_subcode')
        if code in (102, 190) or status == 401:
            kind = 'authentication'
        elif code in (4, 17, 32, 613, 80004, 80000) or status == 429:
            kind = 'rate_limit'
        elif code in (10, 200, 294) or status == 403:
            kind = 'permission'
        elif status and status >= 500 or error.get('is_transient'):
            kind = 'upstream'
        else:
            kind = 'api_error'
        # Persist only numeric diagnostic codes, never Meta's error text or URL.
        return GraphError(kind, code if isinstance(code, int) else None,
                          sub if isinstance(sub, int) else None)

    def pages(self, path, params=None, max_pages=None):
        params = dict(params or {})
        cursors = set()
        for _ in range(max_pages or self.config.max_pages):
            payload = self.get(path, params)
            data = payload.get('data')
            if not isinstance(data, list) or any(not isinstance(x, dict) for x in data):
                raise GraphError('invalid_response')
            paging = payload.get('paging') or {}
            if not isinstance(paging, dict):
                raise GraphError('invalid_response')
            more = bool(paging.get('next'))
            yield data, more
            if not more:
                return
            # Rebuild our fixed Graph URL. Never follow paging.next (may contain credentials).
            cursors_payload = paging.get('cursors') or {}
            cursor = cursors_payload.get('after') if isinstance(cursors_payload, dict) else None
            if not isinstance(cursor, str) or cursor in cursors:
                raise GraphError('invalid_response')
            cursors.add(cursor)
            params['after'] = cursor

    def search(self, name):
        payload = self.get('ig_hashtag_search', {'user_id': self.config.user_id, 'q': name})
        rows = payload.get('data')
        if not isinstance(rows, list):
            raise GraphError('invalid_response')
        if not rows:
            raise GraphError('not_found')
        if not isinstance(rows[0], dict):
            raise GraphError('invalid_response')
        identifier = rows[0].get('id')
        if not isinstance(identifier, str) or not identifier.isdigit():
            raise GraphError('invalid_response')
        return identifier

    def recent(self, hashtag_id):
        return self.pages(f'{hashtag_id}/recent_media', {
            'user_id': self.config.user_id, 'fields': 'id,caption,timestamp,media_type,permalink', 'limit': 50})

    def recent_searches(self):
        ids = set()
        more = False
        for rows, more in self.pages(f'{self.config.user_id}/recently_searched_hashtags', {'limit': 30}, max_pages=3):
            for row in rows:
                if not isinstance(row.get('id'), str) or not row['id'].isdigit():
                    raise GraphError('invalid_response')
                ids.add(row['id'])
        if more:
            raise GraphError('invalid_response')
        return ids
