# ns-media-hub — AI dev guide

Unified media-download hub (based on `ns-gallery-dl`): gallery-dl image-site downloads, yt-dlp video, Discord bot auto-download, local API + queue/history/jobs/cookies Web UI, Chrome extension, and centralised cookie management.

Backend is Python/Flask; frontend is Vue 3/Vite.

> Cluster rules: `D:/backup/CSIA/@PM/.claude/CLAUDE.md` `## Before doing X, read Y`.

## Stack
- Backend: Python 3.11 + Flask (API + serves frontend build); SQLite at `data/app.db`
- Frontend: Vue 3 + Vite 8 + Pinia + vue-router; SCSS (sass)
- Download engines: gallery-dl, yt-dlp — both pip-managed via `venv`, invoked as subprocesses (yt-dlp resolves through PATH/venv `Scripts`, NOT a standalone `.exe`)
- Bot: Discord (Python)
- Chrome extension: `chromeExtension/` (selection export, site-nav, omnibox, redirect cleanup) — before touching the selection engine (`chromeExtension/static/module/selector-*.js`), see `docs/blueprint/entries/BP-EXT-SELECTION-1.md` (approved design, binding decisions; spec linked via its `superpowers:` field at `docs/superpowers/specs/selection-mode-v2-spec.md`)
- External repos absorbed — do NOT modify: `javascript/ns-chrome-tool`
- **Windows:** the global CLAUDE.md Python line

### Downloader package updates (yt-dlp / gallery-dl)
- Central registry: `app/config/downloaders.py` `DOWNLOADER_PACKAGES` — add a future downloader in ONE line here; `app/services/updater_service.py`, the manual API endpoint, and the launcher `-u`/`-update` flag all derive from it.
- **Reactive only** — on a download failure classified as a "stale extractor" error (tight, centralized signature list in `updater_service.STALE_EXTRACTOR_SIGNATURES`), the failing provider's package is upgraded via pip and the job retries ONCE.
  - A cooldown + "already installed version" guard (`app/config/downloaders.py` `UPDATE_COOLDOWN_SECONDS`, `app/storage/repositories/downloader_state_repo.py`) prevents mindless update→fail→update loops.
- **Manual** — `POST /api/downloaders/update` (same-origin guarded, refuses 409 while a job is running) + a "更新下載器" button in the Web UI header.
- **Launcher `-U`** — `dl.cmd -u` / `dl.sh -u` also force-updates every registered downloader package via the same registry.
- **NO scheduled / daily / every-startup auto-update** — by design, to keep startup fast and avoid pointless upstream churn.

## Run commands

### Launcher (`dl.cmd` / `dl.sh`)
| Flag | Action |
|---|---|
| `-s` | Start server + UI (auto-rebuilds frontend if source changed) |
| `-b` | Start Discord bot |
| `-s -b` | Start both |
| `-u` | Reinstall / update dependencies (also force-updates yt-dlp / gallery-dl) |
| `-h` | Show help |

Web UI: `http://127.0.0.1:7601/` — pages: `/`, `/history`, `/queue`, `/jobs`, `/cookies`

### Frontend
```bash
cd frontend && npm install
npm run build   # outputs to app/ui/ (served by Flask)
npm run dev     # dev server at 127.0.0.1:5173
```

## Dev commands

### Python — install dev/gate tooling
```bash
py -3.11 -m pip install -r requirements-dev.txt
```
Use `py -3.11` (the global CLAUDE.md Python line).

The repo's own `venv/` (Python 3.13) carries only runtime deps (Flask, gallery-dl, yt-dlp, discord.py, …) — no pytest/ruff/mypy — and this repo's own CLAUDE.md states "Python 3.11", so `py -3.11` is both the intended-version match AND where the dev tooling actually lives; it's used for every gate command below.

### Python — lint / typecheck / test
```bash
py -3.11 -m ruff check app module tests          # G1 (bare, unbaselined view)
py -3.11 -m mypy app --ignore-missing-imports     # G2 (bare, unbaselined view)
py -3.11 -m pytest -q                             # G3 — 34 test files, 500 tests, ~18s
```

### Frontend — lint / test
```bash
cd frontend
npm run lint    # eslint . (repo-wide, will show the pre-existing 539-warning backlog — G1 itself is diff-scoped, see below)
npm run test    # vitest run (JobsView.spec.js, HistoryView.spec.js; a zero-test-file result FAILS the gate — see G3 below)
```

## Code quality gates

Two independent gate families — Vue/JS (`frontend/quality-gates/`, npm scripts) and Python (`quality-gates/`, `run.py`) — because this repo is a genuine hybrid (Vue 3 + Vite frontend, Flask + Discord-bot Python backend).

Every gate below is proven able to fail (`D:/backup/CSIA/@PM/.claude/context/cluster-conventions.md` `### Proof-of-failure rule (rubric R2 — binding before any command is wired in)`); a gate that cannot be is dropped rather than faked (see "Dropped for this repo" below).

**Every scan is explicitly scoped** — never a bare `.`/repo-root scan — so a gitignored scratch file (this repo has several root-level working dirs: `venv/`, `download/`, `save/`, `data/`, plus `frontend/tmp/` reserved for scratch scripts) can never become a gate input:
- Python: ruff/mypy scan `app module tests` by name (not `.`); pytest is pinned to `testpaths = ["tests"]` in `pyproject.toml` — a stray `test_*.py` dropped in `download/` is invisible to it.
- Frontend: madge (G4) scans `src/` only, never the package root; vitest's `test.include` is pinned to `src/**/*.{test,spec}.{js,mjs,cjs}` in `vite.config.js` (vitest's own default recursive glob is NOT relied on).
- The diff-scoped gates (frontend G1/G3b, Python G3b) inherit this for free: `git diff` can never see an untracked/gitignored file in the first place.

Proof-of-failure evidence for both stacks' scoping: `docs/superpowers/decisions/2026-09-09-quality-gate-history.md` §"Scan-scoping proof".

### Python — `py -3.11 quality-gates/run.py <g1|g2|g3|g4|g5|l0|l1> [--update-baseline]`

| Gate | What | Scope | Baseline |
|---|---|---|---|
| G1 | `ruff check app module tests` (select E,F,I,B,UP,RUF) | 47 pre-existing findings baselined (`ruff-baseline.json`), mostly `I001` unsorted-imports / `F401` unused-import — none fixed, only blocked from growing |
| G2 | `mypy app` (non-strict — see `pyproject.toml` `[tool.mypy]` for why not `strict=true`) | 12 pre-existing errors baselined (`mypy-baseline.json`) across 8 files |
| G3 | `pytest -q` (green) + AST assertion-presence on changed test functions (`check_test_assertions.py`) | 500 tests, all green |
| G4 | `import-linter` `layers` contract: `app.api > app.services > app.providers > app.domain > app.storage > app.config` | 4 pre-existing violations baselined (`import-cycle-baseline.json`) — `app.providers.*` genuinely calls `app.services.path_service`/`token_service` for filesystem/auth helpers; this is a real working dependency, not cleaned up, only blocked from growing |
| G5 | `pytest --cov=app --cov-report=xml` then `diff-cover --fail-under=60` | diff coverage of changed lines only |
| ~~G6~~ | mutation testing | none for Python — @PM cluster-conventions G6 |

`l0` and `l1` run the gates `D:/backup/CSIA/@PM/.claude/context/cluster-conventions.md` `### Levels` assigns to L0 and L1 (~8s and ~15s on the untouched tree).

Commit = staged-file lint + determinism + related tests, within 10 seconds (`py -3.11 quality-gates/run.py commit`: ruff on the staged files, the determinism scan, the assertion check on staged test files, pytest on the related test files from `quality-gates/related_tests.py`); end of the task (before merge) = the task's one full run: `py -3.11 quality-gates/run.py l1`.

**A baseline measured in a worktree goes stale if the merge target moves.**

Regenerate it against the merge target (`main`) immediately before merging, not at branch-cut time — a baseline is a snapshot of a moving tree, not a fixed spec.

**G4 `__init__.py` files:** `app/api`, `app/config`, `app/domain`, `app/providers`, `app/services`, `app/storage` (and 5 `app/providers/*` subpackages) carry empty `__init__.py` files — without one, import-linter's analysis engine (grimp) cannot see into a PEP 420 namespace package at all, so the contract can silently report "0 violations" while unable to find the code.

**Do not remove those `__init__.py` files** — `check_import_cycles.py` fails loud if this regresses.

History: `docs/superpowers/decisions/2026-09-09-quality-gate-history.md` §"G4 vacuous-gate finding".

**G1/G2 config validation + vanished baseline:** neither `mypy` nor `ruff` crashes on a broken/missing `[tool.mypy]`/`[tool.ruff]` config — both can silently fall back to defaults or fail with output a checker could misread as "0 findings".

`check_mypy_baseline.py` and `check_ruff_baseline.py` both guard this in two parts:
1. **Validate the config before trusting the run.**
   - A static `tomllib` check confirms `pyproject.toml` parses and carries the relevant `[tool.mypy]`/`[tool.ruff]` table BEFORE the tool runs; both checkers pass an explicit `--config-file` (mypy) / `--config` (ruff) instead of relying on auto-discovery.
   - Each script also checks the tool's own signal after running: mypy's config diagnostics are matched in stderr; ruff's config/tool errors are caught via its own return-code contract (0 clean / 1 violations found / 2 tool-or-config error — any other code is a hard FAIL).
   - A config problem detected either way is `[G1]`/`[G2]` **FAIL, exit 2**, naming the exact diagnostic — never a silent PASS.
2. **A vanished baseline finding is a FAILURE, not an ignorable note** — `[G1]`/`[G2]` **FAIL, exit 1**, naming every vanished finding.
   - **To legitimately shrink a baseline** (a real fix landed, or a deliberate realignment): confirm *why* the finding vanished first, then run `py -3.11 quality-gates/run.py g1 --update-baseline` (or `g2`) to re-snapshot.
   - Do **not** run `--update-baseline` reflexively just to unblock a FAIL without checking the cause.
   - **Cost to routine development:** a commit that incidentally fixes one of the pre-existing baselined findings as a side effect (not the commit's main goal) will FAIL until `--update-baseline` is run — this is an intended tradeoff, not a bug.

Full reproduction evidence (planted config corruptions, before/after; the measured non-reproduction case) and the ruff-baseline realignment history: `docs/superpowers/decisions/2026-09-09-quality-gate-history.md` §"G1/G2 fail-open fix".

**G1/G2/G4 guard ordering:** the shared `quality-gates/lib/baseline.report_and_decide()` backs all three gates (G1/G2/G4), so they cannot drift apart:
1. `new`/`resolved` are computed ONCE, up front, before EITHER the plain-run branch or the `--update-baseline` branch can act.
2. A plain run FAILs (exit 1) if EITHER set is non-empty, and reports **both** — never only one.
3. `--update-baseline` REFUSES to write (exit 1, zero file change) **only when BOTH `new` and `resolved` are non-empty** — the one state where a plain re-snapshot is genuinely ambiguous.
   - A **new-only** run (deliberately accepting a finding as debt) or a **resolved-only** run (shrinking for a genuine fix) still PROCEEDS, naming every finding it accepts or removes.

See the "ORDERING FIX" docstring block at the top of each of the three checker scripts, and `report_and_decide()`'s own docstring in `quality-gates/lib/baseline.py`.

Defect description + reproduction evidence: `docs/superpowers/decisions/2026-09-09-quality-gate-history.md` §"G1/G2/G4 guard-ordering fix".

### Frontend — `cd frontend && npm run gate:<g1|g3|g4|commit|l0|l1>`

Commit = staged-file lint + determinism + related tests, within 10 seconds (`npm run gate:commit`: ESLint on the staged files' changed lines, the determinism scan, the assertion check on staged test files, `vitest related` on the staged files through `quality-gates/run-related-tests.mjs`, which passes no pool or worker flag); end of the task (before merge) = the task's one full run: `npm run gate:l1`.

| Gate | What | Scope | Baseline |
|---|---|---|---|
| G1 | ESLint, diff-LINE-scoped (only messages on lines the diff actually touched) | 539 pre-existing warnings repo-wide (all `eslint-plugin-vue` stylistic rules — `max-attributes-per-line`, `singleline-html-element-content-newline`, `html-self-closing`; 0 errors) made a bare `--max-warnings=0` unusable, so this gate uses the same line-diff scoping misaka_site2.0 uses for the same reason, at a smaller scale |
| G3 | `vitest run` (green, `passWithNoTests: false`) + `@vitest/eslint-plugin` `expect-expect` on changed test files | `src/views/JobsView.spec.js` + `src/views/HistoryView.spec.js` (8 tests total, real assertions) — a zero-matched-test-file result hard-FAILs (see below); grows as more tests are added |
| G4 | `madge` circular-import check on `src/` | 0 pre-existing cycles |
| `l1` | = `l0` (no G5/G6 — see below) | |

**G3 `passWithNoTests`:** `frontend/vite.config.js`'s `test` block leaves `passWithNoTests` at vitest's own default (`false`), so an "all tests deleted" state hard-fails instead of passing indistinguishably from a genuinely green suite.

**Do not re-add `passWithNoTests: true`** without also adding a gate that separately checks "at least one test file exists".

Full narrative: `docs/superpowers/decisions/2026-09-09-quality-gate-history.md` §"G3 vacuous-gate finding (frontend)".

**Dropped for this repo, with evidence (not faked):**
- **G2 (typecheck)** — this frontend has **zero TypeScript**: 0 `.ts`/`.tsx` files, no `tsconfig.json` (27 source files are `.vue`/`.js`).
  - Revisit if/when the frontend adopts TS.
- **G5 (diff coverage) / G6 (mutation)** — this frontend has two test files today (vs the Python side's 34 files / 500 tests).
  - A coverage or mutation-kill threshold against so little tested surface is still theatre, not signal — skip until real coverage exists across more components, then reconsider both.

Rationale for both drops (vacuous-checker precedent, minimal-diff scoping): `docs/superpowers/decisions/2026-09-09-quality-gate-history.md` §"Dropped gates rationale".

### Enabling the pre-commit hook
```bash
git config core.hooksPath .githooks
```
`.githooks/pre-commit` derives which stack(s) a commit touches from the staged file list and runs only that stack's commit level (frontend `frontend/*` staged -> `npm run gate:commit`; Python `{app,module,scripts,tests}/*.py` staged -> `py -3.11 quality-gates/run.py commit`), and both also run when any `requirements*.txt`, any file under a `quality-gates/` folder, any determinism canary or the hook itself is staged; the whole-project `l0`/`l1` run once at the end of the task — a docs-only or config-only commit runs neither and exits immediately.

Run the command above once per clone; it is not self-installing, and a detached HEAD or `git commit --no-verify` skips it (`D:/backup/CSIA/@PM/.claude/context/cluster-conventions.md` `### Hook carrier (L0 enforcement)`).

## Project structure
```
frontend/           Vue + Vite + Pinia source (styles/ = shared SCSS partials)
app/
  api/              Flask app + API entry (routes/: history/queue/jobs/auth/misc/pages/downloaders)
  config/           paths, env config, feature flags, downloader package registry
  domain/           job / provider / status types
  providers/        gallery-dl, yt-dlp, site-specific, cookies
  services/         queue, history, bot, token, bridge, downloader updater
  storage/          SQLite schema + repositories
  ui/               Vite build output (served by Flask; gitignored)
chromeExtension/    Chrome extension (one canonical copy)
module/             legacy entry compat layer
data/               app.db, tokens, cookies
download/           download output (gitignored)
dl.py               Python entry point → app.main
```

## Domain conventions

### Download paths (provider-directed — no hardcoding outside domain registry)
- Discord: guild-only — `download/discord/<guild>/attachments|embeds/`
- Pixiv: author-level — `download/gallery-dl/pixiv.net/<author>/`
- YouTube / X / Facebook: domain-only — `download/ytdlp/<domain>/`

### Cookies
- Canonical path: `cookies/` (old paths auto-migrate on scan)
- `cookies/*` is gitignored — local-private, never commit
- Scan results written to SQLite registry; providers resolve applicable cookie automatically

### Data storage
- All state (jobs, history, cookies registry) in SQLite (`data/app.db`)
- Legacy `data/history.json` auto-migrates to SQLite on init

### `.env` (copy from `.env.example`)
Key fields: `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_IDS`, `BOT_DOMAIN_ALLOWLIST`, `BOT_DOMAIN_DENYLIST`, `DISCORD_EMOJI_*`

### Site-specific logic
- Preserve nhentai + wnacg specialized download logic — do not generalise away

## Gotchas
- **Web UI:** navigate ONLY via the left menu — direct URL navigation fails to render.
- **Data / schema:** verify data and schema by querying `data/app.db` directly — never infer schema from code.

## graphify
Before answering architecture/code questions: check `graphify-out/GRAPH_REPORT.md` for core nodes; if `graphify-out/wiki/index.md` exists, browse the wiki before reading source files.

Graph refresh and rebuild: follow `D:/backup/CSIA/@PM/.claude/context/graphify-maintenance.md` (the rule lives only there).

## Skills (must use)
- **`superpowers:brainstorming`** — when: `D:/backup/CSIA/@PM/.claude/context/cluster-conventions.md` `## Development flow (canonical — every non-trivial task)` step 2
- **`frontend-design`** — when: `D:/backup/CSIA/@PM/.claude/context/skill-routing.md` row "Any UI / visual / layout / copy on screen"
- **graphify** — use knowledge graph to assist design (see §graphify above)
