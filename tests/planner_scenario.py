"""Executable B1/B2 acceptance example: temporary database, Mock rights, external sockets blocked."""
import json
from pathlib import Path
import socket
import sys
import tempfile
from datetime import datetime,timezone
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from food_trends.food_service import FoodService
from food_trends.storage import Storage
from food_trends.tracking_service import TrackingService
from food_trends.tracking_access import mock_accounts
from food_trends.query_planner import QueryPlanner


def main():
    with tempfile.TemporaryDirectory() as directory,patch.object(socket.socket,'connect',side_effect=AssertionError('No external HTTP')):
        storage=Storage.create(Path(directory)/'acceptance.sqlite3');foods=FoodService(storage)
        c=foods.command('submit',dict(request_key='user-food',name='두쫀쿠',aliases=['두바이 쫀득 쿠키','두바이쫀득쿠키'],entity_kind='PRODUCT'))
        descriptor=foods.command('review',dict(request_key='human-review',candidate_id=c['candidate_id'],expected_revision=c['revision'],action='create',
            canonical_name='두바이 쫀득 쿠키',approved=True,actor_ref='mock-reviewer',reason='세 명칭의 동일 음식임을 확인'))['resolution']
        assert all(foods.resolve_name(n)['resolution']['food_id']==descriptor['food_id'] for n in ('두쫀쿠','두바이 쫀득 쿠키','두바이쫀득쿠키'))
        tracking=TrackingService(storage,mock_accounts(),lambda:datetime(2026,10,27,tzinfo=timezone.utc))
        targets=[]
        for source in ('instagram_graph','youtube','naver_datalab'):
            t=tracking.command('register',dict(request_key=source+'register',food_id=descriptor['food_id'],source_id=source,actor_ref='mock-reviewer',reason='계획 대상 등록'))
            t=tracking.command('activate',dict(request_key=source+'activate',target_id=t['target_id'],expected_revision=t['revision'],approved=True,actor_ref='mock-reviewer',reason='계획 활성화 승인'))
            targets.append(t)
        planner=QueryPlanner(storage,tracking)
        with storage.reader() as db:before=list(db.iterdump())
        preview=planner.preview()
        with storage.reader() as db:assert list(db.iterdump())==before
        assert len(preview['plans'])==6 and preview['reservations_created']==0
        saved=planner.commit('record-specifications')
        examples={source:next(p for p in saved['plans'] if p['source_id']==source) for source in ('instagram_graph','youtube','naver_datalab')}
        rss=next(p for p in planner.preview(options={'include_discovery':True})['plans'] if p['source_id']=='google_trends_rss')
        original=examples['youtube'];target=next(t for t in targets if t['source_id']=='youtube')
        reservation=planner.quota.reserve(original['plan_id'],'mock-attempt-contract')
        planner.quota.mark_attempted(reservation['reservation_id'])
        paused=tracking.command('pause',dict(request_key='pause-youtube',target_id=target['target_id'],expected_revision=target['revision'],actor_ref='mock-reviewer',reason='사용자 일시 중단'))
        assert not planner.plan(original['plan_id'])['current_validation']['valid']
        assert planner.valid_result_targets(reservation['reservation_id'])==[]
        assert planner.plan(original['plan_id'])['plan']==original
        assert storage.status()['total_keywords']==0
        result={'successful':True,'food_descriptor':descriptor,'tracking_targets':targets,'query_examples':examples,'rss_discovery_example':rss,
            'paused_target':paused,'preserved_plans':len(planner.history()),'preview_writes':0,'preview_reservations':0,
            'internal_reservation_contract_tested':True,'late_result_food_mappings':0,'http_requests':0,'production_database_used':False}
    output=ROOT/'reports/development-b1-b2-2026-10-27/acceptance-example.json'
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('successful','preserved_plans','preview_writes','preview_reservations','late_result_food_mappings','http_requests','production_database_used')}))


if __name__=='__main__':main()
