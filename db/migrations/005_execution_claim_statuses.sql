-- migrate: foreign_keys_off
-- Transient claims prevent concurrent restore and Trash Gmail writes.
CREATE TABLE batches_new (
    batch_id TEXT PRIMARY KEY,
    group_key TEXT NOT NULL REFERENCES sender_groups(group_key),
    status TEXT NOT NULL CHECK (status IN ('approved', 'labeling', 'labeled', 'restore_window', 'restoring', 'trashing', 'restored', 'trashed', 'failed')),
    approved_at INTEGER NOT NULL,
    labeled_at INTEGER,
    restore_deadline INTEGER,
    restored_at INTEGER,
    permanent_delete_confirmed_at INTEGER,
    confirmation_snapshot_hash TEXT,
    trashed_at INTEGER,
    message_count INTEGER NOT NULL CHECK (message_count >= 0),
    total_size_bytes INTEGER NOT NULL CHECK (total_size_bytes >= 0),
    window_extensions INTEGER NOT NULL DEFAULT 0 CHECK (window_extensions >= 0)
);

INSERT INTO batches_new (
    batch_id, group_key, status, approved_at, labeled_at, restore_deadline,
    restored_at, permanent_delete_confirmed_at, confirmation_snapshot_hash,
    trashed_at, message_count, total_size_bytes, window_extensions
)
SELECT
    batch_id, group_key, status, approved_at, labeled_at, restore_deadline,
    restored_at, permanent_delete_confirmed_at, confirmation_snapshot_hash,
    trashed_at, message_count, total_size_bytes, window_extensions
FROM batches;

DROP TABLE batches;
ALTER TABLE batches_new RENAME TO batches;
CREATE INDEX idx_batches_executor ON batches(status, approved_at);
CREATE INDEX idx_batches_group_key ON batches(group_key);
