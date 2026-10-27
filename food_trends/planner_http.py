"""Preview and persist specifications only. Quota reservation is an internal B3 contract."""
import json
from urllib.parse import urlsplit
from .food_service import FoodError


def get(handler, planner, path, params):
    try:
        if path=='/api/planner/preview':
            if set(params)-{'target_id','youtube_days','naver_days','youtube_start','youtube_end','naver_start','naver_end',
                            'regionCode','relevanceLanguage','order','timeUnit','include_discovery','operation','filters'}:
                raise FoodError('알 수 없는 계획 조회 옵션입니다.')
            options={}
            for key in ('youtube_days','naver_days'):
                if key in params:options[key]=int(params[key][0])
            for key in ('youtube_start','youtube_end','naver_start','naver_end','regionCode','relevanceLanguage','order','timeUnit','operation'):
                if key in params:options[key]=params[key][0]
            if 'filters' in params:options['filters']=json.loads(params['filters'][0])
            if 'include_discovery' in params:
                if params['include_discovery'][0] not in ('true','false'):raise FoodError('발굴 포함 여부를 확인하세요.')
                options['include_discovery']=params['include_discovery'][0]=='true'
            data=planner.preview(params.get('target_id'),options)
        elif path=='/api/planner/history':
            data={'items':planner.history(params.get('target_id',[None])[0])}
        elif path.startswith('/api/planner/plans/'):
            data=planner.plan(path.rsplit('/',1)[-1])
        else:raise FoodError('경로를 찾을 수 없습니다.',404)
        return handler.send(200,data)
    except FoodError as exc:return handler.send(exc.status,{'error':str(exc)})
    except (ValueError,TypeError):return handler.send(400,{'error':'계획 조회 입력을 확인하세요.'})


def post(handler, planner):
    try:
        if urlsplit('http://'+handler.headers.get('Host','')).hostname not in ('127.0.0.1','localhost','::1'):
            raise FoodError('로컬 주소에서 요청하세요.',403)
        size=int(handler.headers.get('Content-Length','0'))
        if not 1<=size<=32768:raise FoodError('계획 요청 크기를 확인하세요.',413)
        if handler.headers.get('Content-Type','').split(';')[0]!='application/json':raise FoodError('JSON을 사용하세요.',415)
        body=json.loads(handler.rfile.read(size));operation=handler.path.removeprefix('/api/planner/')
        allowed={'target_ids','options'}|({'request_key'} if operation=='commit' else set())
        if operation not in ('preview','commit') or not isinstance(body,dict) or set(body)-allowed:
            raise FoodError('지원하지 않는 계획 요청입니다.')
        result=(planner.preview(body.get('target_ids'),body.get('options')) if operation=='preview' else
                planner.commit(body.get('request_key'),body.get('target_ids'),body.get('options')))
        return handler.send(200,result)
    except FoodError as exc:return handler.send(exc.status,{'error':str(exc)})
    except (ValueError,TypeError,KeyError):return handler.send(400,{'error':'계획 입력 형식을 확인하세요.'})
    except Exception:return handler.send(500,{'error':'계획 처리 중 오류가 발생했습니다.'})
