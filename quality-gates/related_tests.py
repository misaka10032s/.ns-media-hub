#!/usr/bin/env python
"""The test files a commit has to run: the staged test files, plus every test file that imports (statically, by
module path) a staged module. Used by `run.py commit`, so the commit step runs only these tests and never the whole
suite (the whole suite is `run.py l0`, the end-of-task run).

A test file is a file under the `testpaths` of pyproject.toml's `[tool.pytest.ini_options]` whose name matches its
`python_files`. Imports are read with `ast`, so `import a.b`, `from a import b` and `from a.b import c` all count,
wherever in the file they stand. A staged module is named by its dotted path from the repo root
(`app/services/updater_service.py` is `app.services.updater_service`, `app/services/__init__.py` is `app.services`
and also covers every `app.services.*` import) and, when its folder has no `__init__.py`, by its bare file name (`dl.py` is `dl`).

A staged `conftest.py` is related to every test file under its folder. A test file that holds a staged module's
dotted path as a string (`import_module("a.b")`) or a staged file's repo-relative path (with `/` or a backslash, as
in `spec_from_file_location`) is related, the same as an import.

Usage (from the repo root):  py -3.11 quality-gates/related_tests.py   (prints the files, one per line)
"""
from __future__ import annotations

import ast
import fnmatch
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.git_diff import ensure_utf8_stdio, get_staged_files

BACKSLASH = chr(92)
ROOT = Path(__file__).resolve().parent.parent  # repo root


def _pytest_settings(root: Path) -> tuple[list[str], list[str]]:
    """(testpaths, python_files) from pyproject.toml; pytest's own defaults when a key is absent."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    options = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    testpaths = options.get("testpaths", ["."])
    patterns = options.get("python_files", ["test_*.py", "*_test.py"])
    return list(testpaths), list(patterns)


def all_test_files(root: Path) -> list[str]:
    """Every test file pytest collects here, as paths relative to `root`."""
    testpaths, patterns = _pytest_settings(root)
    found: set[str] = set()
    for testpath in testpaths:
        base = root / testpath
        for path in base.rglob("*.py"):
            if any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns):
                found.add(path.relative_to(root).as_posix())
    return sorted(found)


def module_names(root: Path, rel: str) -> tuple[set[str], bool]:
    """(names the module can be imported by, whether it is a package `__init__`)."""
    parts = rel[: -len(".py")].split("/")
    is_package = parts[-1] == "__init__"
    dotted_parts = parts[:-1] if is_package else parts
    names: set[str] = set()
    if dotted_parts and all(part.isidentifier() for part in dotted_parts):
        names.add(".".join(dotted_parts))
    if not is_package and not (root / Path(*parts[:-1]) / "__init__.py").exists():
        names.add(parts[-1])  # a bare module on a test folder's own path
    return names, is_package


def imported_names(path: Path) -> set[str]:
    """Every module name the file imports, plus `package.name` for each `from package import name`."""
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def dotted_module_name(rel: str) -> str | None:
    """The dotted path a string can load a module by (`a/b/c.py` is `a.b.c`, `a/b/__init__.py` is `a.b`), or None."""
    parts = rel[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if parts and all(part.isidentifier() for part in parts):
        return ".".join(parts)
    return None


def loaded_by_string(path: Path, dotted: set[str], dotted_packages: set[str], file_paths: set[str]) -> bool:
    """True when the file holds a staged module's dotted path as a string literal (`import_module("a.b")`), or a
    staged file's repo-relative path anywhere in its text (`spec_from_file_location(..., "a/b.py")`)."""
    text = path.read_text(encoding="utf-8")
    if any(file_path in text for file_path in file_paths):
        return True
    for node in ast.walk(ast.parse(text, filename=str(path))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value in dotted or any(value.startswith(package + ".") for package in dotted_packages):
                return True
    return False


def related_test_files(root: Path = ROOT) -> list[str]:
    """Staged test files, test files that import or load by string a staged module and test files under a staged
    conftest.py's folder, relative to `root`, sorted."""
    staged = get_staged_files(root, ["py"])
    tests = all_test_files(root)
    related = {rel for rel in staged if rel in tests}
    for rel in staged:
        if rel.rsplit("/", 1)[-1] == "conftest.py":
            folder = rel[: -len("conftest.py")]  # "" for a conftest at the repo root
            related.update(test for test in tests if test.startswith(folder))
    exact: set[str] = set()
    packages: set[str] = set()
    dotted: set[str] = set()  # the dotted paths a string can load a staged module by
    dotted_packages: set[str] = set()
    file_paths: set[str] = set()  # the staged files' repo-relative paths, with / and with a backslash
    for rel in staged:
        names, is_package = module_names(root, rel)
        exact |= names
        if is_package:
            packages |= names
        dotted_name = dotted_module_name(rel)
        if dotted_name:
            dotted.add(dotted_name)
            if is_package:
                dotted_packages.add(dotted_name)
        file_paths |= {rel, rel.replace("/", BACKSLASH), rel.replace("/", BACKSLASH * 2)}
    if exact or file_paths:
        for rel in tests:
            if rel in related:
                continue
            for name in imported_names(root / rel):
                if name in exact or any(name.startswith(package + ".") for package in packages):
                    related.add(rel)
                    break
            else:
                if loaded_by_string(root / rel, dotted, dotted_packages, file_paths):
                    related.add(rel)
    return sorted(related)


def main() -> int:
    ensure_utf8_stdio()
    for rel in related_test_files():
        print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
