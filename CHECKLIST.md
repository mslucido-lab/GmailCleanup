# Gmail Cleanup — Build Checklist

Tracks the milestones and review gates defined in the spec's **Build split** section. Each milestone closes only when all three boxes are checked — see the spec for what each role actually verifies (Codex: unit tests; Claude: supplemental/safety-rail review; Mark: manual acceptance as product owner).

Update this file as part of closing each gate — whoever closes a box edits this file and says so, so it stays a reliable source of truth rather than a snapshot that rots.

## Milestones

### 1. `db/` — schema, migrations, connection config
- [x] Codex: implemented (`schema.sql`, `migrate.py`, `connection.py`) + unit tests (4/4 passing)
- [x] Claude: reviewed — schema matches spec v8 exactly across all 9 tables; the `messages.category` finding is fixed and independently re-verified (4/4 tests pass locally)
- [x] Mark: accepted / **schema locked** — reviewed visually in DB Browser for SQLite

### 1a. Schema addendum — `sent_recipients` table (migration 002)
- [x] Codex: implemented `sent_recipients` as `db/migrations/002_sent_recipients.sql`, applied by the shared runner, with idempotency coverage in unit tests
- [x] Claude: reviewed — matches spec exactly, no issues
- [ ] Mark: accepted

Found by Codex during `extract/` planning: `score/` runs as a separate process after extraction, so the Sent-folder pass's two-way-correspondence data has to be a persisted table, not an in-memory set. v8 described the mechanism but never added the table — fixed in spec v9.

### 2. `extract/` — Gmail metadata pull
- [x] Codex: implemented (`extract/` metadata-only runner, Google HTTP-batch adapter, OAuth CLI) + fixture-based unit tests (8/8 full suite passing)
- [ ] Claude: reviewed — **1 high-severity finding open**, 2 moderate, 1 trivial:
  - **HIGH:** `has_attachment()` recurses into `payload.parts`, but `format=metadata` (used throughout `gateway.py`) doesn't return that field at all — every message will silently score `has_attachment=0` regardless of reality. Needs verification against live Gmail, then likely a scoring-model decision (drop `attachment_penalty` for v1 — `gmail.metadata` can't search `has:attachment`, and `format=full` would mean downloading bodies).
  - MEDIUM: an unparseable `From` header aborts the entire extraction run rather than skipping the one message.
  - MEDIUM: retry logic catches bare `Exception` and doesn't honor `Retry-After`, per spec.
  - LOW: `config/settings.yaml`'s `CONSUMER_DOMAINS` is missing `msn.com`/`gmx.com` from the spec list.
- [ ] Mark: accepted

### 3. `score/` — categorization + scoring
- [ ] Codex: implemented + unit tests
- [ ] Claude: reviewed
- [ ] Mark: accepted

### 4. `review-ui/` — local backend + frontend
- [ ] Codex: implemented + unit tests
- [ ] Claude: reviewed
- [ ] Mark: accepted

### 5. `execute/` — preflight, archive, restore, Trash flow
- [ ] Codex: implemented + unit tests
- [ ] Claude: reviewed
- [ ] Mark: accepted (dry-run path)
- [ ] **Mark: live Gmail action explicitly approved** — the one gate that can't be implicit

## Spec status

- Technical spec: **v9**, committed to `main`.
- Open: whether `attachment_penalty` survives v1's scoring model, pending the `has_attachment` finding under gate 2.

## Notes

- `config.toml` in the repo root is unrelated to this project (Codex CLI's own global config) — ignore it here.
