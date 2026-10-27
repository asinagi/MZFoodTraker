"""Bounded live verification. Never persists response series or credentials."""
import json
from .naver_datalab import NaverError


def verify(service, days=30):
    before = service.status()['total_requests']
    try:
        result = service.query_keywords(['두쫀쿠', '두바이 쫀득 쿠키'], days=days)
        return {'success': True, 'days': days, 'origin': result['origin'],
                'groups': [{'title': r['title'], 'keywords': r['keywords'], 'point_count': len(r['data']),
                            'latest': r['data'][-1] if r['data'] else None} for r in result['results']],
                'new_requests': service.status()['total_requests'] - before}
    except NaverError as exc:
        return {'success': False, 'days': days, 'error_type': exc.kind, 'http_status': exc.status,
                'new_requests': service.status()['total_requests'] - before}


def main():
    import argparse
    from .config import load_env, NaverConfig
    from .storage import Storage
    from .naver_service import NaverService
    import os
    parser = argparse.ArgumentParser()
    parser.add_argument('--days', type=int, choices=(7, 30, 90), default=30)
    args = parser.parse_args()
    load_env()
    service = NaverService(Storage(os.getenv('DATABASE_PATH', 'data/trends.sqlite3')), NaverConfig.from_env())
    print(json.dumps(verify(service, args.days), ensure_ascii=False))


if __name__ == '__main__':
    main()
