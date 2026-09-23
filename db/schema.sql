-- Gmail Cleanup's canonical initial SQLite schema (migration version 1).
-- Later changes are append-only files in db/migrations/NNN_description.sql.

CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    sender_domain TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    date INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    is_read INTEGER NOT NULL CHECK (is_read IN (0, 1)),
    is_starred INTEGER NOT NULL CHECK (is_starred IN (0, 1)),
    -- Removed from the current schema by migration 003; retained here because
    -- schema.sql is migration 001 and must remain historically replayable.
    has_attachment INTEGER NOT NULL CHECK (has_attachment IN (0, 1)),
    has_list_unsubscribe INTEGER NOT NULL CHECK (has_list_unsubscribe IN (0, 1)),
    labels TEXT NOT NULL DEFAULT '[]',
    category TEXT CHECK (category IS NULL OR category IN (
        'Marketing / promotional',
        'Automated notifications',
        'Newsletters / subscriptions',
        'Transactional / receipts',
        'Business-critical',
        'Personal correspondence'
    ))
);

CREATE INDEX idx_messages_sender_email ON messages(sender_email);
CREATE INDEX idx_messages_date ON messages(date DESC);

CREATE TABLE sender_identity (
    sender_email TEXT PRIMARY KEY,
    sender_domain TEXT NOT NULL,
    category TEXT NOT NULL CHECK (category IN (
        'Marketing / promotional',
        'Automated notifications',
        'Newsletters / subscriptions',
        'Transactional / receipts',
        'Business-critical',
        'Personal correspondence'
    )),
    category_source TEXT NOT NULL CHECK (category_source IN ('rule', 'llm', 'fallback')),
    inferred_brand TEXT,
    is_esp_routed INTEGER CHECK (is_esp_routed IN (0, 1)),
    brand_source TEXT NOT NULL CHECK (brand_source IN ('llm', 'not_applicable', 'skipped_offline')),
    rationale TEXT NOT NULL DEFAULT '',
    model_version TEXT,
    classified_at INTEGER NOT NULL
);

CREATE INDEX idx_sender_identity_domain ON sender_identity(sender_domain);

CREATE TABLE sender_groups (
    group_key TEXT PRIMARY KEY,
    group_type TEXT NOT NULL CHECK (group_type IN ('address', 'domain', 'brand')),
    domains TEXT NOT NULL DEFAULT '[]',
    category TEXT NOT NULL CHECK (category IN (
        'Marketing / promotional',
        'Automated notifications',
        'Newsletters / subscriptions',
        'Transactional / receipts',
        'Business-critical',
        'Personal correspondence'
    )),
    message_count INTEGER NOT NULL CHECK (message_count >= 0),
    total_size_bytes INTEGER NOT NULL CHECK (total_size_bytes >= 0),
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    avg_date INTEGER NOT NULL,
    pct_unread REAL NOT NULL CHECK (pct_unread >= 0 AND pct_unread <= 1),
    -- Removed from the current schema by migration 003; see comment above.
    pct_attachments REAL NOT NULL CHECK (pct_attachments >= 0 AND pct_attachments <= 1),
    pct_starred REAL NOT NULL CHECK (pct_starred >= 0 AND pct_starred <= 1),
    has_protected_label INTEGER NOT NULL CHECK (has_protected_label IN (0, 1)),
    delete_safety_score REAL NOT NULL CHECK (delete_safety_score >= 0 AND delete_safety_score <= 100),
    approval_status TEXT NOT NULL DEFAULT 'pending' CHECK (approval_status IN ('pending', 'approved', 'rejected', 'skipped'))
);

CREATE INDEX idx_sender_groups_review_order ON sender_groups(approval_status, delete_safety_score DESC);

CREATE TABLE group_members (
    group_key TEXT NOT NULL REFERENCES sender_groups(group_key),
    sender_email TEXT NOT NULL REFERENCES sender_identity(sender_email),
    PRIMARY KEY (group_key, sender_email)
);

CREATE INDEX idx_group_members_sender_email ON group_members(sender_email);

CREATE TABLE label_map (
    label_name TEXT PRIMARY KEY,
    label_id TEXT NOT NULL UNIQUE,
    resolved_at INTEGER NOT NULL
);

CREATE TABLE run_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_page_token TEXT,
    message_count INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    updated_at INTEGER NOT NULL
);

CREATE TABLE batches (
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

CREATE INDEX idx_batches_executor ON batches(status, approved_at);
CREATE INDEX idx_batches_group_key ON batches(group_key);

CREATE TABLE batch_messages (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    message_id TEXT NOT NULL REFERENCES messages(message_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'labeled', 'excluded_protected', 'trashed')),
    original_labels TEXT,
    PRIMARY KEY (batch_id, message_id)
);

CREATE INDEX idx_batch_messages_status ON batch_messages(batch_id, status);

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

CREATE INDEX idx_audit_log_batch_timestamp ON audit_log(batch_id, timestamp);
