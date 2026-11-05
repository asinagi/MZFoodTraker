"""B8 mobile app API: read-only /api/v1 over FoodAnalytics (docs/app-api/openapi.yaml).

Separate from the admin server (api.py): GET/HEAD/OPTIONS only, no admin token (a request that carries
one is refused), no writes and no outbound calls. AppApi.handle() is the whole request contract
(host check, method, route, strict parameters, envelope, errors), so the HTTP adapter, the mock server,
the example generator and the tests all exercise the same code. The database server binds to loopback.
"""
import argparse
import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import sys
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit
from . import app_contract as contract
from .analytics_reader import AnalyticsUnavailable, SQLiteAnalyticsStore, TargetAnalyticsStore
from .analytics_service import B8_SOURCES, FoodAnalytics, FoodNotFound, SourceNotRankable, parse_day, stamp
from .api import LOOPBACK, TOKEN_HEADER
from .classifier import normalize

ALLOW = 'GET, HEAD, OPTIONS'
MIN_DAY = date(2000, 1, 1)
FOOD_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
ROUTES = ((re.compile(r'/api/v1/health'), 'health', ()),
          (re.compile(r'/api/v1/foods'), 'foods', ('q', 'kind', 'sort', 'limit', 'cursor')),
          (re.compile(r'/api/v1/foods/([^/]+)'), 'food', ()),
          (re.compile(r'/api/v1/foods/([^/]+)/timeline'), 'timeline', ('period', 'end_date', 'granularity', 'source')),
          (re.compile(r'/api/v1/foods/([^/]+)/metrics'), 'metrics', ('period', 'end_date', 'source')),
          (re.compile(r'/api/v1/trends/rankings'), 'rankings', ('source', 'metric', 'period', 'end_date', 'kind', 'include_zero', 'limit', 'cursor')),
          (re.compile(r'/api/v1/sources/status'), 'status', ('period',)))
MOCK_NOTICE = '가상 데이터입니다. 실제 유행 분석 결과가 아닙니다.'
TEST_NOTICE = 'Mock 수집 회차로 만든 테스트 데이터입니다. 실제 유행 분석 결과가 아닙니다.'


class ApiError(Exception):
    def __init__(self, code, details=(), headers=None):
        super().__init__(code)
        self.code, self.details, self.headers = code, list(details), headers or {}
        self.status, self.message = contract.ERRORS[code]


def detail(parameter, reason, allowed=None):
    return {'parameter': parameter, 'reason': reason, 'allowed': list(allowed) if allowed is not None else None}


def host_allowed(host):
    """Loopback names or IP literals only: a DNS-rebinding page cannot present an IP literal as Host."""
    if host is None:
        return True
    try:
        name = urlsplit('http://' + host).hostname
    except ValueError:
        return False
    if name in LOOPBACK:
        return True
    try:
        ipaddress.ip_address(name or '')
        return True
    except ValueError:
        return False


class AppApi:
    def __init__(self, analytics, data_mode='database'):
        if data_mode not in ('database', 'mock'):
            raise ValueError('data_mode must be database or mock')
        self.analytics, self.data_mode = analytics, data_mode

    def meta(self):
        notice = None
        if self.data_mode == 'mock':
            notice = MOCK_NOTICE
        elif self.analytics.origin == 'mock':
            notice = TEST_NOTICE
        return {'api_version': contract.API_VERSION, 'data_mode': self.data_mode, 'observation_origin': self.analytics.origin,
                'generated_at': stamp(self.analytics.now()), 'timezone': contract.TIMEZONE, 'notice': notice}

    def handle(self, method, target, headers=None):
        """(status, body, extra headers) for one request. headers: mapping of request header names to values."""
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        try:
            if not host_allowed(headers.get('host')):
                raise ApiError('FORBIDDEN_HOST', [detail('Host', 'not_ip_or_localhost')])
            if TOKEN_HEADER.lower() in headers:
                raise ApiError('ADMIN_TOKEN_NOT_ACCEPTED', [detail(TOKEN_HEADER, 'forbidden_header')])  # the value is never read
            if method not in ('GET', 'HEAD'):
                raise ApiError('METHOD_NOT_ALLOWED', [detail(None, 'method_not_allowed', ALLOW.split(', '))], {'Allow': ALLOW})
            simulated = headers.get('x-mock-error') if self.data_mode == 'mock' else None
            if simulated in ('500', '503'):
                code = 'INTERNAL_ERROR' if simulated == '500' else 'DATA_UNAVAILABLE'
                raise ApiError(code, [detail('X-Mock-Error', 'mock_simulated')])
            url = urlsplit(target)
            for pattern, route, allowed in ROUTES:
                match = pattern.fullmatch(url.path)
                if match:
                    params = self._params(url.query, allowed)
                    body = getattr(self, '_' + route)(*match.groups(), params)
                    return 200, dict(body, meta=self.meta()), {}
            raise ApiError('ROUTE_NOT_FOUND', [detail(None, 'unknown_route')])
        except ApiError as error:
            return error.status, self._error(error.code, error.details, error.message), error.headers
        except FoodNotFound:
            return 404, self._error('FOOD_NOT_FOUND', [detail('food_id', 'not_found')]), {}
        except SourceNotRankable as error:
            return 422, self._error('SOURCE_NOT_RANKABLE', [detail('source', error.reason, B8_SOURCES)]), {}
        except AnalyticsUnavailable as error:
            return 503, self._error('DATA_UNAVAILABLE', [detail(None, error.reason)]), {}
        except Exception:
            logging.exception('app API request failed')  # details stay in the server log, never in the response
            return 500, self._error('INTERNAL_ERROR', [detail(None, 'internal')]), {}

    def _error(self, code, details, message=None):
        return {'error': {'code': code, 'message': message or contract.ERRORS[code][1], 'details': details}, 'meta': self.meta()}

    # Parameters: unknown, repeated or malformed values are refused instead of silently ignored.

    @staticmethod
    def _params(query, allowed):
        params = {}
        for key, value in parse_qsl(query, keep_blank_values=True):
            if key not in allowed:
                raise ApiError('INVALID_PARAMETER', [detail(key, 'unknown_parameter', allowed)])
            if key in params:
                raise ApiError('INVALID_PARAMETER', [detail(key, 'repeated_parameter')])
            params[key] = value
        return params

    @staticmethod
    def _choice(params, name, values, default=None, required=False):
        if name not in params:
            if required:
                raise ApiError('INVALID_PARAMETER', [detail(name, 'required', values)])
            return default
        if params[name] not in values:
            raise ApiError('INVALID_PARAMETER', [detail(name, 'unsupported_value', values)])
        return params[name]

    def _period(self, params, default):
        return contract.PERIODS[self._choice(params, 'period', list(contract.PERIODS), default)]

    def _end(self, params, default=None):
        if 'end_date' not in params:
            return default or self.analytics.default_end()
        try:
            end = parse_day(params['end_date'])
        except ValueError:
            raise ApiError('INVALID_PARAMETER', [detail('end_date', 'invalid_format')]) from None
        if not MIN_DAY <= end <= self.analytics.today():
            raise ApiError('INVALID_PARAMETER', [detail('end_date', 'out_of_range')])
        return end

    @staticmethod
    def _food_id(food_id):
        if not FOOD_ID.fullmatch(food_id):
            raise ApiError('INVALID_PARAMETER', [detail('food_id', 'invalid_format')])
        return food_id

    @staticmethod
    def _limit(params):
        raw = params.get('limit', '20')
        if not re.fullmatch(r'\d{1,4}', raw):
            raise ApiError('INVALID_PARAMETER', [detail('limit', 'invalid_format')])
        if not 1 <= int(raw) <= 100:
            raise ApiError('INVALID_PARAMETER', [detail('limit', 'out_of_range')])
        return int(raw)

    @staticmethod
    def _fingerprint(route, params):
        fixed = {k: v for k, v in params.items() if k not in ('cursor', 'limit')}
        return hashlib.sha256(json.dumps([route, sorted(fixed.items())], ensure_ascii=False).encode()).hexdigest()[:16]

    def _cursor(self, route, params):
        """(offset, end_date carried from the first page or None). Cursors are opaque and bound to their query."""
        if 'cursor' not in params:
            return 0, None
        try:
            raw = params['cursor']
            state = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
            offset, end = state['o'], state.get('e')
            if state.get('v') != 1 or type(offset) is not int or offset < 0 or not (end is None or isinstance(end, str)):
                raise ValueError()
            end = parse_day(end) if end else None
        except (ValueError, KeyError, TypeError, AttributeError):  # binascii and JSON errors are ValueErrors
            raise ApiError('INVALID_CURSOR', [detail('cursor', 'malformed')]) from None
        if state.get('f') != self._fingerprint(route, params):
            raise ApiError('INVALID_CURSOR', [detail('cursor', 'mismatched_query')])
        return offset, end

    def _page(self, route, params, items, offset, limit, end=None):
        more = offset + limit < len(items)
        state = {'v': 1, 'o': offset + limit, 'f': self._fingerprint(route, params), 'e': end.isoformat() if end else None}
        cursor = base64.urlsafe_b64encode(json.dumps(state, separators=(',', ':')).encode()).rstrip(b'=').decode() if more else None
        return items[offset:offset + limit], {'limit': limit, 'total_items': len(items), 'has_more': more, 'next_cursor': cursor}

    # Routes

    def _health(self, params):
        ready = self.analytics.health()['schema_ready']
        return {'data': {'status': 'ok' if ready else 'unavailable', 'api_version': contract.API_VERSION, 'data_mode': self.data_mode,
                         'observation_origin': self.analytics.origin, 'schema_ready': ready}}

    def _foods(self, params):
        q = None
        if 'q' in params:
            if any(ord(c) < 32 for c in params['q']):
                raise ApiError('INVALID_PARAMETER', [detail('q', 'invalid_format')])
            q = normalize(params['q'])
            if not 1 <= len(q) <= 100:
                raise ApiError('INVALID_PARAMETER', [detail('q', 'out_of_range')])
        kind = self._choice(params, 'kind', list(contract.ENTITY_KINDS))
        sort = self._choice(params, 'sort', list(contract.SORTS), 'relevance' if q else 'name')
        if sort == 'relevance' and not q:
            raise ApiError('INVALID_PARAMETER', [detail('sort', 'requires_parameter', ['q'])])
        limit, (offset, _) = self._limit(params), self._cursor('foods', params)
        items, page = self._page('foods', params, self.analytics.foods(q, kind, sort), offset, limit)
        return {'data': {'items': items, 'query': {'q': q, 'kind': kind, 'sort': sort}}, 'page': page}

    def _food(self, food_id, params):
        return {'data': self.analytics.food(self._food_id(food_id))}

    def _timeline(self, food_id, params):
        food_id = self._food_id(food_id)
        days, end = self._period(params, '30d'), self._end(params)
        granularity = self._choice(params, 'granularity', ['day', 'week'], 'day')
        source = self._choice(params, 'source', list(contract.SOURCE_CODES))
        return {'data': self.analytics.timeline(food_id, days, end, granularity, source)}

    def _metrics(self, food_id, params):
        food_id = self._food_id(food_id)
        days, end = self._period(params, '7d'), self._end(params)
        return {'data': self.analytics.metrics(food_id, days, end, self._choice(params, 'source', list(contract.SOURCE_CODES)))}

    def _rankings(self, params):
        source = self._choice(params, 'source', list(contract.SOURCE_CODES), required=True)
        self._choice(params, 'metric', [contract.METRIC_CODE], contract.METRIC_CODE)
        days, kind = self._period(params, '7d'), self._choice(params, 'kind', list(contract.ENTITY_KINDS))
        include_zero = self._choice(params, 'include_zero', ['true', 'false'], 'false') == 'true'
        limit, (offset, carried) = self._limit(params), self._cursor('rankings', params)
        end = self._end(params, carried)  # a cursor keeps its first page's default end date across midnight
        data = self.analytics.rankings(source, days, end, kind, include_zero)
        data['items'], page = self._page('rankings', params, data['items'], offset, limit, end)
        return {'data': data, 'page': page}

    def _status(self, params):
        return {'data': self.analytics.sources_status(self._period(params, '7d'))}


class AppServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR can let two processes bind one port; fail instead (as api.LocalServer does).
    allow_reuse_address = os.name != 'nt'
    daemon_threads = True


def make_app_server(api, host='127.0.0.1', port=8020, cors_origins=(), any_origin=False, allow_lan=False, quiet=False):
    """HTTP adapter for AppApi. Non-loopback binding is only for the synthetic mock server (allow_lan)."""
    if host not in LOOPBACK and not allow_lan:
        raise ValueError('실제 데이터 앱 API는 루프백 주소에만 바인딩합니다.')
    allowed_origins = frozenset(cors_origins)

    class Handler(BaseHTTPRequestHandler):
        def version_string(self):
            return 'MZFoodTraker-AppAPI/1'  # no Python or OS version in responses

        def log_message(self, fmt, *args):
            if not quiet:
                super().log_message(fmt, *args)

        def _cors(self):
            origin = self.headers.get('Origin')
            if any_origin:
                return {'Access-Control-Allow-Origin': '*'}
            if origin in allowed_origins:
                return {'Access-Control-Allow-Origin': origin, 'Vary': 'Origin'}
            return {}

        def _respond(self, method):
            status, body, extra = api.handle(method, self.path, dict(self.headers.items()))
            payload = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            for name, value in {'Content-Type': 'application/json; charset=utf-8', 'Content-Length': str(len(payload)),
                                'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', **self._cors(), **extra}.items():
                self.send_header(name, value)
            self.end_headers()
            if method != 'HEAD':
                self.wfile.write(payload)

        def do_GET(self):
            self._respond('GET')

        def do_HEAD(self):
            self._respond('HEAD')

        def do_OPTIONS(self):
            if not host_allowed(self.headers.get('Host')):
                return self._respond('GET')  # same FORBIDDEN_HOST answer as any other request
            self.send_response(204)
            headers = {'Allow': ALLOW, 'Content-Length': '0', **self._cors()}
            if 'Access-Control-Allow-Origin' in headers:
                headers.update({'Access-Control-Allow-Methods': ALLOW, 'Access-Control-Allow-Headers': 'Accept, Content-Type',
                                'Access-Control-Max-Age': '600'})
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()

        def do_POST(self):
            length = self.headers.get('Content-Length', '0')
            self.rfile.read(min(int(length), 65536) if length.isdigit() else 0)  # drain a small body before refusing
            self._respond(self.command)

        do_PUT = do_PATCH = do_DELETE = do_POST

    return AppServer((host, port), Handler)


STORES = ('sqlite', 'oracle-test', 'oracle-production')


class StartRefused(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def open_store(mode, database=None, environ=None):
    """(analytics store, close) for a server storage mode, chosen explicitly and never inferred.

    sqlite reads the given SQLite database; oracle-test reads the isolated Oracle test schema (ORACLE_TEST_*,
    environment test) through read-only transactions; oracle-production is refused before any connection
    (G-OPS-6). Nothing here runs DDL or migrations: a store whose schema is not current is refused.
    """
    if mode == 'sqlite':
        from .migrations import MigrationRequired
        from .storage import Storage
        try:
            return SQLiteAnalyticsStore(Storage(database)), lambda: None
        except MigrationRequired as exc:
            raise StartRefused(str(exc)) from None
    from .runtime_storage import select
    from .target_db import TargetError
    try:
        repo = select(mode, environ)
    except TargetError as exc:  # production refused, test schema not configured, driver missing
        raise StartRefused(exc.code) from None
    store = TargetAnalyticsStore(repo)
    if not store.ready():
        repo.db.close()
        raise StartRefused('target_schema_not_ready')
    return store, repo.db.close


def main(argv=None):
    parser = argparse.ArgumentParser(description='읽기 전용 앱 데이터 API (/api/v1). DB를 읽기 전용으로 열고 외부 API를 호출하지 않습니다.')
    parser.add_argument('--store', choices=STORES, default='sqlite',
                        help='저장소. sqlite(기본, --database 필요) | oracle-test(ORACLE_TEST_* 격리 테스트 스키마, 읽기 전용 트랜잭션) | '
                             'oracle-production(미승인, 시작 거부)')
    parser.add_argument('--database', help='sqlite 저장소에서 읽을 SQLite DB 경로 (스키마가 최신이어야 함, 변경하지 않음)')
    parser.add_argument('--port', type=int, default=8020)
    parser.add_argument('--observation-origin', choices=('live_api', 'mock'), default='live_api',
                        help='집계에 넣을 관측 회차 origin. 운영은 live_api, Mock 수집 테스트 DB는 mock')
    parser.add_argument('--cors-origin', action='append', default=[], help='브라우저 웹앱 개발 Origin 허용 (여러 번 지정 가능)')
    args = parser.parse_args(argv)
    from pathlib import Path
    if args.store == 'sqlite':
        if not args.database:
            parser.error('sqlite 저장소에는 --database가 필요합니다')
        if not Path(args.database).is_file():
            parser.error('DB 파일이 없습니다: %s' % args.database)
    elif args.database:
        parser.error('--database는 sqlite 저장소에서만 사용합니다')
    try:
        store, close = open_store(args.store, args.database)
    except StartRefused as exc:
        print(json.dumps({'started': False, 'store': args.store, 'error': exc.reason}, ensure_ascii=False))
        return 2
    try:
        api = AppApi(FoodAnalytics(store, origin=args.observation_origin))
        server = make_app_server(api, port=args.port, cors_origins=args.cors_origin)
        print('App API (read-only, %s): http://127.0.0.1:%d/api/v1/health' % (args.store, server.server_port), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
