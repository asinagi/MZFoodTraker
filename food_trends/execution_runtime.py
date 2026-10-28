"""Explicit runtime assembly. Importing or constructing the normal server starts no worker."""
import json
import os
import threading
from dataclasses import replace
from datetime import datetime, timezone
from .contracts import validate_metadata
from .execution import ExecutionService, ExecutionSettings, ExecutionWorker, TERMINAL
from .execution_sources import adapters_from_clients
from .food_service import FoodError
from .ingestion import IngestionService, ingest_safely


class ExecutionController:
    def __init__(self, execution, adapters=None, ingestion=None):
        self.execution=execution
        self.worker=ExecutionWorker(execution,adapters or {})
        self.ingestion=ingestion
        self.stop_event=threading.Event()
        self.thread=None

    def _after(self, result):
        """B4 staging after a terminal job; a staging failure stays pending and never alters the job."""
        if self.ingestion and result and result.get('status') in TERMINAL and result.get('run_id'):
            summary=ingest_safely(self.ingestion.execution_run,result['run_id']) or {}
            result['ingestion']={k:v for k,v in summary.items() if k not in ('storable_metrics','transient_metrics')}
        return result

    def status(self, job_id=None):
        result=self.execution.status(job_id)
        result['available_modes']=['preview']+(['mock'] if any(getattr(a.client,'origin',None)=='mock' for a in self.worker.adapters.values()) else [])
        if self.execution.settings.live_enabled:
            result['available_modes'].append('live')
        result['worker_started']=bool(self.thread and self.thread.is_alive())
        return result

    def run_once(self, job_id=None):
        return self._after(self.worker.run_once(job_id))

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
            results.append(self._after(result))
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


# Legacy scheduler switch per source. Live B3 requires it explicitly false (the default is true).
LEGACY_SCHEDULER_FLAGS={'instagram_graph':'INSTAGRAM_COLLECTION_ENABLED','youtube':'YOUTUBE_COLLECTION_ENABLED',
                        'naver_datalab':'NAVER_DATALAB_ENABLED'}


def _grants(registry_path):
    from pathlib import Path
    grants=json.loads(Path(registry_path).read_text(encoding='utf-8-sig'))
    validate_metadata(grants)
    if not isinstance(grants,list):raise FoodError('출처 승인 registry 목록이 필요합니다.')
    allowed={'source_id','account_scope','permission_verified','policy_allowed','approved_by','evidence'}
    if any(not isinstance(g,dict) or set(g)!=allowed or g['permission_verified'] is not True or g['policy_allowed'] is not True
           or not isinstance(g['approved_by'],str) or not g['approved_by'].strip() or not isinstance(g['evidence'],str) or not g['evidence'].strip() for g in grants):
        raise FoodError('출처별 승인과 근거가 명시된 registry가 필요합니다.',403)
    return grants


def _configured_accounts():
    from .config import InstagramConfig,YouTubeConfig,NaverConfig
    from .tracking_access import accounts_from_services
    from types import SimpleNamespace
    configs={'instagram_graph':InstagramConfig.from_env(),'youtube':YouTubeConfig.from_env(),'naver_datalab':NaverConfig.from_env()}
    accounts=accounts_from_services(SimpleNamespace(config=configs['instagram_graph']),SimpleNamespace(config=configs['naver_datalab']),SimpleNamespace(config=configs['youtube']))
    return configs,accounts


def live_readiness(storage, registry_path, environ=None):
    """Read-only, fail-closed Live gate. Builds no client, sends no HTTP and writes nothing.

    Every listed exclusive source needs: a reviewed registry grant, configured credentials, an
    explicitly disabled legacy scheduler, transferred single-writer ownership and no active block.
    Error codes only; credential values and registry evidence are never echoed.
    """
    from .migrations import schema_version,LATEST_VERSION
    env=os.environ if environ is None else environ
    blockers=[]
    version=schema_version(storage.path)
    if version!=LATEST_VERSION:blockers.append('SCHEMA_VERSION_MISMATCH')
    if env.get('B3_LIVE_ENABLED','false').lower()!='true':blockers.append('B3_LIVE_DISABLED')
    try:
        grants={(g['source_id'],g['account_scope']) for g in _grants(registry_path)} if registry_path else None
    except (OSError,ValueError,FoodError):
        grants=None
    if grants is None:blockers.append('REGISTRY_MISSING_OR_INVALID')
    exclusive=tuple(s for s in env.get('B3_EXCLUSIVE_SOURCES','').split(',') if s)
    if not exclusive:blockers.append('NO_EXCLUSIVE_SOURCE')
    sources={}
    _,accounts=_configured_accounts()
    for source in exclusive:
        reasons=[]
        account=next((a for a in accounts if a.source_id==source),None)
        if account is None:
            sources[source]={'ready':False,'blockers':['UNKNOWN_SOURCE']};continue
        if not grants or (source,account.account_scope) not in grants:reasons.append('NO_REGISTRY_GRANT')
        if not account.configured:reasons.append('NOT_CONFIGURED')
        if env.get(LEGACY_SCHEDULER_FLAGS[source],'true').lower() not in ('false','0','no'):reasons.append('LEGACY_SCHEDULER_ENABLED')
        if version==LATEST_VERSION:
            with storage.reader() as db:
                owner=db.execute('SELECT owner FROM execution_source_ownership WHERE source_id=? AND account_scope=?',(source,account.account_scope)).fetchone()
                block=db.execute('SELECT error_kind FROM execution_blocks WHERE source_id=? AND account_scope=? AND (until_at IS NULL OR until_at>?)',
                                 (source,account.account_scope,datetime.now(timezone.utc).isoformat())).fetchone()
            if not owner or owner[0]!='B3':reasons.append('LEGACY_WRITER_OWNERSHIP_NOT_TRANSFERRED')
            if block:reasons.append('ACCOUNT_BLOCKED_'+block[0])
        sources[source]={'account_scope':account.account_scope,'ready':not reasons,'blockers':reasons}
    ready=not blockers and bool(sources) and all(s['ready'] for s in sources.values())
    return {'ready':ready,'blockers':blockers,'sources':sources,'schema_version':version,'required_version':LATEST_VERSION,
            'automatic_requested':env.get('B3_AUTOMATIC_ENABLED','false').lower()=='true','http_requests':0,'database_writes':0}


def live_controller(storage, registry_path, environ=None):
    """Operator entrypoint; grants are credential-free, reviewed server configuration.

    Refuses unless live_readiness passes. Does not migrate, transfer writers, start threads or
    call APIs; those are separate approved deployment steps. HTTP cannot supply this registry.
    Only exclusive, granted sources receive an adapter.
    """
    env=os.environ if environ is None else environ
    readiness=live_readiness(storage,registry_path,env)
    if not readiness['ready']:
        codes=sorted(set(readiness['blockers']+[c for s in readiness['sources'].values() for c in s['blockers']]))
        raise FoodError('Live 실행 준비 조건 미충족: '+', '.join(codes),403)
    from .instagram_graph import GraphClient
    from .youtube import YouTubeClient
    from .naver_datalab import NaverClient
    from .tracking_service import TrackingService
    from .query_planner import QueryPlanner
    configs,accounts=_configured_accounts()
    exclusive=tuple(readiness['sources'])
    live={(s,v['account_scope']) for s,v in readiness['sources'].items()}
    # Legacy scheduler flags are off for these sources, so B3 enablement comes from the reviewed grant.
    accounts=[replace(a,enabled=True,permission_verified=True,policy_allowed=True) if (a.source_id,a.account_scope) in live else a for a in accounts]
    tracking=TrackingService(storage,accounts);planner=QueryPlanner(storage,tracking)
    settings=ExecutionSettings(live_enabled=True,automatic=readiness['automatic_requested'],exclusive_live_sources=exclusive)
    classes={'instagram_graph':GraphClient,'youtube':YouTubeClient,'naver_datalab':NaverClient}
    clients={(a.source_id,a.account_scope):classes[a.source_id](configs[a.source_id]) for a in accounts if (a.source_id,a.account_scope) in live}
    return ExecutionController(ExecutionService(storage,planner,settings),adapters_from_clients(clients),IngestionService(storage))
