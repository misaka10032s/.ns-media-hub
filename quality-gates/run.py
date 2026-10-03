#!/usr/bin/env python
"""Aggregate quality-gate runner (Python family recipe — ported from learningMachine's
api/quality-gates/run.py, verified recipe; this repo's app/ lives at the repo root, not under
api/, so ROOT is the repo root and there's no "cd api first" step).

Usage (from the repo root):
    py -3.11 quality-gates/run.py <g1|g2|g3|g4|g5|l0|l1> [--update-baseline]

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

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.git_diff import ensure_utf8_stdio

ensure_utf8_stdio()

ROOT = Path(__file__).resolve().parent.parent  # repo root
GATES_DIR = Path(__file__).resolve().parent  # quality-gates/


def _run(cmd: list[str]) -> int:
    print(f"$ {' '.join(cmd)}")
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


GATES = {"g1": g1, "g2": g2, "g3": g3, "g4": g4, "g5": g5, "l0": l0, "l1": l1}


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
