import json
import base64
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from .classifier import classify
from .contracts import envelope, encode

def utcnow():
    return datetime.now(timezone.utc).isoformat()

class Storage:
    def __init__(self, path):
        from .migrations import require_current
        self.path = str(Path(path).resolve())
        require_current(self.path)

    @classmethod
    def create(cls, path):
        """Explicit schema creation/upgrade, primarily for isolated fixture databases."""
        from .migrations import migrate
        migrate(path)
        return cls(path)

    def _classify(self, db, item_id, item):
        result = dict(classify(item), classified_at=utcnow())
        old = db.execute('SELECT * FROM candidates WHERE item_id=?', (item_id,)).fetchone()
        if old:
            previous = dict(old)
            for field in ('reasons', 'evidence_news'):
                previous[field] = json.loads(previous[field])
            comparison = {k:v for k,v in previous.items() if k not in ('item_id','classified_at')}
            if comparison != {k:v for k,v in result.items() if k != 'classified_at'}:
                db.execute('INSERT INTO classification_history(item_id,recorded_at,result_json) VALUES (?,?,?)',
                           (item_id, utcnow(), json.dumps(previous, ensure_ascii=False)))
        db.execute('''INSERT INTO candidates
            (item_id,normalized_name,classification,reasons,classifier_version,entity_type,rule_score,score_basis,evidence_news,classified_at)
            VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET
            normalized_name=excluded.normalized_name,classification=excluded.classification,
            reasons=excluded.reasons,classifier_version=excluded.classifier_version,
            entity_type=excluded.entity_type,rule_score=excluded.rule_score,
            score_basis=excluded.score_basis,evidence_news=excluded.evidence_news,
            classified_at=excluded.classified_at''',
            (item_id, result['normalized_name'], result['classification'], json.dumps(result['reasons'], ensure_ascii=False),
             result['classifier_version'], result['entity_type'], result['rule_score'], result['score_basis'],
             json.dumps(result['evidence_news'], ensure_ascii=False), result['classified_at']))
        return result

    @contextmanager
    def connect(self, readonly=False):
        if readonly:
            db = sqlite3.connect(Path(self.path).as_uri() + '?mode=ro', uri=True, timeout=10)
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
        else:
            db = sqlite3.connect(Path(self.path).as_uri() + '?mode=rw', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def reader(self):
        return self.connect(readonly=True)

    def start(self, origin='legacy_unknown'):
        contract = envelope('google_trends_rss', origin, utcnow(),
                            request={'geo': 'KR', 'endpoint': 'trending/rss'})
        with self.connect() as db:
            return db.execute('INSERT INTO runs(started_at,origin,contract_json) VALUES (?,?,?)',
                              (contract['collected_at'], origin, encode(contract))).lastrowid

    def finish(self, run_id, items=None, raw=None, http_status=None, error=None, warnings=None):
        new_count = 0
        counts = {'FOOD':0, 'REVIEW':0}
        new_counts = {'FOOD':0, 'REVIEW':0}
        unique_ids = set()
        seen = utcnow()
        with self.connect() as db:
            origin = db.execute('SELECT origin FROM runs WHERE id=?', (run_id,)).fetchone()[0]
            for item in items or []:
                if item['source_id'] in unique_ids:
                    continue
                unique_ids.add(item['source_id'])
                payload = json.dumps(item, ensure_ascii=False)
                cur = db.execute('INSERT OR IGNORE INTO items(source_id,keyword,published_at,first_seen,last_seen,payload) VALUES (?,?,?,?,?,?)',
                                 (item['source_id'], item['keyword'], item['published_at'], seen, seen, payload))
                new_count += cur.rowcount
                db.execute('UPDATE items SET last_seen=?,payload=? WHERE source_id=?', (seen, payload, item['source_id']))
                item_id = db.execute('SELECT id FROM items WHERE source_id=?', (item['source_id'],)).fetchone()[0]
                c = self._classify(db, item_id, item)
                mapping = {'title': 'keyword', 'pubDate': 'published_raw', 'approx_traffic': 'traffic',
                           'news_item': 'news'}
                supplied = {mapping[k] for k in item.get('provided_fields', ()) if k in mapping}
                if 'published_raw' in supplied:
                    supplied.add('published_at')
                if item.get('related_queries') is not None:
                    supplied.add('related_queries')
                contract = envelope('google_trends_rss', origin, seen, published_at=item['published_at'],
                                    fields={k: item[k] for k in supplied},
                                    request={'geo': 'KR', 'endpoint': 'trending/rss'},
                                    observation={'run_id': run_id, 'item_key': item['source_id']})
                db.execute('INSERT OR IGNORE INTO observations(run_id,item_id,payload,classification_json,contract_json) VALUES (?,?,?,?,?)',
                           (run_id, item_id, payload, json.dumps(c, ensure_ascii=False), encode(contract)))
                label = c['classification']
                if label in counts:
                    counts[label] += 1
                    new_counts[label] += cur.rowcount
            db.execute('UPDATE runs SET finished_at=?,success=?,error=?,http_status=?,item_count=?,new_count=?,warnings=?,raw_response=? WHERE id=?',
                       (seen, int(error is None), error, http_status, len(items or []), new_count, json.dumps(warnings or [], ensure_ascii=False), raw, run_id))
            db.execute('UPDATE runs SET food_count=?,review_count=?,new_food_count=?,new_review_count=?,classifier_version=? WHERE id=?',
                       (counts['FOOD'] if not error else None, counts['REVIEW'] if not error else None,
                        new_counts['FOOD'] if not error else None, new_counts['REVIEW'] if not error else None,
                        c['classifier_version'] if items and not error else None, run_id))
        return {'new_count':new_count, 'food_count':counts['FOOD'] if not error else None,
                'review_count':counts['REVIEW'] if not error else None}

    def status(self):
        with self.reader() as db:
            run = db.execute('''SELECT id,started_at,finished_at,success,error,http_status,item_count,new_count,warnings,
                food_count,review_count,new_food_count,new_review_count,classifier_version,origin,contract_json
                FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1''').fetchone()
            success = db.execute('SELECT finished_at FROM runs WHERE success=1 ORDER BY id DESC LIMIT 1').fetchone()
            counts = dict(db.execute('SELECT classification,count(*) FROM candidates GROUP BY classification').fetchall())
            total = db.execute('SELECT count(*) FROM items').fetchone()[0]
            observations = db.execute('SELECT count(*) FROM observations').fetchone()[0]
            distinct = db.execute('SELECT count(distinct keyword) FROM items').fetchone()[0]
            discovery = db.execute('SELECT max(first_seen) FROM items').fetchone()[0]
            current_counts = {}
            latest_publication = None
            if run and run['success']:
                current_counts = dict(db.execute('SELECT c.classification,count(*) FROM observations o JOIN candidates c ON c.item_id=o.item_id WHERE o.run_id=? GROUP BY c.classification', (run['id'],)).fetchall())
                values = [json.loads(r[0]).get('published_at') for r in db.execute('SELECT payload FROM observations WHERE run_id=?', (run['id'],))]
                latest_publication = max((v for v in values if v), default=None)
        latest = dict(run) if run else None
        if latest:
            contract_json = latest.pop('contract_json')
            latest['contract'] = json.loads(contract_json) if contract_json else None
            latest['warnings'] = json.loads(latest['warnings'])
            latest['discovery_state'] = ('NEW_ITEMS' if latest['new_count'] else 'NO_NEW_ITEMS') if latest['success'] else 'COLLECTION_FAILED'
            latest['current_food_count'] = current_counts.get('FOOD', 0) if latest['success'] else None
            latest['current_review_count'] = current_counts.get('REVIEW', 0) if latest['success'] else None
            latest['latest_publication_at'] = latest_publication
            latest['feed_age_seconds'] = max(0, (datetime.fromisoformat(latest['finished_at'])-datetime.fromisoformat(latest_publication)).total_seconds()) if latest_publication else None
        return {'latest_run': latest, 'last_success_at': success[0] if success else None,
                'total_keywords': total, 'food_count': counts.get('FOOD', 0),
                'review_count': counts.get('REVIEW', 0), 'non_food_count': counts.get('NON_FOOD', 0),
                'distinct_keywords': distinct, 'last_discovery_at':discovery, 'observation_count': observations}

    def trends(self, category='ALL', query='', item_id=None):
        where, args = [], []
        if category in ('FOOD', 'REVIEW', 'NON_FOOD'):
            where.append('c.classification=?'); args.append(category)
        if query:
            where.append('instr(lower(c.normalized_name),lower(?))>0'); args.append(query)
        if item_id is not None:
            where.append('i.id=?'); args.append(item_id)
        sql = '''SELECT i.*,c.normalized_name,c.classification,c.reasons,c.classifier_version,
                 c.entity_type,c.rule_score,c.score_basis,c.evidence_news,c.classified_at
                 FROM items i JOIN candidates c ON i.id=c.item_id'''
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY i.first_seen DESC,i.published_at DESC,i.id DESC'
        with self.reader() as db:
            rows = db.execute(sql, args).fetchall()
            results = []
            for row in rows:
                r = dict(row)
                contract_row = db.execute('''SELECT contract_json FROM observations
                    WHERE item_id=? ORDER BY run_id DESC LIMIT 1''', (r['id'],)).fetchone()
                r['contract'] = json.loads(contract_row[0]) if contract_row and contract_row[0] else None
                payload = json.loads(r.pop('payload'))
                r.update(payload)
                r['reasons'] = json.loads(r['reasons'])
                r['evidence_news'] = json.loads(r['evidence_news'])
                aggregate = db.execute('SELECT min(first_seen),max(last_seen),count(*) FROM items WHERE keyword=?', (r['keyword'],)).fetchone()
                r.update(keyword_first_seen=aggregate[0], keyword_last_seen=aggregate[1], occurrence_count=aggregate[2])
                if item_id is None:
                    r.pop('raw_xml', None)
                results.append(r)
        return results

    def reanalyze(self):
        with self.connect() as db:
            for row in db.execute('SELECT id,payload FROM items').fetchall():
                self._classify(db, row['id'], json.loads(row['payload']))

    def export(self):
        """RSS-only audit export: excludes credentials and unrelated feature tables."""
        with self.reader() as db:
            # A single read transaction provides a consistent export during collection.
            result = {'exported_at':utcnow(), 'format_version':1, 'encoding':'raw_response_base64 is original bytes',
                      'score_notice':'Rule scores are not probabilities'}
            for table in ('items','candidates','observations','runs','classification_history'):
                rows = []
                for row in db.execute(f'SELECT * FROM {table}'):
                    r = dict(row)
                    if 'raw_response' in r:
                        raw = r.pop('raw_response')
                        r['raw_response_base64'] = base64.b64encode(raw).decode('ascii') if raw is not None else None
                    for field in ('payload','reasons','evidence_news','warnings','classification_json','result_json'):
                        if field in r and r[field] is not None:
                            r[field] = json.loads(r[field])
                    rows.append(r)
                result[table] = rows
        return result
