"""Single local writer ownership. Transferring it is a deployment operation, never a GET."""
from .food_service import FoodError, text
from .tracking_service import stamp


def legacy_allowed(storage, source, scope='local-default'):
    with storage.reader() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='execution_source_ownership'").fetchone():
            return True
        row=db.execute('SELECT owner FROM execution_source_ownership WHERE source_id=? AND account_scope=?',(source,scope)).fetchone()
        return not row or row[0]=='LEGACY'


def transfer(execution, source, scope, *, approved_by, evidence, legacy_stopped=False):
    """Operator-only Python interface; caller must stop legacy processes in an approved window."""
    if legacy_stopped is not True:
        raise FoodError('기존 writer 중지와 별도 운영 전환 승인이 필요합니다.',409)
    account=execution.tracking.account(source,scope)
    if account.mode!='live' or account.blockers() or not execution.settings.live_enabled:
        raise FoodError('검증된 Live 계정과 명시적 실행 설정이 필요합니다.',403)
    with execution.storage.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''INSERT INTO execution_source_ownership VALUES (?,?,'B3',?,?,?) ON CONFLICT(source_id,account_scope)
            DO UPDATE SET owner='B3',approved_by=excluded.approved_by,evidence=excluded.evidence,updated_at=excluded.updated_at''',
            (source,scope,text(approved_by,100),text(evidence,2000),stamp(execution.now())))
