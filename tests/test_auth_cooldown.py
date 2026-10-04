"""
tests/test_auth_cooldown.py

app.domain.auth_cooldown — the domain-scoped, SQLite-backed cooldown a
download provider consults BEFORE making a credentialed request, and arms
after a failure is classified AUTH (dispatch item 1, phase 1a).

Uses the shared `tmp_db` fixture (tests/conftest.py) so this never touches
the real data/app.db — see that fixture's docstring for why all three DB
path constants must be patched together.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.domain import auth_cooldown
from app.storage.repositories import auth_cooldown_repo

FIXED_NOW = datetime(2026, 1, 15, 12, 0, 0)


class _Frozen(datetime):
    """Stands in for auth_cooldown's module-global `datetime`: now() is
    always FIXED_NOW, everything else (fromisoformat, fromtimestamp) is the
    real datetime behaviour."""

    @classmethod
    def now(cls, tz=None):
        return FIXED_NOW


class TestNotYetInCooldown:
    def test_domain_never_recorded_is_not_in_cooldown(self, tmp_db):
        cooling_down, until = auth_cooldown.in_cooldown("x.com")
        assert cooling_down is False
        assert until is None

    def test_empty_domain_is_not_in_cooldown(self, tmp_db):
        # Defensive guard — never crash on a blank/unknown domain.
        assert auth_cooldown.in_cooldown("") == (False, None)


class TestRecordAuthFailureArmsCooldown:
    def test_record_then_immediately_in_cooldown(self, tmp_db):
        until_iso = auth_cooldown.record_auth_failure("x.com", "AuthRequired: Protected Tweet")
        cooling_down, reported_until = auth_cooldown.in_cooldown("x.com")
        assert cooling_down is True
        assert reported_until == until_iso

    def test_cooldown_duration_is_six_hours(self, tmp_db, monkeypatch):
        assert auth_cooldown.AUTH_COOLDOWN_SECONDS == 6 * 60 * 60
        monkeypatch.setattr(auth_cooldown, "datetime", _Frozen)
        until_iso = auth_cooldown.record_auth_failure("x.com")
        until = datetime.fromisoformat(until_iso)
        # The clock is frozen, so the recorded cooldown end is exactly the
        # failure time plus the fixed 6h constant — not a different or
        # growing value.
        assert until == FIXED_NOW + timedelta(seconds=auth_cooldown.AUTH_COOLDOWN_SECONDS)

    def test_cooldown_is_a_fixed_ttl_not_a_growing_backoff(self, tmp_db, monkeypatch):
        """Repeated auth failures for the same domain must NOT push the
        cooldown further and further out — every call sets the SAME offset
        from "now", never compounding. This is the "bounded, not an
        unbounded backoff" requirement from the dispatch brief."""
        clock = [FIXED_NOW]

        class _Stepping(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        monkeypatch.setattr(auth_cooldown, "datetime", _Stepping)
        first_until = datetime.fromisoformat(auth_cooldown.record_auth_failure("x.com"))
        clock[0] = FIXED_NOW + timedelta(seconds=1)
        second_until = datetime.fromisoformat(auth_cooldown.record_auth_failure("x.com"))
        # second_until is refreshed from "now": exactly the 1 s the frozen
        # clock stepped, never another full 6h (that would be compounding).
        assert second_until - first_until == timedelta(seconds=1)

    def test_cooldown_is_scoped_per_domain(self, tmp_db):
        auth_cooldown.record_auth_failure("x.com")
        cooling_down, _ = auth_cooldown.in_cooldown("instagram.com")
        assert cooling_down is False

    def test_record_requires_a_domain(self, tmp_db):
        import pytest

        with pytest.raises(ValueError):
            auth_cooldown.record_auth_failure("")


class TestErrorTextIsSanitizedBeforeStorage:
    def test_stored_error_has_credential_placeholder_not_raw_userinfo(self, tmp_db):
        """The dispatch brief's hard rule: every classified message reaching
        the DB must pass through the existing sanitizer first. A credential
        embedded in a URL (userinfo) must never reach the auth_cooldown row
        verbatim — but the surviving parts (host, path) must still be
        there, per this repo's own hard-won lesson that an assertion which
        only checks "the secret is gone" is blind to over-redaction."""
        raw_error = "HttpError: '401 Unauthorized' for 'https://user:hunter2@example.com/api/x'"
        auth_cooldown.record_auth_failure("example.com", raw_error)
        state = auth_cooldown_repo.get_state("example.com")
        stored = state["last_classified_error"]
        assert "hunter2" not in stored
        assert "user:hunter2" not in stored
        # surviving half: host and path must still be present
        assert "example.com" in stored
        assert "/api/x" in stored
        assert "[@acc]" in stored and "[@pw]" in stored

    def test_no_error_text_given_stores_empty_string(self, tmp_db):
        auth_cooldown.record_auth_failure("example.com")
        state = auth_cooldown_repo.get_state("example.com")
        assert state["last_classified_error"] == ""


class TestCooldownMessage:
    def test_message_names_the_domain_and_until_timestamp(self, tmp_db):
        until_iso = "2026-09-02T18:00:00"
        message = auth_cooldown.cooldown_message("x.com", until_iso)
        assert "x.com" in message
        assert until_iso in message


class TestClearCooldown:
    """B2 fix: a manual override that ends a cooldown immediately, regardless
    of the TTL — used both by cookie_service.save_cookie()/delete_cookie()
    (automatic, on a cookie-jar change) and the manual-override API endpoint
    (DELETE /api/cookies/<domain>/cooldown, on owner request)."""

    def test_clearing_an_armed_cooldown_returns_true_and_ends_it(self, tmp_db):
        auth_cooldown.record_auth_failure("x.com", "AuthRequired: Protected Tweet")
        cooling_down_before, _ = auth_cooldown.in_cooldown("x.com")
        assert cooling_down_before is True

        cleared = auth_cooldown.clear_cooldown("x.com")

        assert cleared is True
        cooling_down_after, until_after = auth_cooldown.in_cooldown("x.com")
        assert cooling_down_after is False
        assert until_after is None

    def test_clearing_a_domain_with_no_cooldown_returns_false_and_is_a_no_op(self, tmp_db):
        cleared = auth_cooldown.clear_cooldown("never-failed.example")
        assert cleared is False

    def test_clearing_an_empty_domain_returns_false(self, tmp_db):
        assert auth_cooldown.clear_cooldown("") is False

    def test_clear_is_scoped_to_its_own_domain(self, tmp_db):
        auth_cooldown.record_auth_failure("x.com")
        auth_cooldown.record_auth_failure("instagram.com")

        auth_cooldown.clear_cooldown("x.com")

        x_cooling, _ = auth_cooldown.in_cooldown("x.com")
        ig_cooling, _ = auth_cooldown.in_cooldown("instagram.com")
        assert x_cooling is False
        assert ig_cooling is True


class TestCookieChangeInvalidatesCooldown:
    """B2 fix: in_cooldown(domain, cookie_path) self-heals when the cookie
    file at `cookie_path` was modified more recently than the cooldown was
    last armed — without needing an explicit clear_cooldown() call. This is
    the general fallback (cookie_service.save_cookie() calling
    clear_cooldown() directly is the PRIMARY, immediate path for the owner's
    own re-seed workflow; this covers a jar rewritten some other way, e.g. a
    MULTI_PROVIDER_DOMAINS sibling domain sharing the same physical file)."""

    def test_cookie_file_modified_after_cooldown_armed_clears_it(self, tmp_db, tmp_path, monkeypatch):
        cookie_file = tmp_path / "cookies-x-com.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

        monkeypatch.setattr(auth_cooldown, "datetime", _Frozen)
        auth_cooldown.record_auth_failure("x.com", "AuthRequired: Protected Tweet")

        # Simulate the jar being rewritten (a re-seed, or an engine's own
        # cookies-update write-back) AFTER the cooldown was armed — the arming
        # time is frozen at FIXED_NOW and the mtime is set to a constant 10 s
        # after it (the same local-time basis the product compares in).
        import os

        cookie_file.write_text("# Netscape HTTP Cookie File\n\n.x.com\tTRUE\t/\tTRUE\t0\ta\t1\n", encoding="utf-8")
        later = FIXED_NOW.timestamp() + 10
        os.utime(cookie_file, (later, later))

        cooling_down, until = auth_cooldown.in_cooldown("x.com", cookie_file)

        assert cooling_down is False
        assert until is None
        # the stale row must actually be gone, not just skipped this once
        assert auth_cooldown_repo.get_state("x.com") is None

    def test_cookie_file_unchanged_since_arming_still_cools_down(self, tmp_db, tmp_path):
        cookie_file = tmp_path / "cookies-x-com.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

        auth_cooldown.record_auth_failure("x.com", "AuthRequired: Protected Tweet")

        cooling_down, until = auth_cooldown.in_cooldown("x.com", cookie_file)

        assert cooling_down is True
        assert until is not None

    def test_missing_cookie_path_is_ignored_not_an_error(self, tmp_db, tmp_path):
        auth_cooldown.record_auth_failure("x.com")
        missing = tmp_path / "does-not-exist.txt"

        cooling_down, _ = auth_cooldown.in_cooldown("x.com", missing)

        assert cooling_down is True

    def test_no_cookie_path_given_reproduces_ttl_only_behaviour(self, tmp_db):
        """Omitting cookie_path (the default, and every EXISTING caller before
        this fix) must behave exactly as before — no self-heal, TTL only."""
        auth_cooldown.record_auth_failure("x.com")
        cooling_down, _ = auth_cooldown.in_cooldown("x.com")
        assert cooling_down is True


class TestDomainAliasesShareOneCooldown:
    """fix-round-2 (B-non-blocking N3): twitter.com/x.com and
    fb.watch/facebook.com are FOUR keys sharing TWO physical cookie jars
    (app.config.settings.MULTI_PROVIDER_DOMAINS), but normalize_domain()
    does not alias them. Without app.config.settings.cooldown_domain_key(),
    a cooldown armed while a job's URL hostname was "twitter.com" survives
    the owner re-seeding a cookie under "x.com" — the owner's own top-
    failure domain (60 of 75 observed failures are [twitter]) on exactly
    the engine (yt-dlp) that has no other self-heal for it."""

    def test_recording_under_twitter_alias_is_found_under_x_com(self, tmp_db):
        auth_cooldown.record_auth_failure("twitter.com", "AuthRequired: Protected Tweet")
        cooling_down, _ = auth_cooldown.in_cooldown("x.com")
        assert cooling_down is True

    def test_clearing_x_com_ends_a_cooldown_armed_under_twitter_alias(self, tmp_db):
        """The concrete owner-facing case: a cooldown armed while a job hit
        a twitter.com URL must be cleared by re-seeding the cookie under
        x.com — the spelling cookie_service.save_cookie() actually stores
        today."""
        auth_cooldown.record_auth_failure("twitter.com", "AuthRequired: Protected Tweet")
        cooling_down_before, _ = auth_cooldown.in_cooldown("twitter.com")
        assert cooling_down_before is True

        cleared = auth_cooldown.clear_cooldown("x.com")

        assert cleared is True
        cooling_down_after, _ = auth_cooldown.in_cooldown("twitter.com")
        assert cooling_down_after is False

    def test_recording_under_fb_watch_alias_is_found_under_facebook_com(self, tmp_db):
        auth_cooldown.record_auth_failure("fb.watch", "AuthRequired: login required")
        cooling_down, _ = auth_cooldown.in_cooldown("facebook.com")
        assert cooling_down is True

    def test_only_one_row_exists_for_an_aliased_pair_not_two(self, tmp_db):
        """Both spellings must resolve to the SAME row, not two independent
        ones — otherwise the alias pair could silently diverge again."""
        auth_cooldown.record_auth_failure("twitter.com")
        assert auth_cooldown_repo.get_state("x.com") is not None
        assert auth_cooldown_repo.get_state("twitter.com") is None

    def test_unrelated_domains_are_unaffected_by_the_alias_map(self, tmp_db):
        auth_cooldown.record_auth_failure("instagram.com")
        cooling_down, _ = auth_cooldown.in_cooldown("pixiv.net")
        assert cooling_down is False
