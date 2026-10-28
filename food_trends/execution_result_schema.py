"""B3 result coverage, recorded independently of execution status. No provider body."""
SQL = '''
ALTER TABLE execution_runs ADD COLUMN result_coverage TEXT
 CHECK(result_coverage IS NULL OR result_coverage IN ('COMPLETE','PARTIAL','LIMITED','NONE'));
ALTER TABLE execution_runs ADD COLUMN committed_pages INTEGER CHECK(committed_pages IS NULL OR committed_pages>=0);
'''


def apply(db):
    for statement in SQL.split(';'):
        if statement.strip():
            db.execute(statement)
