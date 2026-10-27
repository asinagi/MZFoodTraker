"""Legacy additive schema, executed only by the explicit migration runner."""

BASE_SQL = '''
CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    success INTEGER, error TEXT, http_status INTEGER, item_count INTEGER DEFAULT 0,
                    new_count INTEGER DEFAULT 0, warnings TEXT, raw_response BLOB);
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY, source_id TEXT UNIQUE NOT NULL, keyword TEXT NOT NULL,
                    published_at TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                    run_id INTEGER REFERENCES runs(id), item_id INTEGER REFERENCES items(id),
                    payload TEXT NOT NULL, PRIMARY KEY(run_id,item_id));
                CREATE TABLE IF NOT EXISTS candidates (
                    item_id INTEGER PRIMARY KEY REFERENCES items(id), normalized_name TEXT NOT NULL,
                    classification TEXT NOT NULL, reasons TEXT NOT NULL, classifier_version TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS items_keyword ON items(keyword);
                CREATE TABLE IF NOT EXISTS classification_history (
                    id INTEGER PRIMARY KEY, item_id INTEGER REFERENCES items(id),
                    recorded_at TEXT NOT NULL, result_json TEXT NOT NULL);
            

                CREATE TABLE IF NOT EXISTS instagram_hashtags (
                    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                    instagram_id TEXT, created_at TEXT NOT NULL, last_searched_at TEXT,
                    active INTEGER NOT NULL DEFAULT 0, approved_at TEXT,
                    UNIQUE(user_id,name));
                CREATE TABLE IF NOT EXISTS instagram_candidate_hashtags (
                    candidate_id INTEGER NOT NULL REFERENCES candidates(item_id),
                    hashtag_id INTEGER NOT NULL REFERENCES instagram_hashtags(id),
                    PRIMARY KEY(candidate_id,hashtag_id));
                CREATE TABLE IF NOT EXISTS instagram_media (
                    id TEXT PRIMARY KEY, published_at TEXT, first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL, media_type TEXT, permalink TEXT,
                    source TEXT NOT NULL DEFAULT 'instagram_graph');
                CREATE TABLE IF NOT EXISTS instagram_hashtag_media (
                    hashtag_id INTEGER NOT NULL REFERENCES instagram_hashtags(id),
                    media_id TEXT NOT NULL REFERENCES instagram_media(id),
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(hashtag_id,media_id));
                CREATE TABLE IF NOT EXISTS instagram_runs (
                    id INTEGER PRIMARY KEY, hashtag_id INTEGER NOT NULL REFERENCES instagram_hashtags(id),
                    started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
                    request_count INTEGER NOT NULL DEFAULT 0, returned_count INTEGER NOT NULL DEFAULT 0,
                    new_count INTEGER NOT NULL DEFAULT 0, duplicate_count INTEGER NOT NULL DEFAULT 0,
                    error_type TEXT, error_code INTEGER, error_subcode INTEGER,
                    truncated INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS instagram_searches (
                    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                    instagram_id TEXT, searched_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS instagram_metrics (
                    hashtag_id INTEGER NOT NULL REFERENCES instagram_hashtags(id),
                    grain TEXT NOT NULL, bucket TEXT NOT NULL, new_count INTEGER NOT NULL,
                    run_count INTEGER NOT NULL, successful_runs INTEGER NOT NULL,
                    PRIMARY KEY(hashtag_id,grain,bucket));
                CREATE TABLE IF NOT EXISTS instagram_state (
                    user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(user_id,key));
                CREATE TABLE IF NOT EXISTS instagram_leases (
                    user_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS instagram_search_window ON instagram_searches(user_id,searched_at);
                CREATE INDEX IF NOT EXISTS instagram_run_tag ON instagram_runs(hashtag_id,id);
            

                CREATE TABLE IF NOT EXISTS instagram_caption_analysis (
                    media_id TEXT PRIMARY KEY REFERENCES instagram_media(id),
                    caption_state TEXT NOT NULL, caption_hash TEXT,
                    extractor_version TEXT NOT NULL, analyzed_at TEXT NOT NULL,
                    hashtag_count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS instagram_caption_tags (
                    media_id TEXT NOT NULL REFERENCES instagram_media(id),
                    normalized_name TEXT NOT NULL, original_names TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(media_id,normalized_name));
                CREATE TABLE IF NOT EXISTS instagram_analysis_observations (
                    id INTEGER PRIMARY KEY, hashtag_id INTEGER NOT NULL REFERENCES instagram_hashtags(id),
                    media_id TEXT NOT NULL REFERENCES instagram_media(id),
                    run_id INTEGER REFERENCES instagram_runs(id), observed_at TEXT NOT NULL,
                    caption_state TEXT NOT NULL, caption_hash TEXT, hashtag_count INTEGER NOT NULL,
                    extractor_version TEXT NOT NULL, evidence_kind TEXT NOT NULL,
                    UNIQUE(hashtag_id,media_id,observed_at));
                CREATE TABLE IF NOT EXISTS instagram_observation_tags (
                    observation_id INTEGER NOT NULL REFERENCES instagram_analysis_observations(id),
                    normalized_name TEXT NOT NULL, original_names TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(observation_id,normalized_name));
                CREATE INDEX IF NOT EXISTS instagram_analysis_window ON instagram_analysis_observations(hashtag_id,observed_at,media_id);
                CREATE INDEX IF NOT EXISTS instagram_observed_tag ON instagram_observation_tags(normalized_name,observation_id);
            

                CREATE TABLE IF NOT EXISTS naver_targets (
                    candidate_id INTEGER PRIMARY KEY REFERENCES candidates(item_id),
                    topic TEXT NOT NULL, keywords_json TEXT NOT NULL,
                    last_attempt_at TEXT, last_success_at TEXT, status TEXT NOT NULL DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS naver_calls (
                    id INTEGER PRIMARY KEY, day TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    requested_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                    http_status INTEGER, request_json TEXT NOT NULL, candidate_ids TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS naver_calls_day ON naver_calls(day);
                CREATE TABLE IF NOT EXISTS naver_guards (
                    fingerprint TEXT PRIMARY KEY, owner TEXT NOT NULL, until_epoch REAL NOT NULL);
            

                CREATE TABLE IF NOT EXISTS youtube_foods (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
                CREATE TABLE IF NOT EXISTS youtube_candidate_foods (
                    candidate_id INTEGER PRIMARY KEY REFERENCES candidates(item_id) ON DELETE CASCADE,
                    food_id INTEGER NOT NULL REFERENCES youtube_foods(id));
                CREATE TABLE IF NOT EXISTS youtube_queries (
                    id INTEGER PRIMARY KEY, query TEXT NOT NULL, mode TEXT NOT NULL,
                    food_id INTEGER REFERENCES youtube_foods(id), active INTEGER NOT NULL DEFAULT 1,
                    after_at TEXT, before_at TEXT, page_token TEXT, token_at TEXT, last_attempt TEXT,
                    UNIQUE(query,mode));
                CREATE TABLE IF NOT EXISTS youtube_videos (
                    video_id TEXT PRIMARY KEY, payload TEXT NOT NULL, collected_at TEXT NOT NULL,
                    refreshed_at TEXT NOT NULL, expires_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS youtube_matches (
                    query_id INTEGER REFERENCES youtube_queries(id) ON DELETE CASCADE,
                    video_id TEXT REFERENCES youtube_videos(video_id) ON DELETE CASCADE,
                    matched_at TEXT NOT NULL, PRIMARY KEY(query_id,video_id));
                CREATE TABLE IF NOT EXISTS youtube_calls (
                    id INTEGER PRIMARY KEY, day TEXT NOT NULL, endpoint TEXT NOT NULL,
                    query_id INTEGER REFERENCES youtube_queries(id), started_at TEXT NOT NULL,
                    finished_at TEXT, status TEXT NOT NULL, http_status INTEGER);
                CREATE INDEX IF NOT EXISTS youtube_calls_day ON youtube_calls(day,endpoint);
                CREATE INDEX IF NOT EXISTS youtube_expiry ON youtube_videos(expires_at);
                CREATE TABLE IF NOT EXISTS youtube_state (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS youtube_runs (
                    id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    trigger TEXT NOT NULL, origin TEXT NOT NULL, status TEXT NOT NULL,
                    result_json TEXT);
                CREATE TABLE IF NOT EXISTS youtube_video_provenance (
                    video_id TEXT PRIMARY KEY REFERENCES youtube_videos(video_id) ON DELETE CASCADE,
                    origin TEXT NOT NULL, verified_at TEXT NOT NULL, run_id TEXT);
'''

LEGACY_ADDITIONS = {'candidates': {'entity_type': "TEXT NOT NULL DEFAULT 'UNKNOWN'",
                'rule_score': 'INTEGER',
                'score_basis': 'TEXT',
                'evidence_news': "TEXT NOT NULL DEFAULT '[]'",
                'classified_at': 'TEXT'},
 'runs': {'food_count': 'INTEGER',
          'review_count': 'INTEGER',
          'new_food_count': 'INTEGER',
          'new_review_count': 'INTEGER',
          'classifier_version': 'TEXT'},
 'observations': {'classification_json': 'TEXT'},
 'instagram_media': {'caption': 'TEXT', 'caption_captured_at': 'TEXT', 'caption_purged_at': 'TEXT'},
 'youtube_calls': {'origin': "TEXT NOT NULL DEFAULT 'legacy_unknown'",
                   'run_id': 'TEXT',
                   'result_count': 'INTEGER'}}
