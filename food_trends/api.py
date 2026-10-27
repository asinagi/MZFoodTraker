import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from .instagram_graph import GraphError
from .naver_datalab import NaverError
from .youtube import YouTubeError

STATIC = Path(__file__).parent.parent / 'static'

def make_server(service, host='127.0.0.1', port=8000, instagram=None, naver=None, youtube=None, foods=None, tracking=None):
    from .food_service import FoodService
    from . import food_http
    foods = foods or FoodService(service.storage)
    from .tracking_service import TrackingService
    from .tracking_access import accounts_from_services
    from . import tracking_http
    tracking = tracking or TrackingService(service.storage, accounts_from_services(instagram, naver, youtube))
    class Handler(BaseHTTPRequestHandler):
        def send(self, status, data, mime='application/json; charset=utf-8', download=None):
            body = json.dumps(data, ensure_ascii=False).encode() if mime.startswith('application/json') else data
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if download:
                self.send_header('Content-Disposition', f'attachment; filename="{download}"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            params = parse_qs(url.query)
            try:
                if url.path.startswith('/api/tracking/'):
                    return tracking_http.get(self, tracking, url.path, params)
                if url.path.startswith(('/api/foods', '/api/food-candidates', '/api/food-events')):
                    return food_http.get(self, foods, url.path, params)
                if url.path == '/api/youtube':
                    return self.send(200, youtube.status()) if youtube else self.send(503, {'error': 'YouTube 서비스 미연결'})
                if url.path == '/api/naver':
                    return self.send(200, naver.status()) if naver else self.send(503, {'error': 'NAVER 서비스 미연결'})
                if url.path == '/api/instagram':
                    if instagram is None:
                        return self.send(503, {'error': 'Instagram 서비스가 연결되지 않았습니다'})
                    try:
                        days = int(params.get('days', ['7'])[0])
                        candidate = params.get('candidate_id', [None])[0]
                        candidate = int(candidate) if candidate is not None else None
                        hashtag = params.get('hashtag_id', [None])[0]
                        hashtag = int(hashtag) if hashtag is not None else None
                    except ValueError:
                        return self.send(400, {'error': '기간과 후보 ID를 확인하세요'})
                    if days not in (7, 14, 30):
                        return self.send(400, {'error': '기간은 7 / 14 / 30일입니다'})
                    return self.send(200, instagram.status(candidate, days, hashtag))
                if url.path == '/api/instagram/media':
                    if instagram is None:
                        return self.send(503, {'error': 'Instagram 서비스 미연결'})
                    try:
                        tag_id = int(params.get('hashtag_id', [''])[0])
                    except ValueError:
                        return self.send(400, {'error': '디버깅할 추적 태그 ID가 필요합니다'})
                    tag = instagram.store.tag(tag_id)
                    if tag is None:
                        return self.send(404, {'error': '추적 태그 없음'})
                    items = instagram.store.summary(tag, include_media=True)['media']
                    return self.send(200, {'items': [{k: v for k, v in item.items() if k not in ('caption', 'caption_captured_at', 'caption_purged_at')} for item in items]})
                if url.path == '/api/status':
                    return self.send(200, service.status())
                if url.path == '/api/export':
                    return self.send(200, service.storage.export(), download='trends-audit.json')
                if url.path == '/api/trends':
                    category = params.get('classification', ['ALL'])[0]
                    if category not in ('ALL', 'FOOD', 'REVIEW', 'NON_FOOD'):
                        return self.send(400, {'error': '잘못된 classification'})
                    return self.send(200, {'items': service.storage.trends(category, params.get('q', [''])[0])})
                if url.path.startswith('/api/trends/'):
                    try:
                        item_id = int(url.path.rsplit('/', 1)[-1])
                    except ValueError:
                        return self.send(400, {'error': '숫자 ID를 사용하세요'})
                    rows = service.storage.trends(item_id=item_id)
                    return self.send(200, rows[0]) if rows else self.send(404, {'error': '항목 없음'})
                files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if url.path in files:
                    filename, mime = files[url.path]
                    return self.send(200, (STATIC / filename).read_bytes(), mime)
                if url.path == '/instagram.js':
                    return self.send(200, (STATIC / 'instagram.js').read_bytes(), 'text/javascript; charset=utf-8')
                if url.path == '/naver.js':
                    return self.send(200, (STATIC / 'naver.js').read_bytes(), 'text/javascript; charset=utf-8')
                if url.path == '/youtube.js':
                    return self.send(200, (STATIC / 'youtube.js').read_bytes(), 'text/javascript; charset=utf-8')
                if url.path == '/foods.js':
                    return self.send(200, (STATIC / 'foods.js').read_bytes(), 'text/javascript; charset=utf-8')
                if url.path == '/tracking.js':
                    return self.send(200, (STATIC / 'tracking.js').read_bytes(), 'text/javascript; charset=utf-8')
                self.send(404, {'error': '경로 없음'})
            except Exception:
                self.send(500, {'error': '저장 데이터 조회 실패. 서버 로그를 확인하세요.'})

        def do_POST(self):
            if self.path != '/api/collect' and not self.path.startswith(('/api/instagram/', '/api/naver/', '/api/youtube/', '/api/foods/', '/api/tracking/')):
                return self.send(404, {'error': '경로 없음'})
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc != self.headers.get('Host'):
                return self.send(403, {'error': '같은 로컬 페이지에서 요청하세요'})
            if self.path.startswith('/api/tracking/'):
                return tracking_http.post(self, tracking)
            if self.path.startswith('/api/foods/'):
                return food_http.post(self, foods)
            if self.path.startswith('/api/youtube/'):
                if youtube is None:
                    return self.send(503, {'error': 'YouTube 서비스 미연결'})
                if urlsplit('http://' + self.headers.get('Host', '')).hostname not in ('127.0.0.1', 'localhost', '::1'):
                    return self.send(403, {'error': '로컬 주소에서 요청하세요'})
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= size <= 4096:
                        return self.send(413, {'error': '요청 크기 초과'})
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        return self.send(415, {'error': 'application/json을 사용하세요'})
                    body = json.loads(self.rfile.read(size) or b'{}')
                    if not isinstance(body, dict) or body:
                        raise ValueError()
                    if self.path == '/api/youtube/collect':
                        result = youtube.collect()
                    elif self.path == '/api/youtube/refresh':
                        result = youtube.collect(refresh_only=True)
                    elif self.path == '/api/youtube/delete':
                        result = youtube.delete_data()
                    else:
                        return self.send(404, {'error': '경로 없음'})
                    kind = result.get('error_type')
                    code = 200 if result.get('success') else 409 if kind == 'busy' else 429 if kind in ('quota', 'budget', 'rate_limit', 'cooldown') else 503 if kind == 'not_configured' else 502
                    return self.send(code, result)
                except YouTubeError as exc:
                    return self.send(409 if exc.kind == 'busy' else 502, {'success': False, 'error_type': exc.kind, 'error': str(exc)})
                except (ValueError, TypeError):
                    return self.send(400, {'error_type': 'validation', 'error': '요청 형식을 확인하세요'})
                except Exception:
                    return self.send(500, {'error_type': 'internal', 'error': 'YouTube 요청 처리 실패'})
            if self.path.startswith('/api/naver/'):
                if naver is None:
                    return self.send(503, {'error': 'NAVER 서비스 미연결'})
                if urlsplit('http://' + self.headers.get('Host', '')).hostname not in ('127.0.0.1', 'localhost', '::1'):
                    return self.send(403, {'error': '로컬 주소에서 요청하세요'})
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= size <= 4096:
                        return self.send(413, {'error': '요청 크기 초과'})
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        return self.send(415, {'error': 'application/json을 사용하세요'})
                    body = json.loads(self.rfile.read(size) or b'{}')
                    if not isinstance(body, dict):
                        raise ValueError()
                    if self.path == '/api/naver/query':
                        if type(body.get('days', 30)) is not int or body.get('days', 30) not in (7, 30, 90):
                            raise ValueError()
                        if 'keywords' in body:
                            if 'candidate_ids' in body:
                                raise ValueError()
                            result = naver.query_keywords(body['keywords'], body.get('days', 30))
                        else:
                            result = naver.query(body.get('candidate_ids'), body.get('days', 30))
                    elif self.path == '/api/naver/collect':
                        result = naver.collect()
                    else:
                        return self.send(404, {'error': '경로 없음'})
                    return self.send(200 if result.get('success') else 502, result)
                except NaverError as exc:
                    code = (400 if exc.kind == 'validation' else 409 if exc.kind == 'duplicate'
                            else 429 if exc.kind in ('rate_limit', 'budget') else 503 if exc.kind == 'not_configured' else 502)
                    return self.send(code, {'success': False, 'error_type': exc.kind, 'error': str(exc)})
                except (ValueError, TypeError):
                    return self.send(400, {'error_type': 'validation', 'error': '요청 형식을 확인하세요'})
                except Exception:
                    return self.send(500, {'error_type': 'internal', 'error': 'NAVER 요청 처리 실패'})
            if self.path.startswith('/api/instagram/'):
                if instagram is None:
                    return self.send(503, {'error': 'Instagram 서비스가 연결되지 않았습니다'})
                if urlsplit('http://' + self.headers.get('Host', '')).hostname not in ('127.0.0.1', 'localhost', '::1'):
                    return self.send(403, {'error': '로컬 주소에서 요청하세요'})
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= size <= 4096:
                        return self.send(413, {'error': '요청 크기 초과'})
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        return self.send(415, {'error': 'application/json을 사용하세요'})
                    body = json.loads(self.rfile.read(size) or b'{}')
                    if not isinstance(body, dict):
                        raise ValueError()
                    tag_id = body.get('hashtag_id')
                    if tag_id is not None and type(tag_id) is not int:
                        raise ValueError()
                    if self.path == '/api/instagram/start':
                        if body.get('approved') is not True or type(body.get('candidate_id')) is not int:
                            return self.send(400, {'error': '음식 후보와 명시적 추적 승인이 필요합니다'})
                        result = instagram.start_tracking(body.get('name'), body['candidate_id'])
                    elif self.path == '/api/instagram/collect':
                        result = instagram.collect(tag_id)
                    elif self.path == '/api/instagram/pause':
                        if tag_id is None or not instagram.store.pause(tag_id):
                            return self.send(404, {'error': '추적 대상 없음'})
                        result = {'success': True}
                    elif self.path == '/api/instagram/resume':
                        tag = instagram.store.tag(tag_id)
                        if not tag:
                            return self.send(404, {'error': '추적 대상 없음'})
                        result = instagram.start_tracking(tag['name'])
                    elif self.path == '/api/instagram/check':
                        result = instagram.check_connection()
                    else:
                        return self.send(404, {'error': '경로 없음'})
                    return self.send(200 if result.get('success') else 502, result)
                except (ValueError, TypeError):
                    return self.send(400, {'error': '요청 형식을 확인하세요'})
                except GraphError as exc:
                    return self.send(409 if exc.kind in ('busy', 'budget') else 400 if exc.kind in ('validation', 'not_found') else 502,
                                     {'error': str(exc), 'error_type': exc.kind})
                except Exception:
                    return self.send(500, {'error': 'Instagram 요청 처리 실패'})
            result = service.collect()
            self.send(200 if result.get('success') else (409 if not result['accepted'] else 502), result)

    class LocalServer(ThreadingHTTPServer):
        # On Windows SO_REUSEADDR can let two app instances bind the same port.
        # Fail explicitly instead of returning responses from an older process.
        allow_reuse_address = os.name != 'nt'

    return LocalServer((host, port), Handler)
