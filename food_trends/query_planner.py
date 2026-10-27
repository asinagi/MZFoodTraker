"""Versioned source request specifications. Planning does not import or invoke HTTP clients."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone, date
from .contracts import envelope, policy_for, validate_metadata, CONTRACT_VERSION, SOURCES
from .food_service import FoodError, dump, text
from .tracking_access import permitted_operation
from .tracking_service import instant, stamp
from .quota import QuotaManager, KST, COST_VERSION

PLANNER_VERSION = 'source-planner-v1'


def digest(value):
    validate_metadata(value)
    return hashlib.sha256(dump(value).encode()).hexdigest()


class QueryPlanner:
    def __init__(self, storage, tracking, rss_interval=1800):
        self.storage,self.tracking=storage,tracking
        if type(rss_interval) is not int or rss_interval<=0:raise ValueError('Invalid RSS interval')
        self.rss_interval=rss_interval
        self.quota=QuotaManager(storage,tracking,self._validate)

    def _options(self, value, now):
        value={} if value is None else value
        allowed={'youtube_days','youtube_start','youtube_end','regionCode','relevanceLanguage','order',
                 'naver_days','naver_start','naver_end','timeUnit','filters','include_discovery','operation'}
        if not isinstance(value,dict) or set(value)-allowed:raise FoodError('계획 옵션을 확인하세요.')
        validate_metadata(value)
        days=value.get('youtube_days',7);naver_days=value.get('naver_days',30)
        if type(days) is not int or not 1<=days<=3660 or type(naver_days) is not int or not 1<=naver_days<=3660:
            raise FoodError('조회 기간은 1~3660일이어야 합니다.')
        end=instant(value['youtube_end']) if 'youtube_end' in value else datetime.fromtimestamp(int(now.timestamp())//21600*21600,timezone.utc)
        start=instant(value['youtube_start']) if 'youtube_start' in value else end-timedelta(days=days)
        if not start<end or end>now:raise FoodError('YouTube 조회 기간을 확인하세요.')
        nend=date.fromisoformat(value['naver_end']) if 'naver_end' in value else now.astimezone(KST).date()-timedelta(days=1)
        nstart=date.fromisoformat(value['naver_start']) if 'naver_start' in value else nend-timedelta(days=naver_days-1)
        if not date(2016,1,1)<=nstart<=nend<now.astimezone(KST).date():raise FoodError('NAVER는 완료된 KST 날짜만 조회합니다.')
        filters=value.get('filters',{})
        if not isinstance(filters,dict) or set(filters)-{'device','gender','ages'}:raise FoodError('NAVER 필터를 확인하세요.')
        if 'device' in filters and filters['device'] not in ('pc','mo') or 'gender' in filters and filters['gender'] not in ('m','f'):
            raise FoodError('NAVER 필터를 확인하세요.')
        if 'ages' in filters:
            if not isinstance(filters['ages'],list) or not filters['ages'] or any(a not in tuple(map(str,range(1,12))) for a in filters['ages']):raise FoodError('연령 필터를 확인하세요.')
            filters=dict(filters,ages=sorted(set(filters['ages']),key=int))
        unit=value.get('timeUnit','date')
        if unit not in ('date','week','month'):raise FoodError('NAVER 기간 단위를 확인하세요.')
        if value.get('order','date') not in ('date','relevance','viewCount'):raise FoodError('검색 정렬을 확인하세요.')
        if not re.fullmatch(r'[A-Z]{2}',value.get('regionCode','KR')) or not re.fullmatch(r'[a-z]{2}(?:-[A-Za-z]+)?',value.get('relevanceLanguage','ko')):
            raise FoodError('지역·언어를 확인하세요.')
        if type(value.get('include_discovery',False)) is not bool:raise FoodError('발굴 계획 포함 여부를 확인하세요.')
        return dict(value,youtube_start=stamp(start),youtube_end=stamp(end),naver_start=nstart.isoformat(),naver_end=nend.isoformat(),timeUnit=unit,filters=filters)

    def _mapping(self, target, aliases, group=None):
        return dict(target_id=target['target_id'],food_id=target['food_id'],generation=target['generation'],
            food_revision=target['food_revision'],due_at=target['next_due_at'],alias_ids=sorted(a['alias_id'] for a in aliases),group_name=group)

    def _draft(self, source, scope, kind, canonical, mappings, now, account=None, window=None):
        permitted_operation(source,'discovery' if kind=='DISCOVERY' else 'tracking')
        spec=dict(source_id=source,account_scope=scope,query_kind=kind,canonical_query=canonical,planner_version=PLANNER_VERSION,
                  contract_version=CONTRACT_VERSION,adapter_version=SOURCES[source]['adapter_version'],
                  policy_version=policy_for(source)['version'],cost_version=COST_VERSION)
        spec_hash=digest({k:v for k,v in spec.items() if k!='query_kind'})
        mappings=sorted(mappings,key=lambda m:m['target_id'])
        if source=='youtube':
            slot=instant(canonical['parameters']['publishedBefore'])
        elif source=='naver_datalab':
            slot=datetime.combine(now.astimezone(KST).date(),datetime.min.time(),KST)
        else:
            interval=self.rss_interval if source=='google_trends_rss' else (account.interval_seconds or 3600)
            slot=datetime.fromtimestamp(int(now.timestamp())//interval*interval,timezone.utc)
        access=account.public() if account else {'source_id':source,'account_scope':scope,'rss_interval':self.rss_interval,'execution_enabled':False}
        options=json.loads(dump(account.options)) if account else {'rss_interval':self.rss_interval}
        fingerprint=digest(access);mapping_hash=digest(mappings)
        query_id='QUERY_'+spec_hash
        plan_id='PLAN_'+digest([query_id,stamp(slot),mapping_hash,fingerprint])
        params=canonical['parameters']
        if source=='instagram_graph':
            request={'hashtag':params['q'],'limit':params['limit'],'max_pages':canonical['max_pages'],
                     'api_version':params['api_version'],'fields':params['fields']}
        elif source=='youtube':request=params
        elif source=='naver_datalab':request=params
        else:request={'geo':'KR','endpoint':'trending/rss'}
        contract=json.loads(dump(envelope(source,'mock' if account and account.mode=='mock' else 'user',stamp(now),request=request,
            observation={'plan_only':True,'query_id':query_id,'planner_version':PLANNER_VERSION})))
        return dict(spec,query_kinds=[kind],query_id=query_id,plan_id=plan_id,spec_hash=spec_hash,due_slot=stamp(slot),mapping_hash=mapping_hash,
            account_fingerprint=fingerprint,account_options=options,targets=mappings,food_group_mapping={m['food_id']:m['group_name'] for m in mappings},
            policy_status='ALLOWED',effective_window=window or {},contract=contract,created_at=stamp(now),execution_enabled=False,
            mapping_semantics='search_provenance_only; not verified food mentions')

    def preview(self, target_ids=None, options=None):
        now=instant(self.tracking.clock())
        with self.storage.reader() as db:return self._preview(db,target_ids,options,now)

    def _preview(self, db, target_ids, options, now):
        options=self._options(options,now)
        if target_ids is not None and (not isinstance(target_ids,list) or len(target_ids)>200 or any(not isinstance(v,str) for v in target_ids)):
            raise FoodError('대상 ID 목록은 200개 이하여야 합니다.')
        ids=sorted(set(target_ids)) if target_ids is not None else [r[0] for r in db.execute('SELECT target_id FROM tracking_targets ORDER BY target_id')]
        drafts=[];rejections=[];naver={}
        for target_id in ids:
            t=self.tracking._target(db,target_id);source=t['source_id']
            permitted_operation(source,options.get('operation','tracking'))
            if not t['planning_eligible'] or not t['next_due_at']:
                rejections.append(dict(target_id=target_id,policy_status='BLOCKED',budget_status='NOT_EVALUATED',
                                       reasons=t['blockers'] or [t['status']]))
                continue
            account=self.tracking.account(source,t['account_scope'])
            aliases=[dict(r) for r in db.execute("SELECT * FROM food_aliases WHERE food_id=? AND scope=? AND status='ACTIVE' ORDER BY normalized_form,alias_id",(t['food_id'],t['alias_scope']))]
            if not aliases:
                rejections.append(dict(target_id=target_id,policy_status='BLOCKED',reasons=['NO_APPROVED_ALIAS']));continue
            if source=='naver_datalab':
                words=sorted({a['surface_form'] for a in aliases})
                if len(words)>5 or any(len(w)>100 for w in words):
                    rejections.append(dict(target_id=target_id,policy_status='BLOCKED',reasons=['NAVER_5_KEYWORD_LIMIT']));continue
                naver.setdefault(t['account_scope'],[]).append((t,aliases,words,account))
                continue
            for alias in aliases:
                name=alias['surface_form']
                if source=='instagram_graph':
                    tag=(name[1:] if name.startswith('#') else name).casefold()
                    if not re.fullmatch(r'\w{1,100}',tag,re.UNICODE) or tag.isdecimal():
                        rejections.append(dict(target_id=target_id,alias_id=alias['alias_id'],policy_status='BLOCKED',reasons=['HASHTAG_REQUIRES_EXACT_APPROVAL_WITHOUT_SPACES']));continue
                    canonical=dict(endpoint='ig_hashtag_search_then_recent_media',parameters=dict(q=tag,api_version=account.options.get('api_version','v26.0'),
                        limit=50,fields=['id','caption','timestamp','media_type','permalink']),max_pages=account.options.get('max_pages',20),
                        pagination={'next_page_token':'B3_RUNTIME_ONLY','on_page_cap':'PARTIAL_LIMITED','on_error':'PARTIAL_OR_FAILED'})
                    window={'basis':'provider_recent_media','exact_content_coverage_known':False}
                else:
                    if '|' in name or name.startswith('-') or len(name)>100:
                        rejections.append(dict(target_id=target_id,alias_id=alias['alias_id'],policy_status='BLOCKED',reasons=['UNAPPROVED_SEARCH_OPERATOR_OR_LENGTH']));continue
                    params=dict(part='snippet',type='video',q=name,regionCode=options.get('regionCode',account.options.get('regionCode','KR')),
                        relevanceLanguage=options.get('relevanceLanguage',account.options.get('relevanceLanguage','ko')),order=options.get('order',account.options.get('order','date')),
                        publishedAfter=options['youtube_start'],publishedBefore=options['youtube_end'],maxResults=50)
                    canonical=self._youtube_spec(params,account)
                    window={'start':params['publishedAfter'],'end':params['publishedBefore'],'basis':'publication_time'}
                drafts.append(self._draft(source,t['account_scope'],'TRACKING',canonical,[self._mapping(t,[alias])],now,account,window))
        for scope,groups in naver.items():
            owners={}
            for t,aliases,words,a in groups:
                for word in words:owners.setdefault(word,set()).add(t['food_id'])
            overlaps={word for word,foods in owners.items() if len(foods)>1}
            clean=[]
            for t,aliases,words,account in groups:
                if overlaps.intersection(words):
                    rejections.append(dict(target_id=t['target_id'],policy_status='BLOCKED',reasons=['OVERLAPPING_NAVER_GROUPS']))
                else:clean.append((t,aliases,words,account))
            clean.sort(key=lambda item:item[0]['food_id'])
            for offset in range(0,len(clean),5):
                batch=clean[offset:offset+5];account=batch[0][3]
                params=dict(startDate=options['naver_start'],endDate=options['naver_end'],timeUnit=options['timeUnit'],
                    keywordGroups=[dict(groupName=t['food_id'],keywords=words) for t,aliases,words,a in batch],**options['filters'])
                mappings=[self._mapping(t,aliases,t['food_id']) for t,aliases,words,a in batch]
                drafts.append(self._draft('naver_datalab',scope,'TRACKING',{'endpoint':'search-trend/v1/search','parameters':params},mappings,now,account,
                    {'start':params['startDate'],'end':params['endDate'],'timeUnit':params['timeUnit'],'timezone':'Asia/Seoul','context_hash':digest(params)}))
        if options.get('include_discovery'):
            drafts.append(self._draft('google_trends_rss','feed:KR','DISCOVERY',
                {'endpoint':'trending/rss','parameters':{'geo':'KR'},'interval_seconds':self.rss_interval},[],now,
                window={'basis':'fixed_discovery_feed','food_count_zero_is_success':True,'candidate_resolution':'local A4 bridge after collection; no automatic tracking'}))
            for account in self.tracking.accounts.values():
                if account.source_id!='youtube':continue
                if account.blockers():
                    rejections.append(dict(source_id='youtube',account_scope=account.account_scope,policy_status='BLOCKED',reasons=account.blockers()));continue
                queries=account.options.get('discovery_queries',['신상 디저트','유행 음식','요즘 먹방','편의점 신상','신상 간식','카페 신메뉴'])
                for name in sorted(set(queries)):
                    params=dict(part='snippet',type='video',q=name,regionCode='KR',relevanceLanguage='ko',order='date',
                        publishedAfter=options['youtube_start'],publishedBefore=options['youtube_end'],maxResults=50)
                    drafts.append(self._draft('youtube',account.account_scope,'DISCOVERY',self._youtube_spec(params,account),[],now,account,
                        {'start':params['publishedAfter'],'end':params['publishedBefore'],'basis':'publication_time'}))
        # Sharing keys exclude food IDs; retain every target/alias provenance mapping separately.
        grouped={}
        for draft in drafts:
            key=(draft['query_id'],draft['due_slot'])
            if key not in grouped:grouped[key]=draft;continue
            current=grouped[key];mappings={m['target_id']:m for m in current['targets']}
            current['query_kinds']=sorted(set(current['query_kinds']+draft['query_kinds']))
            if len(current['query_kinds'])>1:current['query_kind']='SHARED_DISCOVERY_TRACKING'
            for m in draft['targets']:
                if m['target_id'] in mappings:
                    mappings[m['target_id']]['alias_ids']=sorted(set(mappings[m['target_id']]['alias_ids']+m['alias_ids']))
                else:mappings[m['target_id']]=m
            current['targets']=sorted(mappings.values(),key=lambda m:m['target_id'])
            current['mapping_hash']=digest(current['targets'])
            current['plan_id']='PLAN_'+digest([current['query_id'],current['due_slot'],current['mapping_hash'],current['account_fingerprint']])
            current['food_group_mapping'].update(draft['food_group_mapping'])
        plans=[];simulated=[]
        for plan in sorted(grouped.values(),key=lambda p:(p['source_id'],p['account_scope'],p['query_id'],p['due_slot'])):
            budget=self.quota.assess(db,plan,now,simulated=simulated)
            due=not plan['targets'] or any(instant(m['due_at'])<=now for m in plan['targets'])
            plan['budget']=budget;plan['estimated_cost']=[dict(resource=c['resource'],cost=c['cost']) for c in budget['lines']]
            plan['budget_status']=budget['status'];plan['decision']='NOT_DUE' if not due else 'READY_FOR_B3_RECHECK' if budget['status']=='READY_LOCAL' else 'DEFERRED_BUDGET'
            if due and budget['status']=='READY_LOCAL':simulated.extend(budget['lines'])
            plans.append(plan)
        return {'planner_version':PLANNER_VERSION,'plans':plans,'rejections':rejections,'execution_enabled':False,
                'preview_only':True,'reservations_created':0,'http_requests':0,'evaluated_at':stamp(now)}

    def _youtube_spec(self, params, account):
        return dict(endpoint='search',parameters=params,max_pages=account.options.get('max_pages',1),
            pagination={'next_page_token':'B3_RUNTIME_ONLY','on_page_cap':'PARTIAL_LIMITED'},
            dependent_requests=[{'query_kind':'VIDEO_DETAILS','endpoint':'videos','parameters':{'part':'snippet,statistics,contentDetails,status'},
                                 'id_source':'public_video_ids_from_search','batch_size':50,'requires_runtime_materialization':True}])

    def commit(self, request_key, target_ids=None, options=None):
        key=text(request_key,128);fingerprint=digest({'target_ids':sorted(set(target_ids)) if target_ids is not None else None,'options':options or {}})
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM planner_commands WHERE request_key=?',(key,)).fetchone()
            if old:
                if old['payload_hash']!=fingerprint:raise FoodError('같은 계획 키의 요청이 다릅니다.',409)
                return json.loads(old['result_json'])
            preview=self._preview(db,target_ids,options,instant(self.tracking.clock()))
            for p in preview['plans']:
                db.execute('INSERT OR IGNORE INTO planner_queries VALUES (?,?,?,?,?,?,?,?)',
                    (p['query_id'],p['source_id'],p['account_scope'],p['query_kind'],dump(p['canonical_query']),p['spec_hash'],PLANNER_VERSION,p['created_at']))
                db.execute('INSERT OR IGNORE INTO planner_plans VALUES (?,?,?,?,?,?,?)',
                    (p['plan_id'],p['query_id'],p['due_slot'],p['mapping_hash'],p['account_fingerprint'],dump(p),p['created_at']))
                for m in p['targets']:
                    db.execute('INSERT OR IGNORE INTO planner_query_targets VALUES (?,?,?,?,?)',(p['query_id'],m['target_id'],m['generation'],m['food_revision'],dump(m)))
                    db.execute('INSERT OR IGNORE INTO planner_plan_targets VALUES (?,?,?,?,?,?)',(p['plan_id'],m['target_id'],m['generation'],m['food_revision'],m['due_at'],dump(m)))
            result=dict(preview,preview_only=False,stored_plan_ids=[p['plan_id'] for p in preview['plans']])
            db.execute('INSERT INTO planner_commands VALUES (?,?,?,?)',(key,fingerprint,dump(result),stamp(self.tracking.clock())))
            return result

    def _validate(self, db, plan, now):
        source=plan['source_id'];eligible=[];reasons=[]
        if (plan['planner_version']!=PLANNER_VERSION or plan['contract']['policy_version']!=policy_for(source)['version'] or
                plan['adapter_version']!=SOURCES[source]['adapter_version'] or plan['contract_version']!=CONTRACT_VERSION or plan['cost_version']!=COST_VERSION):
            reasons.append('POLICY_OR_PLANNER_VERSION_CHANGED')
        if source=='google_trends_rss':
            access={'source_id':source,'account_scope':'feed:KR','rss_interval':self.rss_interval,'execution_enabled':False}
            if digest(access)!=plan['account_fingerprint']:reasons.append('CONFIG_CHANGED')
            slot=datetime.fromtimestamp(int(instant(now).timestamp())//self.rss_interval*self.rss_interval,timezone.utc)
            if stamp(slot)!=plan['due_slot']:reasons.append('DISCOVERY_SLOT_EXPIRED')
        else:
            try:
                account=self.tracking.account(source,plan['account_scope'])
                reasons+=account.blockers()
                if digest(account.public())!=plan['account_fingerprint']:reasons.append('ACCOUNT_CONFIG_CHANGED')
            except FoodError:reasons.append('ACCOUNT_UNAVAILABLE')
        for m in plan['targets']:
            if not self.tracking.valid_mapping(db,m['target_id'],m['generation'],m['food_revision']) or instant(m['due_at'])>instant(now):continue
            current=self.tracking._target(db,m['target_id'])
            slot_exists=db.execute('SELECT 1 FROM tracking_slots WHERE target_id=? AND generation=? AND due_slot=?',(m['target_id'],m['generation'],m['due_at'])).fetchone()
            if current['next_due_at']!=m['due_at'] and not slot_exists:continue
            eligible.append(m)
        if plan['targets'] and not eligible:reasons.append('NO_CURRENT_TARGET_MAPPING')
        return {'valid':not reasons,'eligible_targets':eligible if not reasons else [],'reasons':reasons,'execution_enabled':False,
                'lease_fencing_required_from_B3':True}

    def plan(self, plan_id):
        with self.storage.reader() as db:
            stored=self.tracking.foods._row(db,'planner_plans','plan_id',plan_id)
            p=json.loads(stored['payload_json']);now=instant(self.tracking.clock())
            return {'plan':p,'current_validation':self._validate(db,p,now),'current_budget':self.quota.assess(db,p,now)}

    def history(self, target_id=None):
        with self.storage.reader() as db:
            if target_id:
                rows=db.execute('SELECT p.payload_json FROM planner_plans p JOIN planner_plan_targets m USING(plan_id) WHERE m.target_id=? ORDER BY p.created_at,p.plan_id',(target_id,))
            else:rows=db.execute('SELECT payload_json FROM planner_plans ORDER BY created_at,plan_id')
            return [json.loads(r[0]) for r in rows]

    def valid_result_targets(self, reservation_id):
        """Late results may attach only to mappings eligible at the attempt and still current now."""
        with self.storage.reader() as db:
            r=self.tracking.foods._row(db,'quota_reservations','reservation_id',reservation_id)
            if r['state']!='ATTEMPTED':return []
            p=json.loads(self.tracking.foods._row(db,'planner_plans','plan_id',r['plan_id'])['payload_json'])
            valid=self._validate(db,p,instant(self.tracking.clock()));frozen={m['target_id'] for m in json.loads(r['target_snapshot_json'])}
            return [m for m in valid['eligible_targets'] if m['target_id'] in frozen]
