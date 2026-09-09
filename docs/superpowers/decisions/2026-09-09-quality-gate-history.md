# 2026-09-09 — Quality-gate rule-file history (待回答 #56 rollout)

Narrative moved verbatim out of `.claude/CLAUDE.md`'s `## Code quality gates` section during the
rule-file slimming pass (owner-approved 2026-09-09 for every managed repo). Each section below
names its origin heading and the original line range (as of commit `8b7c78d`, pre-slim). The
operative directive/baseline/config that stayed behind in `.claude/CLAUDE.md` is noted at the
end of each section — this file holds only the reasoning, incident history, and investigation
trail, never anything that is itself still binding.

---

## From `.claude/CLAUDE.md`

### §"Scan-scoping proof" — origin: `## Code quality gates`, lines 102-116

> **Every scan is explicitly scoped** — never a bare `.`/repo-root scan — so a gitignored scratch
> file (this repo has several root-level working dirs: `venv/`, `download/`, `save/`, `data/`,
> plus `frontend/tmp/` reserved for scratch scripts) can never become a gate input:
> - Python: ruff/mypy scan `app module tests` by name (not `.`); pytest is pinned to
>   `testpaths = ["tests"]` in `pyproject.toml` — a stray `test_*.py` dropped in `download/` is
>   invisible to it. Proven both ways (planted a failing test in `download/scratch/` -> pytest
>   exit 0; the same test moved into `tests/` -> exit 1).
> - Frontend: madge (G4) scans `src/` only, never the package root; vitest's `test.include` is
>   pinned to `src/**/*.{test,spec}.{js,mjs,cjs}` in `vite.config.js` (vitest's own default
>   recursive glob is NOT relied on — a sibling repo's rollout shipped exactly that vacuous-scope
>   bug, where a gitignored `frontend/tmp/` scratch test silently blocked every commit). Proven
>   both ways (planted a failing test in `frontend/tmp/` -> `vitest run` exit 0; the same test
>   moved into `src/` -> exit 1).
> - The diff-scoped gates (frontend G1/G3b, Python G3b) inherit this for free: `git diff` can
>   never see an untracked/gitignored file in the first place.

Operative directive retained in CLAUDE.md: the same three bullets, minus the two "Proven both
ways" reproduction clauses and the "a sibling repo's rollout shipped exactly that vacuous-scope
bug…" aside — the scope rule itself (which target each scanner is pinned to) stays; the proof
evidence and the cross-repo incident reference move here.

### §"G4 vacuous-gate finding" — origin: `## Code quality gates`, lines 135-145

> **G4 vacuous-gate finding (fixed):** before this install, `app/api`, `app/config`, `app/domain`,
> `app/providers`, `app/services`, `app/storage` (and 5 `app/providers/*` subpackages) had NO
> `__init__.py` — implicit PEP 420 namespace packages. import-linter's analysis engine (grimp
> 3.15) cannot see into a subpackage without one, so the contract silently reported "0
> violations" on every run — not because the tree was clean, but because it couldn't find the
> code at all (`Missing layer 'app.api': module app.api does not exist.`, an ERROR that the
> original wrapper script didn't distinguish from a clean pass). Fixed by adding empty
> `__init__.py` to all 11 affected directories (zero behavior change — verified via
> `python -c "import app.main"`, the full pytest suite, and a grep for any namespace-package-only
> API usage, all clean) — and `check_import_cycles.py` now fails loud instead of silently passing
> if this regresses. **Do not remove those `__init__.py` files.**

Operative directive retained in CLAUDE.md: the 11 affected directories needed empty
`__init__.py` for import-linter to see them at all (PEP 420 namespace packages are invisible to
grimp without one); do not remove those `__init__.py` files; `check_import_cycles.py` fails loud
if this regresses.

### §"G1/G2 fail-open fix" — origin: `## Code quality gates`, lines 147-199

> **G1/G2 fail-open fix (2026-08-27 — closed a cluster-wide gap, see
> `D:/backup/CSIA/@PM/state/runs/CROSS-REPO-mypy-failopen.md`):** neither `mypy` nor `ruff`
> crashes on a broken/missing `[tool.mypy]` / `[tool.ruff]` config — both can silently fall back
> to bare defaults (mypy) or hard-fail with empty stdout that used to be misread as "0 findings"
> (ruff), and the old checker scripts only ever read stdout, never the return code or stderr.
> **Reproduced here concretely** (measured, not assumed): planting a syntactically-valid but
> unrecognized key under `[tool.mypy]` made mypy print a config-parse warning to stderr while
> `check_mypy_baseline.py` silently reported `[G2] PASS` unchanged — the config problem was
> invisible. The same corruption under `[tool.ruff]` made ruff exit 2 with **empty stdout**,
> which the old script coerced to `"[]"` (via `proc.stdout or "[]"`), so all 49 baselined ruff
> violations "vanished" at once and `check_ruff_baseline.py` reported `[G1] PASS — 0 total
> violation(s)`. Both are now fixed, same two-part shape in both `check_mypy_baseline.py` and
> `check_ruff_baseline.py`:
> 1. **Validate the config before trusting the run.** A static `tomllib` check confirms
>    `pyproject.toml` exists, parses as TOML, and carries the relevant `[tool.mypy]` /
>    `[tool.ruff]` table — BEFORE the tool even runs. Both checkers also now pass an explicit
>    `--config-file` (mypy) / `--config` (ruff) instead of relying on silent auto-discovery.
>    That alone is not sufficient (a syntactically valid but semantically bad option, e.g. a
>    typo'd key, passes static TOML validation) — each script also checks the tool's own signal
>    after running: mypy's config diagnostics are matched in stderr (mypy prefixes every
>    config-loading problem with the config file's path, empty on a clean run); ruff's config/tool
>    errors are caught via its own return-code contract (0 clean / 1 violations found / 2
>    tool-or-config error — any other code is now a hard FAIL, never silently read as "0
>    findings"). A config problem detected either way is `[G1]`/`[G2]` **FAIL, exit 2**, naming
>    the exact diagnostic — never a silent PASS.
> 2. **A vanished baseline finding is now a FAILURE, not an ignorable note.** Previously, if a
>    finding present in `mypy-baseline.json`/`ruff-baseline.json` stopped appearing, the gate
>    printed `note: N baseline error(s) no longer exist — consider shrinking the baseline` and
>    still returned PASS. That silence is exactly what a future silent-profile-disable mechanism
>    (not just a broken TOML — any way the check could stop applying) would produce, so it is now
>    `[G1]`/`[G2]` **FAIL, exit 1**, naming every vanished finding. **This is the durable half** —
>    it catches the disappearance regardless of cause, not only the specific TOML-corruption
>    mechanism part 1 targets.
>    - **To legitimately shrink a baseline now** (a real fix landed, or a deliberate baseline
>      realignment): confirm *why* the finding vanished first, then run
>      `py -3.11 quality-gates/run.py g1 --update-baseline` (or `g2`) to re-snapshot. Do **not**
>      run `--update-baseline` reflexively just to unblock a FAIL without checking the cause —
>      that reintroduces exactly the blind spot this fix closes.
>    - **Cost to routine development, stated plainly:** a normal commit that happens to
>      incidentally fix one of the pre-existing baselined findings as a side effect (not the
>      commit's main goal) will now FAIL until `--update-baseline` is run — this is an intended
>      tradeoff, not a bug. The ruff baseline (49 entries) was realigned right before this fix
>      landed (2026-08-27); that realignment is unaffected (it's the current committed baseline,
>      not a vanished-vs-baseline diff), but any *future* incidental fix to one of those 49 needs
>      the same explicit re-snapshot step.
>    - **Measured non-reproduction, for the record:** a full TOML syntax error (duplicate
>      `[tool.mypy]` key) or deleting `pyproject.toml` outright does NOT, on its own, cause a
>      vanishing-findings PASS in this repo's *current* mypy config — losing
>      `ignore_missing_imports = true` only ever ADDS spurious `import-untyped` findings here
>      (this repo's only non-default mypy setting is a suppressor, not a strictness gate), so
>      those two specific corruptions were already visible as an (unexplained) FAIL even before
>      this fix. Relying on that coincidence was the actual risk — part 1's stderr/returncode
>      checks make the FAIL explicit and correctly attributed instead of accidental.

Operative directive retained in CLAUDE.md: neither tool crashes on a broken config, both were
fixed with the same two-part shape (config validated before trusting the run, exit 2 on a config
problem; a vanished baseline finding is now FAIL exit 1, cleared only via a deliberate
`--update-baseline` after confirming why it vanished); the "cost to routine development" tradeoff
stays as a one-line note. (Note: the ruff baseline count cited here, 49, was the count as of
2026-08-27 — re-measured 2026-09-09 at 47; see the CLAUDE.md gate table for the current number.)

### §"G1/G2/G4 guard-ordering fix" — origin: `## Code quality gates`, lines 201-255

> **G1/G2/G4 guard-ordering fix (2026-08-27 — closed a SECOND fail-open introduced BY the fix
> above):** point 2 above made a vanished baseline finding `FAIL, exit 1` on a plain run — but
> the original `main()` had TWO separate defects, both confirmed by direct execution here (not
> inferred from reading line numbers):
> 1. **Plain-run ordering.** `check_ruff_baseline.py`/`check_mypy_baseline.py` checked
>    `if resolved: ... return 1` BEFORE it ever checked `if new:`. `check_import_cycles.py` (G4)
>    had the mirror gap: it only ever printed `resolved` as an ignorable stdout note, never
>    failing on it at all.
> 2. **The `--update-baseline` path itself, in ALL three gates** — the deeper defect: the
>    `--update-baseline` branch ran BEFORE the baseline was even loaded/diffed against `current`,
>    so it wrote `current` to disk unconditionally with NO check of `new` whatsoever, in ANY
>    state — not only after a plain-run FAIL.
>
> Net effect: a commit that simultaneously fixed one baselined finding AND introduced an
> unrelated new one was told ONLY about the resolved finding on a plain run, and — separately —
> running the gate's own suggested `--update-baseline` remedy in that same state silently baked
> the unreviewed new finding into the baseline, hiding it permanently. **Reproduced concretely
> for all three, both defects** (2026-08-27, in a throwaway worktree, fully reverted after, every
> touched file hash-verified back to `HEAD`):
> - G1: fixed the real baselined `app/main.py|F401` finding while planting a new
>   `app/domain/enums.py|F401` — the plain run reported only the resolved finding, and running
>   `--update-baseline` in that exact state absorbed the new one without a word.
> - G2: fixed the real baselined `download_service.py|valid-type` finding (bare `callable` used
>   as a type annotation instead of `Callable`) while planting a new `features.py|assignment`
>   type error — same outcome.
> - G4: removed the real baselined `app.providers.ytdlp.provider -> app.services.path_service`
>   edge while planting a new illegal `app.config.features -> app.storage.db` cross-layer
>   import — same outcome (the vanished entry printed only as a footer note; `--update-baseline`
>   absorbed the new violation).
>
> **Fix, one consistent shape across all three gates — a single shared function
> (`quality-gates/lib/baseline.report_and_decide()`), not three near-copies, so they cannot drift
> apart on this again:**
> 1. `new`/`resolved` are computed ONCE, up front, before EITHER the plain-run branch or the
>    `--update-baseline` branch can act.
> 2. A plain run FAILs (exit 1) if EITHER set is non-empty, and reports **both** — never only one.
> 3. `--update-baseline` REFUSES to write (exit 1, zero file change) **only when BOTH `new` and
>    `resolved` are non-empty** — that is the one state where a plain re-snapshot is genuinely
>    ambiguous (it would silently accept the new finding as if it were the same kind of reviewed
>    decision as the shrink). A **new-only** run (deliberately accepting a finding as debt) or a
>    **resolved-only** run (shrinking for a genuine fix) still PROCEEDS — this is a real,
>    documented, legitimate use of `--update-baseline` (each checker's own docstring: "a
>    deliberate, reviewed cleanup (**or knowingly accepting a new one**)") — but now names every
>    finding it is about to accept or remove, not just a count. Deliberately a hard refusal
>    rather than "print a warning and write anyway" — a printed warning can go unread in a
>    non-interactive/CI invocation (a scripted `--update-baseline && git commit`); a refusal
>    cannot be missed.
>
> All four run-states proven for G1, G2, and G4 (new-only / vanished-only / both-at-once /
> neither), exit codes confirmed for each, PLUS `--update-baseline`'s behavior verified
> separately in every state: new-only and resolved-only both succeed (exit 0) and name the
> finding they act on; both-at-once refuses (exit 1, baseline file byte-for-byte unchanged). See
> the "ORDERING FIX" docstring block at the top of each of the three checker scripts, and
> `report_and_decide()`'s own docstring in `quality-gates/lib/baseline.py`, for the exact
> reproduction evidence.

Operative directive retained in CLAUDE.md: the shared `report_and_decide()` computes `new`/
`resolved` up front for all three gates; a plain run FAILs on either being non-empty and reports
both; `--update-baseline` refuses to write only when both are non-empty, otherwise proceeds and
names what it accepts/removes.

### §"G3 vacuous-gate finding (frontend)" — origin: `## Code quality gates`, lines 266-276

> **G3 vacuous-gate finding (fixed 2026-09-01):** `frontend/vite.config.js`'s `test` block set
> `passWithNoTests: true` with the justification "0 test files today, must still be a PASS". That
> made `npm run gate:g3`/`vitest run` report PASS on a matched-zero-test-files result exactly the
> same as a genuinely green suite — indistinguishable, and it stayed that way since the gate was
> installed (2026-08-27) with nothing ever guarding a frontend regression. Fixed by (1) writing the
> first real frontend test (`src/views/JobsView.spec.js`, not a placeholder — see file for what it
> asserts) and (2) removing the `passWithNoTests: true` override (left at vitest's own default,
> `false`), so a future "all tests deleted" state hard-fails instead of passing. Proven both ways:
> deleting the test file made `gate:g3:test` exit non-zero with `No test files found`; restoring it
> went back to green. **Do not re-add `passWithNoTests: true`** without also adding a gate that
> separately checks "at least one test file exists" — otherwise this defect just moves one layer.

Operative directive retained in CLAUDE.md: `vite.config.js`'s `test` block is left at vitest's
own default (`passWithNoTests: false`); do not re-add `passWithNoTests: true` without also
adding a separate "at least one test file exists" check.

### §"Dropped gates rationale" — origin: `## Code quality gates`, lines 278-290

> **Dropped for this repo, with evidence (not faked):**
> - **G2 (typecheck)** — this frontend has **zero TypeScript**: 0 `.ts`/`.tsx` files, no
>   `tsconfig.json` (verified via `find frontend -name "*.ts"`, 2026-08-27; all 26 source files
>   are `.vue`/`.js`). Wiring `vue-tsc`/`jsconfig.json` `checkJs` against fully unannotated JS
>   would inherit exactly the vacuous-checker failure this cluster already documented for
>   `vue-tsc --noEmit` on misaka_site2.0 (proven exit-0-on-a-real-bug case) — near-zero type
>   inference without any annotations gives false assurance, not real protection. Converting the
>   frontend to TypeScript is a real option but is a 26-file rewrite outside this task's scope
>   (minimal-diff rule) — revisit if/when the frontend adopts TS.
> - **G5 (diff coverage) / G6 (mutation)** — this frontend has one test file today (vs the Python
>   side's 12 files / 197 tests). A coverage or mutation-kill threshold against a single file is
>   still theatre, not signal — skip until real coverage exists across more components, then
>   reconsider both.

Operative directive retained in CLAUDE.md: G2 dropped (0 TypeScript files, verified 2026-09-09 at
27 source files); G5/G6 dropped (too little tested surface for a coverage/mutation threshold to
mean anything). The `vue-tsc --noEmit` vacuous-checker precedent this cluster documented on
misaka_site2.0 is the reasoning for not wiring an equivalent unannotated-JS checker here.

---

## Baseline drift found and corrected during this pass (2026-09-09)

Re-running every gate live in a fresh worktree (`8b7c78d`) against the numbers this file quoted
turned up drift — none of it a regression, all of it the baseline simply not having been
re-measured since 2026-08-27 while the repo kept moving:

- Python G1 (ruff): baseline was quoted as 49 findings; `py -3.11 quality-gates/run.py g1`
  reports `[G1] PASS — 47 total ruff violation(s), 0 new vs baseline (47 pre-existing).`
  `ruff-baseline.json` has 47 entries. CLAUDE.md's gate table updated to 47.
- Python G3 (tests): baseline was quoted as "12 test files, 197 tests"; `py -3.11 quality-gates/run.py l0`
  reports `500 passed, 1 warning in 18.23s`; `find tests -name "test_*.py"` counts 34 files.
  CLAUDE.md's Dev-commands and gate-table lines updated to "34 test files, 500 tests, ~18s" /
  "500 tests, all green".
- Frontend G1 (eslint repo-wide backlog): baseline was quoted as 541 warnings; `npm run lint`
  reports `✖ 539 problems (0 errors, 539 warnings)`. CLAUDE.md updated to 539.
- Frontend G3 (test files): baseline named only `src/views/JobsView.spec.js` (4 tests); a second
  file, `src/views/HistoryView.spec.js` (4 tests), now also exists — `npm run gate:g3:test`
  reports `Test Files 2 passed (2)` / `Tests 8 passed (8)`. CLAUDE.md updated to name both files
  and 8 tests total.
- Frontend source-file count (G2-drop rationale): baseline was quoted as 26 `.vue`/`.js` files;
  a live count (`find src -name "*.vue" -o -name "*.js"`) now returns 27, with 0 `.ts`/`.tsx`
  files unchanged. CLAUDE.md updated to 27.
- Mypy (G2), import-linter (G4, both stacks), and madge (frontend G4) baselines were re-run and
  matched the file exactly (mypy 12 errors / 8 files; Python G4 4 violations; frontend G4 0
  cycles) — no drift found on these four.
