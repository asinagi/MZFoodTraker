"""B3 single-writer ownership and expiring item receipts. No provider response body."""
SQL = '''
CREATE TABLE execution_source_ownership (
 source_id TEXT NOT NULL, account_scope TEXT NOT NULL,
 owner TEXT NOT NULL CHECK(owner IN ('LEGACY','B3')), approved_by TEXT NOT NULL, evidence TEXT NOT NULL,
 updated_at TEXT NOT NULL, PRIMARY KEY(source_id,account_scope));
CREATE TABLE execution_item_receipts (
 job_id TEXT NOT NULL REFERENCES execution_jobs(job_id), item_id TEXT NOT NULL,
 observed_at TEXT NOT NULL, expires_at TEXT NOT NULL, PRIMARY KEY(job_id,item_id));
CREATE INDEX execution_receipt_expiry ON execution_item_receipts(expires_at);
'''


def apply(db):
    for statement in SQL.split(';'):
        if statement.strip():
            db.execute(statement)
