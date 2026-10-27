"""Small dotenv loader; process environment wins and secrets never enter reprs."""
import os
import re
import math
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class YouTubeConfig:
    api_key: str = field(default='', repr=False)
    enabled: bool = True
    interval_seconds: int = 21600
    daily_search_limit: int = 40
    daily_video_limit: int = 1000
    queries_per_run: int = 8
    max_pages: int = 1
    timeout: float = 20
    retries: int = 2
    # Deployment configuration, never mined from YouTube API data.
    discovery_queries: tuple = ('신상 디저트', '유행 음식', '요즘 먹방', '편의점 신상', '신상 간식', '카페 신메뉴')

    @classmethod
    def from_env(cls):
        queries = json.loads(os.getenv('YOUTUBE_DISCOVERY_QUERIES', json.dumps(cls.discovery_queries)))
        return cls(api_key=(os.getenv('YOUTUBE_API_KEY', '').strip() or os.getenv('YOUTUBE_DATA_API_V3_KEY', '').strip()),
                   enabled=os.getenv('YOUTUBE_COLLECTION_ENABLED', 'true').lower() in ('true', '1', 'yes'),
                   interval_seconds=int(os.getenv('YOUTUBE_INTERVAL_SECONDS', '21600')),
                   daily_search_limit=int(os.getenv('YOUTUBE_DAILY_SEARCH_LIMIT', '40')),
                   daily_video_limit=int(os.getenv('YOUTUBE_DAILY_VIDEO_LIMIT', '1000')),
                   queries_per_run=int(os.getenv('YOUTUBE_QUERIES_PER_RUN', '8')),
                   max_pages=int(os.getenv('YOUTUBE_MAX_PAGES', '1')),
                   timeout=float(os.getenv('YOUTUBE_TIMEOUT_SECONDS', '20')),
                   discovery_queries=tuple(queries) if isinstance(queries, list) else ())

    def __post_init__(self):
        if (type(self.interval_seconds) is not int or self.interval_seconds < 21600 or
                not 1 <= self.daily_search_limit <= 100 or not 1 <= self.daily_video_limit <= 10000 or
                not 1 <= self.queries_per_run <= 10 or not 1 <= self.max_pages <= 5 or
                not 0 < self.timeout <= 60 or not 0 <= self.retries <= 2):
            raise ValueError('YouTube 수집 간격/예산/타임아웃 설정을 확인하세요')
        if (not 1 <= len(self.discovery_queries) <= 20 or
                any(not isinstance(q, str) or not q.strip() or len(q) > 100 for q in self.discovery_queries)):
            raise ValueError('YOUTUBE_DISCOVERY_QUERIES에는 검색어의 JSON 배열을 입력하세요')

    @property
    def configured(self):
        return bool(self.api_key)


def load_env(path='.env'):
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        key, sep, value = line.partition('=')
        if not sep or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key.strip()):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(' #', 1)[0].rstrip()
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class InstagramConfig:
    version: str = 'v26.0'
    token: str = field(default='', repr=False)
    user_id: str = '17841473668787556'
    page_id: str = '1397807253414629'
    app_id: str = ''
    app_secret: str = field(default='', repr=False)
    enabled: bool = True
    interval_minutes: float = 60
    timeout: float = 20
    max_pages: int = 20
    search_soft_limit: int = 28
    caption_retention_days: int = 30

    @classmethod
    def from_env(cls):
        return cls(
            version=os.getenv('INSTAGRAM_GRAPH_API_VERSION', 'v26.0'),
            token=os.getenv('INSTAGRAM_ACCESS_TOKEN', '').strip(),
            user_id=os.getenv('INSTAGRAM_USER_ID', '17841473668787556'),
            page_id=os.getenv('FACEBOOK_PAGE_ID', '1397807253414629'),
            app_id=os.getenv('META_APP_ID', ''), app_secret=os.getenv('META_APP_SECRET', ''),
            enabled=os.getenv('INSTAGRAM_COLLECTION_ENABLED', 'true').lower() in ('true', '1', 'yes'),
            interval_minutes=float(os.getenv('INSTAGRAM_COLLECTION_INTERVAL_MINUTES', '60')),
            timeout=float(os.getenv('INSTAGRAM_TIMEOUT_SECONDS', '20')),
            max_pages=int(os.getenv('INSTAGRAM_MAX_PAGES', '20')),
            search_soft_limit=int(os.getenv('INSTAGRAM_SEARCH_SOFT_LIMIT', '28')),
            caption_retention_days=int(os.getenv('INSTAGRAM_CAPTION_RETENTION_DAYS', '30')))

    def __post_init__(self):
        if not re.fullmatch(r'v\d+\.\d+', self.version):
            raise ValueError('INSTAGRAM_GRAPH_API_VERSION 형식: v26.0')
        if not self.user_id.isdigit() or not self.page_id.isdigit():
            raise ValueError('Instagram User / Facebook Page ID는 숫자여야 합니다')
        if not math.isfinite(self.interval_minutes) or self.interval_minutes <= 0 or not 0 < self.timeout <= 60:
            raise ValueError('Instagram 수집 간격은 양수, 타임아웃은 1~60초로 설정하세요')
        if not 1 <= self.max_pages <= 100 or not 1 <= self.search_soft_limit <= 30:
            raise ValueError('Instagram 페이지 수는 1~100, 검색 예산은 1~30으로 설정하세요')
        if not 1 <= self.caption_retention_days <= 365:
            raise ValueError('캡션 보관 기간은 1~365일로 설정하세요')

    @property
    def configured(self):
        return bool(self.token and self.user_id)


@dataclass(frozen=True)
class NaverConfig:
    client_id: str = field(default='', repr=False)
    client_secret: str = field(default='', repr=False)
    enabled: bool = True
    interval_seconds: int = 86400
    daily_limit: int = 100
    timeout: float = 20
    retries: int = 2
    cooldown_seconds: int = 60

    @classmethod
    def from_env(cls):
        return cls(client_id=os.getenv('NAVER_DATALAB_CLIENT_ID', '').strip(),
                   client_secret=os.getenv('NAVER_DATALAB_CLIENT_SECRET', '').strip(),
                   enabled=os.getenv('NAVER_DATALAB_ENABLED', 'true').lower() in ('true', '1', 'yes'),
                   interval_seconds=int(os.getenv('NAVER_DATALAB_INTERVAL_SECONDS', '86400')),
                   daily_limit=int(os.getenv('NAVER_DATALAB_DAILY_LIMIT', '100')),
                   timeout=float(os.getenv('NAVER_DATALAB_TIMEOUT_SECONDS', '20')))

    def __post_init__(self):
        if not 1 <= self.daily_limit <= 100 or self.interval_seconds < 86400:
            raise ValueError('NAVER 수집 간격은 하루 이상, 자체 일일 한도는 1~100입니다')
        if not 0 < self.timeout <= 60 or not 0 <= self.retries <= 2 or self.cooldown_seconds < 0:
            raise ValueError('NAVER 타임아웃/재시도 설정을 확인하세요')

    @property
    def configured(self):
        return bool(self.client_id and self.client_secret)
