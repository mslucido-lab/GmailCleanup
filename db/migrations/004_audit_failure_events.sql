-- Persist concise executor failure reasons without overloading a success event.
ALTER TABLE audit_log RENAME TO audit_log_old;

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    event TEXT NOT NULL CHECK (event IN (
        'approved',
        'labeled',
        'restored',
        'window_extended',
        'permanent_delete_confirmed',
        'moved_to_trash',
        'failed'
    )),
    message_count INTEGER NOT NULL CHECK (message_count >= 0),
    timestamp INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);

INSERT INTO audit_log (id,batch_id,event,message_count,timestamp,note)
SELECT id,batch_id,event,message_count,timestamp,note FROM audit_log_old;

DROP TABLE audit_log_old;
CREATE INDEX idx_audit_log_batch_timestamp ON audit_log(batch_id, timestamp);
