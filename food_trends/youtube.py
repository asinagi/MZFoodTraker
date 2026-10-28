"""YouTube Data API transport and strict parsing. Never log upstream text or URLs."""
import json
import re
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the API key header to a redirected destination.
        return None


class YouTubeError(Exception):
    def __init__(self, kind, status=None, retry_after=0):
        self.kind, self.status = kind, status
        self.retry_after = retry_after
        super().__init__({
            'not_configured': 'YouTube API 키가 설정되지 않았습니다',
            'budget': '자체 일일 요청 예산에 도달했습니다',
            'quota': 'YouTube 할당량 소진 · PT 자정까지 요청 중단',
            'authentication': 'YouTube 인증 또는 API 사용 권한을 확인하세요',
            'permission': 'YouTube 요청 권한이 없습니다',
            'rate_limit': 'YouTube 요청 속도 제한 · 잠시 후 재시도',
            'upstream': 'YouTube 서버 일시 오류', 'network': 'YouTube 네트워크 오류',
            'invalid_response': 'YouTube 응답 형식이 올바르지 않습니다',
            'busy': 'YouTube 작업 실행 중', 'validation': 'YouTube 요청 형식을 확인하세요',
            'cooldown': 'YouTube 요청 재개 대기 중', 'internal': 'YouTube 작업 처리 실패',
        }.get(kind, 'YouTube 작업 실패'))


def items(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('items'), list):
        raise YouTubeError('invalid_response')
    return payload['items']


def video_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', value):
        raise YouTubeError('invalid_response')
    return value


def parse_video(item):
    try:
        identity = video_id(item['id'])
        snippet, stats = item['snippet'], item.get('statistics', {})
        details, status = item.get('contentDetails', {}), item['status']
        if not all(isinstance(x, dict) for x in (snippet, stats, details, status)):
            raise ValueError()
        title, description = snippet['title'], snippet.get('description', '')
        published, channel = snippet['publishedAt'], snippet['channelId']
        channel_title = snippet['channelTitle']
        if not all(isinstance(x, str) for x in (title, description, published, channel, channel_title)):
            raise ValueError()
        if datetime.fromisoformat(published.replace('Z', '+00:00')).tzinfo is None:
            raise ValueError()
        thumbnails = snippet.get('thumbnails', {})
        thumbnail = next((thumbnails[k]['url'] for k in ('high', 'medium', 'default')
                          if k in thumbnails), None)
        if thumbnail and (urlsplit(thumbnail).scheme != 'https' or
                          urlsplit(thumbnail).hostname not in ('i.ytimg.com', 'img.youtube.com')):
            thumbnail = None
        def count(name):
            value = stats.get(name)
            if value is None:
                return None
            if isinstance(value, bool) or not re.fullmatch(r'\d+', str(value)):
                raise ValueError()
            return str(value)  # Preserve exact API count, including numbers beyond JS safe integers.
        duration = details.get('duration')
        if duration is not None and (not isinstance(duration, str) or not re.fullmatch(r'P[0-9DTHMS.]+', duration)):
            raise ValueError()
        privacy = status['privacyStatus']
        if privacy not in ('public', 'private', 'unlisted'):
            raise ValueError()
        return dict(video_id=identity, title=title, description=description, published_at=published,
                    channel_id=channel, channel_title=channel_title,
                    video_url='https://www.youtube.com/watch?v='+identity, thumbnail_url=thumbnail,
                    view_count=count('viewCount'), like_count=count('likeCount'),
                    comment_count=count('commentCount'), duration=duration, privacy_status=privacy,
                    upload_status=status.get('uploadStatus'), embeddable=status.get('embeddable'),
                    provided_fields=['video_id', 'title', 'published_at', 'channel_id', 'channel_title', 'privacy_status']
                    + (['description'] if 'description' in snippet else [])
                    + (['thumbnail_url'] if 'thumbnails' in snippet else [])
                    + (['duration'] if 'duration' in details else [])
                    + [field for field, upstream in (('view_count', 'viewCount'), ('like_count', 'likeCount'),
                                                     ('comment_count', 'commentCount')) if upstream in stats])
    except (KeyError, TypeError, ValueError, AttributeError, StopIteration):
        raise YouTubeError('invalid_response') from None


class YouTubeClient:
    def __init__(self, config, transport=None, sleep=time.sleep):
        self.config, self.transport, self.sleep = config, transport or build_opener(NoRedirect()).open, sleep
        self.origin = 'mock' if transport is not None else 'live_api'

    def get(self, endpoint, params, reserve, finish, *, single_attempt=False):
        if not self.config.configured:
            raise YouTubeError('not_configured')
        if endpoint not in ('search', 'videos'):
            raise YouTubeError('validation')
        attempts = 1 if single_attempt else self.config.retries + 1
        for attempt in range(attempts):
            ticket = reserve(endpoint)
            state, status, result_count = 'internal', None, None
            retry_after = 0
            try:
                # Key in header, never in URLs, logs, DB request records or error messages.
                request = Request('https://www.googleapis.com/youtube/v3/'+endpoint+'?'+urlencode(params),
                                  headers={'X-Goog-Api-Key': self.config.api_key, 'Accept': 'application/json'})
                with self.transport(request, timeout=self.config.timeout) as response:
                    status = response.status
                    raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise YouTubeError('invalid_response')
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise YouTubeError('invalid_response') from None
                items(payload)
                result_count = len(payload['items'])
                state = 'success'
                return payload
            except HTTPError as exc:
                status = exc.code
                from .retry_after import seconds
                retry_after = seconds(exc.headers)
                try:
                    error = json.loads(exc.read(65536)).get('error', {})
                    reasons = {e.get('reason') for e in error.get('errors', []) if isinstance(e, dict)}
                except (ValueError, TypeError, AttributeError):
                    reasons = set()
                finally:
                    exc.close()
                state = ('quota' if reasons & {'quotaExceeded', 'dailyLimitExceeded', 'dailyLimitExceededUnreg'} else
                         'rate_limit' if status == 429 or reasons & {'rateLimitExceeded', 'userRateLimitExceeded'} else
                         'authentication' if status == 401 or reasons & {'keyInvalid', 'accessNotConfigured', 'ipRefererBlocked'} else
                         'permission' if status == 403 else 'upstream' if status >= 500 else 'validation')
            except (URLError, TimeoutError, OSError):
                state = 'network'
            except YouTubeError as exc:
                state = exc.kind
            except Exception:
                state = 'invalid_response'
            finally:
                finish(ticket, state, status, result_count)
            # Rate/quota errors are persisted as cooldowns; do not retry them immediately.
            if state not in ('network', 'upstream') or attempt == attempts - 1 or retry_after > 0:
                raise YouTubeError(state, status, retry_after)
            self.sleep(2 ** attempt)
