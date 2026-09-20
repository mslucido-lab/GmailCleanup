# Gmail Cleanup — Build Checklist

Tracks the milestones and review gates defined in the spec's **Build split** section. Each milestone closes only when all three boxes are checked — see the spec for what each role actually verifies (Codex: unit tests; Claude: supplemental/safety-rail review; Mark: manual acceptance as product owner).

Update this file as part of closing each gate — whoever closes a box edits this file and says so, so it stays a reliable source of truth rather than a snapshot that rots.

## Milestones

### 1. `db/` — schema, migrations, connection config
- [x] Codex: implemented (`schema.sql`, `migrate.py`, `connection.py`) + unit tests (4/4 passing)
- [x] Claude: reviewed — schema matches spec v8 exactly across all 9 tables; the `messages.category` finding is fixed and independently re-verified (4/4 tests pass locally)
- [ ] Mark: accepted / schema locked

### 2. `extract/` — Gmail metadata pull
- [ ] Codex: implemented + unit tests
- [ ] Claude: reviewed
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

- Technical spec: **v8**, committed to `main`.
- No open spec-level blockers as of v8.

## Notes

- `config.toml` in the repo root is unrelated to this project (Codex CLI's own global config) — ignore it here.
