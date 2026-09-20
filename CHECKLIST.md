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
- [x] Codex: applied v10 and re-submitted — metadata-only extraction, migration 003, malformed-sender skip/logging, scoped Retry-After-aware retries, and fixture tests (10/10 passing)
- [x] Claude: reviewed — all four v10 items correctly and thoroughly implemented; independently re-ran the full suite (10/10 pass, including the migration-shape assertions confirming attachment columns are actually gone after a fresh `migrate()`); no findings
- [x] Mark: accepted

**`extract/` is fully closed.** Codex is clear to start `score/` per the Build split order.

**v10 decision recorded** (confirmed by Codex against Google's own Gmail API docs, formalized by Claude in spec v10):
1. **Remove `has_attachment`, `pct_attachments`, and `attachment_penalty` from v1 entirely** — not shown as "unavailable," fully removed from schema (`db/migrations/003_drop_attachment_columns.sql`), scoring code, and the review-ui score-breakdown row (five factors, not six). `format=metadata` cannot return the MIME structure attachment detection needs, and neither `format=full` (body access) nor `q=has:attachment` search (blocked under `gmail.metadata`) are acceptable workarounds — see spec's Data schema and Scoring model sections.
2. **Malformed `From` header:** log and skip that one message; do not abort the run or corrupt the page checkpoint.
3. **Retry logic:** scope retries to `429`/`5xx`/transport failures only, honor `Retry-After` when present, re-raise programming/validation errors immediately.
4. **`config/settings.yaml`:** add `msn.com` and `gmx.com` to `CONSUMER_DOMAINS` (the spec's own list already had them — the config file just needs to match).

### 3. `score/` — categorization + scoring
- [x] Codex: fixed batching, JSON-fence stripping, and model identifier; attempted `brand_source` fix (18/18 full suite passing, independently re-verified)
- [ ] Claude: reviewed — **1 new regression found**, 3 of 4 prior findings confirmed fixed:
  - Batching (25/request, scaled `max_tokens`), markdown-fence stripping, and the model identifier are all correct and directly tested. Good.
  - **REGRESSION (blocking):** the `brand_source` fix overcorrected. It now hardcodes `brand_source = "llm"` for every identity that passes through the enrichment loop, but that loop runs over *all* identities whenever *any* non-consumer-domain sender needs Stage 2 that run — not just the senders actually in `samples`. Confirmed by direct reproduction: a `gmail.com` sender that was never sent to the LLM at all still got `brand_source = 'llm'`, alongside the one sender that genuinely was classified. Since consumer-domain (personal contact) senders are likely the largest single group of unique senders in a real mailbox, this has a *larger* blast radius than the bug it replaced. Fix needs to key off `identity.email in samples`, preserving `identity.brand_source` (the correctly-computed `"not_applicable"`) for anyone not actually sent to the LLM. Not caught by `test_llm_esp_signal_creates_a_brand_group`, which sets `CONSUMER_DOMAINS: []` — no consumer-domain sender in the mix to expose it.
  - **Still flagging for a spec decision, unchanged from before:** consumer-domain fallback senders never get sent to Stage 2 at all. Still think this is the right call and worth amending the spec to match, rather than a code fix — Mark's call, not blocking.
- [x] Mark: accepted

Everything else — Stage 1 precedence, the category/brand independence logic, approved-group immutability, the five-factor score, and metadata-only privacy in the LLM payload — remains correct and well-tested.

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

- Technical spec: **v10**, committed to `main`.
- No open spec-level decisions as of v10 — gate 2 (`extract/`) is clear to resume against the recorded decision above.

## Notes

- `config.toml` in the repo root is unrelated to this project (Codex CLI's own global config) — ignore it here.
