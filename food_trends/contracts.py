"""Versioned metadata contracts. Provider payloads and credentials are not envelopes."""
import copy
import json
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit

CONTRACT_VERSION = 'source-envelope-v1'
ORIGINS = frozenset(('live_api', 'mock', 'user', 'legacy_unknown'))
_SECRET_KEYS = frozenset(('authorization', 'accesstoken', 'inputtoken', 'fbexchangetoken',
                          'clientsecret', 'apikey', 'xgoogapikey', 'xncpapigwapikey',
                          'xncpapigwapikeyid', 'token', 'appsecret', 'password', 'key',
                          'clientid', 'clientkey', 'secret', 'apisecret', 'credentials',
                          'instagramaccesstoken', 'youtubeapikey', 'metaappsecret',
                          'naverdatalabclientid', 'naverdatalabclientsecret'))

SOURCES = {
    'google_trends_rss': {
        'adapter_version': 'rss-adapter-v1', 'data_type': 'trend_item',
        'fields': ('keyword', 'published_at', 'published_raw', 'traffic', 'news', 'related_queries'),
        'metrics': {'approx_traffic': 'approximate_search_traffic_text'},
        'request_fields': ('geo', 'endpoint'),
        'policy': {'version': 'rss-existing-v1', 'response_storage': True, 'response_cache': False,
                   'retention_days': None, 'long_term_retention_verified': False,
                   'expand_retention_allowed': False, 'derived_operations': ('existing_food_rules',)},
    },
    'instagram_graph': {
        'adapter_version': 'instagram-adapter-v1', 'data_type': 'hashtag_media',
        'fields': ('id', 'caption', 'published_at', 'media_type', 'permalink'),
        'metrics': {'observed_posts': 'unique_observed_post', 'co_tag_rate': 'percent_of_observed_posts'},
        'request_fields': ('hashtag_id', 'hashtag', 'fields', 'limit', 'max_pages', 'api_version'),
        'policy': {'version': 'instagram-existing-v1', 'response_storage': False, 'response_cache': False,
                   'retention_days': 30, 'long_term_retention_verified': False,
                   'expand_retention_allowed': False, 'derived_operations': ('existing_caption_hashtags',)},
    },
    'youtube': {
        'adapter_version': 'youtube-adapter-v1', 'data_type': 'video_metadata',
        'fields': ('video_id', 'title', 'description', 'published_at', 'channel_id', 'channel_title',
                   'thumbnail_url', 'view_count', 'like_count', 'comment_count', 'duration', 'privacy_status'),
        'metrics': {'view_count': 'video_view_count_snapshot', 'like_count': 'video_like_count_snapshot',
                    'comment_count': 'video_comment_count_snapshot'},
        'request_fields': ('part', 'type', 'regionCode', 'relevanceLanguage', 'order', 'publishedAfter',
                           'publishedBefore', 'maxResults', 'q', 'id', 'endpoint'),
        'policy': {'version': 'youtube-existing-v1', 'response_storage': False, 'response_cache': False,
                   'retention_days': 29, 'long_term_retention_verified': False,
                   'expand_retention_allowed': False, 'derived_operations': ()},
    },
    'naver_datalab': {
        'adapter_version': 'naver-adapter-v1', 'data_type': 'relative_search_series',
        'fields': ('startDate', 'endDate', 'timeUnit', 'results'),
        'metrics': {'relative_search_index': 'relative_search_index_0_100'},
        'request_fields': ('startDate', 'endDate', 'timeUnit', 'keywordGroups', 'device', 'gender', 'ages'),
        'policy': {'version': 'naver-no-response-storage-v1', 'response_storage': False,
                   'response_cache': False, 'retention_days': 0, 'long_term_retention_verified': False,
                   'expand_retention_allowed': False, 'derived_operations': ()},
    },
}


def _key(value):
    return re.sub(r'[^a-z0-9]', '', value.lower())


def validate_metadata(value):
    """Fail closed on credential-shaped keys, URLs and non-JSON values; never echo values."""
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or _key(key) in _SECRET_KEYS:
                raise ValueError('Credentials are forbidden in contract metadata')
            validate_metadata(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            validate_metadata(child)
    elif isinstance(value, str):
        if value.lower().startswith(('bearer ', 'basic ')):
            raise ValueError('Credentials are forbidden in contract metadata')
        if '://' in value:
            url = urlsplit(value)
            if url.username or url.password or any(_key(k) in _SECRET_KEYS for k, _ in parse_qsl(url.query)):
                raise ValueError('Credential URLs are forbidden in contract metadata')
    elif value is not None and type(value) not in (bool, int, float):
        raise ValueError('Contract metadata must be JSON-compatible')
    json.dumps(value, allow_nan=False)


def _time(value, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('Contract timestamps require a timezone')


def policy_for(source_code, retention_days=None):
    policy = copy.deepcopy(SOURCES[source_code]['policy'])
    if source_code == 'instagram_graph' and retention_days is not None:
        # Contract creation cannot silently extend the existing configured lifetime.
        if type(retention_days) is not int or not 1 <= retention_days <= 365:
            raise ValueError('Invalid existing caption retention setting')
        policy['retention_days'] = retention_days
    return policy


def require_operation(source_code, operation):
    if operation not in SOURCES[source_code]['policy']['derived_operations']:
        raise ValueError('Processing operation is not enabled by the source policy')


def envelope(source_code, origin, collected_at, *, published_at=None, fields=None,
             request=None, observation=None, retention_days=None):
    if source_code not in SOURCES or origin not in ORIGINS:
        raise ValueError('Unknown source or origin')
    _time(collected_at)
    _time(published_at, nullable=True)
    source = SOURCES[source_code]
    request = {} if request is None else request
    if not isinstance(request, dict) or set(request) - set(source['request_fields']):
        raise ValueError('Unknown request metadata field')
    field_evidence = 'not_recorded' if fields is None else 'adapter_observed'
    fields = {} if fields is None else fields
    if not isinstance(fields, dict):
        raise ValueError('Field metadata requires a mapping')
    states = {}
    for name in source['fields'] if field_evidence == 'adapter_observed' else ():
        value = fields.get(name)
        states[name] = ('not_provided' if name not in fields else 'null' if value is None
                        else 'empty' if value == '' or value == [] else 'provided')
    result = {
        'contract_version': CONTRACT_VERSION, 'source_code': source_code, 'origin': origin,
        'collected_at': collected_at, 'published_at': published_at, 'data_type': source['data_type'],
        'metric_units': copy.deepcopy(source['metrics']), 'field_states': states, 'field_evidence': field_evidence,
        'provided_fields': [k for k, v in states.items() if v != 'not_provided'],
        'missing_fields': [k for k, v in states.items() if v == 'not_provided'],
        'request_conditions': copy.deepcopy(request), 'observation_conditions': copy.deepcopy(observation or {}),
        'adapter_version': source['adapter_version'], 'policy': policy_for(source_code, retention_days),
    }
    result['policy_version'] = result['policy']['version']
    validate_metadata(result)
    return result


def encode(contract):
    validate_metadata(contract)
    return json.dumps(contract, ensure_ascii=False, allow_nan=False, sort_keys=True)
