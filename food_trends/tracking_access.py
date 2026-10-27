"""Server-owned planning permissions. HTTP users cannot grant account or policy rights."""
from dataclasses import dataclass, field
import copy
from .contracts import validate_metadata, require_operation
from .food_service import FoodError

SOURCES = ('instagram_graph', 'youtube', 'naver_datalab')
DEFAULT_INTERVALS = {'instagram_graph': 3600, 'youtube': 21600, 'naver_datalab': 86400}


@dataclass(frozen=True)
class Account:
    source_id: str
    account_scope: str = 'local-default'
    configured: bool = False
    enabled: bool = True
    permission_verified: bool = False
    policy_allowed: bool = False
    mode: str = 'live'
    interval_seconds: int = 0
    options: dict = field(default_factory=dict)

    def __post_init__(self):
        import re
        if self.source_id not in SOURCES or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', self.account_scope):
            raise ValueError('Invalid planning account scope')
        if self.mode not in ('live', 'mock'):
            raise ValueError('Invalid account mode')
        if type(self.interval_seconds) is not int or self.interval_seconds < 0:
            raise ValueError('Invalid planning interval')
        allowed = {'max_pages', 'soft_limit', 'daily_search_limit', 'daily_video_limit', 'daily_limit',
                   'discovery_queries', 'api_version', 'regionCode', 'relevanceLanguage', 'order', 'rss_interval'}
        if not isinstance(self.options, dict) or set(self.options) - allowed:
            raise ValueError('Unsupported planning configuration')
        validate_metadata(self.options)
        numeric={'max_pages':(1,100 if self.source_id=='instagram_graph' else 5),'soft_limit':(1,30),
                 'daily_search_limit':(1,100),'daily_video_limit':(1,10000),'daily_limit':(1,100),'rss_interval':(1,366*86400)}
        for key,(minimum,maximum) in numeric.items():
            if key in self.options and (type(self.options[key]) is not int or not minimum<=self.options[key]<=maximum):
                raise ValueError('Planning limit exceeds existing MVP configuration')
        if 'discovery_queries' in self.options:
            queries=self.options['discovery_queries']
            if not isinstance(queries,list) or not 1<=len(queries)<=20 or any(not isinstance(q,str) or not q.strip() or len(q)>100 for q in queries):
                raise ValueError('Invalid configured discovery queries')
        object.__setattr__(self, 'options', copy.deepcopy(self.options))

    def blockers(self):
        return [name for name, ok in [('NOT_CONFIGURED', self.configured), ('DISABLED', self.enabled),
                ('POLICY_UNCONFIRMED', self.policy_allowed), ('PERMISSION_UNVERIFIED', self.permission_verified)] if not ok]

    def public(self):
        return dict(source_id=self.source_id, account_scope=self.account_scope, mode=self.mode,
                    configured=self.configured, enabled=self.enabled, permission_verified=self.permission_verified,
                    policy_allowed=self.policy_allowed, blockers=self.blockers(),
                    interval_seconds=self.interval_seconds or DEFAULT_INTERVALS[self.source_id],
                    options=copy.deepcopy(self.options), execution_enabled=False)


def permitted_operation(source_id, operation):
    # These are request-planning capabilities for existing adapters, not derived-analysis grants.
    allowed = {'instagram_graph': {'tracking'}, 'youtube': {'tracking', 'discovery', 'videos'},
               'naver_datalab': {'tracking'}, 'google_trends_rss': {'discovery'}}
    if operation not in allowed.get(source_id, set()):
        try:
            require_operation(source_id, operation)
        except (ValueError, KeyError):
            raise FoodError('출처 정책이 이 계획 작업을 허용하지 않습니다.', 403) from None
        raise FoodError('해당 작업의 계획 어댑터가 없습니다.', 403)


def mock_accounts():
    return [Account(source, configured=True, permission_verified=True, policy_allowed=True, mode='mock') for source in SOURCES]


def accounts_from_services(instagram=None, naver=None, youtube=None):
    """Read configuration flags only. Credentials and legacy success do not prove current grants."""
    result = []
    for source, service in [('instagram_graph', instagram), ('naver_datalab', naver), ('youtube', youtube)]:
        if service is None:
            result.append(Account(source)); continue
        cfg = service.config
        if source == 'instagram_graph':
            options = dict(max_pages=cfg.max_pages, soft_limit=cfg.search_soft_limit, api_version=cfg.version)
            interval = int(cfg.interval_minutes * 60)
            scope = 'ig:' + cfg.user_id
        elif source == 'youtube':
            options = dict(max_pages=cfg.max_pages, daily_search_limit=cfg.daily_search_limit,
                           daily_video_limit=cfg.daily_video_limit, discovery_queries=list(cfg.discovery_queries))
            interval, scope = cfg.interval_seconds, 'local-default'
        else:
            options = dict(daily_limit=cfg.daily_limit)
            interval, scope = cfg.interval_seconds, 'local-default'
        result.append(Account(source, scope, cfg.configured, cfg.enabled, interval_seconds=interval, options=options))
    return result
