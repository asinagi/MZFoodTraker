"""Local explicit execution commands. GET only reads metadata."""
import json
from urllib.parse import urlsplit
from .food_service import FoodError


def get(handler, controller, path, params):
    try:
        if path=='/api/execution/status':
            if params:raise FoodError('조회 옵션을 확인하세요.')
            result=controller.status()
        elif path.startswith('/api/execution/jobs/'):
            if params:raise FoodError('조회 옵션을 확인하세요.')
            result=controller.status(path.rsplit('/',1)[-1])
        else:raise FoodError('경로를 찾을 수 없습니다.',404)
        return handler.send(200,result)
    except FoodError as exc:return handler.send(exc.status,{'error':str(exc)})


def post(handler, controller):
    try:
        if urlsplit('http://'+handler.headers.get('Host','')).hostname not in ('127.0.0.1','localhost','::1'):
            raise FoodError('로컬 주소에서 요청하세요.',403)
        size=int(handler.headers.get('Content-Length','0'))
        if not 1<=size<=32768:raise FoodError('실행 요청 크기를 확인하세요.',413)
        if handler.headers.get('Content-Type','').split(';')[0]!='application/json':raise FoodError('JSON을 사용하세요.',415)
        body=json.loads(handler.rfile.read(size))
        operation=handler.path.removeprefix('/api/execution/')
        allowed={'enqueue':{'plan_ids','mode'},'schedule':{'options','mode'},'run':{'job_id'},'tick':{'options','mode','max_jobs'},'recover-rss':{'limit'}}
        if operation not in allowed or not isinstance(body,dict) or set(body)-allowed[operation]:
            raise FoodError('실행 요청을 확인하세요.')
        if operation=='enqueue':result={'items':controller.execution.enqueue(body.get('plan_ids'),body.get('mode','mock'))}
        elif operation=='schedule':result={'items':controller.execution.schedule(body.get('options'),body.get('mode','mock'))}
        elif operation=='tick':result=controller.tick(body.get('options'),body.get('mode','mock'),body.get('max_jobs',10))
        elif operation=='recover-rss':result=controller.recover_rss(body.get('limit',100))
        else:
            if not isinstance(body.get('job_id'),str):raise FoodError('작업 ID가 필요합니다.')
            result={'result':controller.run_once(body['job_id'])}
        return handler.send(200,result)
    except FoodError as exc:return handler.send(exc.status,{'error':str(exc)})
    except (ValueError,TypeError,KeyError):return handler.send(400,{'error':'실행 요청 형식을 확인하세요.'})
    except Exception:return handler.send(500,{'error':'실행 요청 처리 실패. 작업 상태를 확인하세요.'})
