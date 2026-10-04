"""Shared diff-scoping helper for the diff-based gates (G3b assertion-presence). Python port of
the JS-family recipe's `frontend/quality-gates/lib/git-diff.mjs` — keep the two in sync in
spirit. Ported from learningMachine's api/quality-gates/lib/git_diff.py, with MAIN_BRANCH
changed to this repo's actual default branch.

Scope model (same as the JS recipe, deliberately simple): the base ref is the merge-base with
`main` (this repo's default branch — confirmed via `git status --branch`, 2026-08-27), and
"changed" means "working tree right now vs that merge base" (``git diff <base>``, no upper
bound). That single definition covers committed-on-branch, staged, AND unstaged-but-tracked
changes in one pass. Override with ``QUALITY_BASE_REF`` for a narrower/explicit comparison.

Known gap (inherited from the exemplar, found in its own review): a brand-new file that has
never been `git add`-ed is invisible to plain ``git diff <base>`` — it is NOT "unstaged", it is
untracked, and this helper does not add ``--others``/``--include-untracked`` to pick those up.
A gate run against such a file silently reports "nothing to check" rather than checking it. In
practice this self-heals the moment the file is staged (the cluster's own workflow stages
before every gate/commit run), so it is not fixed here — flagged rather than silently left
inaccurate. This is the OPPOSITE direction of a scratch-file leak (an untracked file is
under-counted, never over-counted), so it does not weaken the gitignored-scratch-immunity
guarantee documented in quality-gates/run.py.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

MAIN_BRANCH = "main"  # ns-media-hub's default branch is `main` (git status --branch, 2026-08-27).


def ensure_utf8_stdio() -> None:
    """Windows consoles default to a legacy codepage (cp950/cp1252), which raises/garbles on
    the em-dashes this recipe's own gate messages use. Every gate entrypoint calls this first."""
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")


# A commit hook inherits GIT_DIR (and, for a partial commit, GIT_INDEX_FILE) from git: a child `git` that sees them
# lists the wrong folder's files and returns an empty diff, which every diff-scoped gate reads as "nothing changed".
# So every git call here runs without GIT_DIR, GIT_WORK_TREE and GIT_INDEX_FILE and names its folder with `-C <cwd>`.
GIT_ENV_NAMES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def _git(args: list[str], cwd: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_NAMES}
    return subprocess.run(
        ["git", "-C", str(cwd), *args], cwd=cwd, env=env, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout


def resolve_base_ref(cwd: Path) -> str:
    """Resolve the base ref to diff against."""
    override = os.environ.get("QUALITY_BASE_REF", "").strip()
    if override:
        return override
    try:
        base = _git(["merge-base", "HEAD", MAIN_BRANCH], cwd).strip()
        if base:
            return base
    except subprocess.CalledProcessError:
        pass  # `main` not reachable (e.g. detached/shallow) — fall through.
    try:
        return _git(["rev-parse", "HEAD~1"], cwd).strip()
    except subprocess.CalledProcessError:
        return _git(["rev-parse", "HEAD"], cwd).strip()  # single-commit repo: empty diff


def repo_prefix(cwd: Path) -> str:
    """git's own prefix for `cwd` relative to the repo top-level, e.g. "" for repo root."""
    return _git(["rev-parse", "--show-prefix"], cwd).strip()


def _strip_prefix(path: str, prefix: str) -> str:
    return path[len(prefix):] if prefix and path.startswith(prefix) else path


def get_changed_files(cwd: Path, base_ref: str, extensions: list[str]) -> list[str]:
    """Changed files (added/copied/modified/renamed — never deleted) under `cwd`, filtered
    to the given extensions, returned as paths relative to `cwd`."""
    prefix = repo_prefix(cwd)
    patterns = [f"*.{ext}" for ext in extensions]
    out = _git(["diff", "--name-only", "--diff-filter=ACMR", base_ref, "--", *patterns], cwd)
    return [
        _strip_prefix(line.strip().replace("\\", "/"), prefix)
        for line in out.split("\n")
        if line.strip()
    ]


def get_staged_files(cwd: Path, extensions: list[str]) -> list[str]:
    """Files staged for the next commit (added/copied/modified/renamed — never deleted) under `cwd`, filtered
    to the given extensions, returned as paths relative to `cwd`."""
    prefix = repo_prefix(cwd)
    patterns = [f"*.{ext}" for ext in extensions]
    out = _git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "--", *patterns], cwd)
    return [
        _strip_prefix(line.strip().replace("\\", "/"), prefix)
        for line in out.split("\n")
        if line.strip()
    ]


def get_changed_line_ranges(
    cwd: Path, base_ref: str, files: list[str], staged: bool = False
) -> dict[str, set[int]]:
    """Map[relPath, set[lineNumber]] of lines added/changed on the "new" side for the given
    files. Pure deletions contribute no lines (nothing new to require assertions for).
    With `staged` the lines are those of the index against HEAD (what the next commit adds); `base_ref` is then
    unused."""
    result: dict[str, set[int]] = {}
    if not files:
        return result
    prefix = repo_prefix(cwd)
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
    against = ["--cached"] if staged else [base_ref]
    diff_out = _git(["diff", "--unified=0", "--diff-filter=ACMR", *against, "--", *files], cwd)
    current_file: str | None = None
    for line in diff_out.split("\n"):
        if line.startswith("+++ "):
            raw = line[4:].strip()
            if raw == "/dev/null":
                current_file = None
                continue
            cleaned = _strip_prefix(re.sub(r"^b/", "", raw).replace("\\", "/"), prefix)
            current_file = cleaned
            result.setdefault(current_file, set())
            continue
        if line.startswith("@@ ") and current_file:
            m = hunk_re.match(line)
            if m:
                start_line = int(m.group(1))
                count = 1 if m.group(2) is None else int(m.group(2))
                for i in range(count):
                    result[current_file].add(start_line + i)
    return result
