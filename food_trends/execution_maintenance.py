"""Local expiry cleanup. Keep checkpoint tombstones to prohibit expired replay."""
from datetime import datetime,timezone


def purge(storage, at=None):
    now=(at or datetime.now(timezone.utc)).isoformat()
    with storage.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('PRAGMA secure_delete=ON')
        receipts=db.execute('DELETE FROM execution_item_receipts WHERE expires_at<=?',(now,)).rowcount
        checkpoints=db.execute('''UPDATE execution_checkpoints SET state_json='{"expired":true}'
            WHERE expires_at<=? AND state_json!='{"expired":true}' ''',(now,)).rowcount
        return {'expired_item_receipts':receipts,'expired_checkpoints':checkpoints,'http_requests':0}
