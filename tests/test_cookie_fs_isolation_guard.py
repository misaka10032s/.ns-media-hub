"""
tests/test_cookie_fs_isolation_guard.py

review-3.md B1 (fix-round-3, 2026-09-02): the DB isolation fixture closed
app.storage.db's three names but left the cookie jar pointed at production
— app.services.cookie_service.COOKIE_DIR and, separately,
app.providers.cookies.registry.COOKIE_DIR / LEGACY_COOKIE_DIRS. Concretely:
cookie_service.delete_cookie() calls `existing.unlink()` on a path that
scan_cookie_files() (registry, its OWN unpatched COOKIE_DIR) just globbed
from the real jar, and registry.migrate_legacy_cookie_files() calls
shutil.move() on every `*.txt` under the real legacy dirs. This branch adds
the first tests that call save_cookie()/delete_cookie() for real (see
tests/test_cookie_service_atomic_write.py,
tests/test_cookie_service_cooldown_clear.py) — one forgotten opt-in fixture
away from unlinking one of the owner's five hand-seeded cookie files.

Same fix shape as tests/test_tmp_db_isolation_guard.py:
1. TestLandsInTmpPathWithoutAnyOptInFixture — proves the DEFAULT (no
   tmp_cookie_dir requested) is already isolated, per tests/conftest.py's
   autouse fixture.
2. TestGuardFiresOnRealPaths — proves assert_fs_paths_isolated() raises on
   a deliberately real-pointed binding (rubric R2: a check nobody has
   watched fail is not a check).
3. TestWidenedGuardFiresRegardlessOfFixtureUse — proves the STRUCTURAL
   Path.unlink/Path.mkdir/shutil.move guard fires even when a consumer's
   path constant is deliberately re-pointed at the real production
   directory AFTER the autouse fixture already ran — the backstop for a
   future consumer this fixture forgot to redirect.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from app.providers.cookies import registry as cookie_registry_module
from app.services import cookie_service
from tests.conftest import (
    _REAL_COOKIE_DIR,
    _REAL_LEGACY_COOKIE_DIRS,
    assert_fs_paths_isolated,
)


class TestLandsInTmpPathWithoutAnyOptInFixture:
    """No test here requests tmp_cookie_dir — proving the autouse fixture's
    DEFAULT isolation, not the opt-in fixture, is what makes this safe."""

    def test_save_cookie_never_writes_under_the_real_cookie_dir(self):
        record = cookie_service.save_cookie("probe-fs-guard.example", "Cookie: a=1; b=2")
        saved_path = Path(record["file_path"]).resolve()
        assert saved_path != (_REAL_COOKIE_DIR / saved_path.name).resolve()
        assert _REAL_COOKIE_DIR.resolve() not in saved_path.parents
        assert saved_path.exists()

    def test_delete_cookie_never_unlinks_a_file_under_the_real_cookie_dir(self, monkeypatch):
        # Exercises the EXACT route review-3.md flagged: save then delete,
        # both via scan_cookie_files() (registry), with zero opt-in fixture.
        # The save write (cookie_service._atomic_write_text) and the delete
        # (Path.unlink) are wrapped with recorders that delegate; every
        # recorded target is checked against the real jar. Nothing reads the
        # real jar's listing, so a running app or the owner cannot change
        # the outcome.
        recorded_targets: list[Path] = []

        real_atomic_write_text = cookie_service._atomic_write_text

        def _recording_atomic_write_text(path, *args, **kwargs):
            recorded_targets.append(Path(path))
            return real_atomic_write_text(path, *args, **kwargs)

        real_path_unlink = Path.unlink

        def _recording_unlink(self, *args, **kwargs):
            recorded_targets.append(Path(self))
            return real_path_unlink(self, *args, **kwargs)

        monkeypatch.setattr(cookie_service, "_atomic_write_text", _recording_atomic_write_text)
        monkeypatch.setattr(Path, "unlink", _recording_unlink)

        record = cookie_service.save_cookie("probe-fs-guard-2.example", "Cookie: a=1")
        saved_path = Path(record["file_path"])
        assert saved_path.exists()

        deleted = cookie_service.delete_cookie("probe-fs-guard-2.example")

        assert deleted >= 1
        assert not saved_path.exists()
        resolved_targets = [target.resolve() for target in recorded_targets]
        assert saved_path.resolve() in resolved_targets
        real_jar = _REAL_COOKIE_DIR.resolve()
        for target in resolved_targets:
            assert target != real_jar
            assert real_jar not in target.parents

    def test_scan_cookie_files_never_migrates_the_real_legacy_dirs(self):
        # migrate_legacy_cookie_files() is a no-op today because the real
        # legacy dirs are empty/absent (review-3.md §1.5) — but the point of
        # this test is that it runs against the ISOLATED legacy dirs at all,
        # not the real ones, regardless of whether they're empty.
        assert cookie_registry_module.LEGACY_COOKIE_DIRS != _REAL_LEGACY_COOKIE_DIRS
        for d in cookie_registry_module.LEGACY_COOKIE_DIRS:
            assert _REAL_COOKIE_DIR.resolve() != Path(d).resolve()
        cookie_registry_module.scan_cookie_files()  # must not raise


class TestGuardFiresOnRealPaths:
    def test_assert_fs_paths_isolated_raises_when_cookie_service_cookie_dir_is_real(self):
        with pytest.raises(RuntimeError, match=re.escape("cookie_service.COOKIE_DIR")):
            assert_fs_paths_isolated(
                {"cookie_service.COOKIE_DIR": (_REAL_COOKIE_DIR, _REAL_COOKIE_DIR)}
            )

    def test_assert_fs_paths_isolated_raises_when_registry_legacy_dirs_are_real(self):
        with pytest.raises(RuntimeError, match=re.escape("registry.LEGACY_COOKIE_DIRS")):
            assert_fs_paths_isolated(
                {
                    "registry.LEGACY_COOKIE_DIRS": (
                        _REAL_LEGACY_COOKIE_DIRS,
                        _REAL_LEGACY_COOKIE_DIRS,
                    )
                }
            )

    def test_assert_fs_paths_isolated_stays_green_for_a_properly_isolated_pair(self, tmp_path):
        assert (
            assert_fs_paths_isolated(
                {"cookie_service.COOKIE_DIR": (tmp_path / "cookies", _REAL_COOKIE_DIR)}
            )
            is None
        )


class TestWidenedGuardFiresRegardlessOfFixtureUse:
    """Deliberately UNDOES the autouse fixture's isolation for one binding
    (after it already ran for this test), to prove the structural
    Path.unlink/Path.mkdir/shutil.move guard catches it independently —
    not merely because the autouse fixture happened to keep things
    isolated."""

    def test_delete_cookie_guard_fires_when_cookie_service_and_registry_are_repointed_at_real(
        self, monkeypatch
    ):
        # Initialize the isolated DB schema FIRST, while COOKIE_DIR is
        # still safely isolated — otherwise this probe fails for an
        # UNRELATED reason (cookie_entries table not yet created, since
        # this test deliberately doesn't request tmp_db) before ever
        # reaching the guarded filesystem call delete_cookie() makes at its
        # own end (scan_cookie_files() -> migrate_legacy_cookie_files() ->
        # COOKIE_DIR.mkdir()).
        cookie_registry_module.scan_cookie_files()
        monkeypatch.setattr(cookie_service, "COOKIE_DIR", _REAL_COOKIE_DIR)
        monkeypatch.setattr(cookie_registry_module, "COOKIE_DIR", _REAL_COOKIE_DIR)
        with pytest.raises(RuntimeError, match="production"):
            cookie_service.delete_cookie("some-domain.example", missing_ok=True)

    def test_scan_cookie_files_guard_fires_when_registry_cookie_dir_is_repointed_at_real(
        self, monkeypatch
    ):
        monkeypatch.setattr(cookie_registry_module, "COOKIE_DIR", _REAL_COOKIE_DIR)
        with pytest.raises(RuntimeError, match="production"):
            cookie_registry_module.scan_cookie_files()

    def test_migrate_legacy_cookie_files_guard_fires_when_legacy_dirs_are_repointed_at_real(
        self, monkeypatch, tmp_path
    ):
        # Give registry.COOKIE_DIR a harmless tmp target so ONLY the legacy
        # dirs are the deliberately-real part being isolated in this test.
        monkeypatch.setattr(cookie_registry_module, "COOKIE_DIR", tmp_path / "cookies")
        monkeypatch.setattr(
            cookie_registry_module, "LEGACY_COOKIE_DIRS", _REAL_LEGACY_COOKIE_DIRS
        )
        # migrate_legacy_cookie_files() only iterates a legacy dir that
        # `.exists()`, and whether module/cookies/ exists at all is
        # TREE-relative (in this worktree it currently does not — a
        # gitignored dir that was never populated here); the guard is a
        # pure path-value check (see _path_under_guarded_root's docstring),
        # so it fires on the real legacy dir regardless of whether that
        # directory happens to exist on disk right now. Proven directly via
        # shutil.move rather than through migrate_legacy_cookie_files()
        # itself, so the proof does not depend on that existence coincidence.
        real_legacy_dir = _REAL_LEGACY_COOKIE_DIRS[1]
        probe_src = tmp_path / "probe.txt"
        probe_src.write_text("not a real cookie", encoding="utf-8")
        with pytest.raises(RuntimeError, match="production"):
            shutil.move(str(probe_src), str(real_legacy_dir / "probe.txt"))

    def test_direct_path_unlink_under_the_real_cookie_dir_is_blocked_even_for_a_nonexistent_file(
        self,
    ):
        # The guard is a pure path-value check — it fires BEFORE touching
        # the filesystem, so it also blocks a target that doesn't exist.
        target = _REAL_COOKIE_DIR / "this-file-need-not-exist-for-the-guard-to-fire.txt"
        with pytest.raises(RuntimeError, match="production"):
            target.unlink()

    def test_direct_path_mkdir_under_the_real_cookie_dir_is_blocked(self):
        with pytest.raises(RuntimeError, match="production"):
            _REAL_COOKIE_DIR.mkdir(parents=True, exist_ok=True)
