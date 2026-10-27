"""Local management boundary; no account grants can be written through this API."""
import json
from urllib.parse import urlsplit
from .food_service import FoodError


def get(handler, tracking, path, params):
    try:
        if path == '/api/tracking/accounts':
            data = {'items': [a.public() for a in tracking.accounts.values()], 'execution_enabled': False}
        elif path == '/api/tracking/targets':
            data = {'items': tracking.list(params.get('food_id', [None])[0])}
        elif path == '/api/tracking/due':
            data = {'items': tracking.due(), 'execution_enabled': False}
        elif path == '/api/tracking/events':
            data = {'items': tracking.history(params.get('target_id', [''])[0])}
        else:
            raise FoodError('경로를 찾을 수 없습니다.', 404)
        return handler.send(200, data)
    except FoodError as exc:
        return handler.send(exc.status, {'error': str(exc)})


def post(handler, tracking):
    try:
        if urlsplit('http://' + handler.headers.get('Host', '')).hostname not in ('localhost', '127.0.0.1', '::1'):
            raise FoodError('로컬 주소에서 요청하세요.', 403)
        size = int(handler.headers.get('Content-Length', '0'))
        if not 1 <= size <= 16384:
            raise FoodError('요청 크기를 확인하세요.', 413)
        if handler.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise FoodError('application/json을 사용하세요.', 415)
        body = json.loads(handler.rfile.read(size))
        data = tracking.command(handler.path.removeprefix('/api/tracking/'), body)
        return handler.send(200, data)
    except FoodError as exc:
        return handler.send(exc.status, {'error': str(exc)})
    except (ValueError, TypeError, KeyError):
        return handler.send(400, {'error': '추적 요청 형식을 확인하세요.'})
    except Exception:
        return handler.send(500, {'error': '추적 관리 요청을 처리하지 못했습니다.'})
