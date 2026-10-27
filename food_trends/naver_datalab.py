"""NAVER API HUB transport. No response caching, logging or persistence."""
import json
import math
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

ENDPOINT = 'https://naverapihub.apigw.ntruss.com/search-trend/v1/search'
KST = timezone(timedelta(hours=9))
MESSAGES = {
    'not_configured': 'API 키 미설정 · 서버 .env에 NAVER 인증정보를 입력하세요.',
    'disabled': 'NAVER 자동 수집 비활성화',
    'authentication': 'NAVER 인증 실패 · Client ID, Secret 및 API 권한을 확인하세요.',
    'rate_limit': 'NAVER 호출량 제한 · 잠시 후 다시 조회하세요.',
    'budget': '자체 일일 호출 한도에 도달했습니다.',
    'network': 'NAVER 네트워크 오류 · 연결 상태를 확인하세요.',
    'upstream': 'NAVER 서버 오류 · 잠시 후 다시 조회하세요.',
    'invalid_response': 'NAVER 응답 형식을 확인할 수 없습니다.',
    'validation': '키워드 그룹 또는 조회 기간을 확인하세요.',
    'duplicate': '같은 요청이 실행 중이거나 방금 완료되었습니다. 잠시 후 조회하세요.',
}


class NaverError(Exception):
    def __init__(self, kind, status=None, retry_after=0):
        self.kind, self.status, self.retry_after = kind, status, retry_after
        super().__init__(MESSAGES[kind])


def today():
    return datetime.now(KST).date()


def request_body(groups, days=30, start=None, end=None, time_unit='date', filters=None):
    try:
        if type(days) is not int or not 1 <= days <= 3660:
            raise ValueError()
        end_date = date.fromisoformat(end) if end else today() - timedelta(days=1)
        start_date = date.fromisoformat(start) if start else end_date - timedelta(days=days - 1)
        if start_date < date(2016, 1, 1) or not start_date <= end_date < today():
            raise ValueError()
        if time_unit not in ('date', 'week', 'month') or not 1 <= len(groups) <= 5:
            raise ValueError()
        titles = set()
        for group in groups:
            title, words = group['groupName'], group['keywords']
            if not isinstance(title, str) or not title.strip() or len(title) > 100 or title in titles:
                raise ValueError()
            titles.add(title)
            if not isinstance(words, list) or not 1 <= len(words) <= 5:
                raise ValueError()
            if any(not isinstance(w, str) or not w.strip() or len(w) > 100 for w in words):
                raise ValueError()
        filters = filters or {}
        if set(filters) - {'device', 'gender', 'ages'}:
            raise ValueError()
        if 'device' in filters and filters['device'] not in ('pc', 'mo'):
            raise ValueError()
        if 'gender' in filters and filters['gender'] not in ('m', 'f'):
            raise ValueError()
        if 'ages' in filters and (not isinstance(filters['ages'], list) or not filters['ages'] or
                                 any(a not in tuple(map(str, range(1, 12))) for a in filters['ages'])):
            raise ValueError()
        return dict(startDate=start_date.isoformat(), endDate=end_date.isoformat(),
                    timeUnit=time_unit, keywordGroups=groups, **filters)
    except (ValueError, TypeError, KeyError):
        raise NaverError('validation') from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward credentials to a redirected host.


def transport(request, timeout):
    with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
        return json.loads(response.read(2_000_000).decode('utf-8'))


def parse_response(payload, body):
    try:
        if any(payload[k] != body[k] for k in ('startDate', 'endDate', 'timeUnit')):
            raise ValueError()
        results = payload['results']
        if not isinstance(results, list) or len(results) != len(body['keywordGroups']):
            raise ValueError()
        indexed = {r['title']: r for r in results}
        parsed = []
        for group in body['keywordGroups']:
            result = indexed[group['groupName']]
            if result['keywords'] != group['keywords'] or not isinstance(result['data'], list):
                raise ValueError()
            points, seen = [], set()
            for point in result['data']:
                period, ratio = point['period'], point['ratio']
                date.fromisoformat(period)
                if not body['startDate'] <= period <= body['endDate'] or period in seen:
                    raise ValueError()
                if type(ratio) not in (int, float) or not math.isfinite(ratio) or not 0 <= ratio <= 100:
                    raise ValueError()
                seen.add(period)
                points.append({'period': period, 'ratio': ratio})
            parsed.append({'title': result['title'], 'keywords': result['keywords'],
                           'data': sorted(points, key=lambda p: p['period'])})
        return parsed
    except (KeyError, ValueError, TypeError, AttributeError):
        raise NaverError('invalid_response') from None


class NaverClient:
    def __init__(self, config, sender=transport, sleeper=time.sleep):
        self.config, self.sender, self.sleeper = config, sender, sleeper
        self.origin = 'live_api' if sender is transport else 'mock'

    def search(self, body, reserve=lambda: None, finish=lambda *args: None):
        if not self.config.configured:
            raise NaverError('not_configured')
        for attempt in range(self.config.retries + 1):
            ticket = reserve()
            error = None
            try:
                request = Request(ENDPOINT, data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                                  headers={'X-NCP-APIGW-API-KEY-ID': self.config.client_id,
                                           'X-NCP-APIGW-API-KEY': self.config.client_secret,
                                           'Content-Type': 'application/json'}, method='POST')
                result = parse_response(self.sender(request, self.config.timeout), body)
                finish(ticket, 'success', 200)
                return result
            except HTTPError as exc:
                kind = ('authentication' if exc.code in (401, 403) else 'rate_limit' if exc.code == 429
                        else 'upstream' if exc.code >= 500 else 'validation')
                delay = 0
                try:
                    raw = exc.headers.get('Retry-After', '0')
                    try:
                        delay = max(0, float(raw))
                    except ValueError:
                        delay = max(0, (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
                    if not math.isfinite(delay):
                        delay = 60
                except (ValueError, TypeError, AttributeError, OverflowError):
                    pass
                error = NaverError(kind, exc.code, delay)
                exc.close()
            except (URLError, OSError, HTTPException):
                error = NaverError('network')
            except NaverError as exc:
                error = exc
            except (ValueError, UnicodeError):
                error = NaverError('invalid_response')
            finish(ticket, error.kind, error.status)
            if error.kind not in ('network', 'upstream', 'rate_limit') or attempt == self.config.retries:
                raise error
            if error.retry_after > 8:
                raise error  # Caller records a cooldown; do not retry earlier than Retry-After.
            self.sleeper(max(2 ** attempt, error.retry_after))
