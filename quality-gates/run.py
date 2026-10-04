#!/usr/bin/env python
"""Aggregate quality-gate runner (Python family recipe — ported from learningMachine's
api/quality-gates/run.py, verified recipe; this repo's app/ lives at the repo root, not under
api/, so ROOT is the repo root and there's no "cd api first" step).

Usage (from the repo root):
    py -3.11 quality-gates/run.py <g1|g2|g3|g4|g5|commit|l0|l1> [--update-baseline]

  commit = what the pre-commit hook runs, within about 10 seconds, only the steps the change class of the staged files
       needs: it classes every staged file by path (code, test, setup, tooling, docs, style, wording), prints the class
       counts, then runs ruff on the STAGED .py files (no mypy, no import-linter) when a .py of class code, test, setup
       or tooling is staged, the determinism scan of the staged test, setup and tooling files only (`--files`), and the
       assertion check when a test file is staged. No test runs at commit here (`TESTS_AT_COMMIT = False`). Whole-suite
       pytest, the whole-scope determinism scan and every whole-tree step run in the end-of-task run, l1.
  l0 = G1 (ruff lint, baselined) + G2 (mypy typecheck, baselined) + G3 (pytest + AST
       assertion-presence on changed test functions) + G4 (import-linter layers, baselined) —
       seconds-level. G1/G2/G4 all fail only on NEW findings vs a version-controlled baseline
       file (quality-gates/{ruff,mypy,import-cycle}-baseline.json) — see lib/baseline.py.
       `--update-baseline` re-snapshots the CURRENT findings as the new baseline (deliberate,
       reviewed cleanup or accepted new debt only — never a bypass).
  l1 = l0 + G5 (diff coverage, >=60% of changed lines — this repo's tests/ has 12 files / 197
       tests, enough for coverage to mean something, unlike the frontend side which has 0).
       — G6 (diff mutation / mutmut) is REMOVED cluster-wide for the Python family: mutmut's
       latest maintained release refuses to start on native Windows outright ("To run mutmut on
       Windows, please use the WSL."), exit code 1, unconditionally, before mutating anything —
       same evidence learningMachine already recorded for this exact tool. Not attempted here;
       removed outright per the cluster's own rule for this class of finding, not faked.

Every gate here is a thin wrapper around a real external command — this script's only job is
consistent naming/sequencing (mirrors the JS-family recipe's `gate:g1..gate:l1` npm scripts).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.git_diff import _git, ensure_utf8_stdio, repo_prefix

ensure_utf8_stdio()

ROOT = Path(__file__).resolve().parent.parent  # repo root
GATES_DIR = Path(__file__).resolve().parent  # quality-gates/

# --- commit level: the package constants (a copy of this recipe edits only this block) --------------------------------
# Class patterns match a path relative to the repo root with forward slashes. The package is the whole repo root, so
# SCOPE_PATTERNS first drops every staged path that is not Python-side (frontend/, docs/, ...); the repo's pre-commit
# hook stays in. The class of a file is the first rule that matches, in this order: setup, tooling, test, docs, style,
# wording, code.
SCOPE_PATTERNS = [
    re.compile(r"^(?:app|module|scripts|tests|quality-gates)/"),
    re.compile(r"^(?:pyproject\.toml|pytest\.ini)$"),
    re.compile(r"^requirements[^/]*\.txt$"),
    re.compile(r"^(?:\.\./)*\.githooks/pre-commit$"),
]
SETUP_PATTERNS = [
    re.compile(r"(?:^|/)conftest\.py$"),
    re.compile(r"^pytest\.ini$"),
    re.compile(r"^pyproject\.toml$"),
    re.compile(r"(?:^|/)requirements[^/]*\.txt$"),
]
TOOLING_PATTERNS = [
    re.compile(r"^quality-gates/"),
    re.compile(r"(?:^|/)determinism-canaries/"),
    re.compile(r"^(?:\.\./)*\.githooks/pre-commit$"),
]
TEST_FOLDERS = ("tests/",)  # pyproject.toml testpaths: every file under them is a test or a test helper
WORDING_PATTERNS: list[re.Pattern[str]] = []  # no locale files in this package
# Related tests at commit (rule R of the commit-level design): False when no recorded run of this package's test step
# fits the 10-second commit. TEST_CAP: the most related test files allowed (None: no cap).
TESTS_AT_COMMIT = False
TEST_CAP: int | None = None
# --- end of the commit-level constants ------------------------------------------------------------------------------

CLASS_ORDER = ("code", "test", "setup", "tooling", "docs", "style", "wording")


def _run(cmd: list[str]) -> int:
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=ROOT).returncode


def g1(update_baseline: bool = False) -> int:
    cmd = [sys.executable, str(GATES_DIR / "check_ruff_baseline.py")]
    if update_baseline:
        cmd.append("--update-baseline")
    return _run(cmd)


def g2(update_baseline: bool = False) -> int:
    cmd = [sys.executable, str(GATES_DIR / "check_mypy_baseline.py")]
    if update_baseline:
        cmd.append("--update-baseline")
    return _run(cmd)


def g3() -> int:
    rc = _run([sys.executable, "-m", "pytest", "-q"])
    if rc != 0:
        return rc
    rc = _run([sys.executable, str(GATES_DIR / "check_test_assertions.py")])
    if rc != 0:
        return rc
    return _run([sys.executable, str(GATES_DIR / "check_test_determinism.py")])


def _class_of(rel: str) -> str:
    """The change class of a path relative to the repo root, by the first matching rule (the git status is not looked at
    here)."""
    if any(pattern.search(rel) for pattern in SETUP_PATTERNS):
        return "setup"
    if any(pattern.search(rel) for pattern in TOOLING_PATTERNS):
        return "tooling"
    if rel.startswith(TEST_FOLDERS):
        return "test"
    if (
        re.search(r"\.(?:md|mdx)$", rel, re.I)
        or (re.search(r"\.txt$", rel, re.I) and not re.search(r"(?:^|/)requirements[^/]*\.txt$", rel))
        or rel.startswith("docs/")
    ):
        return "docs"
    if re.search(r"\.(?:css|scss|sass|less)$", rel, re.I):
        return "style"
    if any(pattern.search(rel) for pattern in WORDING_PATTERNS):
        return "wording"
    return "code"


def _staged_entries() -> list[tuple[str, str, str]]:
    """The staged entries that belong to the Python scope (SCOPE_PATTERNS), each (status, path relative to the repo
    root, class). A staged delete (D) or rename (R) of a code or test file is class setup."""
    fields = _git(["diff", "--cached", "--name-status", "-M", "-z"], ROOT).split("\0")
    prefix = repo_prefix(ROOT).replace("\\", "/")
    depth = len([part for part in prefix.split("/") if part])
    entries: list[tuple[str, str, str]] = []
    i = 0
    while i < len(fields) and fields[i] != "":
        status = fields[i][0]
        count = 2 if status in ("R", "C") else 1
        top = fields[i + count].replace("\\", "/")
        i += 1 + count
        if prefix == "" or top.startswith(prefix):
            rel = top[len(prefix):]
        elif top == ".githooks/pre-commit":
            rel = "../" * depth + top
        else:
            continue
        if not any(pattern.search(rel) for pattern in SCOPE_PATTERNS):
            continue
        cls = _class_of(rel)
        if status in ("D", "R") and cls in ("code", "test"):
            cls = "setup"
        entries.append((status, rel, cls))
    return entries


def commit() -> int:
    """The pre-commit level: classes the staged files by path, prints the class counts, and runs only the steps the
    classes need (see the module docstring); a step that fails stops the run. l1 stays the end-of-task run."""
    entries = _staged_entries()
    counts = {name: sum(1 for _, _, cls in entries if cls == name) for name in CLASS_ORDER}
    print("[commit] classes: " + " ".join(f"{name}={counts[name]}" for name in CLASS_ORDER), flush=True)
    # A deleted path has nothing to lint or scan (a rename's entry holds the new path).
    alive = [(rel, cls) for status, rel, cls in entries if status != "D"]
    if any(rel.endswith(".py") and cls in ("test", "code", "setup", "tooling") for rel, cls in alive):
        rc = _run([sys.executable, str(GATES_DIR / "check_ruff_baseline.py"), "--staged"])
        if rc != 0:
            return rc
    scanned = [rel for rel, cls in alive if cls in ("test", "setup", "tooling")]
    if scanned:
        rc = _run([sys.executable, str(GATES_DIR / "check_test_determinism.py"), "--stack", "py", "--files", *scanned])
        if rc != 0:
            return rc
    if any(cls == "test" and re.search(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$", rel) for rel, cls in alive):
        rc = _run([sys.executable, str(GATES_DIR / "check_test_assertions.py"), "--staged"])
        if rc != 0:
            return rc
    if not TESTS_AT_COMMIT:
        return 0
    setup_files = [rel for _, rel, cls in entries if cls == "setup"]
    if setup_files:
        print(f"[commit] {setup_files[0]} is test setup: the tests move to the end-of-task run", flush=True)
        return 0
    from related_tests import related_test_files  # only a package that runs tests at commit calls it

    related = related_test_files(ROOT)
    if not related:
        print("[commit] no staged test file and no test file related to a staged file - no related test to run.",
              flush=True)
        return 0
    if TEST_CAP is not None and len(related) > TEST_CAP:
        print(f"[commit] {len(related)} related test files > {TEST_CAP}: the tests move to the end-of-task run",
              flush=True)
        return 0
    return _run([sys.executable, "-m", "pytest", "-q", *related])


def g4(update_baseline: bool = False) -> int:
    cmd = [sys.executable, str(GATES_DIR / "check_import_cycles.py")]
    if update_baseline:
        cmd.append("--update-baseline")
    return _run(cmd)


def g5() -> int:
    rc = _run([sys.executable, "-m", "pytest", "-q", "--cov=app", "--cov-report=xml"])
    if rc != 0:
        return rc
    return _run([sys.executable, str(GATES_DIR / "diff_coverage.py")])


def l0(update_baseline: bool = False) -> int:
    gates = (
        lambda: g1(update_baseline),
        lambda: g2(update_baseline),
        g3,
        lambda: g4(update_baseline),
    )
    for gate in gates:
        rc = gate()
        if rc != 0:
            return rc
    return 0


def l1(update_baseline: bool = False) -> int:
    rc = l0(update_baseline)
    if rc != 0:
        return rc
    return g5()


GATES = {"g1": g1, "g2": g2, "g3": g3, "g4": g4, "g5": g5, "commit": commit, "l0": l0, "l1": l1}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in GATES:
        print(f"usage: run.py <{'|'.join(GATES)}> [--update-baseline]", file=sys.stderr)
        return 2
    name = sys.argv[1]
    update_baseline = "--update-baseline" in sys.argv[2:]
    if name in ("g1", "g2", "g4", "l0", "l1"):
        return GATES[name](update_baseline)
    return GATES[name]()


if __name__ == "__main__":
    sys.exit(main())
