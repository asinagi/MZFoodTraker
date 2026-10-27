"""Explicit token exchange. Never apply a replacement or expose either credential."""
import json
from pathlib import Path
from .instagram_graph import GraphError


def exchange(instagram, destination):
    destination = Path(destination)
    if destination.suffix != '.secret' or destination.exists():
        raise ValueError('새 .secret 파일 경로가 필요합니다')
    config = instagram.config
    if not config.app_id or not config.app_secret:
        raise GraphError('not_configured')
    previous = instagram.check_token()
    if not previous['success'] or previous['token_type'] != 'USER':
        raise GraphError('authentication' if previous['status'] == 'invalid' else 'permission')
    if previous['lifetime'] == 'long_lived' or not previous['expires_at']:
        return {'success': False, 'message': '이미 장기 토큰이거나 만료 정보를 확인할 수 없어 교환을 보류합니다.', 'token': previous}
    # An omitted issued_at does not make a valid, expiring USER token ineligible.
    # This explicit admin command lets Meta decide eligibility; it never auto-applies.
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as output:
        try:
            with instagram.operation():
                payload = instagram.client.get('oauth/access_token', {
                    'grant_type': 'fb_exchange_token', 'client_id': config.app_id,
                    'client_secret': config.app_secret, 'fb_exchange_token': config.token})
            replacement = payload.get('access_token')
            if not isinstance(replacement, str) or not replacement:
                raise GraphError('invalid_response')
            verified = instagram.check_token(token=replacement, persist=False)
            if not verified['success'] or verified['token_type'] != 'USER':
                raise GraphError('authentication')
            output.write(replacement + '\n')
        except Exception:
            output.close()
            destination.unlink(missing_ok=True)
            raise
    duration = payload.get('expires_in')
    duration = duration if type(duration) is int else None
    result = {'success': True, 'message': '교환 토큰을 별도 .secret 파일에 저장하고 검증했습니다. .env 적용에는 사용자 승인이 필요합니다.',
              'applied': False, 'expires_in': duration, 'long_lived': bool(duration and duration > 86400),
              'previous_expires_at': previous['expires_at'], 'replacement': verified}
    destination.with_suffix('.metadata.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result
