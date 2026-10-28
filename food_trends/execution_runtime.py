"""Explicit runtime assembly. Importing or constructing the normal server starts no worker."""
import json
import os
import threading
from dataclasses import replace
from .contracts import validate_metadata
from .execution import ExecutionService, ExecutionSettings, ExecutionWorker
from .execution_sources import adapters_from_clients
from .food_service import FoodError


class ExecutionController:
    def __init__(self, execution, adapters=None):
        self.execution=execution
        self.worker=ExecutionWorker(execution,adapters or {})
        self.stop_event=threading.Event()
        self.thread=None

    def status(self, job_id=None):
        result=self.execution.status(job_id)
        result['available_modes']=['preview']+(['mock'] if any(getattr(a.client,'origin',None)=='mock' for a in self.worker.adapters.values()) else [])
        if self.execution.settings.live_enabled:
            result['available_modes'].append('live')
        result['worker_started']=bool(self.thread and self.thread.is_alive())
        return result

    def run_once(self, job_id=None):
        return self.worker.run_once(job_id)

    def recover_rss(self, limit=100):
        """Local, explicit retry of the A4 bridge, never a feed fetch or auto-approval."""
        if type(limit) is not int or not 1<=limit<=100:
            raise FoodError('재처리 회차 수는 1~100입니다.')
        with self.execution.storage.reader() as db:
            runs=[r[0] for r in db.execute('''SELECT DISTINCT o.run_id FROM observations o
                JOIN runs r ON r.id=o.run_id JOIN candidates c ON c.item_id=o.item_id
                WHERE r.success=1 AND c.classification IN ('FOOD','REVIEW') AND NOT EXISTS
                (SELECT 1 FROM food_candidates f WHERE f.source_code='google_trends_rss' AND f.source_item_id=o.item_id)
                ORDER BY o.run_id LIMIT ?''',(limit,))]
        results=[]
        for run in runs:
            try:results.append(dict(run_id=run,**self.execution.tracking.foods.import_rss_run(run)))
            except Exception:results.append({'run_id':run,'state':'pending_retry'})
        return {'results':results,'http_requests':0}

    def tick(self, options=None, mode='mock', max_jobs=10):
        if mode not in ('mock','live'):
            raise FoodError('실행 모드를 확인하세요.')
        if type(max_jobs) is not int or not 1<=max_jobs<=50:
            raise FoodError('한 번의 처리 작업 수는 1~50입니다.')
        jobs=self.execution.schedule(options,mode)
        results=[]
        for _ in range(max_jobs):
            result=self.worker.run_once(mode=mode)
            if result is None:break
            results.append(result)
        return {'scheduled':jobs,'results':results}

    def start(self):
        if not self.execution.settings.automatic or not self.execution.settings.live_enabled:
            raise FoodError('자동 Live 실행은 명시적 운영 설정이 필요합니다.',403)
        if self.thread and self.thread.is_alive():return
        def loop():
            while not self.stop_event.is_set():
                try:self.tick({'include_discovery':True},'live')
                except Exception:pass  # Individual job failures are recorded without remote text.
                self.stop_event.wait(10)
        self.thread=threading.Thread(target=loop,name='b3-scheduler-worker',daemon=True)
        self.thread.start()


def live_controller(storage, registry_path, environ=None):
    """Operator entrypoint; grants are credential-free, reviewed server configuration.

    Does not migrate, transfer writers, start threads or call APIs. The caller must do
    those separately in an approved deployment window. HTTP cannot supply this registry.
    """
    env=os.environ if environ is None else environ
    if env.get('B3_LIVE_ENABLED','false').lower()!='true':
        raise FoodError('B3_LIVE_ENABLED=true 설정이 필요합니다.',403)
    from .config import InstagramConfig,YouTubeConfig,NaverConfig
    from .instagram_graph import GraphClient
    from .youtube import YouTubeClient
    from .naver_datalab import NaverClient
    from .tracking_access import accounts_from_services
    from .tracking_service import TrackingService
    from .query_planner import QueryPlanner
    from pathlib import Path
    from types import SimpleNamespace
    grants=json.loads(Path(registry_path).read_text(encoding='utf-8-sig'))
    validate_metadata(grants)
    if not isinstance(grants,list):raise FoodError('출처 승인 registry 목록이 필요합니다.')
    allowed={'source_id','account_scope','permission_verified','policy_allowed','approved_by','evidence'}
    if any(not isinstance(g,dict) or set(g)!=allowed or g['permission_verified'] is not True or g['policy_allowed'] is not True
           or not isinstance(g['approved_by'],str) or not g['approved_by'].strip() or not isinstance(g['evidence'],str) or not g['evidence'].strip() for g in grants):
        raise FoodError('출처별 승인과 근거가 명시된 registry가 필요합니다.',403)
    configs={'instagram_graph':InstagramConfig.from_env(),'youtube':YouTubeConfig.from_env(),'naver_datalab':NaverConfig.from_env()}
    accounts=accounts_from_services(SimpleNamespace(config=configs['instagram_graph']),SimpleNamespace(config=configs['naver_datalab']),SimpleNamespace(config=configs['youtube']))
    lookup={(g['source_id'],g['account_scope']):g for g in grants}
    if len(lookup)!=len(grants) or set(lookup)-{(a.source_id,a.account_scope) for a in accounts}:
        raise FoodError('동일 공급자 계정을 여러 scope로 분할할 수 없습니다.')
    accounts=[replace(a,permission_verified=True,policy_allowed=True) if (a.source_id,a.account_scope) in lookup else a for a in accounts]
    tracking=TrackingService(storage,accounts);planner=QueryPlanner(storage,tracking)
    exclusive=tuple(s for s in env.get('B3_EXCLUSIVE_SOURCES','').split(',') if s)
    settings=ExecutionSettings(live_enabled=True,automatic=env.get('B3_AUTOMATIC_ENABLED','false').lower()=='true',exclusive_live_sources=exclusive)
    classes={'instagram_graph':GraphClient,'youtube':YouTubeClient,'naver_datalab':NaverClient}
    clients={(a.source_id,a.account_scope):classes[a.source_id](configs[a.source_id]) for a in accounts if (a.source_id,a.account_scope) in lookup}
    return ExecutionController(ExecutionService(storage,planner,settings),adapters_from_clients(clients))
