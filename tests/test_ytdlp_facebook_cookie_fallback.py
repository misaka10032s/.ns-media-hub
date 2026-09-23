"""
tests/test_ytdlp_facebook_cookie_fallback.py

app.providers.ytdlp.provider — Facebook "Cannot parse data" cookie fallback
(2026-09-23 root cause: passing `--cookies` to yt-dlp makes the facebook
extractor fail with `Cannot parse data` on PUBLIC reels/posts, on every
yt-dlp version tested; dropping `--cookies` makes the SAME url succeed).

Spec (PM decision, state/runs/nsmh-fb-origin-260923/fallback-implement.md):
1. On a facebook.com/fb.watch download that ran WITH a cookie file and
   failed with a `[facebook] ... Cannot parse data` error, retry the SAME
   job ONCE without `--cookies`.
2. This must happen BEFORE download_service's stale-extractor auto-update
   retry, and a success on the cookie-less retry must not trigger the
   updater. A cookie-less-retry failure falls through to existing behaviour
   unchanged (whatever error the retry produced is what download_service
   sees and classifies as it always has).
3. Both attempts are recorded in DownloadResult.metadata, and an info line
   is logged on the retry.
4. No change for any other domain.

subprocess is ALWAYS mocked — no real yt-dlp process is spawned.
"""
from __future__ import annotations

from unittest.mock import patch

from app.providers.ytdlp import provider as ytdlp_provider

FACEBOOK_PARSE_ERROR = (
    "ERROR: [facebook] 1813045736547447: Cannot parse data; please report this issue on "
    "https://github.com/yt-dlp/yt-dlp/issues , filling out the appropriate issue template. "
    "Confirm you are on the latest version using  yt-dlp -U"
)


def _fake_process(lines: list[str], returncode: int):
    from unittest.mock import MagicMock

    process = MagicMock()
    iterator = iter([*lines, ""])  # readline() returns "" to signal EOF
    process.stdout.readline.side_effect = lambda: next(iterator)
    process.wait.return_value = None
    process.returncode = returncode
    return process


# ---------------------------------------------------------------------------
# (a) the error matcher — a small pure function
# ---------------------------------------------------------------------------
class TestIsFacebookCookieParseError:
    def test_matches_the_verbatim_error_line_from_the_diagnosis_report(self):
        assert ytdlp_provider._is_facebook_cookie_parse_error(FACEBOOK_PARSE_ERROR) is True

    def test_other_facebook_error_does_not_match(self):
        assert ytdlp_provider._is_facebook_cookie_parse_error("ERROR: [facebook] 123: Video unavailable") is False

    def test_same_phrase_from_a_non_facebook_extractor_does_not_match(self):
        assert (
            ytdlp_provider._is_facebook_cookie_parse_error(
                "ERROR: [youtube] 123: Cannot parse data; please report this issue on ..."
            )
            is False
        )

    def test_none_error_does_not_match(self):
        assert ytdlp_provider._is_facebook_cookie_parse_error(None) is False

    def test_empty_error_does_not_match(self):
        assert ytdlp_provider._is_facebook_cookie_parse_error("") is False


# ---------------------------------------------------------------------------
# (b) provider flow, subprocess mocked
# ---------------------------------------------------------------------------
class TestCookieRunFailsThenCookielessRetrySucceeds:
    def test_second_call_drops_cookies_and_succeeds(self, tmp_path):
        cookie_fail = _fake_process([FACEBOOK_PARSE_ERROR + "\n"], returncode=1)
        cookieless_success = _fake_process(["/download/path/reel.mp4\n"], returncode=0)

        with (
            patch("app.services.path_service.DOWNLOAD_DIR", tmp_path),
            patch("app.providers.ytdlp.provider._resolve_executable", return_value="yt-dlp"),
            patch("app.providers.ytdlp.provider._probe_user_root", side_effect=lambda executable, url, root, cookie_path: root),
            patch("app.providers.ytdlp.provider.resolve_cookie_file", return_value="C:/cookies/cookies-facebook-com.txt"),
            patch("app.providers.ytdlp.provider.auth_cooldown.in_cooldown", return_value=(False, None)),
            patch(
                "app.providers.ytdlp.provider.subprocess.Popen",
                side_effect=[cookie_fail, cookieless_success],
            ) as mock_popen,
        ):
            result = ytdlp_provider.download("https://www.facebook.com/reel/1813045736547447")

        assert mock_popen.call_count == 2
        first_command = mock_popen.call_args_list[0].args[0]
        second_command = mock_popen.call_args_list[1].args[0]
        assert "--cookies" in first_command
        assert "--cookies" not in second_command
        assert result.status.value == "success"
        assert result.metadata["cookie_fallback_attempts"] == [
            {"cookies": True, "status": "failed"},
            {"cookies": False, "status": "success"},
        ]

    def test_updater_service_is_never_consulted_when_the_cookieless_retry_succeeds(self, tmp_path):
        """End-to-end through download_service.download_request — the retry
        must resolve entirely inside the provider, so a success there never
        reaches download_service's stale-extractor auto-update hook."""
        from app.domain.enums import JobSource, Provider
        from app.domain.jobs import JobRequest
        from app.services.download_service import download_request

        cookie_fail = _fake_process([FACEBOOK_PARSE_ERROR + "\n"], returncode=1)
        cookieless_success = _fake_process(["/download/path/reel.mp4\n"], returncode=0)
        request = JobRequest(url="https://www.facebook.com/reel/1813045736547447", provider=Provider.YTDLP, source=JobSource.API)

        with (
            patch("app.services.path_service.DOWNLOAD_DIR", tmp_path),
            patch("app.providers.ytdlp.provider._resolve_executable", return_value="yt-dlp"),
            patch("app.providers.ytdlp.provider._probe_user_root", side_effect=lambda executable, url, root, cookie_path: root),
            patch("app.providers.ytdlp.provider.resolve_cookie_file", return_value="C:/cookies/cookies-facebook-com.txt"),
            patch("app.providers.ytdlp.provider.auth_cooldown.in_cooldown", return_value=(False, None)),
            patch(
                "app.providers.ytdlp.provider.subprocess.Popen",
                side_effect=[cookie_fail, cookieless_success],
            ),
            patch("app.services.download_service.updater_service.maybe_reactive_update") as mock_update,
        ):
            result = download_request(request, tokens={})

        mock_update.assert_not_called()
        assert result.status.value == "success"


class TestCookielessRetryAlsoFails:
    def test_falls_through_with_the_retrys_own_error_and_records_both_attempts(self, tmp_path):
        cookie_fail = _fake_process([FACEBOOK_PARSE_ERROR + "\n"], returncode=1)
        retry_fail = _fake_process(["ERROR: [facebook] 1813045736547447: Unable to extract video data\n"], returncode=1)

        with (
            patch("app.services.path_service.DOWNLOAD_DIR", tmp_path),
            patch("app.providers.ytdlp.provider._resolve_executable", return_value="yt-dlp"),
            patch("app.providers.ytdlp.provider._probe_user_root", side_effect=lambda executable, url, root, cookie_path: root),
            patch("app.providers.ytdlp.provider.resolve_cookie_file", return_value="C:/cookies/cookies-facebook-com.txt"),
            patch("app.providers.ytdlp.provider.auth_cooldown.in_cooldown", return_value=(False, None)),
            patch(
                "app.providers.ytdlp.provider.subprocess.Popen",
                side_effect=[cookie_fail, retry_fail],
            ) as mock_popen,
        ):
            result = ytdlp_provider.download("https://www.facebook.com/reel/1813045736547447")

        assert mock_popen.call_count == 2
        assert result.status.value == "failed"
        # existing behaviour unchanged: the FINAL (retry) error is what is
        # returned/classified — not the original cookie-attempt's error.
        assert "Unable to extract video data" in result.error
        assert result.metadata["cookie_fallback_attempts"][0]["status"] == "failed"
        assert result.metadata["cookie_fallback_attempts"][1]["status"] == "failed"

    def test_existing_stale_extractor_update_path_still_runs_on_the_retrys_error(self, tmp_path):
        """download_service.download_request is completely unmodified — proving
        it still drives its own auto-update retry off whatever error the
        provider ultimately returns is what keeps req.2's 'fall through
        unchanged' true. We don't re-simulate the internal cookie retry here
        (that's covered above); we mock the provider boundary directly, the
        same way the pre-existing reactive-update tests do."""
        from app.domain.enums import JobSource, JobStatus, Provider
        from app.domain.jobs import DownloadResult, JobRequest
        from app.services.download_service import download_request

        request = JobRequest(url="https://www.facebook.com/reel/1813045736547447", provider=Provider.YTDLP, source=JobSource.API)
        failed_after_cookie_fallback = DownloadResult(
            status=JobStatus.FAILED,
            provider=Provider.YTDLP,
            domain="facebook.com",
            error="ERROR: [facebook] 1813045736547447: Unable to extract video data",
            metadata={
                "cookie_fallback_attempts": [
                    {"cookies": True, "status": "failed"},
                    {"cookies": False, "status": "failed"},
                ]
            },
        )
        succeeded = DownloadResult(status=JobStatus.SUCCESS, provider=Provider.YTDLP, domain="facebook.com", download_path="/x/reel.mp4")

        with (
            patch(
                "app.services.download_service._download_with_provider",
                side_effect=[failed_after_cookie_fallback, succeeded],
            ) as mock_download,
            patch(
                "app.services.download_service.updater_service.maybe_reactive_update",
                return_value={"retried": True, "changed": True, "message": ""},
            ) as mock_update,
        ):
            result = download_request(request, tokens={})

        mock_update.assert_called_once_with(Provider.YTDLP.value)
        assert mock_download.call_count == 2
        assert result.status == JobStatus.SUCCESS


class TestScopedToFacebookOnly:
    def test_non_facebook_domain_with_the_same_error_text_never_retries(self, tmp_path):
        non_facebook_error = _fake_process(
            ["ERROR: [youtube] abc: Cannot parse data; please report this issue on ...\n"], returncode=1
        )
        with (
            patch("app.services.path_service.DOWNLOAD_DIR", tmp_path),
            patch("app.providers.ytdlp.provider._resolve_executable", return_value="yt-dlp"),
            patch("app.providers.ytdlp.provider._probe_user_root", side_effect=lambda executable, url, root, cookie_path: root),
            patch("app.providers.ytdlp.provider.resolve_cookie_file", return_value="C:/cookies/cookies-youtube-com.txt"),
            patch("app.providers.ytdlp.provider.auth_cooldown.in_cooldown", return_value=(False, None)),
            patch("app.providers.ytdlp.provider.subprocess.Popen", return_value=non_facebook_error) as mock_popen,
        ):
            result = ytdlp_provider.download("https://www.youtube.com/watch?v=abc")

        mock_popen.assert_called_once()
        assert result.status.value == "failed"
        assert "cookie_fallback_attempts" not in result.metadata

    def test_no_cookie_registered_never_retries_even_on_a_matching_facebook_error(self, tmp_path):
        """Nothing to remove without a cookie file in the first place."""
        process = _fake_process([FACEBOOK_PARSE_ERROR + "\n"], returncode=1)
        with (
            patch("app.services.path_service.DOWNLOAD_DIR", tmp_path),
            patch("app.providers.ytdlp.provider._resolve_executable", return_value="yt-dlp"),
            patch("app.providers.ytdlp.provider._probe_user_root", side_effect=lambda executable, url, root, cookie_path: root),
            patch("app.providers.ytdlp.provider.resolve_cookie_file", return_value=None),
            patch("app.providers.ytdlp.provider.auth_cooldown.in_cooldown", return_value=(False, None)),
            patch("app.providers.ytdlp.provider.subprocess.Popen", return_value=process) as mock_popen,
        ):
            result = ytdlp_provider.download("https://www.facebook.com/reel/1813045736547447")

        mock_popen.assert_called_once()
        assert result.status.value == "failed"
        assert "cookie_fallback_attempts" not in result.metadata

    def test_fb_watch_domain_is_also_in_scope(self, tmp_path):
        cookie_fail = _fake_process([FACEBOOK_PARSE_ERROR + "\n"], returncode=1)
        cookieless_success = _fake_process(["/download/path/reel.mp4\n"], returncode=0)
        with (
            patch("app.services.path_service.DOWNLOAD_DIR", tmp_path),
            patch("app.providers.ytdlp.provider._resolve_executable", return_value="yt-dlp"),
            patch("app.providers.ytdlp.provider._probe_user_root", side_effect=lambda executable, url, root, cookie_path: root),
            patch("app.providers.ytdlp.provider.resolve_cookie_file", return_value="C:/cookies/cookies-facebook-com.txt"),
            patch("app.providers.ytdlp.provider.auth_cooldown.in_cooldown", return_value=(False, None)),
            patch(
                "app.providers.ytdlp.provider.subprocess.Popen",
                side_effect=[cookie_fail, cookieless_success],
            ) as mock_popen,
        ):
            result = ytdlp_provider.download("https://fb.watch/abc123")

        assert mock_popen.call_count == 2
        assert result.status.value == "success"


class TestInfoLineIsLogged:
    def test_retry_prints_the_required_info_line(self, tmp_path, capsys):
        cookie_fail = _fake_process([FACEBOOK_PARSE_ERROR + "\n"], returncode=1)
        cookieless_success = _fake_process(["/download/path/reel.mp4\n"], returncode=0)
        with (
            patch("app.services.path_service.DOWNLOAD_DIR", tmp_path),
            patch("app.providers.ytdlp.provider._resolve_executable", return_value="yt-dlp"),
            patch("app.providers.ytdlp.provider._probe_user_root", side_effect=lambda executable, url, root, cookie_path: root),
            patch("app.providers.ytdlp.provider.resolve_cookie_file", return_value="C:/cookies/cookies-facebook-com.txt"),
            patch("app.providers.ytdlp.provider.auth_cooldown.in_cooldown", return_value=(False, None)),
            patch(
                "app.providers.ytdlp.provider.subprocess.Popen",
                side_effect=[cookie_fail, cookieless_success],
            ),
        ):
            ytdlp_provider.download("https://www.facebook.com/reel/1813045736547447")

        captured = capsys.readouterr()
        assert "facebook cookies 導致解析失敗，改用無 cookies 重試" in captured.out
