"""AEE Bootstrap v1 — W10 integration tests (spec §16 W10, §8.2, §5.5).

Targeted integration tests for the Bootstrap v1 Phase B surface:

* :class:`TestRedactionEnvVarNames` — :func:`redact_env_var_names`
  covers the §8.2 env-var-name patterns.
* :class:`TestRedactionAuthorizationHeaders` — bearer/basic/JWT.
* :class:`TestRedactionHighEntropy` — long hex/base64 truncation.
* :class:`TestRedactAll` — canonical order, no double-redact.
* :class:`TestRedactionIdempotency` — redacted text is stable.
* :class:`TestRedactionNoFalsePositives` — prose preserved.
* :class:`TestRedactionSentinelStability` — sentinel not self-redacted.
* :class:`TestBootstrapResumeIntegration` — §5.5 resume contract.
* :class:`TestStageTransitionIntegration` — full lifecycle cycle.
* :class:`TestRedactionInStageMarker` — §8.4 stderr_tail redaction.

All tests are stdlib ``unittest`` — no pytest, no filesystem, no network.

NOTE on test construction: test strings that contain ``NAME=secret``
patterns are built at runtime via string concatenation so the literal
``=`` + secret-looking value never appears in source. This avoids the
shell token-substitution trap documented in MEMORY (the ``write_file``
content parameter expands ``$VAR``-style tokens).
"""
from __future__ import annotations

import unittest

from aee.installer.lifecycle import (
    BootstrapLifecycle,
    InMemoryMarkerStore,
    StageName,
    StageState,
)
from aee.installer.redaction import (
    REDACTED_SENTINEL,
    redact_all,
    redact_authorization_headers,
    redact_env_var_names,
    redact_high_entropy_strings,
)

# --- Runtime-constructed test fixtures (avoid $VAR expansion in source) ---
_EQ = "="
_API_KEY = "_API_KEY"
_TOKEN = "_TOKEN"
_SECRET = "_SECRET"
_PASSWORD = "_PASSWORD"
_NAME_SENTINEL_OPEN = "<REDACTED:"
_NAME_SENTINEL_CLOSE = ">"


def _env_line(suffix: str, value: str) -> str:
    """Build ``PREFIX{suffix}={value}`` at runtime."""
    return "OPENAI" + suffix + _EQ + value


def _name_sentinel(suffix: str) -> str:
    """Build ``<REDACTED:OPENAI{SUFFIX}>`` at runtime."""
    return _NAME_SENTINEL_OPEN + "OPENAI" + suffix + _NAME_SENTINEL_CLOSE


# ===========================================================================
# Redaction — env var names (§8.2)
# ===========================================================================


class TestRedactionEnvVarNames(unittest.TestCase):
    """:func:`redact_env_var_names` — §8.2 env-var-name patterns."""

    def _expected_after_env(self, suffix: str) -> str:
        # env-name filter preserves NAME= and replaces value with <REDACTED:NAME>
        return "OPENAI" + suffix + _EQ + _name_sentinel(suffix)

    def _expected_after_all(self, suffix: str) -> str:
        # redact_all = env filter then high-entropy. The <REDACTED:NAME>
        # value contains `<`, `>`, `:` which are < 80% alphanumeric but
        # the high-entropy regex character class excludes those punctuation
        # chars, so the sentinel survives high-entropy truncation. However
        # the high-entropy regex matches the alphanumeric span inside the
        # sentinel (e.g. "OPENAI_API_KEY" in "<REDACTED:OPENAI_API_KEY>")
        # if that span is >= 40 chars. Our test sentinel is shorter, so
        # it survives. Use redact_all to compute the expected value at
        # runtime rather than hard-coding it.
        src = _env_line(suffix, "sk-rea...-key")
        return redact_all(src)

    def test_api_key_redacted(self) -> None:
        src = _env_line(_API_KEY, "sk-rea...-key")
        out = redact_env_var_names(src)
        # env-name filter only (not high-entropy): NAME=<REDACTED:NAME>
        self.assertEqual(self._expected_after_env(_API_KEY), out)

    def test_token_redacted(self) -> None:
        src = _env_line(_TOKEN, "tok-abc123")
        out = redact_env_var_names(src)
        self.assertEqual(self._expected_after_env(_TOKEN), out)

    def test_secret_redacted(self) -> None:
        src = _env_line(_SECRET, "sec-xyz")
        out = redact_env_var_names(src)
        self.assertEqual(self._expected_after_env(_SECRET), out)

    def test_password_redacted(self) -> None:
        src = _env_line(_PASSWORD, "p@ssw0rd")
        out = redact_env_var_names(src)
        self.assertEqual(self._expected_after_env(_PASSWORD), out)

    def test_case_insensitive(self) -> None:
        src = "openai" + _API_KEY.lower() + _EQ + "sk-low"
        out = redact_env_var_names(src)
        self.assertIn(_NAME_SENTINEL_OPEN, out)

    def test_name_preserved_in_sentinel(self) -> None:
        src = _env_line(_API_KEY, "sk-rea...-key")
        out = redact_env_var_names(src)
        self.assertIn("OPENAI" + _API_KEY, out)

    def test_non_secret_env_var_not_redacted(self) -> None:
        out = redact_env_var_names("PATH=/usr/bin:/bin")
        self.assertEqual("PATH=/usr/bin:/bin", out)

    def test_multiple_secrets_in_one_line(self) -> None:
        src = (
            _env_line(_API_KEY, "sk-aaa")
            + " "
            + _env_line(_TOKEN, "tok-bbb")
        )
        out = redact_env_var_names(src)
        self.assertIn(_name_sentinel(_API_KEY), out)
        self.assertIn(_name_sentinel(_TOKEN), out)

    def test_no_value_no_redaction(self) -> None:
        out = redact_env_var_names("export OPENAI_API_KEY")
        self.assertEqual("export OPENAI_API_KEY", out)

    def test_empty_value_redacted(self) -> None:
        src = _env_line(_API_KEY, "")
        out = redact_env_var_names(src)
        self.assertEqual(self._expected_after_env(_API_KEY), out)


# ===========================================================================
# Redaction — Authorization headers (§8.2)
# ===========================================================================


class TestRedactionAuthorizationHeaders(unittest.TestCase):
    """:func:`redact_authorization_headers` — bearer/basic/JWT."""

    def test_bearer_token_redacted(self) -> None:
        src = "Authorization: Bearer abc123def456"
        out = redact_authorization_headers(src)
        self.assertIn(REDACTED_SENTINEL, out)
        self.assertNotIn("abc123def456", out)

    def test_basic_auth_redacted(self) -> None:
        src = "Authorization: Basic dXNlcjpwYXNz"
        out = redact_authorization_headers(src)
        self.assertIn(REDACTED_SENTINEL, out)
        self.assertNotIn("dXNlcjpwYXNz", out)

    def test_jwt_redacted(self) -> None:
        # A realistic JWT shape (header.payload.signature) — built at
        # runtime to avoid the literal being treated as a token.
        jwt = "eyJ" + ("a" * 20) + "." + ("b" * 20) + "." + ("c" * 20)
        out = redact_authorization_headers("token: " + jwt)
        self.assertIn(REDACTED_SENTINEL, out)
        self.assertNotIn(jwt, out)

    def test_case_insensitive_scheme(self) -> None:
        src = "authorization: bearer abc123"
        out = redact_authorization_headers(src)
        self.assertIn(REDACTED_SENTINEL, out)
        self.assertNotIn("abc123", out)

    def test_non_auth_header_not_redacted(self) -> None:
        src = "Content-Type: application/json"
        out = redact_authorization_headers(src)
        self.assertEqual(src, out)

    def test_bearer_prefix_preserved(self) -> None:
        src = "Authorization: Bearer abc123def456"
        out = redact_authorization_headers(src)
        self.assertIn("Authorization: Bearer", out)
        self.assertIn(REDACTED_SENTINEL, out)


# ===========================================================================
# Redaction — high-entropy strings (§8.2)
# ===========================================================================


class TestRedactionHighEntropy(unittest.TestCase):
    """:func:`redact_high_entropy_strings` — long hex/base64."""

    def test_long_hex_truncated(self) -> None:
        token = "a" * 50
        out = redact_high_entropy_strings("sha: " + token)
        self.assertIn("aaaaaaaa" + "\u2026" + "aaaa", out)
        self.assertNotIn(token, out)

    def test_short_hex_not_redacted(self) -> None:
        out = redact_high_entropy_strings("sha: abc123")
        self.assertEqual("sha: abc123", out)

    def test_long_base64_truncated(self) -> None:
        token = "A" * 45
        out = redact_high_entropy_strings("data: " + token)
        self.assertIn("AAAAAAAA" + "\u2026" + "AAAA", out)

    def test_prose_not_redacted(self) -> None:
        prose = "The quick brown fox jumps over the lazy dog and " * 3
        out = redact_high_entropy_strings(prose)
        self.assertEqual(prose, out)

    def test_exactly_40_chars_redacted(self) -> None:
        token = "a" * 40
        out = redact_high_entropy_strings(token)
        self.assertIn("\u2026", out)

    def test_39_chars_not_redacted(self) -> None:
        token = "a" * 39
        out = redact_high_entropy_strings(token)
        self.assertEqual(token, out)

    def test_truncation_format(self) -> None:
        token = "abcdefghij" * 5  # 50 chars
        out = redact_high_entropy_strings(token)
        self.assertEqual("abcdefgh" + "\u2026" + "ghij", out)


# ===========================================================================
# Redaction — redact_all (canonical order)
# ===========================================================================


class TestRedactAll(unittest.TestCase):
    """:func:`redact_all` — all three filters in sequence."""

    def test_env_var_then_high_entropy(self) -> None:
        token = "b" * 50
        src = _env_line(_API_KEY, token)
        out = redact_all(src)
        self.assertIn(_name_sentinel(_API_KEY), out)
        self.assertNotIn(token, out)

    def test_authorization_then_high_entropy(self) -> None:
        token = "b" * 50
        src = "Authorization: Bearer " + token
        out = redact_all(src)
        self.assertIn(REDACTED_SENTINEL, out)
        self.assertNotIn(token, out)

    def test_mixed_secrets(self) -> None:
        token_a = "x" * 50
        token_b = "y" * 20
        src = _env_line(_API_KEY, token_a) + " Authorization: Bearer " + token_b
        out = redact_all(src)
        self.assertIn(_name_sentinel(_API_KEY), out)
        self.assertIn(REDACTED_SENTINEL, out)
        self.assertNotIn(token_a, out)
        self.assertNotIn(token_b, out)

    def test_no_secrets_unchanged(self) -> None:
        src = "profile=full channel=stable"
        self.assertEqual(src, redact_all(src))


# ===========================================================================
# Redaction — idempotency
# ===========================================================================


class TestRedactionIdempotency(unittest.TestCase):
    """Redacting an already-redacted string is a no-op."""

    def test_env_var_idempotent(self) -> None:
        once = redact_env_var_names(_env_line(_API_KEY, "sk-abc"))
        twice = redact_env_var_names(once)
        self.assertEqual(once, twice)

    def test_authorization_idempotent(self) -> None:
        once = redact_authorization_headers("Authorization: Bearer abc123")
        twice = redact_authorization_headers(once)
        self.assertEqual(once, twice)

    def test_high_entropy_idempotent(self) -> None:
        once = redact_high_entropy_strings("a" * 50)
        twice = redact_high_entropy_strings(once)
        self.assertEqual(once, twice)

    def test_redact_all_idempotent(self) -> None:
        token = "c" * 50
        src = _env_line(_API_KEY, token) + " Authorization: Bearer " + token
        once = redact_all(src)
        twice = redact_all(once)
        self.assertEqual(once, twice)


# ===========================================================================
# Redaction — no false positives
# ===========================================================================


class TestRedactionNoFalsePositives(unittest.TestCase):
    """Ordinary text is not redacted."""

    def test_version_string_not_redacted(self) -> None:
        out = redact_all("aee version 2.0.0-rc1")
        self.assertEqual("aee version 2.0.0-rc1", out)

    def test_short_path_not_redacted(self) -> None:
        out = redact_all("/home/ubuntu/hermes-runtime-bridge")
        self.assertEqual("/home/ubuntu/hermes-runtime-bridge", out)

    def test_profile_not_redacted(self) -> None:
        out = redact_all("profile=full")
        self.assertEqual("profile=full", out)

    def test_short_url_not_redacted(self) -> None:
        out = redact_all("https://github.com/nousresearch/aee")
        self.assertEqual("https://github.com/nousresearch/aee", out)

    def test_empty_string(self) -> None:
        self.assertEqual("", redact_all(""))

    def test_plain_text_no_secrets(self) -> None:
        src = "The bootstrap completed stage 01_deps successfully."
        self.assertEqual(src, redact_all(src))


# ===========================================================================
# Redaction — sentinel stability
# ===========================================================================


class TestRedactionSentinelStability(unittest.TestCase):
    """The sentinel itself does not match the secret patterns."""

    def test_sentinel_not_redacted_by_env_filter(self) -> None:
        out = redact_env_var_names(REDACTED_SENTINEL)
        self.assertEqual(REDACTED_SENTINEL, out)

    def test_sentinel_not_redacted_by_header_filter(self) -> None:
        out = redact_authorization_headers(REDACTED_SENTINEL)
        self.assertEqual(REDACTED_SENTINEL, out)

    def test_sentinel_not_redacted_by_high_entropy(self) -> None:
        out = redact_high_entropy_strings(REDACTED_SENTINEL)
        self.assertEqual(REDACTED_SENTINEL, out)

    def test_redacted_name_sentinel_stable(self) -> None:
        sentinel = _name_sentinel(_API_KEY)
        out = redact_all(sentinel)
        self.assertEqual(sentinel, out)


# ===========================================================================
# Bootstrap resume — Python-side integration (§5.5)
# ===========================================================================


class TestBootstrapResumeIntegration(unittest.TestCase):
    """Lifecycle ``get_resume_stage`` + simulated marker directory (§5.5)."""

    def test_fresh_install_resumes_at_detect(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("run-1")
        self.assertEqual(lc.get_resume_stage(), StageName.DETECT)

    def test_partial_completion_resumes_at_clone(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("run-1")
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        lc.record_stage(StageName.DEPS, StageState.COMPLETED)
        self.assertEqual(lc.get_resume_stage(), StageName.CLONE)

    def test_failed_stage_resumes_at_failed(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("run-1")
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        lc.record_stage(StageName.DEPS, StageState.FAILED, error_class="AptError")
        self.assertEqual(lc.get_resume_stage(), StageName.DEPS)

    def test_completed_install_resumes_none(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("run-1")
        for stage in StageName:
            lc.record_stage(stage, StageState.COMPLETED)
        self.assertIsNone(lc.get_resume_stage())
        self.assertTrue(lc.is_complete())

    def test_skipped_smoke_test_still_complete(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("run-1")
        for stage in StageName:
            if stage is StageName.SMOKE_TEST:
                lc.record_stage(stage, StageState.SKIPPED)
            else:
                lc.record_stage(stage, StageState.COMPLETED)
        self.assertIsNone(lc.get_resume_stage())
        self.assertTrue(lc.is_complete())

    def test_in_progress_resumes_at_in_progress(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("run-1")
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        lc.record_stage(StageName.DEPS, StageState.IN_PROGRESS)
        self.assertEqual(lc.get_resume_stage(), StageName.DEPS)


# ===========================================================================
# Stage transition integration (full cycle)
# ===========================================================================


class TestStageTransitionIntegration(unittest.TestCase):
    """Full PENDING→IN_PROGRESS→COMPLETED→FAILED→retry→COMPLETED cycle."""

    def test_full_cycle_with_retry(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("run-1")
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        lc.record_stage(
            StageName.DEPS,
            StageState.FAILED,
            error_class="AptError",
            stderr_tail="E: Unable to locate package foo",
        )
        self.assertEqual(lc.get_resume_stage(), StageName.DEPS)
        marker = lc.get_marker(StageName.DEPS)
        assert marker is not None
        self.assertEqual(marker.state, StageState.FAILED)
        self.assertEqual(marker.error_class, "AptError")
        # First FAILED → retry_count=1 (no previous FAILED marker).
        self.assertEqual(marker.retry_count, 1)
        # Retry succeeds.
        lc.record_stage(StageName.DEPS, StageState.COMPLETED)
        marker = lc.get_marker(StageName.DEPS)
        assert marker is not None
        self.assertEqual(marker.state, StageState.COMPLETED)
        self.assertEqual(lc.get_resume_stage(), StageName.CLONE)

    def test_failed_marker_records_stderr_tail(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("run-1")
        lc.record_stage(
            StageName.DEPS,
            StageState.FAILED,
            error_class="AptError",
            stderr_tail="E: Unable to locate package foo",
        )
        marker = lc.get_marker(StageName.DEPS)
        assert marker is not None
        self.assertEqual(marker.stderr_tail, "E: Unable to locate package foo")

    def test_completed_marker_has_no_error_class(self) -> None:
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("run-1")
        lc.record_stage(StageName.DETECT, StageState.COMPLETED)
        marker = lc.get_marker(StageName.DETECT)
        assert marker is not None
        self.assertIsNone(marker.error_class)
        self.assertIsNone(marker.stderr_tail)


# ===========================================================================
# Redaction in stage markers (§8.4)
# ===========================================================================


class TestRedactionInStageMarker(unittest.TestCase):
    """A failed-stage marker's ``stderr_tail`` is redacted per §8.4."""

    def test_stderr_tail_with_api_key_redacted(self) -> None:
        raw_tail = _env_line(_API_KEY, "sk-rea...-key") + " failed"
        redacted = redact_all(raw_tail)
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("run-1")
        lc.record_stage(
            StageName.DEPS,
            StageState.FAILED,
            error_class="NetworkError",
            stderr_tail=redacted,
        )
        marker = lc.get_marker(StageName.DEPS)
        assert marker is not None
        self.assertNotIn("sk-rea...-key", marker.stderr_tail or "")
        self.assertIn(_name_sentinel(_API_KEY), marker.stderr_tail or "")

    def test_stderr_tail_with_bearer_token_redacted(self) -> None:
        raw_tail = "Authorization: Bearer abc123def456ghi789"
        redacted = redact_all(raw_tail)
        lc = BootstrapLifecycle(InMemoryMarkerStore())
        lc.start("run-1")
        lc.record_stage(
            StageName.HEALTH_CHECK,
            StageState.FAILED,
            error_class="HttpError",
            stderr_tail=redacted,
        )
        marker = lc.get_marker(StageName.HEALTH_CHECK)
        assert marker is not None
        self.assertIn(REDACTED_SENTINEL, marker.stderr_tail or "")
        self.assertNotIn("abc123def456ghi789", marker.stderr_tail or "")

    def test_stderr_tail_without_secrets_unchanged(self) -> None:
        raw_tail = "E: Unable to locate package supervisor"
        redacted = redact_all(raw_tail)
        self.assertEqual(raw_tail, redacted)


# ===========================================================================
# Module-level smoke
# ===========================================================================


class TestModuleSmoke(unittest.TestCase):
    """Module-level imports + public API surface."""

    def test_public_exports(self) -> None:
        from aee.installer import redaction
        for name in (
            "REDACTED_SENTINEL",
            "SECRET_ENV_NAME_PATTERN",
            "BEARER_TOKEN_PATTERN",
            "BASIC_AUTH_PATTERN",
            "JWT_PATTERN",
            "redact_env_var_names",
            "redact_authorization_headers",
            "redact_high_entropy_strings",
            "redact_all",
        ):
            self.assertTrue(hasattr(redaction, name), f"missing: {name}")

    def test_sentinel_value(self) -> None:
        self.assertEqual(REDACTED_SENTINEL, "<REDACTED>")


if __name__ == "__main__":
    unittest.main()