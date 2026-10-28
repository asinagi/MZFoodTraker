"""B2 specifications -> existing provider clients/parsers and fenced legacy stores.

    B3 owns quota. These adapters never invoke legacy collect/reserve methods.
    Bodies stay on the stack; NAVER bodies never enter durable state.
"""
import copy
import json
from datetime import timedelta
from .contracts import envelope, encode, require_operation
from .execution import RequestSession, ExecutionError
from .instagram_service import InstagramService
from .instagram_storage import InstagramStorage
from .naver_datalab import request_body
from .tracking_service import stamp, instant
from .youtube import items, video_id, parse_video
from .youtube_storage import YouTubeStore


class SourceAdapter:
    def __init__(self, client):
        # Isolate request hooks/counters from a legacy service instance.
        self.client = copy.copy(client)
        if hasattr(self.client, 'before_request'):
            self.client.before_request = lambda: None

    def _session(self, execution, claim, plan, fault):
        expected = 'mock' if claim['job']['mode']=='mock' else 'live_api'
        if getattr(self.client, 'origin', None) != expected:
            raise ExecutionError('policy')
        if not self.client.config.configured:
            raise ExecutionError('policy')
        if expected=='live_api':
            expected_scope='ig:'+self.client.config.user_id if plan['source_id']=='instagram_graph' else 'local-default'
            if plan['account_scope']!=expected_scope:
                raise ExecutionError('policy')
        if plan['source_id']=='instagram_graph' and self.client.config.version != plan['canonical_query']['parameters']['api_version']:
            raise ExecutionError('policy')
        return RequestSession(execution, claim, plan, fault)

    @staticmethod
    def _new_items(db, session, values, key, days):
        """One accepted item per logical job, irrespective of duplicate pages/retries."""
        result = []
        for value in {v[key]:v for v in values}.values():
            changed = db.execute('INSERT OR IGNORE INTO execution_item_receipts VALUES (?,?,?,?)',
                (session.claim['job']['job_id'], value[key], stamp(session.execution.now()),
                 stamp(session.execution.now()+timedelta(days=days)))).rowcount
            if changed:
                result.append(value)
        return result


class YouTubeExecutor(SourceAdapter):
    def execute(self, execution, claim, plan, fault):
        s = self._session(execution, claim, plan, fault)
        params = dict(plan['canonical_query']['parameters'])
        store = YouTubeStore(execution.storage)
        if not s.state:
            with execution.storage.connect() as db:
                db.execute('BEGIN IMMEDIATE');s._fence(db)
                mode = 'b3:' + plan['account_scope']
                db.execute('INSERT OR IGNORE INTO youtube_queries(query,mode) VALUES (?,?)', (params['q'],mode))
                query = db.execute('SELECT id FROM youtube_queries WHERE query=? AND mode=?', (params['q'],mode)).fetchone()[0]
                state = {'phase':'search','page':0,'cursor':None,'legacy_query_id':query}
                db.execute('INSERT INTO execution_checkpoints VALUES (?,?,?,?)',
                    (claim['job']['job_id'],json.dumps(state),stamp(execution.now()),stamp(execution.now()+timedelta(days=7))))
                s.state = state
        while not s.state.get('done'):
            state = dict(s.state);page = state['page']
            if state['phase']=='search':
                request = dict(params)
                if state.get('cursor'):
                    request['pageToken'] = state['cursor']
                def parse(payload):
                    rows = items(payload)
                    if len(rows)>50:
                        raise ExecutionError('invalid_response')
                    ids = [video_id(r.get('id',{}).get('videoId')) for r in rows if isinstance(r,dict)]
                    if len(ids)!=len(rows):raise ExecutionError('invalid_response')
                    cursor = payload.get('nextPageToken')
                    if cursor is not None and (not isinstance(cursor,str) or not cursor or len(cursor)>2048 or cursor==state.get('cursor')):
                        raise ExecutionError('invalid_response')
                    return {'ids':list(dict.fromkeys(ids)), 'count':len(rows), 'cursor':cursor}
                def checkpoint(parsed):
                    more = bool(parsed['cursor']);limited=more and page+1>=plan['canonical_query']['max_pages']
                    return dict(state,phase='details' if parsed['ids'] else 'search',pending_ids=parsed['ids'],
                                cursor=parsed['cursor'],more=more,limited=limited,
                                page=page if parsed['ids'] else page+1,done=not parsed['ids'] and (not more or limited))
                def save_search(db, parsed, targets):
                    db.execute('UPDATE youtube_queries SET page_token=?,token_at=?,last_attempt=?,after_at=?,before_at=? WHERE id=?',
                        (parsed['cursor'],stamp(execution.now()),stamp(execution.now()),params['publishedAfter'],params['publishedBefore'],state['legacy_query_id']))
                    return parsed['count'],0
                s.request('search','search:'+str(page),
                    lambda:self.client.get('search',request,lambda endpoint:None,lambda *args:None,single_attempt=True),
                    parse,save_search,checkpoint)
            else:
                requested=state['pending_ids']
                detail={'part':'snippet,statistics,contentDetails,status','id':','.join(requested)}
                def parse(payload):
                    videos=[parse_video(r) for r in items(payload)]
                    if any(v['video_id'] not in requested for v in videos) or len(videos)>50:
                        raise ExecutionError('invalid_response')
                    return videos
                def persist(db, videos, targets):
                    public=[v for v in videos if v['privacy_status']=='public' and v.get('upload_status') not in ('deleted','failed','rejected')]
                    fresh=self._new_items(db,s,public,'video_id',29)
                    absent=set(requested)-{v['video_id'] for v in public}
                    saved=store.save_videos([v['video_id'] for v in fresh]+sorted(absent),fresh,'mock' if claim['job']['mode']=='mock' else 'live_api',
                        claim['job']['run_id'],dict(params,endpoint='search'),connection=db,observed_at=stamp(execution.now())) if fresh or absent else set()
                    # Deletion of unavailable requested IDs is independent of logical receipt deduplication.
                    missing=set(requested)-{v['video_id'] for v in videos}
                    for identity in missing:db.execute('DELETE FROM youtube_videos WHERE video_id=?',(identity,))
                    for identity in saved:
                        db.execute('INSERT OR IGNORE INTO youtube_matches VALUES (?,?,?)',(state['legacy_query_id'],identity,stamp(execution.now())))
                    return len(videos),len(saved)
                s.request('videos','videos:'+str(page),
                    lambda:self.client.get('videos',detail,lambda endpoint:None,lambda *args:None,single_attempt=True),
                    parse,persist,lambda parsed:dict(state,phase='search',page=page+1,pending_ids=[],done=not state['more'] or state['limited']))
        return {'status':'LIMITED' if s.state.get('limited') else 'SUCCEEDED'}


class InstagramExecutor(SourceAdapter):
    def execute(self, execution, claim, plan, fault):
        s=self._session(execution,claim,plan,fault)
        cfg=self.client.config;params=plan['canonical_query']['parameters'];tagname=params['q']
        retention=min(cfg.caption_retention_days,plan['contract']['policy']['retention_days'])
        store=InstagramStorage(execution.storage,cfg.user_id,retention)
        if not s.state:
            with execution.storage.connect() as db:
                db.execute('BEGIN IMMEDIATE');s._fence(db)
                db.execute('INSERT OR IGNORE INTO instagram_hashtags(user_id,name,created_at) VALUES (?,?,?)',(cfg.user_id,tagname,stamp(execution.now())))
                tag=dict(db.execute('SELECT * FROM instagram_hashtags WHERE user_id=? AND name=?',(cfg.user_id,tagname)).fetchone())
                state={'phase':'lookup','page':0,'legacy_tag_id':tag['id'],'hashtag_id':tag['instagram_id']}
                db.execute('INSERT INTO execution_checkpoints VALUES (?,?,?,?)',(claim['job']['job_id'],json.dumps(state),stamp(execution.now()),stamp(execution.now()+timedelta(days=7))))
                s.state=state
        if claim['job']['mode']=='live':
            # Existing collector checks the provider's recent unique-tag set too.
            remote=[];cursor=None
            for page in range(3):
                request={'limit':30}
                if cursor:request['after']=cursor
                def parse_recent(payload):
                    rows=payload.get('data');paging=payload.get('paging') or {}
                    if not isinstance(rows,list) or not isinstance(paging,dict):raise ExecutionError('invalid_response')
                    ids=[r.get('id') for r in rows if isinstance(r,dict)]
                    if len(ids)!=len(rows) or any(not isinstance(v,str) or not v.isdigit() for v in ids):raise ExecutionError('invalid_response')
                    after=(paging.get('cursors') or {}).get('after') if paging.get('next') else None
                    if paging.get('next') and (not isinstance(after,str) or after==cursor):raise ExecutionError('invalid_response')
                    return ids,after
                parsed=s.request('recent_searches','remote-usage:'+str(page),
                    lambda:self.client.get(cfg.user_id+'/recently_searched_hashtags',request,single_attempt=True),parse_recent,
                    lambda db,parsed,targets:(len(parsed[0]),0),lambda parsed:dict(s.state))
                remote.extend(parsed[0]);cursor=parsed[1]
                if not cursor:break
            if cursor:raise ExecutionError('invalid_response')
            if len(set(remote))>=min(30,cfg.search_soft_limit) and s.state.get('hashtag_id') not in remote:
                raise ExecutionError('budget',retry_after=7*86400)
        if s.state['phase']=='lookup':
            def parse(payload):
                rows=payload.get('data')
                if not isinstance(rows,list):raise ExecutionError('invalid_response')
                if not rows:return None
                value=rows[0].get('id') if isinstance(rows[0],dict) else None
                if not isinstance(value,str) or not value.isdigit():raise ExecutionError('invalid_response')
                return value
            def save(db, identifier, targets):
                db.execute('UPDATE instagram_hashtags SET instagram_id=?,last_searched_at=? WHERE id=?',(identifier,stamp(execution.now()),s.state['legacy_tag_id']))
                return int(identifier is not None),0
            s.request('hashtag_lookup','lookup',lambda:self.client.get('ig_hashtag_search',{'user_id':cfg.user_id,'q':tagname},single_attempt=True),
                parse,save,lambda identifier:dict(s.state,phase='media',hashtag_id=identifier,done=identifier is None))
        while not s.state.get('done'):
            state=dict(s.state);page=state['page'];request={'user_id':cfg.user_id,'fields':','.join(params['fields']),'limit':params['limit']}
            if state.get('cursor'):request['after']=state['cursor']
            def parse(payload):
                rows=payload.get('data');paging=payload.get('paging') or {}
                if not isinstance(rows,list) or len(rows)>50 or not isinstance(paging,dict):raise ExecutionError('invalid_response')
                media=[InstagramService.clean_media(r) for r in rows]
                more=bool(paging.get('next'));cursor=(paging.get('cursors') or {}).get('after') if more else None
                if more and (not isinstance(cursor,str) or not cursor or len(cursor)>2048 or cursor==state.get('cursor')):raise ExecutionError('invalid_response')
                return media,more,cursor
            def save(db,parsed,targets):
                require_operation('instagram_graph','existing_caption_hashtags')
                media,more,cursor=parsed
                fresh=self._new_items(db,s,media,'id',retention)
                now=stamp(execution.now())
                contract=envelope('instagram_graph','mock' if claim['job']['mode']=='mock' else 'live_api',now,
                    request=plan['contract']['request_conditions'],retention_days=retention)
                run_id=db.execute('INSERT INTO instagram_runs(hashtag_id,started_at,origin,contract_json) VALUES (?,?,?,?)',
                    (state['legacy_tag_id'],now,contract['origin'],encode(contract))).lastrowid
                store.finish_run(run_id,{m['id']:m for m in fresh},len(media),1,
                    truncated=more,connection=db,observed_at=now)
                return len(media),len(fresh)
            s.request('recent_media','media:'+str(page),lambda:self.client.get(state['hashtag_id']+'/recent_media',request,single_attempt=True),
                parse,save,lambda parsed:dict(state,page=page+1,cursor=parsed[2],more=parsed[1],
                    limited=parsed[1] and page+1>=plan['canonical_query']['max_pages'],done=not parsed[1] or page+1>=plan['canonical_query']['max_pages']))
        return {'status':'LIMITED' if s.state.get('limited') else 'SUCCEEDED'}


class NaverExecutor(SourceAdapter):
    def execute(self, execution, claim, plan, fault):
        s=self._session(execution,claim,plan,fault)
        if s.state.get('done'):
            return {'status':'SUCCEEDED'}
        p=plan['canonical_query']['parameters']
        body=request_body(p['keywordGroups'],start=p['startDate'],end=p['endDate'],time_unit=p['timeUnit'],
                          filters={k:p[k] for k in ('device','gender','ages') if k in p})
        if body!=p:raise ExecutionError('validation')
        # search() already calls the existing strict parse_response. Returned values are transient.
        s.request('naver_search','request',lambda:self.client.search(body,single_attempt=True),lambda result:result,
                  lambda db,result,targets:(len(result),len(result)),lambda result:{'done':True})
        return {'status':'SUCCEEDED'}


def adapters_from_clients(clients):
    classes={'instagram_graph':InstagramExecutor,'youtube':YouTubeExecutor,'naver_datalab':NaverExecutor}
    return {key:classes[key[0]](client) for key,client in clients.items()}
