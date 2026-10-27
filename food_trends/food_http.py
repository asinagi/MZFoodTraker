"""Small localhost HTTP boundary for the reviewed identity service."""
import json
from urllib.parse import urlsplit
from .food_service import FoodError


def get(handler, foods, path, params):
    try:
        if path == '/api/foods':
            result = {'items': foods.foods()}
        elif path == '/api/food-candidates':
            result = {'items': foods.candidates()}
        elif path == '/api/food-events':
            result = {'items': foods.events(params.get('subject_id', [''])[0])}
        elif path == '/api/foods/resolve':
            result = foods.resolve_name(params.get('name', [''])[0], params.get('scope', ['food:ko'])[0])
        elif path.startswith('/api/foods/'):
            result = foods.food(path.rsplit('/', 1)[-1])
        elif path.startswith('/api/food-candidates/'):
            result = foods.candidate(path.rsplit('/', 1)[-1])
        else:
            raise FoodError('경로를 찾을 수 없습니다.', 404)
        return handler.send(200, result)
    except FoodError as exc:
        return handler.send(exc.status, {'error': str(exc)})


def post(handler, foods):
    if urlsplit('http://' + handler.headers.get('Host', '')).hostname not in ('localhost', '127.0.0.1', '::1'):
        return handler.send(403, {'error': '로컬 주소에서 요청하세요.'})
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 1 <= size <= 16384:
            return handler.send(413, {'error': '요청 크기를 확인하세요.'})
        if handler.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return handler.send(415, {'error': 'application/json을 사용하세요.'})
        body = json.loads(handler.rfile.read(size))
        operation = handler.path.removeprefix('/api/foods/')
        result = foods.command(operation, body)
        return handler.send(200, result)
    except FoodError as exc:
        return handler.send(exc.status, {'error': str(exc)})
    except (ValueError, TypeError, OverflowError):
        return handler.send(400, {'error': '요청 형식을 확인하세요.'})
    except Exception:
        return handler.send(500, {'error': '음식 개체 처리에 실패했습니다.'})
