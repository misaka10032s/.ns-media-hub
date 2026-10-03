"""
測試共用 fixtures。
"""
from __future__ import annotations

import builtins
import os
import shutil
import sqlite3
import tempfile
import pytest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

from app.config import paths as _paths_module

# G3(c) pattern P9 runtime guard (see `_no_repo_writes` below): the repo root and the system
# temp folder, resolved once at import time, before any fixture patches anything.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
_RUNNER_OUTPUT_DIRS = {".pytest_cache", "__pycache__", "htmlcov"}
_RUNNER_OUTPUT_FILES = {"coverage.xml"}

# Captured ONCE at collection time, before any fixture ever patches anything —
# the repo's REAL, live production paths. assert_db_paths_isolated() /
# assert_fs_paths_isolated() compare against these fixed values, never
# against a consumer module's own (patchable) module globals, so the check
# stays meaningful even while a test has them patched.
_REAL_DATA_DIR = _paths_module.DATA_DIR
_REAL_DB_FILE = _paths_module.DB_FILE
_REAL_LEGACY_HISTORY_FILE = _paths_module.LEGACY_HISTORY_FILE
_REAL_COOKIE_DIR = _paths_module.COOKIE_DIR
_REAL_DOWNLOAD_DIR = _paths_module.DOWNLOAD_DIR
_REAL_TOKENS_FILE = _paths_module.TOKENS_FILE
_REAL_LEGACY_COOKIE_DIRS = _paths_module.LEGACY_COOKIE_DIRS

# review-3.md B1 (fix-round-3): every real, on-disk root a test must never
# mkdir into, unlink from, or shutil.move a file into/out of. The DB leak's
# fix (assert_db_paths_isolated / _guarded_connect below) only ever covered
# app.storage.db's three names; PROBE F in that review found nine OTHER
# consumer-bound path constants (cookie jar / token file / download tree)
# still resolving to production inside a live test. This tuple is the
# detection layer's ground truth — computed once, before any patching, from
# the same untouched app.config.paths module _REAL_DATA_DIR etc. above come
# from.
_REAL_GUARDED_ROOTS: tuple[Path, ...] = (
    _REAL_DATA_DIR.resolve(),
    _REAL_COOKIE_DIR.resolve(),
    _REAL_DOWNLOAD_DIR.resolve(),
    *(p.resolve() for p in _REAL_LEGACY_COOKIE_DIRS),
)


def _path_under_guarded_root(path) -> Path | None:
    """Return the specific real production root `path` resolves under or
    equals, or None if it is clear of all of them. Used by the Path.unlink /
    Path.mkdir / shutil.move guards installed in the autouse fixture below —
    it is deliberately a pure path-value check (never touches the
    filesystem beyond `.resolve()`), so it fires even for a target that
    does not exist yet, exactly like _guarded_connect below fires on a
    path value alone rather than waiting for a connection to succeed."""
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return None
    for root in _REAL_GUARDED_ROOTS:
        if resolved == root or root in resolved.parents:
            return root
    return None


def _inside(parent: Path, child: Path) -> bool:
    parent_key = os.path.normcase(str(parent))
    child_key = os.path.normcase(str(child))
    return child_key == parent_key or child_key.startswith(parent_key + os.sep)


def _repo_write_target(value) -> Path | None:
    """The resolved path `value` points at when a write to it is NOT allowed: inside the repo
    root, outside the system temp folder, and not a test-runner output. None otherwise (also for
    a file descriptor, ':memory:' or anything that is not a path)."""
    if isinstance(value, bytes):
        value = os.fsdecode(value)
    if not isinstance(value, str | os.PathLike):
        return None
    text = os.fspath(value)
    if isinstance(text, bytes):
        text = os.fsdecode(text)
    if text in ("", ":memory:") or text.startswith("file::memory:"):
        return None
    if text.startswith("file:"):
        # SQLite URI (`sqlite3.connect(..., uri=True)`): the file is the path part, and a
        # read-only open (`mode=ro` / `immutable=1`) is not a write.
        rest, _, query = text[len("file:"):].partition("?")
        flags = {part.strip().lower() for part in query.split("&")}
        if "mode=ro" in flags or "immutable=1" in flags:
            return None
        if rest.startswith("//"):
            rest = rest[2:].partition("/")[2]
            rest = rest if (len(rest) > 1 and rest[1] == ":") else "/" + rest
        elif len(rest) > 2 and rest[0] == "/" and rest[2] == ":":
            rest = rest[1:]
        text = unquote(rest)
    try:
        resolved = Path(text).resolve()
    except (OSError, ValueError):
        return None
    if _inside(_TEMP_ROOT, resolved) or not _inside(_REPO_ROOT, resolved):
        return None
    relative_parts = resolved.relative_to(_REPO_ROOT).parts if _inside(_REPO_ROOT, resolved) else ()
    if any(part in _RUNNER_OUTPUT_DIRS for part in relative_parts):
        return None
    if resolved.name in _RUNNER_OUTPUT_FILES or resolved.name.startswith(".coverage"):
        return None
    return resolved


def _open_writes(mode) -> bool:
    return isinstance(mode, str) and any(flag in mode for flag in "wax+")


@pytest.fixture(scope="session", autouse=True)
def _no_repo_writes():
    """G3(c) pattern P9, runtime half: a static check cannot see a file written by product code a
    test calls, so every function that creates, changes, moves or removes a file or opens a
    SQLite file is wrapped for the whole session. A target inside the repo root and outside the
    system temp folder raises instead of writing. Allowed: `.pytest_cache`, `__pycache__`,
    `.coverage*`, `coverage.xml`, `htmlcov/`. This sits under (and does not replace) the
    per-test redirection and guards of `_isolate_every_test_from_the_real_database`."""

    def guarded(original, name: str, indexes: tuple[int, ...], keyword: str | None = None):
        def wrapper(*args, **kwargs):
            for index in indexes:
                value = args[index] if index < len(args) else (kwargs.get(keyword) if keyword and index == indexes[0] else None)
                target = _repo_write_target(value)
                if target is not None:
                    raise RuntimeError(
                        f"no-repo-writes guard: {name}() would write {target}, which is inside the "
                        "repo and outside the system temp folder. Route the write through tmp_path."
                    )
            return original(*args, **kwargs)

        return wrapper

    def guarded_open(original, name: str, mode_index: int):
        def wrapper(*args, **kwargs):
            mode = args[mode_index] if len(args) > mode_index else kwargs.get("mode", "r")
            if _open_writes(mode):
                target = _repo_write_target(args[0] if args else kwargs.get("file"))
                if target is not None:
                    raise RuntimeError(
                        f"no-repo-writes guard: {name}() would write {target}, which is inside the "
                        "repo and outside the system temp folder. Route the write through tmp_path."
                    )
            return original(*args, **kwargs)

        return wrapper

    with pytest.MonkeyPatch.context() as session_patch:
        session_patch.setattr(builtins, "open", guarded_open(builtins.open, "open", 1))
        session_patch.setattr(Path, "open", guarded_open(Path.open, "Path.open", 1))
        for name in ("write_text", "write_bytes", "mkdir", "touch", "unlink", "rmdir"):
            session_patch.setattr(Path, name, guarded(getattr(Path, name), f"Path.{name}", (0,)))
        for name in ("rename", "replace"):
            session_patch.setattr(Path, name, guarded(getattr(Path, name), f"Path.{name}", (0, 1)))
        for name in ("remove", "unlink", "rmdir", "mkdir", "makedirs", "truncate"):
            session_patch.setattr(os, name, guarded(getattr(os, name), f"os.{name}", (0,), "path"))
        for name in ("rename", "replace"):
            session_patch.setattr(os, name, guarded(getattr(os, name), f"os.{name}", (0, 1)))
        for name in ("symlink", "link"):
            session_patch.setattr(os, name, guarded(getattr(os, name), f"os.{name}", (1,)))
        session_patch.setattr(shutil, "rmtree", guarded(shutil.rmtree, "shutil.rmtree", (0,)))
        for name in ("copy", "copy2", "copyfile", "copytree"):
            session_patch.setattr(shutil, name, guarded(getattr(shutil, name), f"shutil.{name}", (1,)))
        session_patch.setattr(shutil, "move", guarded(shutil.move, "shutil.move", (0, 1)))
        session_patch.setattr(sqlite3, "connect", guarded(sqlite3.connect, "sqlite3.connect", (0,), "database"))
        yield


def assert_db_paths_isolated(db_module) -> None:
    """Fail loudly if any of db_module's currently-active DB-related path
    constants resolve to this repo's real, live production files.

    2026-09-01 incident: tmp_db patched DATA_DIR/DB_FILE but NOT
    LEGACY_HISTORY_FILE. app/storage/db.py:11 binds its own module-level copy
    of LEGACY_HISTORY_FILE at db.py's own import time (`from
    app.config.paths import ... LEGACY_HISTORY_FILE`) — a separate name from
    the two patched ones, never re-derived from the patched DATA_DIR.
    init_db() -> migrate_legacy_history() (app/storage/db.py:301-306) reads
    LEGACY_HISTORY_FILE unconditionally whenever history_entries is empty —
    true for every "fresh" tmp_db-backed test — so every one of them silently
    reseeded its supposedly isolated temp db from the real data/history.json
    (4669 grouped / ~4642 row entries) whenever that file happened to exist.
    On a fresh worktree data/history.json is gitignored and absent, so the
    migration is a no-op and tests pass vacuously; on the main tree, where
    the real file exists, the two new test files (test_queue_service.py,
    test_history_service.py) failed against real user download history.
    `data/app.db` itself was never touched — DATA_DIR/DB_FILE patching was
    always correct for that file; the leak was read-only and specific to
    LEGACY_HISTORY_FILE. This guard makes reintroducing that class of leak
    (or ever pointing DB_FILE itself at the real app.db) structurally
    impossible: any DB-related path that resolves back to a real file raises
    immediately instead of quietly letting production data into test state.
    """
    checks = {
        "DATA_DIR": (getattr(db_module, "DATA_DIR", None), _REAL_DATA_DIR),
        "DB_FILE": (getattr(db_module, "DB_FILE", None), _REAL_DB_FILE),
        "LEGACY_HISTORY_FILE": (getattr(db_module, "LEGACY_HISTORY_FILE", None), _REAL_LEGACY_HISTORY_FILE),
    }
    for name, (current, real) in checks.items():
        if current is None:
            continue
        if Path(current).resolve() == Path(real).resolve():
            raise RuntimeError(
                f"tmp_db isolation guard: db_module.{name} resolves to the REAL "
                f"production path {real} — refusing to let a test run against "
                "live data. Patch it to a tmp_path-based location before use."
            )


def assert_fs_paths_isolated(bindings: dict[str, tuple[object, object]]) -> None:
    """Same contract and tone as assert_db_paths_isolated(), generalised to
    the cookie-jar / token-file / download-tree consumer modules review-3.md
    B1's PROBE F enumerated (tests/conftest.py's autouse fixture patches
    each consumer's OWN module-level binding, not just the app.config.paths
    source module — see that fixture's docstring for why).

    `bindings` maps a human-readable "module.NAME" label to a
    (current_value, real_value) pair. `real_value` (and `current_value`) may
    each be a single Path (COOKIE_DIR, TOKENS_FILE, DOWNLOAD_DIR) or a
    tuple/list of Paths (LEGACY_COOKIE_DIRS) — both shapes are checked by
    comparing every current item against every real item, so a leak is
    caught regardless of which slot of a multi-path tuple it appears in."""
    for label, (current, real) in bindings.items():
        if current is None:
            continue
        real_items = real if isinstance(real, (tuple, list)) else (real,)
        current_items = current if isinstance(current, (tuple, list)) else (current,)
        for current_item in current_items:
            for real_item in real_items:
                if Path(current_item).resolve() == Path(real_item).resolve():
                    raise RuntimeError(
                        f"assert_fs_paths_isolated: {label} resolves to the REAL "
                        f"production path {real_item} — refusing to let a test "
                        "run against live data. Patch it to a tmp_path-based "
                        "location before use."
                    )


@pytest.fixture(autouse=True)
def _isolate_every_test_from_the_real_database(tmp_path: Path, monkeypatch):
    """fix-round-2 (B2, 2026-09-02): `assert_db_paths_isolated()` used to be
    reachable ONLY from inside the `tmp_db` fixture, so any test that
    reached app.storage.db WITHOUT explicitly requesting `tmp_db` was never
    checked at all. Two new tests in this branch
    (test_cookie_service_atomic_write.py, test_gallery_dl_auth_retry.py) did
    exactly that — reaching init_db() indirectly via
    cookie_service.save_cookie()->scan_cookie_files() and
    gallery_provider.download()->resolve_cookie_file() — and so did the
    pre-existing test_gallery_dl_error_capture.py. In a worktree this is
    harmless (ROOT_DIR-derived DATA_DIR resolves to the worktree's own
    throwaway data/), but on the MAIN tree the identical code path resolves
    to the owner's real, live data/app.db — reproduced end-to-end: this
    exact mechanism created `auth_cooldown` in production (see
    fix-round-2.md).

    This fixture closes that class of leak STRUCTURALLY rather than by
    chasing individual tests: every test in the suite gets its DB paths
    isolated to a per-test tmp_path, whether or not it ever asks for
    `tmp_db` — a test cannot reintroduce this leak by simply forgetting to
    request a fixture, because there is no "forgetting" left; isolation is
    unconditional. `tmp_db` (below) still works unchanged on top of this —
    it just re-patches the same three names to its OWN tmp file, which is a
    no-op change of WHICH tmp path is active, never a return to the real one.

    `_READY` is a module-level singleton in app.storage.db (`if _READY:
    return` short-circuits init_db()'s schema creation) — it must be reset
    on both entry and exit here, exactly like tmp_db already does, or the
    SECOND test in a run would see `_READY=True` from the first test's
    (different tmp) database and silently skip creating tables in its own.

    review-3.md B1 (fix-round-3, 2026-09-02): this fixture used to isolate
    ONLY app.storage.db's three names. PROBE F in that review found nine
    OTHER consumer-bound path constants — cookie jar (cookie_service +
    registry.COOKIE_DIR/LEGACY_COOKIE_DIRS), token file (token_service.
    DATA_DIR/TOKENS_FILE), download tree (gallery_service/path_service/
    doujin_service.DOWNLOAD_DIR) — still resolving to production inside a
    live test, with this branch's own new tests (test_cookie_service_atomic_
    write.py etc.) being the first to actually call save_cookie()/
    delete_cookie() for real. Closed the SAME way the DB class was: every
    one of those bindings is now redirected here, unconditionally, for
    every test — not by an opt-in fixture a test author has to remember to
    request — plus a structural Path.unlink/Path.mkdir/shutil.move guard
    (below) mirroring _guarded_connect, so a future consumer nobody added
    here yet fails loudly instead of silently touching production."""
    from app.storage import db as db_module
    from app.services import (
        token_service,
        cookie_service,
        gallery_service,
        path_service,
        doujin_service,
    )
    from app.providers.cookies import registry as cookie_registry_module

    db_file = tmp_path / "autouse_isolated_app.db"
    legacy_history_file = tmp_path / "autouse_isolated_legacy_history.json"  # deliberately absent
    data_dir = tmp_path / "autouse_isolated_data"
    download_dir = tmp_path / "autouse_isolated_download"
    cookie_dir = tmp_path / "autouse_isolated_cookies"
    tokens_file = data_dir / "tokens.json"
    legacy_cookie_dirs = (
        tmp_path / "autouse_isolated_legacy_cookies_data",
        tmp_path / "autouse_isolated_legacy_cookies_module",
    )

    patchers = [
        patch.object(db_module, "DATA_DIR", tmp_path),
        patch.object(db_module, "DB_FILE", db_file),
        patch.object(db_module, "LEGACY_HISTORY_FILE", legacy_history_file),
        patch.object(token_service, "DATA_DIR", data_dir),
        patch.object(token_service, "TOKENS_FILE", tokens_file),
        patch.object(cookie_service, "COOKIE_DIR", cookie_dir),
        patch.object(cookie_registry_module, "COOKIE_DIR", cookie_dir),
        patch.object(cookie_registry_module, "LEGACY_COOKIE_DIRS", legacy_cookie_dirs),
        patch.object(gallery_service, "DOWNLOAD_DIR", download_dir),
        patch.object(path_service, "DOWNLOAD_DIR", download_dir),
        patch.object(doujin_service, "DOWNLOAD_DIR", download_dir),
        # The SOURCE module itself (app.config.paths) — review-3.md flagged
        # it as "never patched at all". No current consumer reads these
        # names off the module at call time (every consumer above bound its
        # own module-level copy at import time, which is why each has to be
        # patched individually above too), but patching the source as well
        # means a FUTURE `from app.config import paths; paths.X` usage
        # inherits isolation for free instead of reopening this exact class
        # of gap an eleventh time.
        patch.object(_paths_module, "DATA_DIR", data_dir),
        patch.object(_paths_module, "DOWNLOAD_DIR", download_dir),
        patch.object(_paths_module, "COOKIE_DIR", cookie_dir),
        patch.object(_paths_module, "TOKENS_FILE", tokens_file),
        patch.object(_paths_module, "LEGACY_COOKIE_DIRS", legacy_cookie_dirs),
        patch.object(_paths_module, "DB_FILE", db_file),
        patch.object(_paths_module, "LEGACY_HISTORY_FILE", legacy_history_file),
    ]

    with ExitStack() as stack:
        for patcher in patchers:
            stack.enter_context(patcher)

        assert_db_paths_isolated(db_module)
        assert_fs_paths_isolated(
            {
                "token_service.DATA_DIR": (token_service.DATA_DIR, _REAL_DATA_DIR),
                "token_service.TOKENS_FILE": (token_service.TOKENS_FILE, _REAL_TOKENS_FILE),
                "cookie_service.COOKIE_DIR": (cookie_service.COOKIE_DIR, _REAL_COOKIE_DIR),
                "registry.COOKIE_DIR": (cookie_registry_module.COOKIE_DIR, _REAL_COOKIE_DIR),
                "registry.LEGACY_COOKIE_DIRS": (
                    cookie_registry_module.LEGACY_COOKIE_DIRS,
                    _REAL_LEGACY_COOKIE_DIRS,
                ),
                "gallery_service.DOWNLOAD_DIR": (gallery_service.DOWNLOAD_DIR, _REAL_DOWNLOAD_DIR),
                "path_service.DOWNLOAD_DIR": (path_service.DOWNLOAD_DIR, _REAL_DOWNLOAD_DIR),
                "doujin_service.DOWNLOAD_DIR": (doujin_service.DOWNLOAD_DIR, _REAL_DOWNLOAD_DIR),
            }
        )
        db_module._READY = False

        # Structural, cannot-recur guard (widened per fix-round-2's brief):
        # wrap the SINGLE choke point every read/write goes through
        # (_connect(), which execute/insert/fetch_one/fetch_all/init_db all
        # call via connection()) so ANY attempt — from this test or a future
        # one, whether or not it requests tmp_db, even one that deliberately
        # or accidentally re-patches DB_FILE back afterwards — to open a
        # real connection at the live production DB_FILE fails LOUDLY before
        # sqlite3.connect() is ever invoked, instead of silently writing to
        # it. This is deliberately independent of the isolation above: the
        # isolation is prevention (nothing CAN resolve to the real path by
        # default); this wrap is detection (if something still does, it is
        # caught at the moment of connection, not assumed away). See
        # tests/test_tmp_db_isolation_guard.py::test_probe_connect_guard_fires_on_real_db_file_when_deliberately_repointed
        # for the red-before-green proof this fires.
        real_connect = db_module._connect

        def _guarded_connect():
            current = getattr(db_module, "DB_FILE", None)
            if current is not None and Path(current).resolve() == _REAL_DB_FILE.resolve():
                raise RuntimeError(
                    "assert_db_paths_isolated: a test attempted to open a real, live "
                    f"connection to {_REAL_DB_FILE} — this is the owner's production "
                    "database. Route this test through the tmp_db fixture (or rely on "
                    "this autouse fixture's default isolation) instead."
                )
            return real_connect()

        monkeypatch.setattr(db_module, "_connect", _guarded_connect)

        # review-3.md B1's structural guard, mirroring _guarded_connect
        # exactly but for the filesystem side: wrap Path.unlink / Path.mkdir
        # (class-level, so it fires for every Path instance regardless of
        # which consumer module created it) and shutil.move — the three
        # destructive operations review-3.md named — so that ANY attempt to
        # touch a path under a real production root (DATA_DIR, COOKIE_DIR,
        # DOWNLOAD_DIR, or either legacy cookie dir) fails LOUDLY before the
        # real filesystem call ever runs, instead of quietly deleting/
        # moving/creating against the owner's live files. This is detection,
        # independent of the redirection above: the redirection is
        # prevention (nothing CAN resolve to a real path by default); this
        # wrap is the backstop for a consumer this fixture has not (yet)
        # been told to redirect. See tests/test_cookie_fs_isolation_guard.py,
        # tests/test_token_fs_isolation_guard.py and
        # tests/test_download_fs_isolation_guard.py for the red-before-green
        # proof each of these fires.
        real_path_unlink = Path.unlink
        real_path_mkdir = Path.mkdir
        real_shutil_move = shutil.move

        def _guard_message(op: str, target) -> str:
            return (
                f"assert_fs_paths_isolated: a test attempted to {op} a real, live "
                f"production path {target} — this is (or is under) the owner's "
                "live cookie jar / token file / download tree. Patch the "
                "consumer's own path constant to a tmp_path-based location (or "
                "rely on this autouse fixture's default isolation) instead."
            )

        def _guarded_unlink(self, *args, **kwargs):
            if _path_under_guarded_root(self) is not None:
                raise RuntimeError(_guard_message("unlink()", self))
            return real_path_unlink(self, *args, **kwargs)

        def _guarded_mkdir(self, *args, **kwargs):
            if _path_under_guarded_root(self) is not None:
                raise RuntimeError(_guard_message("mkdir()", self))
            return real_path_mkdir(self, *args, **kwargs)

        def _guarded_move(src, dst, *args, **kwargs):
            if _path_under_guarded_root(src) is not None or _path_under_guarded_root(dst) is not None:
                raise RuntimeError(_guard_message("shutil.move()", f"{src} -> {dst}"))
            return real_shutil_move(src, dst, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", _guarded_unlink)
        monkeypatch.setattr(Path, "mkdir", _guarded_mkdir)
        monkeypatch.setattr(shutil, "move", _guarded_move)

        yield
        db_module._READY = False


@pytest.fixture(autouse=True)
def _no_real_wait_in_doujin_meta_service(monkeypatch):
    """doujin_meta_service._throttle sleeps up to MIN_INTERVAL_SECONDS and a
    retry sleeps BACKOFF_SECONDS, against the module-global _last_request_at.
    A test that forgets to patch them would wait real seconds and share that
    dict with the next test — so every test gets a recording sleep (never
    waits) and a fresh _last_request_at. Per-test patches still override."""
    from app.services import doujin_meta_service

    recorded_sleeps: list[float] = []
    monkeypatch.setattr(doujin_meta_service.time, "sleep", recorded_sleeps.append)
    monkeypatch.setattr(doujin_meta_service, "_last_request_at", {})
    yield recorded_sleeps


@pytest.fixture
def tmp_download_dir(tmp_path: Path):
    """提供臨時的下載目錄，隔離 gallery_service 的檔案系統操作。"""
    d = tmp_path / "download"
    d.mkdir()
    with patch("app.services.gallery_service.DOWNLOAD_DIR", d):
        yield d


@pytest.fixture
def tmp_cookie_dir(tmp_path: Path):
    """提供臨時的 cookie 目錄，隔離 cookie_service 的檔案系統操作。

    review-3.md B1: this used to patch ONLY cookie_service.COOKIE_DIR, never
    app.providers.cookies.registry.COOKIE_DIR — so a test requesting this
    fixture and then calling anything that reaches scan_cookie_files()
    (save_cookie(), delete_cookie(), list_cookies()) still had registry glob
    a DIFFERENT directory than the one this fixture just wrote to (before
    fix-round-3, registry's own binding was the unpatched, real production
    cookies/ dir; the autouse fixture now redirects that default too, but
    to its OWN separate tmp dir — still not this fixture's `c`). Patching
    registry.COOKIE_DIR to the SAME `c` here closes that mismatch: a test
    using this fixture now sees one consistent cookie directory across both
    cookie_service and registry, matching what save_cookie()->
    scan_cookie_files() actually needs to behave correctly, not just safely."""
    c = tmp_path / "cookies"
    c.mkdir()
    with patch("app.services.cookie_service.COOKIE_DIR", c), \
         patch("app.providers.cookies.registry.COOKIE_DIR", c), \
         patch("app.services.path_service.DOWNLOAD_DIR", tmp_path / "download"):
        yield c


@pytest.fixture
def tmp_doujin_download_dir(tmp_path: Path):
    """提供臨時的下載目錄，隔離 doujin_service 的檔案系統操作。"""
    d = tmp_path / "download"
    d.mkdir()
    with patch("app.services.doujin_service.DOWNLOAD_DIR", d):
        yield d


@pytest.fixture
def tmp_db(tmp_path: Path):
    """提供臨時 SQLite 檔案，隔離 schema/repo 測試，不碰真正的 data/app.db /
    data/history.json。三個路徑常數都要 patch —— LEGACY_HISTORY_FILE 是
    app.storage.db 在自己 import 時從 app.config.paths 另外綁定的一份，不會
    因為只 patch DATA_DIR 而跟著變（2026-09-01 事故：init_db() 的 legacy 遷
    移步驟因此讀了真正的 data/history.json，灌進看似「乾淨」的暫存 db）。
    assert_db_paths_isolated() 是結構性防線：往後任何一個路徑常數又意外指回
    真正的正式檔案，這裡會立刻大聲失敗，而不是安靜讀到正式資料。"""
    from app.storage import db as db_module

    db_file = tmp_path / "test_app.db"
    legacy_history_file = tmp_path / "unused_legacy_history.json"  # deliberately absent
    with patch.object(db_module, "DATA_DIR", tmp_path), \
         patch.object(db_module, "DB_FILE", db_file), \
         patch.object(db_module, "LEGACY_HISTORY_FILE", legacy_history_file):
        assert_db_paths_isolated(db_module)
        db_module._READY = False
        db_module.init_db()
        yield db_file
        db_module._READY = False
