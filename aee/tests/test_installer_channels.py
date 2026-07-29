"""AEE Phase 7 / W9 — Release channel + ref pinning + drift detection tests.

Spec reference: ``reports/aee_bootstrap_v1_spec.md`` §9 (Release Channels),
§16 W9, §17.3 Phase D.

This test module covers the **backend-side** release-channel vocabulary
and pin/drift dataclasses added to :mod:`aee.installer.backend` in
Phase 7 / W9. The CLI-side coverage (``run_update``, ``DriftResult``,
``UpdateCliOptions``) lives in ``aee/tests/test_aee_phase4c_update_cli.py``
(Phase 4C); this module covers the backend additions only.

Run::

    PYTHONPATH=. python3 -m unittest aee.tests.test_installer_channels -v
"""
from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from aee.installer.backend import (
    DEFAULT_CHANNEL,
    DriftReport,
    KNOWN_CHANNELS,
    ReleasePin,
    UnknownChannelError,
    validate_channel,
)


# ---------------------------------------------------------------------------#
# Channel vocabulary
# ---------------------------------------------------------------------------#


class KnownChannelsTests(unittest.TestCase):
    """The canonical release-channel set (spec §9.1)."""

    def test_known_channels_tuple(self) -> None:
        self.assertIsInstance(KNOWN_CHANNELS, tuple)
        self.assertEqual(KNOWN_CHANNELS, ("stable", "rc", "dev"))

    def test_known_channels_frozen_content(self) -> None:
        # The spec defines exactly three channels; no more, no fewer.
        self.assertEqual(len(KNOWN_CHANNELS), 3)
        self.assertIn("stable", KNOWN_CHANNELS)
        self.assertIn("rc", KNOWN_CHANNELS)
        self.assertIn("dev", KNOWN_CHANNELS)

    def test_default_channel_is_stable(self) -> None:
        self.assertEqual(DEFAULT_CHANNEL, "stable")

    def test_default_channel_in_known(self) -> None:
        self.assertIn(DEFAULT_CHANNEL, KNOWN_CHANNELS)


class ValidateChannelTests(unittest.TestCase):
    """``validate_channel`` — the backend-side channel validator."""

    def test_stable(self) -> None:
        self.assertEqual(validate_channel("stable"), "stable")

    def test_rc(self) -> None:
        self.assertEqual(validate_channel("rc"), "rc")

    def test_dev(self) -> None:
        self.assertEqual(validate_channel("dev"), "dev")

    def test_case_insensitive_uppercase(self) -> None:
        self.assertEqual(validate_channel("STABLE"), "stable")

    def test_case_insensitive_mixed(self) -> None:
        self.assertEqual(validate_channel("Stable"), "stable")

    def test_strips_whitespace(self) -> None:
        self.assertEqual(validate_channel("  stable  "), "stable")

    def test_unknown_channel_raises(self) -> None:
        with self.assertRaises(UnknownChannelError) as ctx:
            validate_channel("nightly")
        self.assertEqual(ctx.exception.channel, "nightly")

    def test_empty_string_raises(self) -> None:
        with self.assertRaises(UnknownChannelError):
            validate_channel("")

    def test_none_raises(self) -> None:
        with self.assertRaises(UnknownChannelError):
            validate_channel(None)  # type: ignore[arg-type]

    def test_non_string_raises(self) -> None:
        with self.assertRaises(UnknownChannelError):
            validate_channel(123)  # type: ignore[arg-type]

    def test_error_message_lists_known_channels(self) -> None:
        try:
            validate_channel("beta")
        except UnknownChannelError as exc:
            msg = str(exc)
            for ch in KNOWN_CHANNELS:
                self.assertIn(ch, msg)
        else:
            self.fail("expected UnknownChannelError")

    def test_error_exit_code_is_profile_invalid(self) -> None:
        # No new exit code is introduced; channel errors reuse 3
        # (EXIT_PROFILE_INVALID) as documented in the backend.
        from aee.installer.backend import EXIT_PROFILE_INVALID
        try:
            validate_channel("beta")
        except UnknownChannelError as exc:
            self.assertEqual(exc.exit_code, EXIT_PROFILE_INVALID)


# ---------------------------------------------------------------------------#
# ReleasePin dataclass
# ---------------------------------------------------------------------------#


class ReleasePinTests(unittest.TestCase):
    """``ReleasePin`` — the recorded version pin (spec §9.2)."""

    def _sample_pin(self) -> ReleasePin:
        return ReleasePin(
            channel="stable",
            ref="refs/tags/v1.0.0",
            commit_sha="a" * 40,
            pinned_at="2026-07-29T00:00:00Z",
            requirements_lock_sha256="deadbeef" * 8,
        )

    def test_construction(self) -> None:
        pin = self._sample_pin()
        self.assertEqual(pin.channel, "stable")
        self.assertEqual(pin.ref, "refs/tags/v1.0.0")
        self.assertEqual(pin.commit_sha, "a" * 40)
        self.assertEqual(pin.pinned_at, "2026-07-29T00:00:00Z")
        self.assertEqual(pin.requirements_lock_sha256, "deadbeef" * 8)

    def test_optional_lock_sha_defaults_none(self) -> None:
        pin = ReleasePin(
            channel="dev",
            ref="main",
            commit_sha="b" * 40,
            pinned_at="2026-07-29T01:00:00Z",
        )
        self.assertIsNone(pin.requirements_lock_sha256)

    def test_frozen(self) -> None:
        pin = self._sample_pin()
        with self.assertRaises(FrozenInstanceError):
            pin.channel = "rc"  # type: ignore[misc]

    def test_to_dict_keys(self) -> None:
        d = self._sample_pin().to_dict()
        self.assertEqual(
            set(d.keys()),
            {
                "channel",
                "ref",
                "commit_sha",
                "pinned_at",
                "requirements_lock_sha256",
            },
        )

    def test_to_dict_values(self) -> None:
        d = self._sample_pin().to_dict()
        self.assertEqual(d["channel"], "stable")
        self.assertEqual(d["ref"], "refs/tags/v1.0.0")
        self.assertEqual(d["commit_sha"], "a" * 40)
        self.assertEqual(d["pinned_at"], "2026-07-29T00:00:00Z")
        self.assertEqual(d["requirements_lock_sha256"], "deadbeef" * 8)

    def test_to_dict_lock_sha_none(self) -> None:
        pin = ReleasePin(
            channel="dev",
            ref="main",
            commit_sha="c" * 40,
            pinned_at="2026-07-29T02:00:00Z",
        )
        self.assertIsNone(pin.to_dict()["requirements_lock_sha256"])

    def test_from_dict_round_trip(self) -> None:
        original = self._sample_pin()
        d = original.to_dict()
        restored = ReleasePin.from_dict(d)
        self.assertEqual(restored, original)

    def test_from_dict_missing_lock_sha(self) -> None:
        d = {
            "channel": "rc",
            "ref": "refs/tags/latest-rc",
            "commit_sha": "d" * 40,
            "pinned_at": "2026-07-29T03:00:00Z",
        }
        pin = ReleasePin.from_dict(d)
        self.assertEqual(pin.channel, "rc")
        self.assertIsNone(pin.requirements_lock_sha256)

    def test_from_dict_empty_dict(self) -> None:
        pin = ReleasePin.from_dict({})
        self.assertEqual(pin.channel, "")
        self.assertEqual(pin.ref, "")
        self.assertEqual(pin.commit_sha, "")
        self.assertEqual(pin.pinned_at, "")
        self.assertIsNone(pin.requirements_lock_sha256)

    def test_from_dict_extra_keys_ignored(self) -> None:
        d = {
            "channel": "stable",
            "ref": "main",
            "commit_sha": "e" * 40,
            "pinned_at": "2026-07-29T04:00:00Z",
            "requirements_lock_sha256": "f" * 64,
            "extra_key": "ignored",
        }
        pin = ReleasePin.from_dict(d)
        self.assertEqual(pin.channel, "stable")
        self.assertEqual(pin.requirements_lock_sha256, "f" * 64)

    def test_equality(self) -> None:
        p1 = self._sample_pin()
        p2 = self._sample_pin()
        self.assertEqual(p1, p2)

    def test_inequality_different_channel(self) -> None:
        p1 = self._sample_pin()
        p2 = ReleasePin(
            channel="rc",
            ref=p1.ref,
            commit_sha=p1.commit_sha,
            pinned_at=p1.pinned_at,
            requirements_lock_sha256=p1.requirements_lock_sha256,
        )
        self.assertNotEqual(p1, p2)

    def test_inequality_different_sha(self) -> None:
        p1 = self._sample_pin()
        p2 = ReleasePin(
            channel=p1.channel,
            ref=p1.ref,
            commit_sha="z" * 40,
            pinned_at=p1.pinned_at,
            requirements_lock_sha256=p1.requirements_lock_sha256,
        )
        self.assertNotEqual(p1, p2)


# ---------------------------------------------------------------------------#
# DriftReport dataclass
# ---------------------------------------------------------------------------#


class DriftReportTests(unittest.TestCase):
    """``DriftReport`` — the backend-side drift-detection result."""

    def _sample_pin(self) -> ReleasePin:
        return ReleasePin(
            channel="stable",
            ref="refs/tags/v1.0.0",
            commit_sha="a" * 40,
            pinned_at="2026-07-29T00:00:00Z",
        )

    def test_drifted_true(self) -> None:
        pin = self._sample_pin()
        report = DriftReport(
            drifted=True,
            reason="commit_sha mismatch",
            recorded=pin,
            actual_commit_sha="b" * 40,
            actual_lock_sha256=None,
        )
        self.assertTrue(report.drifted)
        self.assertEqual(report.reason, "commit_sha mismatch")
        self.assertEqual(report.recorded, pin)
        self.assertEqual(report.actual_commit_sha, "b" * 40)
        self.assertIsNone(report.actual_lock_sha256)

    def test_drifted_false(self) -> None:
        pin = self._sample_pin()
        report = DriftReport(
            drifted=False,
            reason="",
            recorded=pin,
            actual_commit_sha=pin.commit_sha,
            actual_lock_sha256=None,
        )
        self.assertFalse(report.drifted)

    def test_no_pin_recorded_none(self) -> None:
        report = DriftReport(
            drifted=False,
            reason="no pin recorded",
            recorded=None,
            actual_commit_sha=None,
            actual_lock_sha256=None,
        )
        self.assertIsNone(report.recorded)

    def test_frozen(self) -> None:
        report = DriftReport(
            drifted=False,
            reason="",
            recorded=None,
            actual_commit_sha=None,
            actual_lock_sha256=None,
        )
        with self.assertRaises(FrozenInstanceError):
            report.drifted = True  # type: ignore[misc]

    def test_to_dict_drifted(self) -> None:
        pin = self._sample_pin()
        report = DriftReport(
            drifted=True,
            reason="sha mismatch",
            recorded=pin,
            actual_commit_sha="c" * 40,
            actual_lock_sha256="d" * 64,
        )
        d = report.to_dict()
        self.assertTrue(d["drifted"])
        self.assertEqual(d["reason"], "sha mismatch")
        self.assertIsNotNone(d["recorded"])
        self.assertEqual(d["recorded"]["commit_sha"], "a" * 40)
        self.assertEqual(d["actual_commit_sha"], "c" * 40)
        self.assertEqual(d["actual_lock_sha256"], "d" * 64)

    def test_to_dict_no_pin(self) -> None:
        report = DriftReport(
            drifted=False,
            reason="no pin",
            recorded=None,
            actual_commit_sha=None,
            actual_lock_sha256=None,
        )
        d = report.to_dict()
        self.assertFalse(d["drifted"])
        self.assertIsNone(d["recorded"])
        self.assertIsNone(d["actual_commit_sha"])
        self.assertIsNone(d["actual_lock_sha256"])

    def test_to_dict_keys(self) -> None:
        report = DriftReport(
            drifted=False,
            reason="",
            recorded=None,
            actual_commit_sha=None,
            actual_lock_sha256=None,
        )
        d = report.to_dict()
        self.assertEqual(
            set(d.keys()),
            {"drifted", "reason", "recorded", "actual_commit_sha",
             "actual_lock_sha256"},
        )

    def test_equality(self) -> None:
        pin = self._sample_pin()
        r1 = DriftReport(True, "x", pin, "a", None)
        r2 = DriftReport(True, "x", pin, "a", None)
        self.assertEqual(r1, r2)


# ---------------------------------------------------------------------------#
# Cross-module consistency
# ---------------------------------------------------------------------------#


class CrossModuleConsistencyTests(unittest.TestCase):
    """The update CLI (Phase 4C) and backend (Phase 7) must agree."""

    def test_known_channels_match_update_module(self) -> None:
        from aee.installer.update import KNOWN_CHANNELS as upd_channels
        self.assertEqual(KNOWN_CHANNELS, upd_channels)

    def test_default_channel_matches_update_module(self) -> None:
        from aee.installer.update import DEFAULT_CHANNEL as upd_default
        self.assertEqual(DEFAULT_CHANNEL, upd_default)

    def test_validate_channel_matches_update_module(self) -> None:
        from aee.installer.update import validate_channel as upd_validate
        # After Phase 7 minimal-fix harmonisation, both validate_channel
        # functions are case-insensitive and strip whitespace. They agree
        # on all canonical lowercase inputs AND on non-lowercase inputs.
        for ch in ("stable", "rc", "dev", "STABLE", "Stable", "  stable  "):
            self.assertEqual(validate_channel(ch), upd_validate(ch))

    def test_validate_channel_both_reject_unknown(self) -> None:
        from aee.installer.update import validate_channel as upd_validate
        # Both surfaces reject unknown channels — backend raises
        # UnknownChannelError (InstallerError subclass), update CLI
        # raises ValueError. The *acceptance* contract is: both reject.
        import traceback
        for ch in ("bogus", "nightly", ""):
            with self.assertRaises(Exception):
                validate_channel(ch)
            with self.assertRaises(Exception):
                upd_validate(ch)

    def test_unknown_channel_error_is_installer_error(self) -> None:
        from aee.installer.backend import InstallerError
        self.assertTrue(issubclass(UnknownChannelError, InstallerError))


# ---------------------------------------------------------------------------#
# JSON serialisable
# ---------------------------------------------------------------------------#


class JsonSerializableTests(unittest.TestCase):
    """All new dataclasses must be JSON-serialisable via ``to_dict``."""

    def test_release_pin_json(self) -> None:
        import json
        pin = ReleasePin(
            channel="stable",
            ref="refs/tags/v1.0.0",
            commit_sha="a" * 40,
            pinned_at="2026-07-29T00:00:00Z",
        )
        d = pin.to_dict()
        json.dumps(d)  # must not raise

    def test_drift_report_json(self) -> None:
        import json
        report = DriftReport(
            drifted=True,
            reason="mismatch",
            recorded=ReleasePin(
                channel="stable",
                ref="main",
                commit_sha="a" * 40,
                pinned_at="2026-07-29T00:00:00Z",
            ),
            actual_commit_sha="b" * 40,
            actual_lock_sha256=None,
        )
        d = report.to_dict()
        json.dumps(d)  # must not raise


if __name__ == "__main__":
    unittest.main()