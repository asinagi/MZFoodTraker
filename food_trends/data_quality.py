"""B4 data quality rules. Outcomes carry rule codes and field names only, never provider values."""
import json
import math
import re
from datetime import datetime
from .contracts import validate_metadata

RULE_VERSION = 'dq-rules-v1'
ID_PATTERNS = {'google_trends_rss': r'[0-9a-f]{64}', 'instagram_graph': r'[0-9]{1,30}', 'youtube': r'[A-Za-z0-9_-]{11}'}
KNOWN_SCHEMAS = {'google_trends_rss': 'rss-item-v1', 'instagram_graph': 'graph-media-v1', 'youtube': 'youtube-v3-video-v1',
                 'naver_datalab': 'datalab-search-v1'}
# Field states, extending A2's not_provided/null/empty/provided with explicit lifecycle states.
FIELD_STATES = frozenset(('provided', 'not_provided', 'null', 'empty', 'expired', 'deleted'))
COUNT_FIELDS = ('view_count', 'like_count', 'comment_count')
SEVERITY = {'PASS': 'INFO', 'NOT_APPLICABLE': 'INFO', 'WARN': 'WARNING', 'FAIL': 'ERROR'}


def result(rule, outcome, **details):
    return {'rule_code': rule, 'outcome': outcome, 'severity': SEVERITY[outcome], 'details': details}


def status(results):
    outcomes = {r['outcome'] for r in results}
    return 'FAIL' if 'FAIL' in outcomes else 'WARN' if 'WARN' in outcomes else 'PASS'


def _instant(value):
    """True: valid aware ISO-8601; False: malformed or naive; None: absent."""
    if value is None:
        return None
    try:
        return isinstance(value, str) and datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None
    except ValueError:
        return False


def _clean_text(value):
    if isinstance(value, str):
        try:
            value.encode('utf-8')
        except UnicodeEncodeError:
            return False  # lone surrogate
        return not any(ord(c) < 32 and c not in '\n\r\t' for c in value)
    if isinstance(value, (list, tuple)):
        return all(_clean_text(v) for v in value)
    if isinstance(value, dict):
        return all(_clean_text(v) for v in value.values())
    return True


def run_rules(run):
    out = []
    try:
        validate_metadata(run['request_context'])
        out.append(result('request.metadata', 'PASS'))
    except ValueError:
        out.append(result('request.metadata', 'FAIL', reason='credential_or_non_json_metadata'))
    schema = run['source_schema_version']
    out.append(result('version.source_schema', 'PASS' if KNOWN_SCHEMAS.get(run['source_code']) == schema else 'WARN',
                      expected=KNOWN_SCHEMAS.get(run['source_code'])))
    if run['source_code'] == 'instagram_graph':
        version = run['request_context'].get('api_version')
        out.append(result('version.api', 'NOT_APPLICABLE' if version is None else
                          'PASS' if isinstance(version, str) and re.fullmatch(r'v\d+\.\d+', version) else 'FAIL'))
    coverage = run['coverage']
    out.append(result('coverage.scope', 'PASS' if coverage == 'COMPLETE' else 'WARN', coverage=coverage,
                      meaning={'PARTIAL': 'committed_pages_only', 'LIMITED': 'page_cap_reached', 'NONE': 'not_observed',
                               'UNKNOWN': 'legacy_scope_not_recorded'}.get(coverage, 'complete_request')))
    if run['source_code'] == 'naver_datalab':
        out.append(result('policy.naver_values', 'PASS', storage='transient_only'))
    return out


def content_rules(source, record):
    out = []
    key = record.get('external_key')
    if not isinstance(key, str) or not key:
        return [result('required.external_key', 'FAIL')]
    out.append(result('required.external_key', 'PASS'))
    out.append(result('format.external_key', 'PASS' if re.fullmatch(ID_PATTERNS[source], key) else 'FAIL'))
    out.append(result('payload.json', 'PASS' if record.get('payload_valid', True) else 'FAIL'))
    fields = record.get('fields', {})
    bad_text = sorted(k for k, v in fields.items() if not _clean_text(v))
    out.append(result('encoding.text', 'FAIL' if bad_text else 'PASS', fields=bad_text))
    published = _instant(record.get('published_at'))
    out.append(result('time.published_at', 'NOT_APPLICABLE' if published is None else 'PASS' if published else 'FAIL'))
    counts = {k: fields[k] for k in COUNT_FIELDS if k in fields}
    if counts:
        invalid = sorted(k for k, v in counts.items() if v is not None and not (isinstance(v, str) and v.isdigit()))
        out.append(result('type.counts', 'FAIL' if invalid else 'PASS', fields=invalid))
    states = record.get('field_states', {})
    unknown = sorted(k for k, v in states.items() if v not in FIELD_STATES)
    absent = sorted(k for k, v in states.items() if v in ('null', 'empty', 'not_provided', 'expired', 'deleted'))
    out.append(result('fields.states', 'FAIL' if unknown else 'WARN' if absent else 'PASS', not_present=absent, unknown=unknown))
    return out


def metric_rules(metric, definitions, storable):
    out = []
    definition = definitions.get(metric['metric_code'])
    out.append(result('unit.defined', 'PASS' if definition and definition['unit'] == metric['unit'] else 'FAIL'))
    value = metric.get('decimal_value', metric.get('count_value'))
    if metric['value_state'] == 'OBSERVED':
        numeric = [v for v in (metric.get('count_value'), metric.get('decimal_value')) if v is not None]
        ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in numeric)
        ok = ok and (numeric or metric.get('raw_value') is not None)
        out.append(result('type.metric_value', 'PASS' if ok else 'FAIL'))
    else:
        out.append(result('type.metric_value', 'PASS' if value is None else 'FAIL', value_state=metric['value_state']))
    start, end = _instant(metric.get('period_start')), _instant(metric.get('period_end'))
    if start is None and end is None:
        out.append(result('time.metric_period', 'NOT_APPLICABLE'))
    else:
        ordered = start and end and metric['period_end'] > metric['period_start']
        out.append(result('time.metric_period', 'PASS' if ordered else 'FAIL'))
    out.append(result('policy.metric_storage', 'PASS' if storable else 'NOT_APPLICABLE',
                      storage='stored' if storable else 'transient_only'))
    return out


def details_json(details):
    validate_metadata(details)
    return json.dumps(details, ensure_ascii=False, sort_keys=True)
