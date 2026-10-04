#!/usr/bin/env python
"""G1 — ruff lint, baselined: FAIL only on NEW violations relative to a version-controlled
baseline (quality-gates/ruff-baseline.json). Same pattern as G2/G4, built on lib/baseline.py.

Why baselined rather than a bare `ruff check`: this tree already carries 49 pre-existing
findings across app/module/tests (measured 2026-08-27 with the select set below — mostly I001
unsorted-imports and F401 unused-import) — a bare pass/fail would make L0 permanently red on
the untouched tree. This gate does NOT fix those 49 — they stay in the baseline for a human to
burn down; --update-baseline is for a deliberate, reviewed cleanup (or knowingly accepting a
new one), never a blanket bypass.

SCOPE (explicit, not "."): only app/, module/, scripts/, tests/ are scanned — the exact same
list every OTHER gate in this recipe scans. This is a deliberate exclusion, not ruff's own
default excludes: `venv/`, `download/`, `save/`, `data/`, `cookies/`, `.pytest_cache/`,
`__pycache__/` all sit at the repo root and are gitignored working/scratch dirs (see
.gitignore) — a stray .py file dropped there must NEVER become a gate input. Proven both ways
(scratch dir ignored -> exit 0; same violation in a real scanned dir -> exit 1) — evidence in
.claude/CLAUDE.md -> `## Code quality gates`.

Identity key = "relative/file.py|CODE|message text" — deliberately excludes the line number
so a violation that merely shifted a few lines from an unrelated edit above it doesn't
register as new.

FAIL-OPEN FIX (2026-08-27 — cross-repo defect, see
D:/backup/CSIA/@PM/state/runs/CROSS-REPO-mypy-failopen.md): originally filed against mypy, but
MEASURED to reproduce here for ruff too, more starkly. Corrupting `[tool.ruff]` with a
syntactically-valid-but-unrecognized key (`not_a_real_ruff_option = true`) makes ruff itself
exit 2 ("ruff failed ... unknown field ...") and print NOTHING to stdout (not even `[]`). This
script's old `json.loads(proc.stdout or "[]")` treated that empty stdout as "0 violations" —
so ALL 49 baselined violations "vanished" at once, printed only as an ignorable note, and the
gate reported `[G1] PASS — 0 total violation(s)`. Two parts, both required:
  1. `_validate_config()` — a static, tool-independent check (stdlib `tomllib`) that the
     config file exists, parses as TOML, and carries a `[tool.ruff]` table, BEFORE ruff is
     even invoked. Also pass `--config` explicitly for the same reason check_mypy_baseline.py
     does — a hard, visible error if the path vanishes between this check and the subprocess
     call, instead of silent re-discovery.
  2. A returncode guard: ruff's own contract is 0 (clean) / 1 (violations found) / 2 (tool or
     config error) — confirmed 2026-08-27 (clean run on this tree exits 1, corrupted-config
     run exits 2). Any returncode outside {0, 1} is a hard FAIL, quoting ruff's own stderr,
     never silently parsed as "no violations".
  3. Vanished baseline violations now FAIL instead of printing an ignorable note (see
     `main()`) — the durable half, catching any future silent-profile-change mechanism, not
     just this one. NOTE for reviewers: this repo's ruff baseline (49 entries) was recently
     realigned after feature commits landed (2026-08-27) — a routine commit that happens to
     incidentally fix one of those 49 as a side effect will now need an explicit
     `--update-baseline` before it can land. That is the intended tradeoff (see
     .claude/CLAUDE.md `## Code quality gates`), not a regression.

ORDERING FIX (2026-08-27 — a second fail-open introduced BY the fix above; TWO separate defects
in the original `main()`, both confirmed by direct execution here, not inferred from line
numbers): (1) the plain-run path checked `if resolved: ... return 1` BEFORE it ever checked
`if new:` — so a commit that simultaneously fixed one baselined violation AND introduced an
unrelated new one was told ONLY about the resolved finding; (2) separately and more subtly, the
`--update-baseline` branch ran BEFORE the baseline was even loaded/diffed — it wrote `current`
to disk unconditionally, so it never saw `new` at all, in ANY state, not just after a plain-run
FAIL. Both were reproduced concretely 2026-08-27 in a throwaway worktree, fully reverted after:
fixed the real baselined `app/main.py|F401` finding while planting a new
`app/domain/enums.py|F401` in the same run — the plain run reported ONLY the resolved finding,
and running `--update-baseline` in that exact state absorbed the planted new finding without a
word (a follow-up run then PASSed with it baked in as pre-existing debt). Fixed via the shared
`lib.baseline.report_and_decide()` (see its docstring — used identically by G2/G4 so the three
gates cannot drift apart on this again): `new`/`resolved` are computed ONCE, up front, before
either the plain-run or the `--update-baseline` branch can act; a plain run FAILs and reports
BOTH when either is non-empty; `--update-baseline` REFUSES (FAIL, zero file change) only when
BOTH are non-empty — a new-only debt-accept or a resolved-only shrink still proceeds, now
naming every finding it accepts or removes rather than only a count.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import baseline as baseline_lib
from lib.git_diff import ensure_utf8_stdio, get_staged_files

ensure_utf8_stdio()

ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "ruff-baseline.json"
CONFIG_PATH = ROOT / "pyproject.toml"
# `scripts/` is currently EMPTY (0 .py files; git does not track empty dirs, so it isn't even
# materialized in a fresh checkout — confirmed 2026-08-27) and would make ruff itself error
# ("file not found") if passed as a scan target. Add it back the moment it gains a .py file.
SCAN_DIRS = ["app", "module", "tests"]


def _validate_config() -> str | None:
    """Confirm CONFIG_PATH exists, parses as TOML, and carries a `[tool.ruff]` table. Returns
    an error message if not, else None. A syntactically valid but semantically bad option
    (e.g. a typo'd key) passes this check and is caught instead by the returncode guard in
    `main()` after ruff actually runs (ruff hard-fails, exit 2, on that class of problem —
    measured 2026-08-27, unlike mypy which can silently fall back to defaults)."""
    if not CONFIG_PATH.exists():
        return f"config file not found: {CONFIG_PATH} (ruff would silently fall back to bare defaults)"
    try:
        data = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return f"{CONFIG_PATH.name} is not valid TOML: {exc}"
    if not isinstance(data.get("tool", {}).get("ruff"), dict):
        return f"{CONFIG_PATH.name} has no [tool.ruff] table"
    return None


def _staged_in_scope() -> list[str]:
    """Staged .py files (paths relative to the repo root) that sit under one of SCAN_DIRS."""
    return [f for f in get_staged_files(ROOT, ["py"]) if f.split("/", 1)[0] in SCAN_DIRS]


def _run_ruff(targets: list[str] = SCAN_DIRS) -> tuple[list[dict], str, int]:
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", *targets, "--output-format=json", f"--config={CONFIG_PATH}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # ruff writes the JSON report to stdout when it actually runs ([] + exit 0 when clean,
    # populated + exit 1 when violations exist). A tool/config error (exit 2) prints NOTHING
    # to stdout — that empty string must never be silently coerced into "[]"; the returncode
    # guard in main() catches it before this function's caller trusts `items`.
    try:
        items = json.loads(proc.stdout) if proc.stdout.strip() else []
    except json.JSONDecodeError:
        items = []
    return items, proc.stderr, proc.returncode


def _identity(item: dict) -> str:
    rel = Path(item["filename"]).resolve().relative_to(ROOT).as_posix()
    return f"{rel}|{item['code']}|{item['message']}"


def _main_staged() -> int:
    """The commit-time step: ruff on the staged files only; a finding in them that is not in the baseline fails, and a
    baseline entry of theirs that vanished fails (the whole-tree run is `run.py g1`, part of l0)."""
    config_error = _validate_config()
    if config_error:
        print(f"[G1] FAIL — ruff config problem: {config_error}", file=sys.stderr)
        return 2
    files = _staged_in_scope()
    if not files:
        print("[G1] no staged .py file in the ruff scope — nothing to check.")
        return 0
    items, stderr, returncode = _run_ruff(files)
    if returncode not in (0, 1):
        print(
            f"[G1] FAIL — ruff exited {returncode} (tool/config error, not a lint result) — "
            "refusing to treat that as '0 violations':",
            file=sys.stderr,
        )
        print(stderr or "(no stderr captured)", file=sys.stderr)
        return 2
    current = sorted({_identity(i) for i in items})
    staged_set = set(files)
    baseline = [b for b in baseline_lib.load(BASELINE_PATH) if b.split("|", 1)[0] in staged_set]
    new, resolved = baseline_lib.diff(current, baseline)
    for v in resolved:
        print(f"  - baseline violation no longer detected: {v}", file=sys.stderr)
    for v in new:
        print(f"  - NEW ruff violation: {v}", file=sys.stderr)
    if new or resolved:
        print(
            f"\n[G1] FAIL — {len(new)} new and {len(resolved)} vanished ruff violation(s) in {len(files)} staged "
            "file(s) vs the baseline (fix them, or review and run `run.py g1 --update-baseline`).",
            file=sys.stderr,
        )
        return 1
    print(f"[G1] PASS — {len(files)} staged file(s), {len(current)} violation(s), 0 new/vanished vs baseline.")
    return 0


def main() -> int:
    if "--staged" in sys.argv[1:]:
        if "--update-baseline" in sys.argv[1:]:
            print(
                "[G1] --update-baseline needs the whole-tree run: use `run.py g1 --update-baseline`.",
                file=sys.stderr,
            )
            return 2
        return _main_staged()
    update_mode = "--update-baseline" in sys.argv[1:]

    config_error = _validate_config()
    if config_error:
        print(f"[G1] FAIL — ruff config problem: {config_error}", file=sys.stderr)
        return 2

    items, stderr, returncode = _run_ruff()

    if returncode not in (0, 1):
        print(
            f"[G1] FAIL — ruff exited {returncode} (tool/config error, not a lint result) — "
            "refusing to treat that as '0 violations':",
            file=sys.stderr,
        )
        print(stderr or "(no stderr captured)", file=sys.stderr)
        return 2

    current = sorted({_identity(i) for i in items})
    # Decision logic (new-vs-resolved ordering, --update-baseline refusal) is centralized in
    # lib/baseline.report_and_decide() — shared IDENTICALLY by G1/G2/G4 so the three gates
    # cannot drift apart on this again. See that function's docstring and each checker's
    # "ORDERING FIX" module docstring for the reproduction that made this the shared shape.
    return baseline_lib.report_and_decide(
        gate_tag="[G1]",
        noun="ruff violation",
        profile_desc="lint profile",
        current=current,
        baseline_path=BASELINE_PATH,
        update_mode=update_mode,
        update_baseline_cmd="py -3.11 quality-gates/run.py g1 --update-baseline",
    )


if __name__ == "__main__":
    sys.exit(main())
