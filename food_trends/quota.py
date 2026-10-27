"""Local budget accounting. Preview reads only; explicit B3-facing reservations never send HTTP."""
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .food_service import FoodError, identity, dump, text
from .tracking_service import instant, stamp

KST = timezone(timedelta(hours=9))
COST_VERSION = 'youtube-doc-2026-10-15-v1'


def pacific_offset(now):
    """Same post-2007 US rule as the existing adapter, when Windows has no tzdata."""
    now = instant(now)
    march, november = datetime(now.year,3,8,10,tzinfo=timezone.utc), datetime(now.year,11,1,9,tzinfo=timezone.utc)
    start = march + timedelta(days=(6-march.weekday())%7)
    end = november + timedelta(days=(6-november.weekday())%7)
    return -7 if start <= now < end else -8


def daily_window(now, zone):
    now = instant(now)
    if zone == 'Asia/Seoul':
        day = now.astimezone(KST).date()
        start = datetime.combine(day,datetime.min.time(),KST)
        end = start + timedelta(days=1)
    elif zone == 'America/Los_Angeles':
        try:
            tz = ZoneInfo(zone); day = now.astimezone(tz).date()
            start = datetime.combine(day,datetime.min.time(),tz)
            end = datetime.combine(day+timedelta(days=1),datetime.min.time(),tz)
        except ZoneInfoNotFoundError:
            day = (now+timedelta(hours=pacific_offset(now))).date()
            # Local midnight occurs before the 02:00 DST transitions. Probe its UTC instant.
            def midnight(d):
                probe = datetime.combine(d,datetime.min.time(),timezone.utc)+timedelta(hours=8)
                return datetime.combine(d,datetime.min.time(),timezone.utc)-timedelta(hours=pacific_offset(probe))
            start, end = midnight(day), midnight(day+timedelta(days=1))
    else:
        day=now.date();start=datetime.combine(day,datetime.min.time(),timezone.utc);end=start+timedelta(days=1)
    return {'start':stamp(start),'end':stamp(end),'timezone':zone,'day':day.isoformat()}


class QuotaManager:
    def __init__(self, storage, tracking, validator=None):
        self.storage,self.tracking,self.validator = storage,tracking,validator

    def requirements(self, plan, now):
        source=plan['source_id'];options=plan['account_options'];pages=plan['canonical_query'].get('max_pages',1)
        def line(resource,cost,limit,provider,zone,distinct=None):
            window = ({'start':stamp(now),'end':stamp(instant(now)+timedelta(days=7)),'timezone':'rolling-7d'}
                      if distinct is not None else daily_window(now,zone))
            return dict(resource=resource,cost=cost,local_limit=limit,provider_limit=provider,
                        distinct_key=distinct,window_start=window['start'],window_end=window['end'],reset_timezone=window['timezone'])
        if source=='youtube':
            return [line('youtube.search_calls',pages,options.get('daily_search_limit',40),100,'America/Los_Angeles'),
                    line('youtube.video_calls',pages,options.get('daily_video_limit',1000),10000,'America/Los_Angeles')]
        if source=='naver_datalab':
            return [line('naver.calls',1,options.get('daily_limit',100),None,'Asia/Seoul')]
        if source=='instagram_graph':
            tag=plan['canonical_query']['parameters']['q']
            return [line('instagram.unique_tags',1,options.get('soft_limit',28),30,'UTC',tag),
                    line('instagram.lookup_calls',1,None,None,'UTC'),line('instagram.media_calls',pages,None,None,'UTC')]
        return [line('rss.feed_calls',1,None,None,'UTC')]

    def _usage(self, db, plan, requirement, now, exclude=None, simulated=()):
        """Existing local adapters and new reservations share a conservative budget view."""
        resource=requirement['resource'];source,scope=plan['source_id'],plan['account_scope']
        start,end=instant(requirement['window_start']),instant(requirement['window_end'])
        observed,reserved,known=0,0,{}
        observed_tags=set();reserved_tags=set()
        if resource.startswith('youtube.') and scope=='local-default':
            endpoint='search' if resource.endswith('search_calls') else 'videos'
            observed=db.execute('SELECT count(*) FROM youtube_calls WHERE endpoint=? AND day=?',(endpoint,daily_window(now,'America/Los_Angeles')['day'])).fetchone()[0]
        elif resource=='naver.calls' and scope=='local-default':
            observed=db.execute('SELECT count(*) FROM naver_calls WHERE day=?',(daily_window(now,'Asia/Seoul')['day'],)).fetchone()[0]
        elif resource=='instagram.unique_tags' and scope.startswith('ig:'):
            cutoff=stamp(instant(now)-timedelta(days=7))
            for row in db.execute('SELECT name,searched_at FROM instagram_searches WHERE user_id=? AND searched_at>?',(scope[3:],cutoff)):
                tag=row['name'].lstrip('#').casefold();known[tag]=stamp(instant(row['searched_at'])+timedelta(days=7))
            observed=len(known)
            observed_tags=set(known)
        charges=[dict(r) for r in db.execute('''SELECT c.*,r.state,r.expires_at FROM quota_charges c JOIN quota_reservations r USING(reservation_id)
            JOIN planner_queries q ON q.query_id=r.query_id WHERE q.source_id=? AND q.account_scope=? AND c.resource=?
            AND r.state IN ('RESERVED','ATTEMPTED')''',(source,scope,resource))]
        charges += [dict(c, state='RESERVED',expires_at=c['window_end']) for c in simulated
                    if c.get('source_id')==source and c.get('account_scope')==scope and c['resource']==resource]
        for c in charges:
            if (exclude is not None and c.get('reservation_id')==exclude) or c['state']=='RESERVED' and instant(c['expires_at'])<=instant(now):
                continue
            if resource=='instagram.unique_tags':
                if c['cost']>0 and instant(c['window_end'])>instant(now):
                    tag=c['distinct_key'];known.setdefault(tag,c['window_end'])
                    (observed_tags if c['state']=='ATTEMPTED' else reserved_tags).add(tag)
            elif instant(c['window_start'])==start and instant(c['window_end'])==end:
                if c['state']=='RESERVED':reserved+=c['cost']
                else:observed+=c['cost']
        if resource=='instagram.unique_tags':
            observed=len(observed_tags);reserved=len(reserved_tags-observed_tags)
        return observed,reserved,known

    def assess(self, db, plan, now, exclude=None, simulated=()):
        lines=[];ok=True
        for req in self.requirements(plan,now):
            observed,reserved,known=self._usage(db,plan,req,now,exclude,simulated)
            cost=req['cost'];limit=req['local_limit']
            if req['distinct_key'] in known:
                cost=0
                req['window_end']=known[req['distinct_key']]
                req['window_start']=stamp(instant(req['window_end'])-timedelta(days=7))
            remaining=None if limit is None else max(0,limit-observed-reserved)
            fits=remaining is None or cost<=remaining
            ok &= fits
            lines.append(dict(req,cost=cost,estimated_cost=cost,reserved_cost=reserved,observed_usage=observed,
                remaining_budget=remaining,budget_status='READY_LOCAL' if fits else 'DEFERRED',provider_observed_usage=None,
                provider_remaining_budget=None,provider_usage_verified=False,cost_version=COST_VERSION,
                quota_type=req['resource'],provider_reset_timezone=req['reset_timezone'],
                effective_window={'start':req['window_start'],'end':req['window_end']},
                observed_usage_basis='legacy_local_calls_plus_recorded_workflow_upper_bounds',actual_new_http_attempts=None,
                reservation_status='PREVIEW_ONLY',source_id=plan['source_id'],account_scope=plan['account_scope']))
        return {'status':'READY_LOCAL' if ok else 'DEFERRED','lines':lines,'remote_debit_confirmed':False}

    def _event(self, db, reservation, action, now, metadata):
        db.execute('INSERT INTO quota_events VALUES (?,?,?,?,?)',(identity('QEVT'),reservation,action,stamp(now),dump(metadata)))

    def reserve(self, plan_id, attempt_key, attempt_no=1, ttl_seconds=120):
        """Explicit local B3 handoff, never called by preview or planning HTTP endpoints."""
        key=text(attempt_key,128)
        if type(attempt_no) is not int or attempt_no<1 or type(ttl_seconds) is not int or not 1<=ttl_seconds<=300:
            raise FoodError('예약 번호·기한을 확인하세요.')
        now=instant(self.tracking.clock())
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM quota_reservations WHERE attempt_key=?',(key,)).fetchone()
            if old:
                if old['plan_id']!=plan_id or old['attempt_no']!=attempt_no:
                    raise FoodError('같은 예약 키의 요청이 다릅니다.',409)
                return dict(old)
            plan=json.loads(self.tracking.foods._row(db,'planner_plans','plan_id',plan_id)['payload_json'])
            validation=self.validator(db,plan,now) if self.validator else {'valid':False}
            if not validation['valid']:
                raise FoodError('현재 대상·세대·정책에 유효한 계획이 아닙니다.',409)
            previous=db.execute('SELECT * FROM quota_reservations WHERE query_id=? AND due_slot=? ORDER BY attempt_no DESC LIMIT 1', (plan['query_id'],plan['due_slot'])).fetchone()
            if attempt_no != (previous['attempt_no']+1 if previous else 1):
                raise FoodError('이 요청 슬롯의 예약 시도 번호가 중복되거나 순서가 다릅니다.',409)
            if previous and previous['state']=='RESERVED' and instant(previous['expires_at'])>now:
                raise FoodError('이미 유효한 예약이 있습니다.',409)
            budget=self.assess(db,plan,now)
            if budget['status']!='READY_LOCAL':raise FoodError('예약 가능한 로컬 예산이 부족합니다.',409)
            expiry=min([now+timedelta(seconds=ttl_seconds)]+[instant(c['window_end']) for c in budget['lines']])
            reservation=identity('RESERVATION')
            db.execute('INSERT INTO quota_reservations VALUES (?,?,?,?,?,?,\'RESERVED\',?,?,NULL,?)',
                (reservation,plan_id,plan['query_id'],plan['due_slot'],key,attempt_no,stamp(now),stamp(expiry),dump(validation['eligible_targets'])))
            self._charges(db,reservation,budget['lines'])
            self._event(db,reservation,'RESERVED',now,{'local_only':True,'remote_debit_confirmed':False})
            return dict(db.execute('SELECT * FROM quota_reservations WHERE reservation_id=?',(reservation,)).fetchone())

    def _charges(self, db, reservation, lines):
        db.execute('DELETE FROM quota_charges WHERE reservation_id=?',(reservation,))
        for c in lines:
            db.execute('INSERT INTO quota_charges VALUES (?,?,?,?,?,?,?,?,?)',(reservation,c['resource'],c['cost'],c['distinct_key'],
                c['window_start'],c['window_end'],c['reset_timezone'],c['local_limit'],c['provider_limit']))

    def mark_attempted(self, reservation_id):
        """Records a local attempt intention; B3 must supply lease/fencing and then send HTTP."""
        now=instant(self.tracking.clock())
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=self.tracking.foods._row(db,'quota_reservations','reservation_id',reservation_id)
            if row['state']=='ATTEMPTED':return row
            if row['state']!='RESERVED' or instant(row['expires_at'])<=now:raise FoodError('예약이 해제되었거나 만료됐습니다.',409)
            plan=json.loads(self.tracking.foods._row(db,'planner_plans','plan_id',row['plan_id'])['payload_json'])
            validation=self.validator(db,plan,now)
            frozen={t['target_id'] for t in json.loads(row['target_snapshot_json'])}
            eligible=[t for t in validation['eligible_targets'] if t['target_id'] in frozen]
            if not validation['valid'] or not eligible and plan['targets']:
                raise FoodError('예약 이후 대상·정책이 변경됐습니다.',409)
            budget=self.assess(db,plan,now,exclude=reservation_id)
            if budget['status']!='READY_LOCAL':raise FoodError('실행 시도 전 예산 재검사 실패',409)
            self._charges(db,reservation_id,budget['lines'])
            db.execute("UPDATE quota_reservations SET state='ATTEMPTED',attempted_at=?,target_snapshot_json=? WHERE reservation_id=?",(stamp(now),dump(eligible),reservation_id))
            self._event(db,reservation_id,'ATTEMPTED',now,{'remote_debit_confirmed':False,'lease_required_from_B3':True})
            return dict(db.execute('SELECT * FROM quota_reservations WHERE reservation_id=?',(reservation_id,)).fetchone())

    def release(self, reservation_id):
        with self.storage.connect() as db:
            db.execute('BEGIN IMMEDIATE');row=self.tracking.foods._row(db,'quota_reservations','reservation_id',reservation_id)
            if row['state']=='ATTEMPTED':raise FoodError('실행 시도는 원격 차감 여부와 관계없이 환불할 수 없습니다.',409)
            if row['state']=='RESERVED':
                db.execute("UPDATE quota_reservations SET state='RELEASED' WHERE reservation_id=?",(reservation_id,))
                self._event(db,reservation_id,'RELEASED',self.tracking.clock(),{'before_attempt_only':True})
            return dict(db.execute('SELECT * FROM quota_reservations WHERE reservation_id=?',(reservation_id,)).fetchone())
