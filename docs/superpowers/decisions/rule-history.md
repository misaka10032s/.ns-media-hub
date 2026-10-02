# Rule history — dated parentheticals and incident narration moved out of `.claude/` rule files

> Each block below is copied verbatim from the named source file as it stood before the rule-cleanup commit; the
> rule file keeps only the rule.

## `.claude/CLAUDE.md`

### Code quality gates — opening paragraph

> Installed 2026-08-27; every gate below was proven to
> actually fail before shipping (plant a known violation -> non-zero exit -> revert -> green
> again)

### Python gate table and Frontend gate table — Baseline column header

> Baseline (verified 2026-09-09)

### G4 fix

> **G4 fix (2026-08-27):** `app/api`, `app/config`, `app/domain`, `app/providers`, `app/services`,
> `app/storage` (and 5 `app/providers/*` subpackages) needed empty `__init__.py` added

### G1/G2 config-validation + vanished-baseline fix

> (2026-08-27 — closed a cluster-wide gap, see
> `D:/backup/CSIA/@PM/state/runs/CROSS-REPO-mypy-failopen.md`)

### G1/G2/G4 guard-ordering fix

> (2026-08-27 — closed a SECOND fail-open introduced BY the fix
> above)

> All four run-states proven for G1, G2, and G4 (new-only / vanished-only / both-at-once /
> neither), exit codes confirmed for each, PLUS `--update-baseline`'s behavior verified separately
> in every state.

### Frontend G3 table row

> a zero-matched-test-file result now hard-FAILs (fixed 2026-09-01, see below)

### G3 vacuous-gate finding (frontend)

> **G3 vacuous-gate finding (fixed 2026-09-01):** `frontend/vite.config.js`'s `test` block used to
> set `passWithNoTests: true`, which made a matched-zero-test-files result PASS indistinguishably
> from a genuinely green suite. Fixed by writing the first real frontend test and removing that
> override (left at vitest's own default, `false`), so an "all tests deleted" state now hard-fails
> instead of passing.

### Dropped for this repo — G2 (typecheck)

> verified 2026-09-09;

### Python-stack prerequisites — yt-dlp line

> the old sibling `.ns-yt-dlp` repo fallback is gone — that repo no longer exists

### G4 vacuous-gate paragraph

> now fails loud instead of silently passing if this regresses. Full discovery narrative:

### G1/G2 config-validation + vanished-baseline fix

> the old checker scripts misread as "0 findings". Fixed, same two-part shape

### G1/G2/G4 guard ordering

> replacing three near-copies that could drift apart
