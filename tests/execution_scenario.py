"""B3 acceptance: FOOD 0 -> approval -> scheduler -> outbox -> mock clients -> existing stores."""
import json
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from food_trends.storage import Storage
from food_trends.food_service import FoodService
from food_trends.tracking_service import TrackingService
from food_trends.tracking_access import mock_accounts
from food_trends.query_planner import QueryPlanner
from food_trends.execution_mock import mock_controller,MockTransport


def main():
    with tempfile.TemporaryDirectory() as directory, patch.object(socket.socket,'connect',side_effect=AssertionError('External HTTP forbidden')):
        storage=Storage.create(Path(directory)/'acceptance.sqlite3');foods=FoodService(storage)
        candidate=foods.command('submit',dict(request_key='b3-input',name='두쫀쿠',aliases=['두바이 쫀득 쿠키','두바이쫀득쿠키']))
        food=foods.command('review',dict(request_key='b3-approval',candidate_id=candidate['candidate_id'],expected_revision=candidate['revision'],
            action='create',canonical_name='두바이 쫀득 쿠키',approved=True,actor_ref='mock-reviewer',reason='B3 격리 실행 검증'))['resolution']
        tracking=TrackingService(storage,mock_accounts());planner=QueryPlanner(storage,tracking)
        for source in ('instagram_graph','youtube','naver_datalab'):
            target=tracking.command('register',dict(request_key=source+'-register',food_id=food['food_id'],source_id=source,actor_ref='mock-reviewer',reason='격리 계획 등록'))
            tracking.command('activate',dict(request_key=source+'-activate',target_id=target['target_id'],expected_revision=target['revision'],approved=True,actor_ref='mock-reviewer',reason='격리 계획 승인'))
        transport=MockTransport();controller=mock_controller(storage,planner,transport)
        with storage.reader() as db:before=list(db.iterdump())
        planner.preview();controller.status()
        with storage.reader() as db:assert list(db.iterdump())==before
        jobs=controller.execution.schedule()
        assert len(jobs)==6
        calls_before=len(transport.calls)
        assert calls_before==0
        completed=[controller.run_once(job['job_id']) for job in jobs]
        assert all(j['status']=='SUCCEEDED' for j in completed)
        call_count=len(transport.calls)
        for job in jobs:assert controller.run_once(job['job_id']) is None
        assert len(transport.calls)==call_count
        assert storage.status()['total_keywords']==0
        assert controller.execution.schedule()==[]
        with storage.reader() as db:
            counts={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in
                ('execution_jobs','execution_outbox','execution_calls','execution_results','quota_reservations','youtube_videos','instagram_media','food_entities','items')}
            assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        result={'successful':True,'food_id':food['food_id'],'food_descriptor':food,'jobs':len(jobs),'mock_http_attempts':call_count,
            'real_http_attempts':0,'preview_database_changes':0,'duplicate_http_attempts':0,'rss_food_items':0,'database_counts':counts,
            'statuses':[{'source':j['source_id'],'status':j['status'],'run_id':j['run_id'],'contract':j['run']['result_contract']} for j in completed],
            'live_enabled':False,'automatic_enabled':False,'production_database_used':False}
    output=ROOT/'reports/development-b3-2026-10-27/acceptance.json';output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('successful','jobs','mock_http_attempts','real_http_attempts','preview_database_changes','duplicate_http_attempts')}))


if __name__=='__main__':main()
