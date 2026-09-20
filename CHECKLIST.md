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
- [x] Mark: accepted

Found by Codex during `extract/` planning: `score/` runs as a separate process after extraction, so the Sent-folder pass's two-way-correspondence data has to be a persisted table, not an in-memory set. v8 described the mechanism but never added the table — fixed in spec v9.

### 2. `extract/` — Gmail metadata pull
- [x] Codex: applied v10 and re-submitted — metadata-only extraction, migration 003, malformed-sender skip/logging, scoped Retry-After-aware retries, and fixture tests (10/10 passing)
- [x] Claude: reviewed — all four v10 items correctly and thoroughly implemented; independently re-ran the full suite (10/10 pass, including the migration-shape assertions confirming attachment columns are actually gone after a fresh `migrate()`); no findings
- [x] Mark: accepted

**`extract/` is fully closed** (see the live-run finding below, discovered afterward against Mark's real mailbox). Codex is clear to start `score/` per the Build split order.

**Live-run finding (2026-09-20) — reopening for a fix:** running `extract/run.py` for real against Mark's mailbox failed after 400 messages with `HttpError 403` from Gmail, `reason: 'rateLimitExceeded'` (quota metric `Total Query Cost`, `Units per minute per user`). `Extractor._is_transient()` in `extract/runner.py` only classifies HTTP `429` and `5xx` as retryable — a `403` with a rate-limit reason falls outside that check and gets re-raised immediately instead of retried with backoff. Not caught by the fixture-based tests, since nothing in the test suite exercises real Gmail quota behavior. No data was lost (400 messages are stored; `run_state` has no checkpoint yet since the first page didn't finish, so a resume will safely re-fetch and idempotently overwrite the same first ~500 ids) — but resuming before this is fixed will very likely hit the same 403 again almost immediately. **Fix needed:** `_is_transient()` should also treat HTTP `403` responses carrying `reason: 'rateLimitExceeded'` (or `domain: 'usageLimits'`) as retryable, using the same backoff path as 429/5xx.

**v10 decision recorded** (confirmed by Codex against Google's own Gmail API docs, formalized by Claude in spec v10):
1. **Remove `has_attachment`, `pct_attachments`, and `attachment_penalty` from v1 entirely** — not shown as "unavailable," fully removed from schema (`db/migrations/003_drop_attachment_columns.sql`), scoring code, and the review-ui score-breakdown row (five factors, not six). `format=metadata` cannot return the MIME structure attachment detection needs, and neither `format=full` (body access) nor `q=has:attachment` search (blocked under `gmail.metadata`) are acceptable workarounds — see spec's Data schema and Scoring model sections.
2. **Malformed `From` header:** log and skip that one message; do not abort the run or corrupt the page checkpoint.
3. **Retry logic:** scope retries to `429`/`5xx`/transport failures only, honor `Retry-After` when present, re-raise programming/validation errors immediately.
4. **`config/settings.yaml`:** add `msn.com` and `gmx.com` to `CONSUMER_DOMAINS` (the spec's own list already had them — the config file just needs to match).

### 3. `score/` — categorization + scoring
- [x] Codex: fixed batching, JSON-fence stripping, model identifier, and the `brand_source` regression, with a dedicated mixed consumer/non-consumer regression test (19/19 full suite passing, independently re-verified)
- [x] Claude: reviewed — all findings resolved. `brand_source` fix independently re-confirmed by direct reproduction (consumer-domain sender correctly shows `not_applicable`, actually-classified sender correctly shows `llm`). Also recorded a spec decision: amended v11 to explicitly state Stage 2 is reserved for non-consumer domains (a consumer-domain fallback sender's safe default is already correct; sending it to a third party for a second opinion costs privacy for no benefit) — this matches what was already implemented, so no further code change needed.
- [x] Mark: accepted

**`score/` is fully closed.** Codex is clear to start `review-ui/` per the Build split order.

Stage 1 precedence, the category/brand independence logic, approved-group immutability, the five-factor score, three-way address/domain/brand grouping (static + discovered ESP), and metadata-only privacy in the LLM payload are all correct and well-tested.

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

- Technical spec: **v11**, committed to `main`.
- No open spec-level decisions as of v11.

## Notes

- `config.toml` in the repo root is unrelated to this project (Codex CLI's own global config) — ignore it here.
