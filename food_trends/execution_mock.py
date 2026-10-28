"""Deterministic provider-shaped demo responses. Never opens a socket or loads .env."""
import io
import json
from urllib.parse import urlsplit,parse_qs
from .config import InstagramConfig,YouTubeConfig,NaverConfig
from .instagram_graph import GraphClient
from .youtube import YouTubeClient
from .naver_datalab import NaverClient
from .execution import ExecutionService
from .execution_runtime import ExecutionController
from .execution_sources import adapters_from_clients


class MockResponse(io.BytesIO):
    status=200


class MockTransport:
    def __init__(self):
        self.calls=[]

    def __call__(self, request, timeout=None):
        path=urlsplit(request.full_url).path;query=parse_qs(urlsplit(request.full_url).query)
        self.calls.append(path)
        if path.endswith('ig_hashtag_search'):
            result={'data':[{'id':'123456'}]}
        elif path.endswith('recently_searched_hashtags'):
            result={'data':[]}
        elif path.endswith('recent_media'):
            result={'data':[{'id':'900001','timestamp':'2026-10-26T00:00:00Z','media_type':'IMAGE',
                    'caption':'#두쫀쿠 #디저트','permalink':'https://www.instagram.com/p/mockexample/'}]}
        elif path.endswith('/search') and request.data is not None:
            body=json.loads(request.data)
            return dict(startDate=body['startDate'],endDate=body['endDate'],timeUnit=body['timeUnit'],
                        results=[{'title':g['groupName'],'keywords':g['keywords'],'data':[]} for g in body['keywordGroups']])
        elif path.endswith('/search'):
            result={'items':[{'id':{'videoId':'mockvideo01'}}]}
        elif path.endswith('/videos'):
            result={'items':[{'id':identity,'snippet':{'title':'Mock public video','description':'Mock snapshot',
                'publishedAt':'2026-10-26T00:00:00Z','channelId':'mock-channel','channelTitle':'Mock channel'},
                'status':{'privacyStatus':'public','uploadStatus':'processed'},'statistics':{'viewCount':'0'},
                'contentDetails':{'duration':'PT1M'}} for identity in query.get('id',[''])[0].split(',')]}
        else:
            raise AssertionError('Unexpected mock endpoint')
        return MockResponse(json.dumps(result,ensure_ascii=False).encode())


def mock_controller(storage, planner, transport=None):
    transport=transport or MockTransport()
    clients={}
    for account in planner.tracking.accounts.values():
        if account.mode!='mock':continue
        if account.source_id=='instagram_graph':
            config=InstagramConfig(token='synthetic-mock-token',version=account.options.get('api_version','v26.0'),
                max_pages=account.options.get('max_pages',20))
            client=GraphClient(config,transport)
        elif account.source_id=='youtube':
            client=YouTubeClient(YouTubeConfig(api_key='synthetic-mock-key',retries=0),transport)
        else:
            client=NaverClient(NaverConfig(client_id='synthetic-mock-id',client_secret='synthetic-mock-secret',retries=0),transport)
        clients[(account.source_id,account.account_scope)]=client
    from .ingestion import IngestionService
    return ExecutionController(ExecutionService(storage,planner),adapters_from_clients(clients),IngestionService(storage,planner.tracking.clock))
