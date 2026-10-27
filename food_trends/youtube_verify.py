"""One search (five results) and one detail batch; retain operational evidence only."""
import hashlib
import json
from pathlib import Path
from .storage import utcnow


def protected_fingerprints(storage):
    result = {}
    with storage.connect() as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
                 if r[0] in ('runs', 'items', 'observations', 'candidates', 'classification_history') or r[0].startswith('naver_')]
        for name in sorted(names):
            digest, count = hashlib.sha256(), 0
            for row in db.execute('SELECT * FROM "'+name.replace('"', '""')+'" ORDER BY rowid'):
                digest.update(repr(tuple(row)).encode('utf-8'))
                count += 1
            result[name] = {'rows': count, 'sha256': digest.hexdigest()}
    return result


def verify(service):
    before = protected_fingerprints(service.storage)
    with service.storage.connect() as db:
        videos_before = db.execute('SELECT count(*) FROM youtube_videos').fetchone()[0]
        identities_before = {r['video_id']: (json.loads(r['payload'])['channel_id'], json.loads(r['payload'])['published_at'])
                             for r in db.execute('SELECT video_id,payload FROM youtube_videos')}
    result = service.collect(verification=True)
    after = protected_fingerprints(service.storage)
    with service.storage.connect() as db:
        videos_after = db.execute('SELECT count(*) FROM youtube_videos').fetchone()[0]
        calls = [dict(r) for r in db.execute('''SELECT endpoint,origin,status,http_status,result_count
            FROM youtube_calls WHERE run_id=? ORDER BY id''', (result.get('run_id'),))]
        sampled = [dict(r) for r in db.execute('''SELECT v.video_id,v.payload FROM youtube_videos v
            JOIN youtube_video_provenance p ON v.video_id=p.video_id WHERE p.run_id=?''', (result.get('run_id'),))]
        identity_matches = sum(identities_before.get(r['video_id']) ==
                               (json.loads(r['payload'])['channel_id'], json.loads(r['payload'])['published_at']) for r in sampled)
    return dict(result, verified_at=utcnow(), calls=calls, stored_videos_before=videos_before,
                stored_videos_after=videos_after, protected_tables_unchanged=before == after,
                existing_video_identities_confirmed=identity_matches,
                protected_tables_before=before, protected_tables_after=after,
                data='live_api' if result.get('origin') == 'live_api' else 'mock',
                verification_passed=bool(result.get('success') and result.get('live_connection_verified') and
                                         any(c['endpoint']=='search' and c['http_status']==200 for c in calls) and
                                         any(c['endpoint']=='videos' and c['http_status']==200 for c in calls) and before == after))


def save_report(result, path='data/youtube-live-verification.json'):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
