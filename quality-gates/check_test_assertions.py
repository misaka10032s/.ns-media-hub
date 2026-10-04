#!/usr/bin/env python
"""G3(b) — "newly added/changed test functions must not contain zero assertions."

Deliberately scoped to the DIFF, not the whole repo: bolting this onto every test in the
codebase would also fail on pre-existing assertion-free tests, a different (bigger) problem
than "don't let NEW ones in." Hand-rolled AST walk (Python has no maintained
`vitest/expect-expect` equivalent). Ported verbatim from learningMachine's
api/quality-gates/check_test_assertions.py (verified recipe).

Scope immunity: this reads `git diff`-reported changed files only — a gitignored scratch test
(e.g. dropped in download/ or save/) can never appear in a git diff, so it structurally cannot
reach this gate. This is the diff-based counterpart to check_ruff_baseline.py's/G3's explicit
directory scoping.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.git_diff import (
    ensure_utf8_stdio,
    get_changed_files,
    get_changed_line_ranges,
    get_staged_files,
    resolve_base_ref,
)

ensure_utf8_stdio()

TEST_FILE_RE_SUFFIXES = ("_test.py",)
TEST_FILE_PREFIX = "test_"


def _is_test_file(rel_path: str) -> bool:
    name = rel_path.rsplit("/", 1)[-1]
    return name.startswith(TEST_FILE_PREFIX) or name.endswith(TEST_FILE_RE_SUFFIXES)


def _is_pytest_raises(func: ast.expr) -> bool:
    if isinstance(func, ast.Attribute):
        return func.attr == "raises"
    if isinstance(func, ast.Name):
        return func.id == "raises"
    return False


def _has_assertion(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            return True
        if isinstance(child, ast.With):
            for item in child.items:
                call = item.context_expr
                if isinstance(call, ast.Call) and _is_pytest_raises(call.func):
                    return True
    return False


def main() -> int:
    cwd = Path.cwd()
    # `--staged` (the commit-time step): only the test files staged for the next commit.
    staged_mode = "--staged" in sys.argv[1:]
    base_ref = "the index" if staged_mode else resolve_base_ref(cwd)
    staged_tests = [f for f in get_staged_files(cwd, ["py"]) if _is_test_file(f)]
    changed = staged_tests if staged_mode else [
        f for f in get_changed_files(cwd, base_ref, ["py"]) if _is_test_file(f)
    ]

    if not changed and staged_tests:
        # The staged list holds test files but the diff listed none: the listing is broken, so nothing was checked.
        print(
            f"[G3b] FAIL — 0 changed test files vs {base_ref} while {len(staged_tests)} test file(s) are staged "
            f"({', '.join(staged_tests)}): the changed-file listing is wrong, nothing was checked.",
            file=sys.stderr,
        )
        return 1
    if not changed:
        print(f"[G3b] no new/changed test files vs {base_ref} — nothing to check.")
        return 0

    changed_lines = get_changed_line_ranges(cwd, base_ref, changed, staged_mode)
    violations: list[tuple[str, int, str]] = []

    for rel in changed:
        path = cwd / rel
        if not path.exists():
            continue  # deleted file — nothing to check
        lines = changed_lines.get(rel, set())
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith(TEST_FILE_PREFIX):
                continue
            func_lines = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
            if not (func_lines & lines):
                continue  # this test function wasn't touched by the diff
            if not _has_assertion(node):
                violations.append((rel, node.lineno, node.name))

    if violations:
        for rel, lineno, name in violations:
            print(f"{rel}:{lineno} test '{name}' has zero assertions", file=sys.stderr)
        print(
            f"\n[G3b] FAIL — {len(violations)} new/changed test function(s) with zero "
            f"assertions (base {base_ref}).",
            file=sys.stderr,
        )
        return 1

    print(
        f"[G3b] PASS — {len(changed)} changed test file(s), all touched test functions "
        f"assert something (base {base_ref})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
