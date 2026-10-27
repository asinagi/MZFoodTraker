"""Read-only RSS integrity audit and explicit human-label evaluation.

python -m food_trends.audit --database data/trends.sqlite3 --output data/audit-current
Optional --compare BEFORE.sqlite3, --labels independently-reviewed.json.
"""
import argparse
import base64
import csv
import json
import sqlite3
from collections import Counter
from pathlib import Path
from .collector import parse


def read_database(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        return {table:[dict(r) for r in db.execute(f'SELECT * FROM {table}')] for table in
                ('items','candidates','observations','runs')}
    finally:
        db.close()


def evaluate(rows, labels):
    # An agent's own classification or annotation is not independent ground truth.
    verified = {r['item_id']:r['actual_food'] for r in labels
                if r.get('review_status') == 'human_verified' and type(r.get('actual_food')) is bool}
    totals = Counter()
    for row in rows:
        if row['id'] not in verified:
            continue
        actual, predicted = verified[row['id']], row['classification']
        if predicted == 'REVIEW':
            totals['review_positive' if actual else 'review_negative'] += 1
        elif predicted == 'FOOD':
            totals['tp' if actual else 'fp'] += 1
        else:
            totals['fn' if actual else 'tn'] += 1
    tp, fp, fn = (totals[k] for k in ('tp','fp','fn'))
    precision = tp/(tp+fp) if tp+fp else None
    recall = tp/(tp+fn) if tp+fn else None
    f1 = 2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None
    return {'human_verified_count':sum(totals.values()), 'total_items':len(rows),
            'counts':dict(totals), 'precision':precision, 'recall_among_decided':recall, 'f1_among_decided':f1,
            'end_to_end_food_recall':tp/(tp+fn+totals['review_positive']) if tp+fn+totals['review_positive'] else None,
            'notice':'REVIEW is reported separately; decided recall excludes REVIEW. Metrics cover verified rows only. Undefined denominators -> null.',
            'unavailable_reason':None if verified else '독립적으로 사람이 검증한 정답 라벨이 없어 실데이터 Precision/Recall/F1을 산출하지 않음'}


def audit(database, output, compare=None, labels=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    data = read_database(database)
    candidates = {r['item_id']:r for r in data['candidates']}
    rows = []
    errors, missing = [], Counter()
    for item in data['items']:
        payload = json.loads(item['payload'])
        candidate = candidates[item['id']]
        row = dict(item, **{k:v for k,v in candidate.items() if k!='item_id'})
        row['payload'] = payload
        for field in ('reasons','evidence_news'):
            if field in row:
                row[field] = json.loads(row[field])
        rows.append(row)
        for field in ('traffic','published_at','related_queries'):
            if payload.get(field) is None:
                missing[field] += 1
        if item['keyword'] != payload['keyword'] or item['source_id'] != payload['source_id'] or item['published_at'] != payload['published_at']:
            errors.append({'item_id':item['id'], 'error':'item columns differ from payload'})
        if item['first_seen'] > item['last_seen']:
            errors.append({'item_id':item['id'], 'error':'first_seen > last_seen'})
    observations = {}
    for obs in data['observations']:
        observations.setdefault(obs['run_id'], []).append(obs)
    parsed_runs = 0
    all_fields = Counter()
    response_duplicates = 0
    for run in data['runs']:
        if not run['success']:
            continue
        if run['raw_response'] is None:
            errors.append({'run_id':run['id'], 'error':'successful run without raw response'})
            continue
        try:
            items, warnings = parse(run['raw_response'])
            parsed_runs += 1
            expected = {i['source_id']:i for i in items}
            response_duplicates += len(items)-len(expected)
            actual = {json.loads(o['payload'])['source_id']:json.loads(o['payload']) for o in observations.get(run['id'], [])}
            if expected != actual or len(items) != run['item_count']:
                errors.append({'run_id':run['id'], 'error':'raw RSS and saved observation/count differ'})
            for i in items:
                all_fields.update(i['provided_fields'])
        except Exception as exc:
            errors.append({'run_id':run['id'], 'error':str(exc)})
    counts = Counter(r['classification'] for r in rows)
    report = {'database':str(database), 'items':len(rows), 'distinct_keywords':len({r['keyword'] for r in rows}),
              'first_seen':min((r['first_seen'] for r in rows),default=None), 'last_seen':max((r['last_seen'] for r in rows),default=None),
              'publication_min':min((r['published_at'] for r in rows if r['published_at']),default=None),
              'publication_max':max((r['published_at'] for r in rows if r['published_at']),default=None),
              'runs':len(data['runs']), 'success_runs':sum(bool(r['success']) for r in data['runs']),
              'parsed_success_raw_runs':parsed_runs, 'observations':len(data['observations']),
              'unfinished_runs':sum(r['finished_at'] is None for r in data['runs']),
              'counts':dict(counts), 'review_ratio':counts['REVIEW']/len(rows) if rows else None,
              'missing_fields':dict(missing), 'provided_fields_across_responses':dict(all_fields),
              'duplicate_source_ids_in_responses':response_duplicates, 'integrity_errors':errors,
              'evaluation':evaluate(rows, labels or [])}
    db = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        report['sqlite_integrity_check'] = [r[0] for r in db.execute('PRAGMA integrity_check')]
        report['foreign_key_errors'] = [list(r) for r in db.execute('PRAGMA foreign_key_check')]
    finally:
        db.close()
    if compare:
        old = read_database(compare)
        before = {r['item_id']:r for r in old['candidates']}
        changes = [dict(id=r['id'],keyword=r['keyword'],before=before[r['id']]['classification'],after=r['classification'],
                        before_reasons=json.loads(before[r['id']]['reasons']),after_reasons=r['reasons'])
                   for r in rows if r['id'] in before and before[r['id']]['classification'] != r['classification']]
        common = [r for r in rows if r['id'] in before]
        report['comparison'] = {'before_items':len(old['items']), 'same_snapshot_items':len(common),
                                'before_counts':dict(Counter(r['classification'] for r in before.values())),
                                'after_same_snapshot_counts':dict(Counter(r['classification'] for r in common)), 'changes':changes}
    (output/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'items.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
    runs = [dict(r,raw_response_base64=base64.b64encode(r['raw_response']).decode() if r['raw_response'] is not None else None) for r in data['runs']]
    for r in runs:
        r.pop('raw_response')
    (output/'runs.json').write_text(json.dumps(runs,ensure_ascii=False,indent=2),encoding='utf-8')
    with (output/'human-review-template.csv').open('w',encoding='utf-8-sig',newline='') as file:
        writer = csv.writer(file)
        writer.writerow(['item_id','keyword','published_at','news_titles','news_urls','actual_food','review_status','reviewer','notes'])
        for r in rows:
            writer.writerow([r['id'],r['keyword'],r['published_at'],' | '.join(n.get('title') or '' for n in r['payload']['news']),
                             ' | '.join(n.get('url') or '' for n in r['payload']['news']),'','pending','',''])
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', default='data/trends.sqlite3')
    parser.add_argument('--output', required=True)
    parser.add_argument('--compare')
    parser.add_argument('--labels', help='JSON list with item_id, actual_food boolean, review_status=human_verified')
    args = parser.parse_args()
    labels = json.loads(Path(args.labels).read_text(encoding='utf-8')) if args.labels else []
    report = audit(args.database,args.output,args.compare,labels)
    print(json.dumps({k:v for k,v in report.items() if k!='comparison'},ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
