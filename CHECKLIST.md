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

**`extract/` is fully closed**, including the live-run finding below. Codex is clear to start `score/` per the Build split order.

**Live-run finding #1 (2026-09-20) — resolved.** Running `extract/run.py` for real against Mark's mailbox failed after 400 messages with `HttpError 403`, `reason: 'rateLimitExceeded'` — outside `_is_transient()`'s original 429/5xx-only check. Fix: `_is_transient()` now inspects the actual 403 response body (`error.errors[].reason`/`domain`) and only retries when it's genuinely a rate-limit 403 (`rateLimitExceeded`/`usageLimits`), correctly leaving a real permissions-denied 403 non-retryable — with a safe fallback to non-transient on any parse failure. Independently verified: constructed both a quota-shaped and a genuine-forbidden-shaped 403 and confirmed only the former is treated as transient; directly re-ran the dedicated regression test. 20/20 full suite passing.

**Live-run finding #2 (2026-09-20) — reopening again, different root cause this time.** Resumed extraction with finding #1's fix in place; it failed **again**, at the **same exact point** (400 messages, 9th HTTP batch), with the **identical** `rateLimitExceeded` 403. This time it's not a classification bug — I constructed a real `googleapiclient.errors.HttpError` matching the exact failure shape and confirmed `_is_transient()` correctly returns `True` for it. The problem is retry *budget*: `max_attempts=5` with exponential backoff capped at 32s only adds up to roughly 15–19 seconds of total sleep across all attempts, which isn't enough to outlast Gmail's per-minute quota window — all 5 attempts get exhausted while the bucket is still empty, and the final attempt re-raises as designed (by `attempt == max_attempts - 1`), regardless of transience. Reproducing at the *exact same point* twice in a row means this is a consistent, deterministic resource constraint, not a fluke — the original spec's assumption that "250 quota units/user/second... keeps this well under budget even at 412k messages" doesn't hold up against Mark's real account in practice. **Recommend proactive pacing** (a small delay between successful HTTP batch calls, not just reactive backoff after a failure) as the more robust fix, rather than just further widening the retry budget and hoping it outlasts a window whose exact size isn't confirmed. No data lost again — still 400 messages stored, no checkpoint written. Not resuming again until this lands, to avoid repeatedly burning time and API calls hitting the same wall.

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
- [ ] Codex: implemented + unit tests (in progress)
- [ ] Claude: reviewed (in progress)
- [ ] Mark: accepted

**In-progress checkpoints, resolved:**
- Approval/decision, restore-window extension, `delete_pending` computation, and second-confirmation endpoints reviewed early — correct, plus a server-side hard block on approving Business-critical groups beyond the spec's UI-only requirement.
- Cross-process hash risk fixed: `confirmation_snapshot_hash()` extracted into `db/snapshots.py` as the single canonical implementation, imported by review-ui; `execute/` (Gate 5) will import and call the same function rather than risk an independently-reimplemented, possibly-mismatched hash. Independently re-verified the extracted function's output byte-for-byte against a hand-constructed expected value, including correct filtering to `status='labeled'` only and correct sorting.
- `GmailAPICreds.txt` (a plaintext copy of the OAuth client secret) — recommended deletion; it's at least now correctly gitignored after a rename.
- No `review-ui/` tests yet — expected at this checkpoint stage.

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
