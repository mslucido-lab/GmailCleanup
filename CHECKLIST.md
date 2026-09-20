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

**Minor cleanup noted, not blocking:** four pre-existing tests in `tests/test_extract.py` instantiate `Extractor` without overriding `batch_interval_seconds`/`sleep`, so they now incur real ~1.1s sleeps per batch instead of running at unit-test speed (suite runtime went from ~0.2s to ~5.8s). Worth passing `batch_interval_seconds=0` in those older tests since they aren't testing pacing behavior.

### 2. `extract/` — Gmail metadata pull
- [x] Codex: applied v10 and re-submitted — metadata-only extraction, migration 003, malformed-sender skip/logging, scoped Retry-After-aware retries, and fixture tests (10/10 passing)
- [x] Claude: reviewed — all four v10 items correctly and thoroughly implemented; independently re-ran the full suite (10/10 pass, including the migration-shape assertions confirming attachment columns are actually gone after a fresh `migrate()`); no findings
- [x] Mark: accepted

**`extract/` is fully closed**, including the live-run finding below. Codex is clear to start `score/` per the Build split order.

**Live-run finding #1 (2026-09-20) — resolved.** Running `extract/run.py` for real against Mark's mailbox failed after 400 messages with `HttpError 403`, `reason: 'rateLimitExceeded'` — outside `_is_transient()`'s original 429/5xx-only check. Fix: `_is_transient()` now inspects the actual 403 response body (`error.errors[].reason`/`domain`) and only retries when it's genuinely a rate-limit 403 (`rateLimitExceeded`/`usageLimits`), correctly leaving a real permissions-denied 403 non-retryable — with a safe fallback to non-transient on any parse failure. Independently verified: constructed both a quota-shaped and a genuine-forbidden-shaped 403 and confirmed only the former is treated as transient; directly re-ran the dedicated regression test. 20/20 full suite passing.

**Live-run finding #2 (2026-09-20) — resolved.** Resumed extraction with finding #1's fix in place; it failed **again**, at the **same exact point** (400 messages, 9th HTTP batch), with the **identical** `rateLimitExceeded` 403. This time it's not a classification bug — I constructed a real `googleapiclient.errors.HttpError` matching the exact failure shape and confirmed `_is_transient()` correctly returns `True` for it. The problem was retry *budget*: `max_attempts=5` with exponential backoff capped at 32s only adds up to roughly 15–19 seconds of total sleep across all attempts, not enough to outlast Gmail's per-minute quota window. Fixed with proactive pacing (`GMAIL_METADATA_BATCH_INTERVAL_SECONDS`, default 1.1s, applied after every successful HTTP batch call in both the main and Sent passes) rather than just widening the retry budget. 21/21 tests passing, including a new pacing regression test.

**Live-run finding #3 (2026-09-20) — partially resolved; see finding #4.** Resumed extraction a third time with 1.1s pacing in place; it failed **again, at the exact same point** (400 messages), which motivated checking the actual configured limit instead of continuing to guess. Mark pulled it from Google Cloud Console (APIs & Services → Gmail API → Quotas): **"Units per minute per user" = 6,000** — this project's active limit has been reduced from the "previous quota" of 15,000 shown in the same console, which is what the original spec's "250 units/second sustained" assumption (≈15,000/minute) was implicitly based on.

At ~5 quota units per `messages.get` (Gmail's documented cost) and 50 messages/batch (~250 units/batch), the exact ceiling is 6,000 ÷ 250 = 24 batches/minute (2.5s/batch); targeting ~19–20 batches/minute leaves headroom for `messages.list` calls and retries sharing the same bucket. `GMAIL_METADATA_BATCH_INTERVAL_SECONDS` updated to `3.1` in both `config/settings.yaml` and the `Extractor` constructor's fallback default. 21/21 tests passing, independently re-verified. **Known consequence, accepted:** at ~20 batches/minute, the full 412,847-message main pass will take roughly **7 hours** — Mark chose to run it overnight rather than pursue a Google quota increase (the row is marked "Adjustable: Yes" if that's ever wanted instead).

**Live-run finding #4 (2026-09-20) — resolved (cooldown theory ruled out, see finding #5).** Started the overnight run with 3.1s pacing; it failed **again**, this time at 401 messages (one more than before, still essentially the same point) with the identical `rateLimitExceeded` 403. At the time this looked like residual quota carrying over from the day's earlier attempts, since 8 successful batches only take ~25–40 seconds of wall time — well under a minute. Recommended a real cooldown before the next attempt to rule that in or out cleanly.

**Live-run finding #5 (2026-09-20) — resolved.** Resumed after a genuine 2+ hour cooldown (not the 10–15 minutes recommended — a much cleaner test). It failed **again, at essentially the same point** (408 messages, ~8 batches) with the identical error. This rules out residual carryover entirely and points to the actual root cause: **the per-batch quota cost assumption (~5 units/message, ~250/batch) is wrong** — real cost is roughly 3x higher. The tell: both 1.1s and 3.1s pacing failed at the *same batch count* regardless of spacing, because 8–9 batches at either interval still complete within the same first 60-second window (8 × 3.1s ≈ 25s, nowhere near a full minute) — so pacing was never actually slow enough to *spread* consumption across a full minute, just to front-load the same amount slightly slower. Working backward from what's empirically observed (8 batches reliably succeed, the 9th reliably fails) rather than the documented-but-apparently-wrong per-message figure: the real ceiling is closer to **~8 batches/minute**, not ~24/minute. **Recommend `GMAIL_METADATA_BATCH_INTERVAL_SECONDS` = ~10s** (targeting ~6 batches/minute, safely under the observed ~8/minute breaking point) — roughly 3x more conservative again than the current `3.1`. **Known consequence, significant:** at 6 batches/minute, the full 412,847-message main pass becomes **~23 hours**, not the ~7 hours previously estimated. Mark chose to proceed at the slower, correct pacing rather than request a Google quota increase, using the extraction pipeline's existing per-page checkpoint (`run_state`, written every 500 messages/10 batches) to run this across multiple paused/resumed sessions rather than one continuous ~23-hour process — no new checkpointing code needed, since every attempt so far has failed *within* the first page and never actually exercised this resumability yet. No data lost — 408 messages stored, no checkpoint written.

`GMAIL_METADATA_BATCH_INTERVAL_SECONDS` updated to `10.0` in `config/settings.yaml`, `extract/run.py`'s fallback default, and `extract/runner.py`'s constructor default, plus the pacing regression test's asserted value for consistency. Applied directly (Codex is focused on `execute/`/Gate 5) rather than routed through Codex, per Mark's request. Verified in isolation: 21/21 tests pass across `extract/`, `db/`, and `score/` — the full suite currently shows one unrelated failure in `review-ui`'s executor-bridge test, caused by Codex's in-progress `execute/` work touching the shared `invoke()` signature; left untouched since it's inside their active changes.

**Live-run finding #6 (2026-09-20) — open; the pacing theory itself is now falsified.** Resumed with the new 10s interval; it failed **again, at essentially the same point** (409 messages, ~8-9 batches) — but this time 8-9 batches at 10s/batch spans **~80-90 seconds**, genuinely more than a full minute, not less. If the constraint were a clean per-minute sliding window, spreading the same 8 batches across 90 seconds instead of the ~25 seconds finding #5 used should have let noticeably more through before hitting the ceiling. It didn't. **Six consecutive failures at the identical point across pacing that varied 8x (1.1s / 3.1s / 10s, spanning ~10s to ~90s of wall time for the same ~8 batches) means inter-batch spacing is almost certainly not the actual lever** — continuing to guess at ever-larger intervals is very likely the wrong path forward. **Recommend a different diagnostic step instead of another interval guess:** go back to Google Cloud Console's Gmail API Quotas page (the one finding #3 used) and check the **live "Current usage" number for "Units per minute per user"** right after a failed run (it showed "—"/not populated last time it was checked, before any real usage had occurred) — and separately check whether the console's Errors/Troubleshooting view for the Gmail API shows the actual timing and volume of the 403s, which would show real usage against the real constraint rather than continuing to infer it from batch counts. It's also worth considering whether a longer-window budget (daily, or some other accumulation that six same-day attempts could have driven toward exhaustion) is the actual constraint rather than the per-minute one at all. No data lost — 409 messages stored, no checkpoint written. Not resuming again until this is actually diagnosed rather than guessed at further.

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
- [x] Codex: completed resubmission — actionable local frontend (group detail, archive, restore, extension, and Trash-confirmation controls), executor bridge, and safety-path API tests (29/29 full-suite tests passing)
- [x] Claude: reviewed — both high-severity gaps from the prior round are correctly fixed: `start-archive` endpoint added (mirrors `restore_batch`'s pattern exactly) and wired to an "Approval queue" button; Progress screen now renders restore/extend/confirm-trash buttons via a shared `batchButtons()` helper, with "Confirm move to Trash" correctly gated to only appear once `display_status === 'delete_pending'` (matches the backend's own eligibility check rather than showing a button that would just 409). New test coverage directly exercises every safety path that was previously untested: Business-critical blocking, extension's exact deadline math + audit record, confirm-trash eligibility (both accept-when-expired and reject-when-active) with hash verification, the executor bridge for both restore and archive via a clean invoke-mock, the 503-when-unavailable path, and a frontend content smoke test. Independently re-ran the full suite: 29/29 pass. Frontend also properly HTML-escapes all dynamic content (subjects, sender addresses, rationale) before interpolating into the page — correct hygiene given subject lines are attacker-controlled text from senders. **Accepting this gate.**
- [x] Mark: accepted

**What's correct and good:** `restore_batch`'s eligibility check correctly allows restoring during the active window, past the deadline, and even after the second confirmation has been given (since `confirm_trash` never moves `status` away from `restore_window`) — matches the spec's "restore stays available any time before the move-to-Trash call actually runs" precisely. The executor bridge (`review-ui/executor.py`) cleanly handles "Gate 5 doesn't exist yet" as a 503 rather than crashing. Approval Queue and Progress copy is accurate (correctly says "queued for reclamation," matches the archive-first safety flow). Checked `requirements.txt`'s `httpx2>=0.1` (initially looked like a typo for `httpx`) and confirmed it's actually correct — the installed Starlette (1.6.0) internally imports `httpx2 as httpx`, so this is the right current dependency, not a mistake. The minor double-click concern from the prior round is reasonably mitigated: the frontend disables a button while its request is in flight.

**Accepted simplification, not a defect:** Group Review shows the final `delete_safety_score` + rationale text, not the five-factor breakdown bars from the original prototype. `sender_groups` only ever stored the combined score, never the individual factor contributions — there's nothing to break down without `score/` also persisting those components, which is a schema decision for a future version, not something `review-ui` could fix on its own here.

**Cleanup done:** deleted the superseded `static/index.html` skeleton, which was still reachable directly at `/index.html` even though `/` correctly serves `app.html` — no need for two out-of-sync frontend files.

**Resolved from earlier checkpoints:**
- Approval/decision, restore-window extension, `delete_pending` computation, and second-confirmation endpoints reviewed early — correct, plus a server-side hard block on approving Business-critical groups beyond the spec's UI-only requirement.
- Cross-process hash risk fixed: `confirmation_snapshot_hash()` extracted into `db/snapshots.py` as the single canonical implementation, imported by review-ui; `execute/` (Gate 5) will import and call the same function rather than risk an independently-reimplemented, possibly-mismatched hash. Independently re-verified the extracted function's output byte-for-byte against a hand-constructed expected value, including correct filtering to `status='labeled'` only and correct sorting.
- `GmailAPICreds.txt` (a plaintext copy of the OAuth client secret) — recommended deletion; it's at least now correctly gitignored after a rename.

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
