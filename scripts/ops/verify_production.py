"""Non-destructive check of the running production backend (ORACLE-PRODUCTION-CUTOVER-1): counts, codes and ids only.

Reads the admin server and the B8 app API over HTTP (every B8 answer checked against OpenAPI v1) and the production schema
through a read-only status read. With --state-change it also makes one identifiable admin change: a candidate named
'ORACLE-PRODUCTION-CUTOVER-1 verification' submitted and rejected (never a food, so the app API never shows it).
"""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
OPENER = build_opener(ProxyHandler({}))
MARK = 'ORACLE-PRODUCTION-CUTOVER-1 verification'
TABLES = ('rss_runs', 'food_candidates', 'food_entities', 'food_aliases', 'tracking_targets', 'execution_jobs', 'execution_calls',
          'ingest_runs', 'source_items', 'source_contents', 'item_snapshots', 'food_mentions', 'metric_observations', 'quality_results')


def get(base, path, token=None, body=None, method='GET'):
    headers = {'Content-Type': 'application/json'} if body is not None else {}
    if token:
        headers['X-MZ-Admin-Token'] = token
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    try:
        with OPENER.open(Request(base + path, data=data, headers=headers, method=method), timeout=120) as response:
            return response.status, json.loads(response.read() or b'null')
    except HTTPError as error:
        return error.code, json.loads(error.read() or b'null')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--admin', default='http://127.0.0.1:8000')
    parser.add_argument('--app', default='http://127.0.0.1:8020')
    parser.add_argument('--state-change', action='store_true')
    parser.add_argument('--output')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from food_trends import app_contract as contract
    from food_trends.runtime_storage import select
    token = os.environ.get('MZ_ADMIN_TOKEN')
    doc, out, failures = contract.openapi(), {'checked_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat()}, []
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).date().isoformat()

    def b8(path):
        status, body = get(args.app, path)
        schema = contract.response_schema(doc, contract.operation_for(doc, urlsplit(path).path)[1], status)
        errors = contract.check(doc, body, schema)
        if status != 200 or errors:
            failures.append('b8 %s %s %d' % (urlsplit(path).path, status, len(errors)))
        return body
    out['b8_health'] = (b8('/api/v1/health').get('data') or {}).get('status')
    foods = (b8('/api/v1/foods?limit=100').get('data') or {}).get('items', [])
    out['b8_foods'] = sorted((f['food_id'], f.get('canonical_name')) for f in foods)
    for food in foods:
        b8('/api/v1/foods/' + food['food_id'])
        series = (b8('/api/v1/foods/%s/timeline?period=7d&end_date=%s&source=instagram_graph' % (food['food_id'], today)).get('data') or {}).get('series', [])
        out.setdefault('b8_instagram_points', {})[food['food_id']] = [(p['start_date'], p['value'], p['value_state']) for p in (series[0]['points'] if series else []) if p['value_state'] != 'NOT_OBSERVED']
    out['b8_sources'] = {s['source_code']: s.get('last_successful_run_at') for s in (b8('/api/v1/sources/status').get('data') or {}).get('sources', [])}
    status, admin = get(args.admin, '/api/status', token)
    out['admin_status'] = {'http': status, 'latest_run_origin': ((admin or {}).get('latest_run') or {}).get('origin'), 'storage': (admin or {}).get('storage')}
    status, candidates = get(args.admin, '/api/food-candidates', token)
    out['admin_candidates'] = {'http': status, 'count': len((candidates or {}).get('items', []))}
    status, targets = get(args.admin, '/api/tracking/targets', token)
    out['admin_tracking'] = {'http': status, 'effective': sorted('%s:%s' % (t['source_id'], t.get('effective_status')) for t in (targets or {}).get('items', []))}
    marks = [c for c in (candidates or {}).get('items', []) if c.get('surface_form') == MARK]
    if args.state_change and not marks:
        status, submitted = get(args.admin, '/api/foods/submit', token, dict(request_key=uuid.uuid4().hex, name=MARK, aliases=[]), 'POST')
        status, rejected = get(args.admin, '/api/foods/review', token, dict(request_key=uuid.uuid4().hex, candidate_id=submitted['candidate_id'],
                               expected_revision=submitted['revision'], action='reject', approved=True, actor_ref='cutover-verification',
                               reason='재기동 영속성 확인용 후보, 음식 아님'), 'POST')
        out['state_change'] = {'http': status, 'candidate_id': submitted['candidate_id']}
        status, candidates = get(args.admin, '/api/food-candidates', token)
        marks = [c for c in (candidates or {}).get('items', []) if c.get('surface_form') == MARK]
    out['verification_candidate'] = [(c['candidate_id'], c.get('status')) for c in marks]
    repo = select('oracle-production')
    try:
        out['schema'] = repo.db.settings.user.upper()
        out['rows'] = {t: repo.count(t) for t in TABLES}
    finally:
        repo.db.close()
    for name, code in (('admin_status', out['admin_status']['http']), ('admin_candidates', out['admin_candidates']['http']),
                       ('admin_tracking', out['admin_tracking']['http'])):
        if code != 200:
            failures.append('%s %s' % (name, code))
    out['failures'] = failures
    text = json.dumps(out, ensure_ascii=False, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(text, encoding='utf-8')
    print(json.dumps({'failures': failures, 'foods': len(out['b8_foods']), 'candidates': out['admin_candidates']['count'], 'rows': out['rows']}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
