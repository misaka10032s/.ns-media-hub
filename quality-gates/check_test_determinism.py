#!/usr/bin/env python
"""G3(c) — determinism: no test, test helper, test config or gate script may contain a pattern whose
result can differ between runs of the same code (cluster-conventions `## Code quality gates`, G3 row).

The patterns (one tag each in the output):
  P1 a raised or custom time limit      P2 a retry                  P3 a real sleep or polling
  P4 a real clock read                  P5 unseeded randomness      P6 a dynamic import or module reset
  P8 any skip, and a silent early return                            P9 a write to a real repo path
(P7, the real-router pattern, exists only for the TypeScript stack.)

The whole scope in the end-of-task run (no option); at commit only the given files (`--files`, the staged test,
setup and gate files), scanned by the same rules and the same scope rules, so a file outside the scope stays out of it.
No way to switch a hit off: no baseline file, no allow-list file, no ignore comment, no environment variable, no flag
that skips a rule. The only options pick WHAT to scan (`--stack`, `--root`, `--files`). The only exemptions are the
coded constructs each rule names. A file that does not parse counts as a hit (`P0 parse error`, file:line of the
error), because it was not checked, and the run exits non-zero. Python files are parsed with `ast`; the token
stacks (cs, java, ts) have no parser here, so their parse check is the bracket nesting of the source with comments,
strings and regex literals blanked out.

Stacks:
  py    (default) Python tests, scope = what pytest collects (testpaths / python_files from the
        pytest config) plus every conftest.py and test helper under a testpath, plus config, gate
        scripts and the git hook.
  cs    C# / xUnit: the .cs files of every test project (a .csproj that references xunit or
        Microsoft.NET.Test.Sdk), .csproj, .runsettings, gate scripts, the git hook. Token rules
        over the source with comments and string literals blanked out (no C# parser in the cluster).
  java  Java / JUnit: the .java files under src/test/, pom.xml, gate scripts, the git hook. Same
        token approach.
  ts    TypeScript / vitest tests, for a repo whose gate runs no Node checker (run with --root <the
        frontend folder>): *.test.* / *.spec.* files, src/**/test.ts, test helpers (*.testUtils.*,
        test-utils/, .test/), vite / vitest / playwright config, the root package.json scripts, gate
        scripts and the git hook. The same nine patterns (P1-P9) as the .mjs checker, as token
        rules over the source with comments, strings and template literals blanked out.

Usage (from the package folder):
  py -3.11 quality-gates/check_test_determinism.py [--stack py|cs|java|ts] [--root DIR] [--files PATH ...]
  --files: paths relative to --root; only these are scanned (the git hook only when it is one of them).
"""
from __future__ import annotations

import argparse
import ast
import configparser
import fnmatch
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Skip list: dependency and tool folders only. A folder named build, env, bin or dist never drops a test file,
# and coverage output is skipped only outside a test tree (a test folder, or a file named like a test).
DEPENDENCY_DIRS = {".git", "node_modules", ".venv", ".claude", "venv", ".stryker-tmp", "__pycache__", "site-packages", ".tox", "obj", "target"}
COVERAGE_DIRS = {"coverage", "htmlcov"}
TEST_FOLDER_NAMES = {"tests", "test", ".test", "__tests__"}
TEST_FILE_NAME = re.compile(r"^test_|_test\.py$|\.(?:test|spec)\.[cm]?[jt]sx?$|Tests?\.(?:cs|java)$")
# A test runner named in the arguments of a call: pytest, vitest, jest, playwright, stryker, npm test,
# dotnet test, mvn test, gradle test.
RUNNER_TOKEN = re.compile(
    r"(?:^|[\s/\\])(?:vitest|jest|playwright|stryker|pytest)(?:$|[\s./\\-])"
    r"|npm(?:\s+run)?\s+test|dotnet\s+test"
    r"|(?:^|[\s/\\])(?:mvnw?|gradlew?)(?:\.cmd|\.bat)?\s+(?:\S+\s+)*test\b",
    re.I,
)
TEST_COMMAND = re.compile(
    r"\b(?:npm\s+(?:run\s+)?(?:test|gate[\w:.-]*)|npx\s+vitest|vitest|jest|playwright|stryker|pytest|run\.py"
    r"|dotnet\s+test|mvn\b|gate-[\w-]+\.(?:sh|ps1))",
    re.I,
)
SUBPROCESS_CALLS = {
    "subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen",
    "os.system",
}
SQLITE_RELATIVE = re.compile(r"sqlite:/{3}(?!/|:memory:)[^\s'\"`]")

Hit = tuple[int, str, str]  # line, tag label, text

# The checker never scans itself: its own file and its canary folder hold the pattern text on purpose.
# Compared by resolved path, so another working folder or a drive-letter case cannot hide a match.
CHECKER_FILE = Path(__file__).resolve()
CANARY_DIR = CHECKER_FILE.parent / "determinism-canaries"


# ---------------------------------------------------------------------------- file listing and reading


def is_checker_own(path: Path) -> bool:
    resolved = path.resolve()
    return resolved == CHECKER_FILE or CANARY_DIR in resolved.parents


# Every git call runs with -C <scan root> and without GIT_DIR, GIT_WORK_TREE and GIT_INDEX_FILE: a commit hook
# inherits GIT_DIR, and the file listing then comes out relative to the wrong folder.
GIT_ENV_NAMES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def run_git(root: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_NAMES}
    out = subprocess.run(["git", "-C", str(root), *args], cwd=root, env=env, capture_output=True, check=True).stdout
    return out.decode("utf-8", errors="replace")


def git_files(root: Path) -> list[str]:
    out = run_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    files = [f.replace("\\", "/") for f in out.split("\0") if f]
    return [f for f in files if (root / f).is_file() and not is_checker_own(root / f)]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def skipped_dir(rel: str) -> bool:
    segs = rel.split("/")
    folders = segs[:-1]
    if any(seg in DEPENDENCY_DIRS for seg in folders):
        return True
    in_test_tree = any(seg in TEST_FOLDER_NAMES for seg in folders) or TEST_FILE_NAME.search(segs[-1]) is not None
    return not in_test_tree and any(seg in COVERAGE_DIRS for seg in folders)


def gate_index(rel: str) -> int:
    for i, seg in enumerate(rel.split("/")[:-1]):
        if seg.startswith("quality-gates"):
            return i
    return -1


def snippet(lines: list[str], line: int) -> str:
    return lines[line - 1].strip()[:160] if 1 <= line <= len(lines) else ""


# ---------------------------------------------------------------------------- shell / hook scripts


def blank_shell_comments(text: str) -> str:
    return "\n".join("" if re.match(r"^\s*(?:#|REM\s|::)", ln, re.I) else ln for ln in text.split("\n"))


def brace_body(lines: list[str], i: int) -> str:
    rest = "\n".join(lines[i:])
    open_at = rest.find("{")
    if open_at < 0:
        return ""
    depth = 0
    for k in range(open_at, len(rest)):
        if rest[k] == "{":
            depth += 1
        elif rest[k] == "}":
            depth -= 1
            if depth == 0:
                return rest[open_at:k]
    return rest[open_at:]


def scan_shell(path: Path) -> list[Hit]:
    name = path.name.lower()
    style = "ps" if name.endswith(".ps1") else "cmd" if name.endswith((".cmd", ".bat")) else "sh"
    lines = blank_shell_comments(read(path)).split("\n")
    hits: list[Hit] = []

    def add(i: int, label: str) -> None:
        hits.append((i + 1, label, lines[i].strip()[:160]))

    for i, ln in enumerate(lines):
        if re.search(
            r"--(?:testTimeout|hookTimeout|teardownTimeout|blame-hang-timeout|forkedProcessTimeoutInSeconds)\b", ln
        ):
            add(i, "P1 time-limit")
        if re.search(r"--(?:retry|reruns)\b|rerunFailingTestsCount", ln):
            add(i, "P2 retry")
        dbl = re.match(r"^(.*?\S)\s*\|\|\s*\1\s*$", ln)
        if dbl and TEST_COMMAND.search(dbl.group(1)):
            add(i, "P2 retry")
        if style == "sh" and re.match(r"^\s*(?:for|while|until)\b", ln):
            if re.search(r"\bdone\b", ln):
                if TEST_COMMAND.search(ln):
                    add(i, "P2 retry")
                continue
            depth = 1
            body: list[str] = []
            for j in range(i + 1, len(lines)):
                if re.match(r"^\s*(?:for|while|until)\b", lines[j]) and not re.search(r"\bdone\b", lines[j]):
                    depth += 1
                elif re.match(r"^\s*done\b", lines[j]):
                    depth -= 1
                if depth <= 0:
                    break
                body.append(lines[j])
            if TEST_COMMAND.search("\n".join(body)):
                add(i, "P2 retry")
        elif style == "ps" and re.match(r"^\s*(?:foreach|for|while|do)\b", ln, re.I):
            if TEST_COMMAND.search(brace_body(lines, i)):
                add(i, "P2 retry")
        elif style == "cmd" and re.match(r"^\s*for\s", ln, re.I):
            body = [ln]
            if re.search(r"\(\s*$", ln):
                for j in range(i + 1, len(lines)):
                    if re.match(r"^\s*\)", lines[j]):
                        break
                    body.append(lines[j])
            if TEST_COMMAND.search("\n".join(body)):
                add(i, "P2 retry")
        # a filter that leaves tests out of a gate run is a skip
        if style != "cmd" and re.search(r"(?:^|\s)(?:--filter|-Dtest\b|-pl\b)", ln) and TEST_COMMAND.search(ln):
            add(i, "P8 skip")
    return hits


# ---------------------------------------------------------------------------- Python source


CLOCK_CALLS = {
    "time.time", "time.monotonic", "time.perf_counter", "time.time_ns", "time.monotonic_ns",
    "time.process_time", "time.perf_counter_ns", "time.process_time_ns",
    "datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today", "datetime.date.today",
}
RANDOM_FUNCS = {
    "random", "randint", "randrange", "choice", "choices", "shuffle", "sample", "uniform", "gauss", "getrandbits",
    "betavariate", "expovariate", "normalvariate", "triangular", "randbytes", "lognormvariate",
    "vonmisesvariate", "paretovariate", "weibullvariate", "gammavariate",
}
NUMPY_LEGACY = {
    "rand", "randn", "randint", "random", "random_sample", "ranf", "sample", "choice", "bytes", "shuffle",
    "permutation", "uniform", "normal", "standard_normal", "beta", "binomial", "poisson", "exponential",
    "gamma", "geometric", "integers", "multinomial", "lognormal",
}
TORCH_RANDOM = {
    "rand", "randn", "randint", "randperm", "normal", "rand_like", "randn_like", "randint_like", "bernoulli",
    "multinomial",
}
LOOP_FACTORIES = {"asyncio.get_event_loop", "asyncio.get_running_loop", "asyncio.new_event_loop"}
WAIT_METHODS = {"wait", "join", "result", "exception", "acquire", "communicate", "wait_for"}
SKIP_ATTRS = {
    "pytest.mark.skip", "pytest.mark.skipif", "pytest.mark.xfail", "unittest.skip", "unittest.skipIf",
    "unittest.skipUnless", "unittest.expectedFailure", "unittest.case.skip", "unittest.case.skipIf",
    "unittest.case.skipUnless", "unittest.case.expectedFailure",
}
SKIP_CALLS = {"pytest.skip", "pytest.importorskip", "pytest.xfail"}
SKIP_EXCEPTIONS = {"unittest.SkipTest", "unittest.case.SkipTest"}
WRITE_OS = {
    "os.remove", "os.unlink", "os.rename", "os.replace", "os.makedirs", "os.mkdir", "os.rmdir", "os.removedirs",
    "os.truncate",
}
WRITE_SHUTIL = {"shutil.rmtree", "shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree", "shutil.move"}
BOTH_TARGETS = {"os.rename", "os.replace", "shutil.move"}
DEST_ONLY = {"shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree"}
PATH_WRITE_METHODS = {
    "write_text", "write_bytes", "mkdir", "touch", "unlink", "rename", "replace", "rmdir", "symlink_to",
    "hardlink_to", "truncate",
}
PATH_CALLS = {
    "os.path.join", "os.path.abspath", "os.path.normpath", "os.path.realpath", "os.path.dirname", "os.path.basename",
    "os.path.expanduser", "os.path.relpath", "os.fspath", "str", "pathlib.Path", "pathlib.PurePath",
    "pathlib.PosixPath", "pathlib.WindowsPath",
}
PATH_ATTRS = {"parent", "parents", "name", "stem", "suffix", "parts"}
TMP_NAMES = {"tmp_path", "tmp_path_factory", "tmpdir", "tmpdir_factory"}
TEST_CONTEXT_NAMES = {
    "setUp", "tearDown", "setUpClass", "tearDownClass", "setup_method", "teardown_method", "setup_function",
    "teardown_function",
}


def is_relative_literal(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value[0] in "/\\~":
        return False
    if re.match(r"^[A-Za-z]:[\\/]", value) or re.match(r"^[A-Za-z][\w+.-]*:", value):
        return False
    return True


def combine_taint(parts: list[str | None]) -> str | None:
    if "tmp" in parts:
        return "tmp"
    if "repo" in parts:
        return "repo"
    return None


def is_number(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)


class PythonScan:
    def __init__(self, rel: str, text: str, kind: str) -> None:
        self.rel = rel
        self.kind = kind  # "test" or "gate"
        self.lines = text.splitlines()
        self.tree = ast.parse(text, filename=rel)
        self.parent: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node
        self.imports: dict[str, str] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.imports[alias.asname] = alias.name
                    else:
                        root_name = alias.name.split(".")[0]
                        self.imports[root_name] = root_name
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                for alias in node.names:
                    if alias.name != "*":
                        self.imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        self.hits: list[Hit] = []
        self.aliases: dict[str, str] = {}
        self._collect_clock_aliases()
        self.file_frozen = self._file_frozen()

    # -- helpers
    def add(self, node: ast.AST, label: str) -> None:
        line = getattr(node, "lineno", 1)
        self.hits.append((line, label, snippet(self.lines, line)))

    def _collect_clock_aliases(self) -> None:
        """`clock = time.time` binds a clock function to a plain name: remember it, so `clock()` is a clock read.
        Two passes, so an alias of an alias is found too."""
        for _ in range(2):
            for node in ast.walk(self.tree):
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    targets, value = [node.target], node.value
                else:
                    continue
                if not isinstance(value, (ast.Name, ast.Attribute)):
                    continue
                target_name = self.dotted(value)
                if target_name in CLOCK_CALLS:
                    for t in targets:
                        if isinstance(t, ast.Name):
                            self.aliases[t.id] = target_name

    def dotted(self, node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases.get(node.id) or self.imports.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            base = self.dotted(node.value)
            return None if base is None else f"{base}.{node.attr}"
        return None

    def ancestors(self, node: ast.AST) -> list[ast.AST]:
        out: list[ast.AST] = []
        cur = self.parent.get(node)
        while cur is not None:
            out.append(cur)
            cur = self.parent.get(cur)
        return out

    def is_loop_time(self, call: ast.Call) -> bool:
        """`loop.time()`, `asyncio.get_event_loop().time()`, `asyncio.get_running_loop().time()`: the loop's own clock."""
        func = call.func
        if not (isinstance(func, ast.Attribute) and func.attr == "time" and not call.args and not call.keywords):
            return False
        receiver = func.value
        if isinstance(receiver, ast.Call):
            return self.dotted(receiver.func) in LOOP_FACTORIES
        if isinstance(receiver, ast.Name):
            if receiver.id in ("loop", "event_loop"):
                return True
            value = self.assigned_value(receiver.id, receiver)
            return isinstance(value, ast.Call) and self.dotted(value.func) in LOOP_FACTORIES
        return False

    def is_clock_call(self, node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and (self.dotted(node.func) in CLOCK_CALLS or self.is_loop_time(node))

    def reads_clock(self, node: ast.AST) -> bool:
        return any(self.is_clock_call(n) for n in ast.walk(node))

    def own_nodes(self, scope: ast.AST) -> list[ast.AST]:
        """Nodes of `scope` that are not inside a nested function, class or lambda."""
        out: list[ast.AST] = []
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            n = stack.pop()
            out.append(n)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            stack.extend(ast.iter_child_nodes(n))
        return out

    def scope_of(self, node: ast.AST) -> ast.AST:
        for a in self.ancestors(node):
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return a
        return self.tree

    def in_test_context(self, node: ast.AST) -> bool:
        for a in self.ancestors(node):
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if a.name.startswith("test") or a.name in TEST_CONTEXT_NAMES:
                    return True
                for dec in a.decorator_list:
                    target = dec.func if isinstance(dec, ast.Call) else dec
                    if self.dotted(target) in ("pytest.fixture", "pytest.yield_fixture"):
                        return True
        return False

    # -- frozen clock
    def frozen_call(self, call: ast.AST) -> bool:
        if not isinstance(call, ast.Call):
            return False
        name = self.dotted(call.func)
        if name == "time_machine.travel":
            tick_false = any(
                k.arg == "tick" and isinstance(k.value, ast.Constant) and k.value.value is False
                for k in call.keywords
            )
            arg = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "destination"), None)
            return tick_false and arg is not None and not self.reads_clock(arg)
        if name == "freezegun.freeze_time":
            arg = call.args[0] if call.args else next(
                (k.value for k in call.keywords if k.arg == "time_to_freeze"), None
            )
            return arg is not None and not self.reads_clock(arg)
        return False

    def _file_frozen(self) -> bool:
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            autouse = any(
                isinstance(dec, ast.Call)
                and self.dotted(dec.func) == "pytest.fixture"
                and any(
                    k.arg == "autouse" and isinstance(k.value, ast.Constant) and k.value.value is True
                    for k in dec.keywords
                )
                for dec in node.decorator_list
            )
            if autouse and any(self.frozen_call(n) for n in ast.walk(node)):
                return True
        return False

    def in_frozen_scope(self, node: ast.AST) -> bool:
        if self.file_frozen:
            return True
        cur = node
        while cur in self.parent:
            p = self.parent[cur]
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and cur in p.body:
                if any(self.frozen_call(d) for d in p.decorator_list):
                    return True
            if isinstance(p, (ast.With, ast.AsyncWith)) and cur in p.body:
                if any(self.frozen_call(item.context_expr) for item in p.items):
                    return True
            cur = p
        return False

    # -- repo-write taint
    def assigned_value(self, name: str, node: ast.AST) -> ast.AST | None:
        for a in [node, *self.ancestors(node)]:
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                params = a.args
                names = [p.arg for p in params.posonlyargs + params.args + params.kwonlyargs]
                if params.vararg:
                    names.append(params.vararg.arg)
                if params.kwarg:
                    names.append(params.kwarg.arg)
                if name in names:
                    return None
            if isinstance(a, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                for n in self.own_nodes(a):
                    if (
                        isinstance(n, ast.Assign)
                        and len(n.targets) == 1
                        and isinstance(n.targets[0], ast.Name)
                        and n.targets[0].id == name
                    ):
                        return n.value
                    if (
                        isinstance(n, ast.AnnAssign)
                        and isinstance(n.target, ast.Name)
                        and n.target.id == name
                        and n.value is not None
                    ):
                        return n.value
        return None

    def taint(self, node: ast.AST | None, depth: int = 0, first: bool = True) -> str | None:
        if node is None or depth > 6:
            return None
        if isinstance(node, ast.Constant):
            return "repo" if first and is_relative_literal(node.value) else None
        if isinstance(node, ast.JoinedStr):
            parts: list[str | None] = []
            lead = node.values[0] if node.values else None
            if first and isinstance(lead, ast.Constant) and is_relative_literal(lead.value):
                parts.append("repo")
            for i, v in enumerate(node.values):
                if isinstance(v, ast.FormattedValue):
                    parts.append(self.taint(v.value, depth + 1, first and i == 0))
            return combine_taint(parts)
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return "repo"
            if node.id in TMP_NAMES:
                return "tmp"
            value = self.assigned_value(node.id, node)
            return self.taint(value, depth + 1, first) if value is not None else None
        if isinstance(node, ast.Attribute):
            return self.taint(node.value, depth + 1, first) if node.attr in PATH_ATTRS else None
        if isinstance(node, ast.Subscript):
            return self.taint(node.value, depth + 1, first)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
            return combine_taint([self.taint(node.left, depth + 1, first), self.taint(node.right, depth + 1, False)])
        if isinstance(node, ast.IfExp):
            return combine_taint([self.taint(node.body, depth + 1, first), self.taint(node.orelse, depth + 1, first)])
        if isinstance(node, ast.Call):
            name = self.dotted(node.func)
            if name and name.startswith("tempfile."):
                return "tmp"
            if name in ("os.getcwd", "os.getcwdb", "pathlib.Path.cwd", "pathlib.PurePath.cwd"):
                return "repo"
            if name in PATH_CALLS:
                return combine_taint([self.taint(a, depth + 1, first and i == 0) for i, a in enumerate(node.args)])
            if isinstance(node.func, ast.Attribute):
                receiver = self.taint(node.func.value, depth + 1, first)
                return receiver if receiver in ("tmp", "repo") else None
        return None

    def write_mode(self, call: ast.Call) -> bool:
        mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == "mode"), None)
        return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and any(c in mode.value for c in "wax+")

    # -- the rules
    def run(self) -> list[Hit]:
        if self.kind == "gate":
            self.gate_rules()
        else:
            self.test_rules()
        return self.hits

    def gate_rules(self) -> None:
        subprocess_fns, runner_fns = self.runner_functions()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.match(r"^--reruns", node.value):
                self.add(node, "P2 retry")
            if isinstance(node, (ast.For, ast.AsyncFor, ast.While)) and self.retry_shaped(node):
                for stmt in node.body:
                    for n in ast.walk(stmt):
                        if isinstance(n, ast.Call) and self.runs_runner(n, subprocess_fns, runner_fns):
                            self.add(n, "P2 retry")  # on the line of the call, not of the loop

    def retry_shaped(self, loop: ast.AST) -> bool:
        """True when the loop repeats until success: it counts (range(...), a counter test) or it leaves the loop.
        A loop over a list of files or packages that runs the runner once per item, with no exit, is not a retry."""
        if isinstance(loop, (ast.For, ast.AsyncFor)):
            if isinstance(loop.iter, ast.Call) and self.dotted(loop.iter.func) == "range":
                return True
        elif isinstance(loop.test, ast.Compare):
            return True
        for stmt in loop.body:
            for n in ast.walk(stmt):
                if isinstance(n, (ast.Break, ast.Return)) or (isinstance(n, ast.Continue) and isinstance(loop, ast.While)):
                    return True
        return False

    def is_subprocess_call(self, call: ast.Call) -> bool:
        return self.dotted(call.func) in SUBPROCESS_CALLS

    def runs_runner(self, call: ast.Call, subprocess_fns: set[str], runner_fns: set[str]) -> bool:
        """A subprocess call with a runner name in its arguments, or a call to a function of this file that makes a
        subprocess call (with the runner name in this call's arguments, or inside that function)."""
        if self.is_subprocess_call(call):
            return self.passes_runner(call)
        if isinstance(call.func, ast.Name):
            if call.func.id in runner_fns:
                return True
            if call.func.id in subprocess_fns:
                return self.passes_runner(call)
        return False

    def passes_runner(self, call: ast.Call) -> bool:
        """True when any argument of the call names a test runner."""
        pieces = [
            n.value
            for a in [*call.args, *(k.value for k in call.keywords)]
            for n in ast.walk(a)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        ]
        return bool(RUNNER_TOKEN.search(" ".join(pieces)))

    def runner_functions(self) -> tuple[set[str], set[str]]:
        """(functions of this file that make a subprocess call, directly or through another one of them;
        the subset whose own body names a test runner in such a call)."""
        fns = {n.name: n for n in ast.walk(self.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        subs: set[str] = set()
        runners: set[str] = set()
        changed = True
        while changed:
            changed = False
            for name, fn in fns.items():
                for n in ast.walk(fn):
                    if not isinstance(n, ast.Call):
                        continue
                    callee = n.func.id if isinstance(n.func, ast.Name) else None
                    if self.is_subprocess_call(n) or callee in subs:
                        if name not in subs:
                            subs.add(name)
                            changed = True
                    if (self.is_subprocess_call(n) and self.passes_runner(n)) or callee in runners:
                        if name not in runners:
                            runners.add(name)
                            changed = True
        return subs, runners

    def test_rules(self) -> None:
        for node in ast.walk(self.tree):
            self.p1(node)
            self.p2(node)
            self.p3(node)
            self.p4(node)
            self.p5(node)
            self.p6(node)
            self.p8(node)
            self.p9(node)
        if Path(self.rel).name == "conftest.py":
            for node in ast.walk(self.tree):
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in ("collect_ignore", "collect_ignore_glob")
                    for t in node.targets
                ):
                    self.add(node, "P8 skip")

    # P1 — a raised or custom time limit
    def p1(self, node: ast.AST) -> None:
        if isinstance(node, ast.Attribute) and self.dotted(node) == "pytest.mark.timeout":
            self.add(node, "P1 time-limit")
            return
        if not isinstance(node, ast.Call):
            return
        name = self.dotted(node.func) or ""
        attr = (
            node.func.attr
            if isinstance(node.func, ast.Attribute)
            else (node.func.id if isinstance(node.func, ast.Name) else "")
        )
        has_timeout_kw = any(k.arg == "timeout" for k in node.keywords)
        hit = False
        if name in ("subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output"):
            hit = has_timeout_kw
        elif name in ("asyncio.wait_for", "asyncio.timeout", "asyncio.timeout_at"):
            hit = True
        elif name in ("asyncio.wait", "threading.Barrier") or name.endswith("urlopen"):
            hit = has_timeout_kw
        elif name.startswith(("requests.", "httpx.")):
            hit = has_timeout_kw
        elif attr == "settimeout":
            hit = True
        elif isinstance(node.func, ast.Attribute) and attr in WAIT_METHODS:
            hit = has_timeout_kw or any(is_number(a) for a in node.args)
        elif isinstance(node.func, ast.Attribute) and attr in ("get", "put"):
            hit = has_timeout_kw
        if hit:
            self.add(node, "P1 time-limit")

    # P2 — a retry
    def p2(self, node: ast.AST) -> None:
        if isinstance(node, ast.Attribute) and self.dotted(node) == "pytest.mark.flaky":
            self.add(node, "P2 retry")
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and self.imports.get(node.id) == "flaky.flaky"
        ):
            self.add(node, "P2 retry")

    # P3 — a real sleep, or polling against real time
    def p3(self, node: ast.AST) -> None:
        if isinstance(node, ast.Call):
            name = self.dotted(node.func)
            if name in ("time.sleep", "asyncio.sleep"):
                zero = (
                    bool(node.args)
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == 0
                    and not isinstance(node.args[0].value, bool)
                )
                if not zero:
                    self.add(node, "P3 sleep-or-poll")
            elif name == "threading.Timer":
                self.add(node, "P3 sleep-or-poll")
        elif isinstance(node, ast.While):
            if self.reads_clock(node.test):
                self.add(node, "P3 sleep-or-poll")
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            if self.reads_clock(node.iter):
                self.add(node, "P3 sleep-or-poll")

    # P4 — a real clock read
    def p4(self, node: ast.AST) -> None:
        if not isinstance(node, ast.Call):
            return
        # the loop's own clock (loop.time()) is not frozen by time_machine or freezegun
        if self.is_loop_time(node) or (self.dotted(node.func) in CLOCK_CALLS and not self.in_frozen_scope(node)):
            self.add(node, "P4 clock-read")

    # P5 — unseeded randomness
    def p5(self, node: ast.AST) -> None:
        if not isinstance(node, ast.Call):
            return
        name = self.dotted(node.func) or ""
        no_args = not node.args and not node.keywords
        hit = False
        if name.startswith("random.") and name.split(".", 1)[1] in RANDOM_FUNCS:
            hit = True
        elif name in ("random.Random", "random.seed"):
            hit = no_args or self.none_argument(node)
        elif name == "random.SystemRandom":
            hit = True
        elif name in ("numpy.random.default_rng", "numpy.random.RandomState"):
            hit = no_args
        elif name.startswith("numpy.random.") and name.rsplit(".", 1)[1] in NUMPY_LEGACY:
            hit = True
        elif name == "os.urandom" or name.startswith("secrets."):
            hit = True
        elif name.startswith("torch.") and name.split(".", 1)[1] in TORCH_RANDOM:
            scope = self.scope_of(node)
            seeded = any(
                isinstance(n, ast.Call)
                and self.dotted(n.func) == "torch.manual_seed"
                and getattr(n, "lineno", 0) < node.lineno
                for n in ast.walk(scope)
            )
            hit = not seeded
        if hit:
            self.add(node, "P5 unseeded-random")

    def none_argument(self, call: ast.Call) -> bool:
        """`random.Random(None)` / `random.seed(None)`: the explicit form of "no seed"."""
        arg = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg in ("a", "x")), None)
        return isinstance(arg, ast.Constant) and arg.value is None

    # P6 — a dynamic import or module reset inside a test, fixture, setUp or tearDown
    def p6(self, node: ast.AST) -> None:
        if not self.in_test_context(node):
            return
        if isinstance(node, ast.Call):
            name = self.dotted(node.func)
            if name in ("importlib.reload", "imp.reload"):
                self.add(node, "P6 dynamic-import")
                return
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in ("pop", "update", "setdefault")
                and self.dotted(node.func.value) == "sys.modules"
            ):
                self.add(node, "P6 dynamic-import")
                return
            if name == "importlib.import_module":
                scope = self.scope_of(node)
                if any(self.edits_sys_modules(n) and getattr(n, "lineno", 0) < node.lineno for n in ast.walk(scope)):
                    self.add(node, "P6 dynamic-import")
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)) and self.edits_sys_modules(node):
            self.add(node, "P6 dynamic-import")

    def edits_sys_modules(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Assign):
            targets: list[ast.AST] = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        elif isinstance(node, ast.Delete):
            targets = list(node.targets)
        elif isinstance(node, ast.Call):
            return (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in ("pop", "update", "setdefault")
                and self.dotted(node.func.value) == "sys.modules"
            )
        else:
            return False
        return any(isinstance(t, ast.Subscript) and self.dotted(t.value) == "sys.modules" for t in targets)

    # P8 — any skip, and a silent early return
    def p8(self, node: ast.AST) -> None:
        if isinstance(node, ast.Attribute):
            if self.dotted(node) in SKIP_ATTRS:
                self.add(node, "P8 skip")
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and self.imports.get(node.id) in SKIP_ATTRS:
                self.add(node, "P8 skip")
        elif isinstance(node, ast.Call):
            name = self.dotted(node.func)
            if name in SKIP_CALLS or (isinstance(node.func, ast.Attribute) and node.func.attr == "skipTest"):
                self.add(node, "P8 skip")
        elif isinstance(node, ast.Raise) and node.exc is not None:
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if self.dotted(target) in SKIP_EXCEPTIONS:
                self.add(node, "P8 skip")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            self.silent_return(node)

    def is_assertion(self, n: ast.AST) -> bool:
        if isinstance(n, ast.Assert):
            return True
        if isinstance(n, ast.Call):
            called = (
                n.func.attr
                if isinstance(n.func, ast.Attribute)
                else (n.func.id if isinstance(n.func, ast.Name) else "")
            )
            if called.startswith(("assert", "expect")):
                return True
        if isinstance(n, ast.With):
            for item in n.items:
                call = item.context_expr
                if isinstance(call, ast.Call) and self.dotted(call.func) in ("pytest.raises", "pytest.warns"):
                    return True
        return False

    def has_assertion(self, node: ast.AST) -> bool:
        return any(self.is_assertion(n) for n in ast.walk(node))

    def silent_return(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """A `return` the test reaches before its first assertion. Reported on the `return` line."""
        seen_assert = False
        for stmt in fn.body:
            asserts = self.has_assertion(stmt)
            if not seen_assert and not asserts:
                if isinstance(stmt, ast.Return):
                    self.add(stmt, "P8 skip")
                elif isinstance(stmt, ast.If):
                    for s in stmt.body:
                        if isinstance(s, ast.Return):
                            self.add(s, "P8 skip")
            if asserts:
                seen_assert = True
        self.try_returns(fn)

    def try_returns(self, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """A `return` inside a try/except that the test reaches before its first assertion: either the return
        comes before that assertion, or it sits in an except handler of a try whose body holds the assertion
        (an exception earlier in the body skips it). Reported on the `return` line."""
        own = self.own_nodes(fn)
        asserts = [(n.lineno, n.col_offset) for n in own if self.is_assertion(n)]
        first = min(asserts) if asserts else None
        for node in own:
            if not isinstance(node, ast.Try | ast.TryStar):
                continue
            handler_returns = {
                id(r)
                for h in node.handlers
                for r in self.own_nodes(h)
                if isinstance(r, ast.Return)
            }
            body_start = (node.body[0].lineno, node.body[0].col_offset)
            last = node.body[-1]
            body_end = (last.end_lineno or last.lineno, last.end_col_offset or 0)
            assertion_in_body = first is not None and body_start <= first <= body_end
            for r in self.own_nodes(node):
                if not isinstance(r, ast.Return):
                    continue
                before_first = first is None or (r.lineno, r.col_offset) < first
                if before_first or (id(r) in handler_returns and assertion_in_body):
                    self.add(r, "P8 skip")

    # P9 — a write to a real repo path
    def p9(self, node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and SQLITE_RELATIVE.search(node.value):
            self.add(node, "P9 repo-write")
            return
        if not isinstance(node, ast.Call):
            return
        name = self.dotted(node.func) or ""
        targets: list[ast.AST] = []
        if name in ("open", "io.open", "codecs.open"):
            if self.write_mode(node):
                target = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "file"), None)
                targets = [target] if target is not None else []
        elif name in WRITE_OS or name in WRITE_SHUTIL:
            args = list(node.args)
            if name in BOTH_TARGETS:
                targets = args[:2]
            elif name in DEST_ONLY:
                targets = args[1:2]
            else:
                targets = args[:1]
        elif isinstance(node.func, ast.Attribute) and node.func.attr in PATH_WRITE_METHODS | {"open"}:
            # a pathlib method; the receiver must really be a Path (a string's .replace is not a write)
            if node.func.attr == "open" and not self.write_mode_for_method(node):
                return
            if self.pathlib_origin(node.func.value) and self.taint(node.func.value) == "repo":
                self.add(node, "P9 repo-write")
            return
        if any(t is not None and self.taint(t) == "repo" for t in targets):
            self.add(node, "P9 repo-write")

    def pathlib_origin(self, node: ast.AST | None, depth: int = 0) -> bool:
        """True when the expression bottoms out at a pathlib.Path construction."""
        if node is None or depth > 6:
            return False
        if isinstance(node, ast.Call):
            name = self.dotted(node.func) or ""
            if name in (
                "pathlib.Path", "pathlib.PurePath", "pathlib.PosixPath", "pathlib.WindowsPath", "pathlib.Path.cwd",
                "pathlib.Path.home",
            ):
                return True
            if isinstance(node.func, ast.Attribute):
                return self.pathlib_origin(node.func.value, depth + 1)
            return False
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            return self.pathlib_origin(node.value, depth + 1)
        if isinstance(node, ast.BinOp):
            return self.pathlib_origin(node.left, depth + 1)
        if isinstance(node, ast.Name):
            value = self.assigned_value(node.id, node)
            return self.pathlib_origin(value, depth + 1) if value is not None else False
        return False

    def write_mode_for_method(self, call: ast.Call) -> bool:
        mode = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "mode"), None)
        return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and any(c in mode.value for c in "wax+")


# ---------------------------------------------------------------------------- Python config files


CONFIG_FILE_NAMES = ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")
PYTEST_HEADER = re.compile(r"^\s*\[(?:tool:)?pytest\]", re.M)


def pytest_configs(root: Path, files: list[str]) -> list[dict]:
    """Every pytest config file among `files`, with the folder it sits in (relative to the scan root, "" for the
    root itself). A file with no pytest section (a pyproject.toml of another tool, a tox.ini or setup.cfg without
    [pytest]) is not a pytest config and is left out, never a crash. One that does not parse is kept with
    failed=True, so the scan reports it as not checked."""
    out: list[dict] = []
    for rel in files:
        base = rel.rsplit("/", 1)[-1]
        if base not in CONFIG_FILE_NAMES or skipped_dir(rel):
            continue
        text = read(root / rel)
        entry: dict = {
            "rel": rel,
            "folder": rel.rsplit("/", 1)[0] if "/" in rel else "",
            "testpaths": [],
            "python_files": [],
            "failed": False,
        }
        try:
            if base == "pyproject.toml":
                tool = tomllib.loads(text).get("tool", {})
                pytest_table = tool.get("pytest", {}) if isinstance(tool, dict) else {}
                section = pytest_table.get("ini_options") if isinstance(pytest_table, dict) else None
                if not isinstance(section, dict):
                    continue
                tp = section.get("testpaths", [])
                pf = section.get("python_files", [])
                entry["testpaths"] = [tp] if isinstance(tp, str) else [str(x) for x in tp]
                entry["python_files"] = pf.split() if isinstance(pf, str) else [str(x) for x in pf]
            else:
                cp = configparser.ConfigParser(interpolation=None, strict=False)
                cp.read_string(text)
                sections = [s for s in ("pytest", "tool:pytest") if cp.has_section(s)]
                if not sections and base != "pytest.ini":
                    continue
                for sect in sections:
                    entry["testpaths"] += cp.get(sect, "testpaths", fallback="").split()
                    entry["python_files"] += cp.get(sect, "python_files", fallback="").split()
        except (tomllib.TOMLDecodeError, configparser.Error):
            if base not in ("pyproject.toml", "pytest.ini") and not PYTEST_HEADER.search(text):
                continue
            entry["failed"] = True
        out.append(entry)
    return out


def scan_pytest_config(name: str, text: str) -> list[Hit]:
    base = name.rsplit("/", 1)[-1]
    try:
        if base == "pyproject.toml":
            tomllib.loads(text)
        else:
            configparser.ConfigParser(interpolation=None, strict=False).read_string(text)
    except (tomllib.TOMLDecodeError, configparser.Error) as err:
        return [(1, "P0 parse error", f"{base} does not parse (file not checked): {err}"[:160])]
    hits: list[Hit] = []
    in_section = base == "pytest.ini"
    for i, raw in enumerate(text.split("\n")):
        line = raw.strip()
        if line.startswith("#") or line.startswith(";"):
            continue
        header = re.match(r"^\[([^\]]+)\]", line)
        if header:
            title = header.group(1).strip()
            in_section = title in ("pytest", "tool:pytest", "tool.pytest.ini_options") or (base == "pytest.ini")
            continue
        if not in_section:
            continue
        if re.match(r"^(?:timeout|timeout_method|faulthandler_timeout)\s*[=:]", line):
            hits.append((i + 1, "P1 time-limit", line[:160]))
        if re.match(r"^reruns\s*[=:]", line) or re.search(r"--reruns\b|-p\s+rerunfailures", line):
            hits.append((i + 1, "P2 retry", line[:160]))
        if (
            re.search(r"""(?:^|[\s'"])-m\s*['"]?\s*not\b""", line)
            or re.search(r"--deselect\b", line)
            or re.search(r"""(?:^|[\s'"=])-k\b""", line)
        ):
            hits.append((i + 1, "P8 skip", line[:160]))
    return hits


def scan_requirements(text: str) -> list[Hit]:
    hits: list[Hit] = []
    for i, raw in enumerate(text.split("\n")):
        line = raw.split("#", 1)[0]
        if re.search(r"pytest[-_]timeout", line, re.I):
            hits.append((i + 1, "P1 time-limit", raw.strip()[:160]))
        if re.search(r"pytest[-_]rerunfailures", line, re.I) or re.match(r"""^\s*["']?flaky\b""", line, re.I):
            hits.append((i + 1, "P2 retry", raw.strip()[:160]))
    return hits


# Scope rule: a .py file under a pytest testpath is in scope only if (a) its name matches the pytest
# `python_files` patterns (from the config that governs it when that config sets them, else `test_*.py` and
# `*_test.py`), (b) it is a conftest.py, or (c) it sits in a folder named tests, test or .test, or below one.
# Any other .py file under a testpath is product code and is not scanned. Each config's testpaths are
# resolved relative to the folder of the config file that declares them.
def python_test_scope(root: Path, files: list[str]) -> tuple[set[str], set[str]]:
    """Returns (test files, helper files) from the pytest configs' testpaths and python_files."""
    configs = [c for c in pytest_configs(root, files) if not c["failed"]]
    prefixes: list[str] = []
    for c in configs:
        declared = [t.strip().replace("\\", "/").removeprefix("./").strip("/") for t in c["testpaths"]]
        for tp in declared or [""]:
            prefixes.append(f"{c['folder']}/{tp}".strip("/") if c["folder"] else tp)

    def under(rel: str) -> bool:
        if not configs:
            return True
        return any(p == "" or rel == p or rel.startswith(p + "/") for p in prefixes)

    def governing_patterns(rel: str) -> list[str]:
        owners = [c for c in configs if c["folder"] == "" or rel.startswith(c["folder"] + "/")]
        owners.sort(key=lambda c: len(c["folder"]), reverse=True)
        for c in owners:
            if c["python_files"]:
                return c["python_files"]
        return ["test_*.py", "*_test.py"]

    tests: set[str] = set()
    helpers: set[str] = set()
    for rel in files:
        if not rel.endswith(".py") or skipped_dir(rel):
            continue
        base = rel.rsplit("/", 1)[-1]
        if base == "conftest.py":
            helpers.add(rel)
        elif not under(rel):
            continue
        elif any(fnmatch.fnmatch(base, pat) for pat in governing_patterns(rel)):
            tests.add(rel)
        elif any(seg in TEST_FOLDER_NAMES for seg in rel.split("/")[:-1]):
            helpers.add(rel)
    return tests, helpers


# ---------------------------------------------------------------------------- C# and Java (token rules)


REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%~^")
REGEX_KEYWORDS = {"return", "typeof", "case", "in", "of", "do", "else", "void", "delete", "throw", "new", "yield", "await"}


def regex_allowed_before(text: str, i: int) -> bool:
    """Whether a `/` at text[i] starts a regex literal (not a division): the token before it is an operator,
    an opening bracket, a keyword or nothing. `<` is left out so JSX closing tags are never read as regexes."""
    j = i - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j < 0:
        return True
    if text[j] in REGEX_PRECEDERS:
        return True
    m = re.search(r"[A-Za-z_$][\w$]*$", text[:j + 1])
    return m is not None and m.group(0) in REGEX_KEYWORDS


def regex_literal_end(text: str, i: int) -> int | None:
    """Index just after the closing `/` of the regex literal that opens at text[i], or None when the line ends first."""
    j = i + 1
    in_class = False
    n = len(text)
    while j < n and text[j] != "\n":
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            return j + 1
        j += 1
    return None


def blank_source(text: str, csharp: bool, ts: bool = False) -> str:
    """Replace comments, string literals and char literals with spaces (newlines kept, offsets kept).

    With ts=True, template literals are blanked too and `\"\"\"` is not a raw string."""
    out = list(text)
    n = len(text)
    i = 0

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if two == "//":
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
        elif two == "/*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
        elif ts and c == "`":
            start = i
            i += 1
            while i < n and text[i] != "`":
                i += 2 if text[i] == "\\" else 1
            i += 1
            blank(start, i)
        elif not ts and text.startswith('"""', i):
            j = text.find('"""', i + 3)
            j = n if j < 0 else j + 3
            blank(i, j)
            i = j
        elif csharp and re.match(r'(?:\$@|@\$|@)"', text[i:i + 3]):
            start = i
            i = text.index('"', i) + 1
            while i < n:
                if text[i] == '"':
                    if text[i + 1:i + 2] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            blank(start, i)
        elif c == '"' or (csharp and text[i:i + 2] == '$"'):
            start = i
            i = text.index('"', i) + 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
            blank(start, i)
        elif c == "'":
            start = i
            i += 1
            while i < n and text[i] != "'" and text[i] != "\n":
                i += 2 if text[i] == "\\" else 1
            i += 1
            blank(start, i)
        elif ts and c == "/" and regex_allowed_before(text, i):
            end = regex_literal_end(text, i)
            if end is None:
                i += 1
            else:
                blank(i, end)
                i = end
        else:
            i += 1
    return "".join(out)


def call_args(blanked: str, open_at: int, raw: str | None = None) -> list[tuple[int, int]]:
    """Top-level argument spans (start, end) of the call whose '(' is at open_at.

    An argument that is only a string literal is empty in `blanked`; pass `raw` so it still counts."""
    depth = 0
    spans: list[tuple[int, int]] = []
    start = open_at + 1
    for k in range(open_at, len(blanked)):
        ch = blanked[k]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                if (raw if raw is not None else blanked)[start:k].strip():
                    spans.append((start, k))
                return spans
        elif ch == "," and depth == 1:
            spans.append((start, k))
            start = k + 1
    return spans


def line_at(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


TMP_TOKEN = re.compile(
    r"GetTempPath|CreateTempSubdirectory|GetTempFileName|Files\s*\.\s*createTemp\w*|java\.io\.tmpdir"
)
ANCHOR_TOKEN = re.compile(
    r"AppContext\s*\.\s*BaseDirectory|Directory\s*\.\s*GetCurrentDirectory\s*\(\s*\)"
    r"|Environment\s*\.\s*CurrentDirectory|user\.dir"
)
# A path wrapper, plain or fully qualified (`Paths.get(`, `java.nio.file.Paths.get(`, `new java.io.File(`).
WRAPPER = re.compile(
    r"^\s*(?:(?:\w+\s*\.\s*)*(?:Paths\s*\.\s*get|Path\s*\.\s*of|Path\s*\.\s*Combine)"
    r"|new\s+(?:\w+\s*\.\s*)*(?:File|FileInfo))\s*\(\s*"
)


def text_taint(expr: str, whole: str, depth: int = 0) -> str | None:
    """'tmp', 'repo' or None for a path expression written in C# or Java source (raw text)."""
    if depth > 3:
        return None
    if TMP_TOKEN.search(expr):
        return "tmp"
    if ANCHOR_TOKEN.search(expr):
        return "repo"
    inner = WRAPPER.sub("", expr, count=1).strip()
    lit = re.match(r'^(?:@|\$@|@\$|\$)?"((?:[^"\\]|\\.)*)"', inner)
    if lit:
        return "repo" if is_relative_literal(lit.group(1)) else None
    ident = re.match(r"^(?:this\s*\.\s*)?([A-Za-z_]\w*)\s*(?:[,)]|$)", inner)
    if ident:
        decl = re.search(
            r"\b(?:var|string|auto|Path|File|String|DirectoryInfo)\s+"
            + re.escape(ident.group(1))
            + r"\s*=\s*([^;]+);",
            whole,
        )
        if decl:
            return text_taint(decl.group(1), whole, depth + 1)
    return None


class TokenRule:
    def __init__(self, label: str, pattern: str, check=None) -> None:
        self.label = label
        self.regex = re.compile(pattern)
        self.check = check


def cs_rules() -> list[TokenRule]:
    def task_wait_has_timeout(m: re.Match[str], raw: str, blanked: str) -> bool:
        spans = call_args(blanked, m.end() - 1)
        return len(spans) > 1

    def delay_not_zero(m: re.Match[str], raw: str, blanked: str) -> bool:
        spans = call_args(blanked, m.end() - 1)
        return not spans or raw[spans[0][0]:spans[0][1]].strip() != "0"

    def write_to_repo(m: re.Match[str], raw: str, blanked: str) -> bool:
        spans = call_args(blanked, m.end() - 1, raw)
        if not spans:
            return False
        taints = [text_taint(raw[a:b], raw) for a, b in spans]
        return "tmp" not in taints and text_taint(raw[spans[0][0]:spans[0][1]], raw) == "repo"

    return [
        TokenRule("P1 time-limit", r"\[\s*(?:Fact|Theory)\s*\([^\]]*\bTimeout\s*="),
        TokenRule("P1 time-limit", r"\.\s*Wait\s*\(\s*[^)\s]"),
        TokenRule("P1 time-limit", r"\.\s*WaitOne\s*\(\s*[^)\s]"),
        TokenRule("P1 time-limit", r"\.\s*WaitAsync\s*\("),
        TokenRule("P1 time-limit", r"\bTask\s*\.\s*Wait(?:All|Any)\s*\(", task_wait_has_timeout),
        TokenRule("P1 time-limit", r"\.\s*CancelAfter\s*\("),
        TokenRule("P1 time-limit", r"\bnew\s+CancellationTokenSource\s*\(\s*[^)\s]"),
        TokenRule("P2 retry", r"\[\s*Retry(?:Fact|Theory)\b"),
        TokenRule("P3 sleep-or-poll", r"\bThread\s*\.\s*Sleep\s*\("),
        TokenRule("P3 sleep-or-poll", r"\bTask\s*\.\s*Delay\s*\(", delay_not_zero),
        TokenRule("P3 sleep-or-poll", r"\bSpinWait\s*\.\s*SpinUntil\s*\("),
        TokenRule("P3 sleep-or-poll", r"\bnew\s+(?:System\s*\.\s*Threading\s*\.\s*)?Timer\s*\("),
        TokenRule("P3 sleep-or-poll", r"\bnew\s+PeriodicTimer\s*\("),
        TokenRule("P4 clock-read", r"\bDateTime(?:Offset)?\s*\.\s*(?:Now|UtcNow|Today)\b"),
        TokenRule("P4 clock-read", r"\bStopwatch\b"),
        TokenRule("P4 clock-read", r"\bEnvironment\s*\.\s*TickCount(?:64)?\b"),
        TokenRule("P4 clock-read", r"\bTimeProvider\s*\.\s*System\b"),
        TokenRule("P4 clock-read", r"\busing\s+static\s+System\s*\.\s*DateTime\s*;"),
        TokenRule("P5 unseeded-random", r"\bnew\s+Random\s*\(\s*\)"),
        TokenRule("P5 unseeded-random", r"\bRandom\s*\.\s*Shared\b"),
        TokenRule("P5 unseeded-random", r"\bRandomNumberGenerator\b"),
        TokenRule("P8 skip", r"\[\s*(?:Fact|Theory)\s*\([^\]]*\bSkip\s*="),
        TokenRule("P8 skip", r"\[\s*SkippableFact\b"),
        TokenRule("P8 skip", r"\bSkip\s*\.\s*If(?:Not)?\s*\("),
        TokenRule(
            "P9 repo-write",
            r"\bFile\s*\.\s*(?:Write\w*|Create|Delete|Move|Copy|Replace|AppendAll\w*)\s*\(",
            write_to_repo,
        ),
        TokenRule("P9 repo-write", r"\bDirectory\s*\.\s*(?:CreateDirectory|Delete|Move)\s*\(", write_to_repo),
    ]


def java_rules() -> list[TokenRule]:
    def write_to_repo(m: re.Match[str], raw: str, blanked: str) -> bool:
        spans = call_args(blanked, m.end() - 1, raw)
        if not spans:
            return False
        taints = [text_taint(raw[a:b], raw) for a, b in spans]
        return "tmp" not in taints and text_taint(raw[spans[0][0]:spans[0][1]], raw) == "repo"

    return [
        TokenRule("P1 time-limit", r"@Timeout\b"),
        TokenRule("P1 time-limit", r"\bassertTimeout(?:Preemptively)?\s*\("),
        TokenRule("P1 time-limit", r"\.\s*get\s*\(\s*[^,()]+,\s*(?:\w+\s*\.\s*)*TimeUnit\s*\."),
        TokenRule("P1 time-limit", r"\.\s*await\s*\(\s*[^,()]+,\s*(?:\w+\s*\.\s*)*TimeUnit\s*\."),
        TokenRule("P1 time-limit", r"\.\s*join\s*\(\s*\d"),
        TokenRule("P2 retry", r"@(?:RetryingTest|RepeatedIfExceptionsTest)\b"),
        TokenRule("P3 sleep-or-poll", r"\bThread\s*\.\s*sleep\s*\("),
        TokenRule("P3 sleep-or-poll", r"\bTimeUnit\s*\.\s*\w+\s*\.\s*sleep\s*\("),
        TokenRule("P3 sleep-or-poll", r"\bAwaitility\b"),
        TokenRule("P3 sleep-or-poll", r"\bawait\s*\(\s*\)\s*\.\s*(?:atMost|until|pollInterval)"),
        TokenRule("P4 clock-read", r"\bSystem\s*\.\s*(?:currentTimeMillis|nanoTime)\s*\("),
        TokenRule(
            "P4 clock-read",
            r"\b(?:Instant|LocalDate|LocalDateTime|LocalTime|ZonedDateTime|OffsetDateTime)\s*\.\s*now\s*\(\s*\)",
        ),
        TokenRule("P4 clock-read", r"\bClock\s*\.\s*system(?:UTC|DefaultZone)\s*\("),
        TokenRule("P5 unseeded-random", r"\bnew\s+(?:\w+\s*\.\s*)*Random\s*\(\s*\)"),
        TokenRule("P5 unseeded-random", r"\bMath\s*\.\s*random\s*\("),
        TokenRule("P5 unseeded-random", r"\bThreadLocalRandom\b"),
        TokenRule("P5 unseeded-random", r"\bnew\s+(?:\w+\s*\.\s*)*SecureRandom\s*\(\s*\)"),
        TokenRule("P8 skip", r"@Disabled\b"),
        TokenRule("P8 skip", r"@(?:Disabled|Enabled)On\w+"),
        TokenRule("P8 skip", r"@(?:Enabled|Disabled)If\w*"),
        TokenRule("P8 skip", r"\bAssumptions\s*\.\s*assume\w*\s*\("),
        TokenRule("P8 skip", r"\bassume(?:True|False|That|NotNull)\s*\("),
        TokenRule(
            "P9 repo-write",
            r"\bFiles\s*\.\s*(?:write\w*|newBufferedWriter|createDirector(?:y|ies)|createFile|delete\w*|move|copy)\s*\(",
            write_to_repo,
        ),
        TokenRule(
            "P9 repo-write", r"\bnew\s+(?:\w+\s*\.\s*)*(?:FileOutputStream|FileWriter)\s*\(", write_to_repo
        ),
    ]


def bracket_error(blanked: str) -> tuple[int, str] | None:
    """The first break in the nesting of ( [ { in source whose comments, strings and regex literals are blanked:
    (line, message), or None. The token stacks have no parser, so this is their parse check."""
    closing = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    for i, ch in enumerate(blanked):
        if ch in "([{":
            stack.append((ch, i))
        elif ch in ")]}":
            if not stack or stack[-1][0] != closing[ch]:
                return line_at(blanked, i), f"unexpected '{ch}'"
            stack.pop()
    if stack:
        ch, i = stack[-1]
        return line_at(blanked, i), f"'{ch}' is never closed"
    return None


def scan_tokens(text: str, rules: list[TokenRule], csharp: bool) -> list[Hit]:
    blanked = blank_source(text, csharp)
    lines = text.splitlines()
    hits: list[Hit] = []
    broken = bracket_error(blanked)
    if broken is not None:
        return [(broken[0], "P0 parse error", f"file not checked: {broken[1]}")]
    for rule in rules:
        for m in rule.regex.finditer(blanked):
            if rule.check is not None and not rule.check(m, text, blanked):
                continue
            line = line_at(text, m.start())
            hits.append((line, rule.label, snippet(lines, line)))
    return hits


def scan_xml_config(text: str, stack: str) -> list[Hit]:
    """pom.xml, *.csproj and *.runsettings: no limit, no retry, no excluded test."""
    text = re.sub(r"<!--.*?-->", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)
    lines = text.split("\n")
    checks = [
        ("P1 time-limit", r"forkedProcessTimeoutInSeconds|forkedProcessExitTimeoutInSeconds|TestSessionTimeout"),
        ("P2 retry", r"rerunFailingTestsCount|xunit\.retry|Xunit\.Retry|Retry(?:Fact|Theory)"),
    ]
    # surefire / failsafe plugin blocks: the only place a pom leaves a test out of the run
    in_runner_plugin = [False] * len(lines)
    inside = False
    for i, ln in enumerate(lines):
        if re.search(r"<artifactId>\s*maven-(?:surefire|failsafe)-plugin\s*</artifactId>", ln):
            inside = True
        in_runner_plugin[i] = inside
        if inside and re.search(r"</plugin>", ln):
            inside = False
    hits: list[Hit] = []
    for i, ln in enumerate(lines):
        for label, pat in checks:
            if re.search(pat, ln, re.I):
                hits.append((i + 1, label, ln.strip()[:160]))
        if in_runner_plugin[i] and re.search(
            r"<\s*skipTests\b|<\s*skip\s*>|<\s*excludes?\b|<\s*excludedGroups\b", ln, re.I
        ):
            hits.append((i + 1, "P8 skip", ln.strip()[:160]))
    return hits


# ---------------------------------------------------------------------------- TypeScript (token rules, --stack ts)

TS_CALL = re.compile(
    r"(?<![\w$.])(it|test|bench|describe|suite|beforeAll|beforeEach|afterAll|afterEach)((?:\s*\.\s*\w+)*)\s*\("
)
TS_HOOKS = {"beforeAll", "beforeEach", "afterAll", "afterEach"}
TS_BODY = {"it", "test", "bench"} | TS_HOOKS
TS_TEST_LEVEL = {"it", "test", "bench"}
TS_SKIP_PROPS = {"skip", "skipIf", "runIf", "todo", "only", "fails", "fixme"}
TS_CLOCK = re.compile(
    r"(?<![\w$.])(?:(?:globalThis|window|self)\s*\.\s*)?(?:Date|performance)\s*\.\s*now\s*\("
    r"|new\s+Date\s*\(\s*\)|process\s*\.\s*hrtime"
)
TS_FS_MODULES = r"(?:node:)?(?:fs|fs/promises|fs-extra)"
TS_WRITE_FNS = (
    "writeFile", "writeFileSync", "appendFile", "appendFileSync", "mkdir", "mkdirSync", "mkdtemp", "mkdtempSync",
    "rm", "rmSync", "rmdir", "rmdirSync", "unlink", "unlinkSync", "rename", "renameSync", "copyFile", "copyFileSync",
    "cp", "cpSync", "createWriteStream", "truncate", "truncateSync", "outputFile", "outputFileSync", "ensureDir",
    "ensureDirSync", "remove", "removeSync", "emptyDir", "emptyDirSync", "move", "moveSync", "copy", "copySync",
)
TS_DEST_ONLY = {"copyFile", "copyFileSync", "cp", "cpSync", "copy", "copySync"}
TS_BOTH = {"rename", "renameSync", "move", "moveSync"}
TS_ROUTES_MODULE = re.compile(r"(^|/)router(/(routes|index))?(\.[cm]?[jt]s)?$")
TS_ROUTER_INDEX_MODULE = re.compile(r"(^|/)router(/index)?(\.[cm]?[jt]s)?$")


def ts_taint(expr: str, whole: str, depth: int = 0, first: bool = True) -> str | None:
    """'tmp', 'repo' or None for a path expression written in TypeScript (raw text)."""
    e = expr.strip()
    if depth > 4 or not e:
        return None
    if re.search(r"\btmpdir\s*\(|\bmkdtemp(?:Sync)?\s*\(", e):
        return "tmp"
    if re.search(r"__dirname|__filename|import\s*\.\s*meta\s*\.|process\s*\.\s*cwd\s*\(\s*\)", e):
        return "repo"
    wrapper = re.match(r"(?:path\s*\.\s*)?(?:join|resolve|normalize|dirname|basename)\s*\(\s*", e)
    inner = e[wrapper.end():] if wrapper else e
    lit = re.match(r"""(['"`])((?:\\.|(?!\1).)*)\1""", inner)
    if lit:
        return "repo" if first and not lit.group(2).startswith("${") and is_relative_literal(lit.group(2)) else None
    ident = re.match(r"([A-Za-z_$][\w$]*)\s*(?:[,)]|$)", inner)
    if ident:
        decl = re.search(r"\b(?:const|let|var)\s+" + re.escape(ident.group(1)) + r"\b[^=;]*=\s*([^;\n]+)", whole)
        if decl:
            return ts_taint(decl.group(1), whole, depth + 1, first)
    return None


def ts_alias_map(root: Path) -> dict[str, Path]:
    aliases: dict[str, Path] = {}
    for name in ("vite.config.ts", "vitest.config.ts", "vite.config.mts", "vite.config.js"):
        p = root / name
        if not p.is_file():
            continue
        text = read(p)
        for m in re.finditer(r"""['"](@[\w-]*|~)['"]\s*:\s*fileURLToPath\(\s*new URL\(\s*['"]([^'"]+)['"]""", text):
            aliases[m.group(1)] = (root / m.group(2)).resolve()
        for m in re.finditer(r"""['"](@[\w-]*|~)['"]\s*:\s*path\.resolve\(\s*__dirname\s*,\s*['"]([^'"]+)['"]""", text):
            aliases[m.group(1)] = (root / m.group(2)).resolve()
    return aliases


def ts_resolve(spec: str, from_dir: Path, aliases: dict[str, Path]) -> str | None:
    base: Path | None = None
    if spec.startswith("."):
        base = (from_dir / spec).resolve()
    else:
        for alias, target in aliases.items():
            if spec == alias:
                base = target
                break
            if spec.startswith(alias + "/"):
                base = target / spec[len(alias) + 1:]
                break
    if base is None:
        return None
    out = base.as_posix()
    out = re.sub(r"\.[cm]?[jt]sx?$", "", out)
    return re.sub(r"/index$", "", out)


def ts_module_file(base: str) -> Path | None:
    for ext in (".ts", ".mts", ".js", ".mjs"):
        if Path(base + ext).is_file():
            return Path(base + ext)
    for ext in (".ts", ".mts", ".js", ".mjs"):
        if (Path(base) / f"index{ext}").is_file():
            return Path(base) / f"index{ext}"
    return None


class TsScan:
    def __init__(self, rel: str, raw: str, kind: str, root: Path, path: Path) -> None:
        self.rel = rel
        self.raw = raw
        self.kind = kind  # "test" or "config"
        self.root = root
        self.path = path
        self.b = blank_source(raw, False, True)
        self.lines = raw.splitlines()
        self.hits: list[Hit] = []
        self.calls = self._calls()
        self._timers: list[dict] | None = None
        self._spies: list[dict] | None = None

    # -- helpers
    def hit(self, idx: int, label: str) -> None:
        line = self.raw.count("\n", 0, idx) + 1
        self.hits.append((line, label, snippet(self.lines, line)))

    def close_of(self, open_at: int) -> int | None:
        depth = 0
        for k in range(open_at, len(self.b)):
            ch = self.b[k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    return k
        return None

    def expr_end(self, start: int) -> int:
        depth = 0
        seen = False
        for k in range(start, len(self.b)):
            ch = self.b[k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    return k
                depth -= 1
            elif depth == 0 and ch in ",;":
                return k
            elif depth == 0 and ch == "\n" and seen:
                return k
            if not ch.isspace():
                seen = True
        return len(self.b)

    def first(self, a: int, b: int) -> int:
        seg = self.raw[a:b]
        return a + (len(seg) - len(seg.lstrip()))

    def args(self, open_at: int) -> list[tuple[int, int]]:
        return call_args(self.b, open_at, self.raw)

    def _calls(self) -> list[dict]:
        """Every it/test/describe/hook call (including the curried call after `.each(table)` or
        `.skipIf(cond)`), as {root, open, close, start, members}. A `.each(table)` call is data, not a test call."""
        out: list[dict] = []
        for m in TS_CALL.finditer(self.b):
            members = re.findall(r"\w+", m.group(2))
            open_at = m.end() - 1
            first = True
            while True:
                close = self.close_of(open_at)
                if close is None:
                    break
                data_table = first and bool(members) and members[-1] in ("each", "for")
                if not data_table:
                    out.append(
                        {"root": m.group(1), "open": open_at, "close": close, "start": m.start(), "members": members}
                    )
                j = close + 1
                while j < len(self.b) and self.b[j].isspace():
                    j += 1
                if members and j < len(self.b) and self.b[j] == "(":
                    open_at = j
                    first = False
                    continue
                break
        return out

    def containing(self, pos: int) -> list[dict]:
        return sorted((c for c in self.calls if c["open"] < pos < c["close"]), key=lambda c: c["open"])

    def effective_scope(self, pos: int):
        cs = self.containing(pos)
        if not cs:
            return "FILE"
        inner = cs[-1]
        if inner["root"] in TS_HOOKS:
            if inner["root"].startswith("after"):
                return None
            outer = [c for c in cs[:-1] if c["root"] not in TS_HOOKS]
            return outer[-1]["open"] if outer else "FILE"
        return inner["open"]

    def chain(self, pos: int) -> list:
        return [*(c["open"] for c in reversed(self.containing(pos)) if c["root"] not in TS_HOOKS), "FILE"]

    def test_level(self, scope) -> bool:
        return scope != "FILE" and any(c["open"] == scope and c["root"] in TS_TEST_LEVEL for c in self.calls)

    def timers(self) -> list[dict]:
        if self._timers is None:
            out: list[dict] = []
            for m in re.finditer(
                r"(?<![\w$.])(?:vi|vitest)\s*\.\s*(useFakeTimers|useRealTimers|setSystemTime)\s*\(", self.b
            ):
                open_at = m.end() - 1
                close = self.close_of(open_at)
                scope = self.effective_scope(m.start())
                if close is None or scope is None:
                    continue
                arg = self.raw[open_at + 1:close]
                clockless = not TS_CLOCK.search(arg)
                kind = {"useFakeTimers": "fake", "useRealTimers": "real", "setSystemTime": "sys"}[m.group(1)]
                out.append({
                    "pos": m.start(), "open": open_at, "close": close, "kind": kind, "scope": scope,
                    "pinned": kind == "fake" and bool(re.search(r"\bnow\s*:", arg)) and clockless,
                    "fixed": kind == "sys" and bool(arg.strip()) and clockless,
                })
            self._timers = out
        return self._timers

    def faked_state(self, pos: int) -> tuple[bool, bool]:
        faked = pinned_by_fake = pinned_by_sys = False
        for scope in self.chain(pos):
            here = [t for t in self.timers() if t["scope"] == scope]
            test_level = self.test_level(scope)
            # a switch back to real timers ends the freeze only for what comes after it (a test may clean up at its end)
            if any(t["kind"] == "real" and (not test_level or t["pos"] < pos) for t in here):
                continue
            fakes = [t for t in here if t["kind"] == "fake" and (not test_level or t["pos"] < pos)]
            if fakes:
                faked = True
                pinned_by_fake = pinned_by_fake or any(t["pinned"] for t in fakes)
            if any(t["kind"] == "sys" and t["fixed"] and (not test_level or t["pos"] < pos) for t in here):
                pinned_by_sys = True
        # A clock fixed by a literal `vi.setSystemTime(<fixed>)` counts on its own (vitest then mocks Date alone); a
        # `vi.useFakeTimers({ now: <fixed> })` counts only as the fake timers that carry it.
        return faked, (faked and pinned_by_fake) or pinned_by_sys

    def spies(self) -> list[dict]:
        if self._spies is None:
            out: list[dict] = []
            mock = r"\s*\.\s*(?:mockReturnValue|mockReturnValueOnce|mockImplementation|mockImplementationOnce)\s*\("
            for m in re.finditer(
                r"""(?<![\w$.])(?:vi|vitest)\s*\.\s*spyOn\s*\(\s*Math\s*,\s*['"]random['"]\s*\)""", self.raw
            ):
                mocked = bool(re.match(mock, self.raw[m.end():]))
                if not mocked:
                    decl = re.search(r"(?:const|let|var)\s+(\w+)\s*=\s*$", self.raw[:m.start()])
                    if decl:
                        mocked = bool(re.search(r"(?<![\w$.])" + re.escape(decl.group(1)) + mock, self.raw))
                scope = self.effective_scope(m.start())
                if mocked and scope is not None:
                    out.append({"pos": m.start(), "scope": scope})
            self._spies = out
        return self._spies

    def random_spied(self, pos: int) -> bool:
        for scope in self.chain(pos):
            test_level = self.test_level(scope)
            if any(s["scope"] == scope and (not test_level or s["pos"] < pos) for s in self.spies()):
                return True
        return False

    def in_loop(self, pos: int) -> bool:
        cs = self.containing(pos)
        floor = cs[-1]["open"] if cs else -1
        for m in re.finditer(r"(?<![\w$.])(?:for|while)\s*\(|(?<![\w$.])do\s*\{", self.b):
            if m.start() < floor:
                continue
            if m.group(0).startswith("do"):
                body_open = m.end() - 1
            else:
                head_close = self.close_of(m.end() - 1)
                if head_close is None:
                    continue
                if m.start() < pos < head_close:
                    return True
                j = head_close + 1
                while j < len(self.b) and self.b[j].isspace():
                    j += 1
                if j >= len(self.b) or self.b[j] != "{":
                    continue
                body_open = j
            body_close = self.close_of(body_open)
            if body_close is not None and body_open < pos < body_close:
                return True
        return False

    # -- the rules
    def run(self) -> list[Hit]:
        broken = bracket_error(self.b)
        if broken is not None:  # not parsed, so not checked: that alone is the hit
            self.hits.append((broken[0], "P0 parse error", f"file not checked: {broken[1]}"))
            return self.hits
        if self.kind == "config":
            self.config_rules()
        else:
            for rule in (self.p1, self.p2, self.p3, self.p4, self.p5, self.p6, self.p7, self.p8, self.p9):
                rule()
        return self.hits

    def config_rules(self) -> None:
        name = Path(self.rel).name.lower()
        for m in re.finditer(r"(?<![\w$.])(?:testTimeout|hookTimeout|teardownTimeout)\s*:", self.b):
            self.hit(m.start(), "P1 time-limit")
        if name.startswith("playwright"):
            for m in re.finditer(r"(?<![\w$.])(?:timeout|globalTimeout|actionTimeout|navigationTimeout)\s*:", self.b):
                self.hit(m.start(), "P1 time-limit")
        for m in re.finditer(r"(?<![\w$.])(?:retry|retries)\s*:", self.b):
            self.hit(m.start(), "P2 retry")
        for m in re.finditer(r"(?<![\w$.])(?:include|exclude)\s*:", self.b):
            if re.search(r"process\s*\.\s*env", self.b[m.end():self.expr_end(m.end())]):
                self.hit(m.start(), "P8 skip")

    # P1 — a raised or custom time limit
    def p1(self) -> None:
        for c in self.calls:
            seen_callback = False
            for a, b in self.args(c["open"]):
                t = self.b[a:b].strip()
                r = self.raw[a:b].strip()
                if t.startswith("{"):
                    if re.search(r"(?<![\w$.])timeout\s*:", t):
                        self.hit(self.first(a, b), "P1 time-limit")
                elif "=>" in t or re.match(r"(?:async\s+)?function\b", t):
                    seen_callback = True
                elif re.fullmatch(r"[-+]?\d[\d_]*(?:\.\d+)?", r):
                    self.hit(self.first(a, b), "P1 time-limit")
                elif seen_callback and r and c["root"] != "bench":
                    self.hit(self.first(a, b), "P1 time-limit")
        for m in re.finditer(
            r"(?<![\w$.])vi\s*\.\s*setConfig\s*\(|(?<![\w$.])test\s*\.\s*(?:setTimeout|slow)\s*\(", self.b
        ):
            self.hit(m.start(), "P1 time-limit")
        if re.search(r"""from\s+['"](?:@playwright/test|playwright)['"]""", self.raw):
            for m in re.finditer(r"(?<![\w$.])(?:timeout|globalTimeout|actionTimeout|navigationTimeout)\s*:", self.b):
                self.hit(m.start(), "P1 time-limit")

    # P2 — a retry
    def p2(self) -> None:
        for c in self.calls:
            for a, b in self.args(c["open"]):
                t = self.b[a:b].strip()
                if t.startswith("{") and re.search(r"(?<![\w$.])(?:retry|retries)\s*:", t):
                    self.hit(self.first(a, b), "P2 retry")

    # sleeps through node:timers/promises (setTimeout, setInterval, scheduler.wait), under any imported name
    def promise_timer_sleeps(self) -> set[int]:
        """Reports those sleeps; returns the positions of the calls it saw (the global-setTimeout rule skips them)."""
        source = r"""['"](?:node:)?timers/promises['"]"""
        named: dict[str, str] = {}
        spaces: set[str] = set()
        for m in re.finditer(r"import\s*\{([^}]*)\}\s*from\s*" + source, self.raw):
            for part in m.group(1).split(","):
                bits = re.split(r"\s+as\s+", part.strip())
                if bits[0]:
                    named[bits[-1].strip()] = bits[0].strip()
        for m in re.finditer(r"import\s+(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)\s+from\s*" + source, self.raw):
            spaces.add(m.group(1))
        loaded = r"=\s*(?:await\s+)?(?:require\s*\(|import\s*\()\s*" + source
        for m in re.finditer(r"(?:const|let|var)\s*\{([^}]*)\}\s*" + loaded, self.raw):
            for part in m.group(1).split(","):
                bits = re.split(r"\s*:\s*", part.strip())
                if bits[0]:
                    named[bits[-1].split("=")[0].strip()] = bits[0].strip()
        for m in re.finditer(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*" + loaded, self.raw):
            spaces.add(m.group(1))
        seen: set[int] = set()
        calls: list[tuple[int, str]] = []
        for local, fn in named.items():
            if fn in ("setTimeout", "setInterval"):
                for m in re.finditer(r"(?<![\w$.])" + re.escape(local) + r"\s*\(", self.b):
                    calls.append((m.start(), fn))
            elif fn == "scheduler":
                for m in re.finditer(r"(?<![\w$.])" + re.escape(local) + r"\s*\.\s*wait\s*\(", self.b):
                    calls.append((m.start(), "wait"))
        for local in spaces:
            for m in re.finditer(
                r"(?<![\w$.])" + re.escape(local) + r"\s*\.\s*(setTimeout|setInterval)\s*\(", self.b
            ):
                calls.append((m.start(), m.group(1)))
            for m in re.finditer(r"(?<![\w$.])" + re.escape(local) + r"\s*\.\s*scheduler\s*\.\s*wait\s*\(", self.b):
                calls.append((m.start(), "wait"))
        for pos, fn in calls:
            seen.add(pos)
            open_at = self.b.index("(", pos)
            spans = self.args(open_at)
            zero = bool(spans) and self.raw[spans[0][0]:spans[0][1]].strip() == "0"
            # the delay is the first argument here; one zero-delay yield stays allowed, as with the global setTimeout
            if fn == "setInterval" or not zero or self.in_loop(pos):
                self.hit(pos, "P3 sleep-or-poll")
        return seen

    # P3 — a real sleep, or polling against real time
    def p3(self) -> None:
        promise_calls = self.promise_timer_sleeps()
        for m in re.finditer(
            r"(?<![\w$.])(?:(?:globalThis|window|self|global)\s*\.\s*)?(setTimeout|setInterval)\s*\(", self.b
        ):
            if m.start() in promise_calls:
                continue
            open_at = m.end() - 1
            spans = self.args(open_at)
            zero = len(spans) < 2 or self.raw[spans[1][0]:spans[1][1]].strip() == "0"
            needs = m.group(1) == "setInterval" or not zero or self.in_loop(m.start())
            if needs and not self.faked_state(m.start())[0]:
                self.hit(m.start(), "P3 sleep-or-poll")
        for m in re.finditer(r"(?<![\w$.])(?:(?:globalThis|window|self)\s*\.\s*)?requestAnimationFrame\s*\(", self.b):
            for p in re.finditer(r"new\s+Promise\s*\(", self.b):
                close = self.close_of(p.end() - 1)
                if close is not None and p.end() - 1 < m.start() < close:
                    self.hit(m.start(), "P3 sleep-or-poll")
                    break
        for m in re.finditer(
            r"(?<![\w$.])vi\s*\.\s*(?:waitFor|waitUntil)\s*\(|(?<![\w$.])expect\s*\.\s*poll\s*\(", self.b
        ):
            self.hit(m.start(), "P3 sleep-or-poll")
        tl: dict[str, str] = {}
        for m in re.finditer(r"""import\s*\{([^}]*)\}\s*from\s*['"]@testing-library/[^'"]+['"]""", self.raw):
            for part in m.group(1).split(","):
                bits = re.split(r"\s+as\s+", part.strip())
                if bits[0]:
                    tl[bits[-1].strip()] = bits[0].strip()
        for local, imported in tl.items():
            if imported in ("waitFor", "waitForElementToBeRemoved") or re.match(r"find(?:All)?By", imported):
                for m in re.finditer(r"(?<![\w$.])" + re.escape(local) + r"\s*\(", self.b):
                    self.hit(m.start(), "P3 sleep-or-poll")
            if imported == "screen":
                for m in re.finditer(r"(?<![\w$.])" + re.escape(local) + r"\s*\.\s*find(?:All)?By\w*\s*\(", self.b):
                    self.hit(m.start(), "P3 sleep-or-poll")
            if imported == "within":
                for m in re.finditer(r"(?<![\w$.])" + re.escape(local) + r"\s*\(", self.b):
                    close = self.close_of(m.end() - 1)
                    if close is not None and re.match(r"\s*\.\s*find(?:All)?By", self.b[close + 1:]):
                        self.hit(m.start(), "P3 sleep-or-poll")
        for m in re.finditer(r"\.\s*waitForTimeout\s*\(", self.b):
            self.hit(m.start(), "P3 sleep-or-poll")
        for m in re.finditer(r"""\.\s*waitForLoadState\s*\(\s*['"]networkidle['"]""", self.raw):
            self.hit(m.start(), "P3 sleep-or-poll")
        for m in re.finditer(r"(?<![\w$.])(?:for|while)\s*\(", self.b):
            head_close = self.close_of(m.end() - 1)
            if head_close is not None and TS_CLOCK.search(self.raw[m.end():head_close]):
                self.hit(m.start(), "P3 sleep-or-poll")

    # P4 — a real clock read
    def p4(self) -> None:
        reads: list[int] = []
        for m in re.finditer(
            r"(?<![\w$.])(?:(?:globalThis|window|self)\s*\.\s*)?(?:Date|performance)\s*\.\s*now\s*\("
            r"|process\s*\.\s*hrtime\s*(?:\.\s*bigint\s*)?\(",
            self.b,
        ):
            reads.append(m.start())
        for m in re.finditer(r"new\s+Date\s*\(", self.b):
            close = self.close_of(m.end() - 1)
            if close is not None and not self.raw[m.end():close].strip():
                reads.append(m.start())
        for m in re.finditer(r"(?<![\w$.])Date\s*\(", self.b):
            if not re.search(r"new\s*$", self.b[:m.start()]):
                reads.append(m.start())
        in_set_system_time = [t for t in self.timers() if t["kind"] == "sys"]
        for pos in sorted(set(reads)):
            if any(t["open"] < pos < t["close"] for t in in_set_system_time):
                self.hit(pos, "P4 clock-read")
                continue
            if not self.faked_state(pos)[1]:
                self.hit(pos, "P4 clock-read")

    # P5 — unseeded randomness
    def p5(self) -> None:
        for m in re.finditer(r"(?<![\w$.])(?:(?:globalThis|window|self)\s*\.\s*)?Math\s*\.\s*random\s*\(", self.b):
            if not self.random_spied(m.start()):
                self.hit(m.start(), "P5 unseeded-random")
        for m in re.finditer(
            r"(?<![\w$.])(?:(?:globalThis|window|self)\s*\.\s*)?crypto\s*\.\s*getRandomValues\s*\(", self.b
        ):
            self.hit(m.start(), "P5 unseeded-random")

    # P6 — a dynamic import or module reset inside a test body or hook
    def p6(self) -> None:
        factories = []
        for m in re.finditer(r"(?<![\w$.])vi\s*\.\s*(?:mock|doMock|hoisted)\s*\(", self.b):
            close = self.close_of(m.end() - 1)
            if close is not None:
                factories.append((m.end() - 1, close))
        found: list[int] = []
        for m in re.finditer(r"(?<![\w$.])import\s*\(", self.b):
            close = self.close_of(m.end() - 1)
            arg = self.raw[m.end():close].strip() if close is not None else ""
            if re.fullmatch(r"""['"`](?:node:[^'"`]*|fs|path|url|os)['"`]""", arg):
                continue
            found.append(m.start())
        for m in re.finditer(r"(?<![\w$.])(?:vi|vitest)\s*\.\s*(?:importActual|importMock|resetModules)\s*\(", self.b):
            found.append(m.start())
        for pos in found:
            if not any(c["root"] in TS_BODY and c["open"] < pos < c["close"] for c in self.calls):
                continue
            if any(a < pos < b for a, b in factories):
                continue
            self.hit(pos, "P6 dynamic-import")

    # P7 — a real router that lazy-loads pages
    def routes_real(self, expr: str, depth: int) -> bool:
        e = expr.strip()
        if depth > 5 or not e:
            return False
        stub = re.match(r"stubRouteComponents\s*\(", e)
        if stub:
            shared = re.search(
                r"""import\s*\{[^}]*\bstubRouteComponents\b[^}]*\}\s*from\s*"""
                r"""['"][^'"]*stubRouteComponents\.testUtils(?:\.[cm]?[jt]s)?['"]""",
                self.raw,
            )
            if shared:
                return False
            return self.routes_real(e[stub.end():].rsplit(")", 1)[0], depth + 1)
        if re.fullmatch(r"[A-Za-z_$][\w$]*", e):
            for m in re.finditer(r"""import\s*\{([^}]*)\}\s*from\s*['"]([^'"]+)['"]""", self.raw):
                names = [re.split(r"\s+as\s+", p.strip())[-1].strip() for p in m.group(1).split(",")]
                if e in names and TS_ROUTES_MODULE.search(m.group(2)):
                    return True
            decl = re.search(r"\b(?:const|let|var)\s+" + re.escape(e) + r"\b[^=;]*=\s*", self.raw)
            if decl:
                return self.routes_real(self.raw[decl.end():self.expr_end(decl.end())], depth + 1)
            return False
        return bool(re.search(r"(?<![\w$])components?\s*:\s*(?:async\s*)?\(?[^,{}]*?=>\s*import\s*\(", e))

    def p7(self) -> None:
        if re.search(r"""import\s*\{[^}]*\bcreateRouter\b[^}]*\}\s*from\s*['"]vue-router['"]""", self.raw):
            for m in re.finditer(r"(?<![\w$.])createRouter\s*\(", self.b):
                spans = self.args(m.end() - 1)
                if not spans:
                    continue
                a, b = spans[0]
                for r in re.finditer(r"(?<![\w$.])routes\b\s*", self.b[a:b]):
                    after = a + r.end()
                    if after < len(self.b) and self.b[after] == ":":
                        start = after + 1
                        expr = self.raw[start:self.expr_end(start)]
                    else:
                        expr = "routes"
                    if self.routes_real(expr, 0):
                        self.hit(m.start(), "P7 real-router")
                    break
        # a router instance or factory imported from the app's own router/index, in a file that does not
        # vi.mock every page that router/index loads lazily
        aliases = ts_alias_map(self.root)
        test_dir = self.path.resolve().parent
        mocked = set()
        for m in re.finditer(r"""vi\s*\.\s*(?:mock|doMock)\s*\(\s*['"]([^'"]+)['"]""", self.raw):
            r = ts_resolve(m.group(1), test_dir, aliases)
            if r is not None:
                mocked.add(r)
        for m in re.finditer(
            r"""import\s+(?:(\w+)|\{([^}]*)\})\s+from\s*['"]([^'"]+)['"]"""
            r"""|import\s*\{([^}]*)\}\s*from\s*['"]([^'"]+)['"]""",
            self.raw,
        ):
            source = m.group(3) or m.group(5) or ""
            if not TS_ROUTER_INDEX_MODULE.search(source) or "type " in self.raw[m.start():m.start() + 12]:
                continue
            base = ts_resolve(source, test_dir, aliases)
            file = ts_module_file(base) if base is not None else None
            if file is None:
                continue
            clause = m.group(1) or m.group(2) or m.group(4) or ""
            names = [re.split(r"\s+as\s+", p.strip())[-1].strip() for p in clause.split(",") if p.strip()]
            used = any(re.search(r"(?<![\w$.])" + re.escape(n) + r"\s*(?:\(|\.\s*\w+\s*\()", self.b) for n in names)
            if not used:
                continue
            unmocked = False
            for spec in re.findall(r"""import\(\s*['"`]([^'"`]+)['"`]\s*\)""", read(file)):
                r = ts_resolve(spec, file.resolve().parent, aliases)
                if r is None or r not in mocked:
                    unmocked = True
            if unmocked:
                self.hit(m.start(), "P7 real-router")

    # P8 — any skip, and a silent early return
    def callback_body(self, a: int, b: int) -> tuple[int, int] | None:
        t = self.b[a:b]
        m = re.search(r"=>\s*\{", t)
        if m and not t.lstrip().startswith("{"):
            open_at = a + m.end() - 1
        else:
            f = re.match(r"\s*(?:async\s+)?function[^{]*\{", t)
            if not f:
                return None
            open_at = a + f.end() - 1
        close = self.close_of(open_at)
        return (open_at, close) if close is not None else None

    def statements(self, start: int, end: int) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        depth = 0
        s = start
        for k in range(start, end):
            ch = self.b[k]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif depth == 0 and ch in ";\n":
                if self.b[s:k].strip():
                    out.append((s, k))
                s = k + 1
        if self.b[s:end].strip():
            out.append((s, end))
        return out

    def p8(self) -> None:
        for m in re.finditer(r"(?<![\w$.])(it|test|describe|suite)((?:\s*\.\s*\w+)*)", self.b):
            if any(x in TS_SKIP_PROPS for x in re.findall(r"\w+", m.group(2))):
                self.hit(m.start(), "P8 skip")
        for c in self.calls:
            if c["root"] not in ("it", "test"):
                continue
            for a, b in self.args(c["open"]):
                body = self.callback_body(a, b)
                if body is None:
                    continue
                text = self.b[body[0]:body[1]]
                param = re.match(r"\s*(?:async\s*)?\(?\s*(\{[^}]*\}|\w+)", self.raw[a:b])
                if param:
                    p = param.group(1)
                    if p.startswith("{"):
                        sk = re.search(r"\bskip(?:\s*:\s*(\w+))?", p)
                        if sk:
                            local = sk.group(1) or "skip"
                            for mm in re.finditer(r"(?<![\w$.])" + re.escape(local) + r"\s*\(", text):
                                self.hit(body[0] + mm.start(), "P8 skip")
                    else:
                        for mm in re.finditer(r"(?<![\w$.])" + re.escape(p) + r"\s*\.\s*skip\s*\(", text):
                            self.hit(body[0] + mm.start(), "P8 skip")
                seen_assert = False
                for s, e in self.statements(body[0] + 1, body[1]):
                    chunk = self.b[s:e].strip()
                    has_assert = bool(
                        re.search(r"(?<![\w$])(?:expect|assert)\w*\s*[.(]|\.\s*(?:expect|assert)\w*\s*\(", chunk)
                    )
                    if not seen_assert and not has_assert:
                        if re.match(r"return\b", chunk) or (
                            re.match(r"if\s*\(", chunk)
                            and re.search(r"\breturn\b", chunk)
                            and not re.search(r"\belse\b", chunk)
                        ):
                            self.hit(self.first(s, e), "P8 skip")
                    if has_assert:
                        seen_assert = True
                break

    # P9 — a write to a real repo path
    def p9(self) -> None:
        for lineno, line in enumerate(self.raw.splitlines(), start=1):
            if SQLITE_RELATIVE.search(line) and not line.lstrip().startswith(("//", "*", "/*")):
                self.hits.append((lineno, "P9 repo-write", line.strip()[:160]))
        objects: set[str] = set()
        named: dict[str, str] = {}
        clause_part = r"(?:\*\s+as\s+\w+|\w+|\{[^}]*\})"
        for m in re.finditer(
            r"import\s+("
            + clause_part
            + r"(?:\s*,\s*"
            + clause_part
            + r")*)\s+from\s*['\"]"
            + TS_FS_MODULES
            + r"['\"]",
            self.raw,
        ):
            clause = m.group(1)
            braces = re.search(r"\{([^}]*)\}", clause)
            if braces:
                for part in braces.group(1).split(","):
                    bits = re.split(r"\s+as\s+", part.strip())
                    if bits[0] in TS_WRITE_FNS:
                        named[bits[-1].strip()] = bits[0]
            rest = re.sub(r"\{[^}]*\}", "", clause)
            for ident in re.findall(r"(?:\*\s+as\s+)?([A-Za-z_$][\w$]*)", rest):
                objects.add(ident)
        for m in re.finditer(
            r"""(?:const|let|var)\s+(\w+)\s*=\s*(?:await\s+)?(?:require|import)\s*\(\s*['"]"""
            + TS_FS_MODULES
            + r"""['"]""",
            self.raw,
        ):
            objects.add(m.group(1))
        calls: list[tuple[int, str, int]] = []
        write_names = "|".join(TS_WRITE_FNS)
        if objects:
            obj_names = "|".join(re.escape(o) for o in objects)
            for m in re.finditer(
                r"(?<![\w$.])(?:" + obj_names + r")(?:\s*\.\s*promises)?\s*\.\s*(" + write_names + r")\s*\(",
                self.b,
            ):
                calls.append((m.start(), m.group(1), m.end() - 1))
        for local, fname in named.items():
            for m in re.finditer(r"(?<![\w$.])" + re.escape(local) + r"\s*\(", self.b):
                calls.append((m.start(), fname, m.end() - 1))
        for pos, fname, open_at in calls:
            spans = self.args(open_at)
            if fname in TS_DEST_ONLY:
                targets = spans[1:2]
            elif fname in TS_BOTH:
                targets = spans[:2]
            else:
                targets = spans[:1]
            if any(ts_taint(self.raw[a:b], self.raw) == "repo" for a, b in targets):
                self.hit(pos, "P9 repo-write")


def scan_package_json(text: str) -> list[Hit]:
    import json

    try:
        pkg = json.loads(text)
    except ValueError as err:
        return [(1, "P0 parse error", f"package.json does not parse (file not checked): {err}")]
    hits: list[Hit] = []
    for name, command in (pkg.get("scripts") or {}).items():
        at = max(text.find(f'"{name}"'), 0)
        line = text.count("\n", 0, at) + 1
        if re.search(r"--(?:testTimeout|hookTimeout|teardownTimeout)\b", str(command)):
            hits.append((line, "P1 time-limit", f'"{name}": "{command}"'[:160]))
        if re.search(r"--retry\b", str(command)):
            hits.append((line, "P2 retry", f'"{name}": "{command}"'[:160]))
    return hits


# ---------------------------------------------------------------------------- driver


def collect(root: Path, stack: str) -> dict[str, list[tuple[str, str]]]:
    """Scope: {kind: [(rel path, how to scan)]}.

    Kinds: py-test, py-gate, token, xml, shell, pytest-config, requirements."""
    files = [f for f in git_files(root)]
    scope: dict[str, list[str]] = {
        "py-test": [], "py-gate": [], "token": [], "xml": [], "shell": [], "pytest-config": [], "requirements": [],
        "ts-test": [], "ts-config": [], "package": [],
    }

    def gate_file(rel: str) -> bool:
        return gate_index(rel) >= 0

    if stack == "py":
        tests, helpers = python_test_scope(root, files)
        scope["py-test"] = sorted(tests | helpers)
        for rel in files:
            if skipped_dir(rel) or not gate_file(rel):
                continue
            if rel.endswith(".py") and rel not in tests and rel not in helpers:
                scope["py-gate"].append(rel)
            elif rel.endswith((".sh", ".ps1", ".cmd", ".bat")):
                scope["shell"].append(rel)
        scope["pytest-config"] = [c["rel"] for c in pytest_configs(root, files)]
        scope["requirements"] = [
            f
            for f in files
            if re.match(r"^requirements[\w.-]*\.txt$", f.rsplit("/", 1)[-1]) and not skipped_dir(f)
        ]
        scope["requirements"] += [f for f in files if f.rsplit("/", 1)[-1] == "pyproject.toml" and not skipped_dir(f)]
    elif stack == "cs":
        projects = []
        for rel in files:
            if rel.endswith(".csproj") and not skipped_dir(rel):
                if re.search(r"xunit|Microsoft\.NET\.Test\.Sdk", read(root / rel), re.I):
                    projects.append(rel)
                    scope["xml"].append(rel)
        dirs = [p.rsplit("/", 1)[0] + "/" if "/" in p else "" for p in projects]
        for rel in files:
            if rel.endswith(".cs") and not skipped_dir(rel) and any(rel.startswith(d) for d in dirs):
                scope["token"].append(rel)
            elif rel.endswith(".runsettings"):
                scope["xml"].append(rel)
            elif gate_file(rel) and rel.endswith((".sh", ".ps1", ".cmd", ".bat")):
                scope["shell"].append(rel)
    elif stack == "ts":
        for rel in files:
            if skipped_dir(rel):
                continue
            segs = rel.split("/")
            base = segs[-1]
            ts_ext = re.search(r"\.[cm]?[jt]sx?$", base) is not None
            if ts_ext and (
                re.search(r"\.(?:test|spec)\.[cm]?[jt]sx?$", base)
                or re.match(r"^src/(?:.*/)?test\.ts$", rel)
                or re.search(r"\.testUtils\.[cm]?[jt]sx?$", base)
                or "test-utils" in segs[:-1]
                or ".test" in segs[:-1]
            ):
                scope["ts-test"].append(rel)
            elif re.match(r"^(?:vite|vitest|playwright)[\w.-]*\.config\.[cm]?[jt]s$", base):
                scope["ts-config"].append(rel)
            elif rel == "package.json":
                scope["package"].append(rel)
            elif gate_file(rel) and rel.endswith((".sh", ".ps1", ".cmd", ".bat")):
                scope["shell"].append(rel)
    else:  # java
        for rel in files:
            if skipped_dir(rel):
                continue
            segs = rel.split("/")
            if rel.endswith(".java") and any(segs[i] == "src" and segs[i + 1] == "test" for i in range(len(segs) - 2)):
                scope["token"].append(rel)
            elif segs[-1] == "pom.xml":
                scope["xml"].append(rel)
            elif gate_file(rel) and rel.endswith((".sh", ".ps1", ".cmd", ".bat")):
                scope["shell"].append(rel)
    return {k: [(rel, k) for rel in v] for k, v in scope.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="G3(c) determinism check (no option switches a hit off).")
    parser.add_argument("--stack", choices=("py", "cs", "java", "ts"), default="py", help="which stack's tests to scan")
    parser.add_argument("--root", default=".", help="package folder to scan (default: current folder)")
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        metavar="PATH",
        help="scan only these files (paths relative to --root); default: the whole scope",
    )
    args = parser.parse_args()
    root = (Path.cwd() / args.root).resolve()
    scope = collect(root, args.stack)
    given_abs: set[Path] = set()
    if args.files is not None:
        given_abs = {(root / given).resolve() for given in args.files}
        given_rel: set[str] = set()
        for path in given_abs:
            try:
                given_rel.add(path.relative_to(root).as_posix())
            except ValueError:  # outside --root: only the git hook can match it
                continue
        scope = {kind: [(rel, how) for rel, how in entries if rel in given_rel] for kind, entries in scope.items()}

    results: list[tuple[str, int, str, str]] = []
    checked = 0

    def record(path: Path, hits: list[Hit]) -> None:
        try:
            shown = Path(os.path.relpath(path.resolve(), Path.cwd().resolve())).as_posix()
        except ValueError:  # another drive
            shown = path.resolve().as_posix()
        for line, label, text in hits:
            results.append((shown, line, label, text))

    for rel, _ in scope["py-test"]:
        checked += 1
        path = root / rel
        try:
            record(path, PythonScan(rel, read(path), "test").run())
        except SyntaxError as err:
            record(path, [(err.lineno or 1, "P0 parse error", f"file not checked: {err.msg}")])
    for rel, _ in scope["py-gate"]:
        checked += 1
        path = root / rel
        try:
            record(path, PythonScan(rel, read(path), "gate").run())
        except SyntaxError as err:
            record(path, [(err.lineno or 1, "P0 parse error", f"file not checked: {err.msg}")])
    for kind in ("ts-test", "ts-config"):
        for rel, _ in scope[kind]:
            checked += 1
            path = root / rel
            record(path, TsScan(rel, read(path), kind.split("-")[1], root, path).run())
    for rel, _ in scope["package"]:
        checked += 1
        record(root / rel, scan_package_json(read(root / rel)))
    for rel, _ in scope["pytest-config"]:
        checked += 1
        record(root / rel, scan_pytest_config(rel, read(root / rel)))
    for rel, _ in scope["requirements"]:
        checked += 1
        record(root / rel, scan_requirements(read(root / rel)))
    for rel, _ in scope["token"]:
        checked += 1
        csharp = args.stack == "cs"
        record(root / rel, scan_tokens(read(root / rel), cs_rules() if csharp else java_rules(), csharp))
    for rel, _ in scope["xml"]:
        checked += 1
        record(root / rel, scan_xml_config(read(root / rel), args.stack))
    shell_paths = [root / rel for rel, _ in scope["shell"]]
    top = run_git(root, "rev-parse", "--show-toplevel").strip()
    hook = Path(top) / ".githooks" / "pre-commit"
    if hook.is_file() and (args.files is None or hook.resolve() in given_abs):
        shell_paths.append(hook)
    for path in shell_paths:
        checked += 1
        record(path, scan_shell(path))

    # The git hook sits at the repo top, outside the scan root's own scope, so it does not count here.
    if args.files is None and sum(len(v) for v in scope.values()) == 0:
        print(
            f"[G3c] FAIL — 0 files in scope under {root} (--stack {args.stack}): "
            "nothing was checked, so this run proves nothing.",
            file=sys.stderr,
        )
        return 1
    unique = sorted(set(results), key=lambda r: (r[0], r[1], r[2]))
    seen: set[tuple[str, int, str]] = set()
    final = []
    for shown, line, label, text in unique:
        key = (shown, line, label.split(" ")[0])
        if key in seen:
            continue
        seen.add(key)
        final.append((shown, line, label, text))
    for shown, line, label, text in final:
        print(f"{shown}:{line} [{label}] {text}", file=sys.stderr)
    if final:
        print(f"\n[G3c] FAIL — {len(final)} hit(s) in {checked} file(s) checked.", file=sys.stderr)
        return 1
    if args.files is not None and checked == 0:
        print(f"[G3c] PASS — no given file is in scope ({len(args.files)} given)")
        return 0
    print(f"[G3c] PASS — {checked} file(s) checked, 0 hits.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # a crash is a failure, never a pass
        print(f"[G3c] gate crashed: {exc!r}", file=sys.stderr)
        sys.exit(1)
