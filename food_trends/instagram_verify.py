"""Explicit, minimal live verification. Reports contain no raw API payloads."""
from .storage import utcnow
from .instagram_graph import GraphError


def verify(instagram):
    config = instagram.config
    report = {'started_at': utcnow(), 'status': 'running', 'target': '두쫀쿠', 'checks': {},
              'api_requests': 0, 'actual_hashtags': 0, 'actual_media': 0}
    checks = report['checks']
    missing = [key for key, value in [('INSTAGRAM_ACCESS_TOKEN', config.token),
                                      ('META_APP_ID', config.app_id), ('META_APP_SECRET', config.app_secret)] if not value]
    checks['environment'] = {'status': 'passed' if not missing else 'missing', 'missing': missing}
    before = instagram.client.request_count
    try:
        if not config.configured:
            report['status'] = 'blocked'
            report['reason'] = '인증 정보가 디스크에 저장되지 않았습니다. 값을 출력하지 않고 실호출을 건너뜁니다.'
            return report
        if not config.enabled:
            raise GraphError('disabled')
        if config.app_id and config.app_secret:
            token = instagram.check_token()
            checks['token'] = token
            if not token['success']:
                raise GraphError('authentication' if token['status'] == 'invalid' else 'permission')
        else:
            checks['token'] = {'status': 'skipped', 'reason': 'App 인증정보 누락으로 토큰 메타데이터 미검증'}
        connection = instagram.check_connection()
        checks['account_connection'] = {'status': 'passed' if connection['success'] else 'failed',
                                        'page_found': connection['page_found'],
                                        'instagram_account_linked': connection['instagram_account_linked']}
        if not connection['success']:
            raise GraphError('permission')
        candidates = instagram.storage.trends(query=report['target'])
        candidate = next((c for c in candidates if c['normalized_name'] == report['target']), None)
        result = instagram.start_tracking(report['target'], candidate['id'] if candidate else None)
        if result.get('already_tracking'):
            result = instagram.collect(result['hashtag_id'])['results'][0]
        checks['collection'] = {'status': 'passed' if result['success'] else 'failed', 'run': result.get('run')}
        if not result['success']:
            report['error_type'] = result.get('error_type')
            report['status'] = 'failed'
            return report
        tag = next(t for t in instagram.store.tags() if t['name'] == report['target'])
        summary = instagram.store.summary(tag, days=30)
        report.update(actual_hashtags=1, actual_media=summary['total_observed'], new_24h=summary['new_24h'],
                      first_observation_at=summary['first_observation_at'])
        with instagram.storage.connect() as db:
            unique = db.execute('SELECT count(DISTINCT media_id) FROM instagram_hashtag_media WHERE hashtag_id=?', (tag['id'],)).fetchone()[0]
            daily = db.execute("SELECT coalesce(sum(new_count),0) FROM instagram_metrics WHERE hashtag_id=? AND grain='day'", (tag['id'],)).fetchone()[0]
            hourly = db.execute("SELECT coalesce(sum(new_count),0) FROM instagram_metrics WHERE hashtag_id=? AND grain='hour'", (tag['id'],)).fetchone()[0]
        checks['database'] = {'status': 'passed' if unique == daily == hourly == summary['total_observed'] else 'failed',
                              'unique_media': unique, 'daily_sum': daily, 'hourly_sum': hourly}
        report['status'] = 'passed' if checks['database']['status'] == 'passed' and summary['total_observed'] else 'no_media' if not summary['total_observed'] else 'failed'
        checks['frontend'] = {'status': 'pending', 'reason': '실제 Chrome 화면과 수동 수집은 별도 검증이 필요합니다.'}
        return report
    except GraphError as exc:
        report.update(status='failed', error_type=exc.kind, error_code=exc.code, message=str(exc))
        return report
    except Exception:
        report.update(status='failed', error_type='internal', message='검증을 완료하지 못했습니다. 인증정보나 원본 오류는 출력하지 않습니다.')
        return report
    finally:
        report['finished_at'] = utcnow()
        report['api_requests'] = instagram.client.request_count-before
