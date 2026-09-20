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
- [x] Codex: implemented deterministic + opt-in LLM categorization, grouping, five-factor scoring, and SQLite integration coverage (16/16 full suite passing, independently re-verified)
- [ ] Claude: reviewed — **1 high-severity finding open**, 2 moderate, 1 trivial (blocking, since the trivial one alone makes the LLM path non-functional as configured):
  - **HIGH:** `AnthropicClassifier.classify()` sends every sample sender in one API call with no batching (spec calls for 20-50/request) and a fixed `max_tokens=4096` — on a real mailbox with hundreds+ of non-consumer-domain senders, the response will truncate mid-JSON-array and `json.loads` will throw. Not covered by any test — `test_score.py` only exercises a `FakeLlm` stub, never the real HTTP path in `score/llm.py`.
  - MEDIUM: `brand_source` is set to `"skipped_offline"` whenever the LLM returns no brand, even when Stage 2 genuinely ran for that sender (e.g. a non-ESP-routed domain, a normal outcome) — conflates "never ran" with "ran, found nothing." Doesn't affect grouping (`is_esp_routed` is read directly), but misleads the audit trail.
  - MEDIUM: no defense against the model wrapping its JSON response in markdown fences or similar — no tool-use/structured-output enforcement, just a text instruction.
  - LOW (but currently fully blocking): `ANTHROPIC_MODEL: claude-haiku` in `config/settings.yaml` isn't a complete, real model identifier — every Stage 2 call would fail outright before the batching issue even has a chance to matter.
  - **Not a finding, but flagging for a spec decision:** consumer-domain senders that land in "fallback" never get sent to Stage 2 (only non-consumer-domain senders are sampled). This deviates from the spec's literal wording but is arguably the right call — an ambiguous personal address is already correctly defaulting to Personal correspondence, so there's little to gain and a privacy cost to sending it to a third party anyway. Leaning toward amending the spec to match rather than asking for a code fix — Mark's call.
- [x] Mark: accepted

Everything else — Stage 1 precedence, the category/brand independence logic (subtle and done exactly right), approved-group immutability, the five-factor score, and metadata-only privacy in the LLM payload — is correct and well-tested. Both findings from the earlier checkpoint (ESP-domain signal in Marketing classification, three-way address/domain/brand grouping) are fixed and directly tested.

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
