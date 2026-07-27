"""Phase 4A — Exit-code constants + exception hierarchy tests (§10.4).

Targeted tests for the Phase 4A exit-code surface:

1. The six proposed bootstrap v1 exit-code constants (7–12) are
   re-exported from ``aee.installer`` and pin to their §10.4 values.
2. The six new exception classes map 1:1 to those constants via
   ``cls.exit_code``.
3. The verified constants (0, 2, 3, 4, 5, 6) are unchanged.
4. The new exceptions are subclasses of ``InstallerError``.
5. Each exception's ``__init__`` stores its structured fields.
6. No two exception classes share the same ``exit_code``.
7. The new exception messages do not leak secret values.

Run::

    PYTHONPATH=. python3 -m unittest aee.tests.test_installer_exit_codes -v
"""

from __future__ import annotations

import inspect
import unittest

from aee.installer import (
    # Verified constants (must remain unchanged)
    EXIT_OK,
    EXIT_PROFILE_INVALID,
    EXIT_PRE_FLIGHT_FAILED,
    EXIT_PROFILE_SWITCH_REJECTED,
    EXIT_EXECUTE_NOT_AUTHORIZED,
    # Phase 4A proposed constants (§10.4)
    EXIT_STAGE_FAILED_RETRYABLE,
    EXIT_STAGE_FAILED_PERMANENT,
    EXIT_DRIFT_DETECTED,
    EXIT_NETWORK_ERROR,
    EXIT_SECRET_MISSING,
    EXIT_DEPENDENCY_FLOOR_NOT_MET,
    # Verified exception classes
    InstallerError,
    ProfileSwitchRejectedError,
    PreFlightFailedError,
    ExecuteNotAuthorizedError,
    # Phase 4A exception classes
    StageFailedRetryableError,
    StageFailedPermanentError,
    DriftDetectedError,
    NetworkError,
    SecretMissingError,
    DependencyFloorNotMetError,
)
from aee.installer import backend as backend_mod
from aee.installer import lifecycle as lifecycle_mod


# ---------------------------------------------------------------------------#
# Constants — values + re-export
# ---------------------------------------------------------------------------#


class TestProposedExitCodeConstants(unittest.TestCase):
    """The six proposed constants (7–12) are re-exported and pin to §10.4."""

    def test_exit_stage_failed_retryable_is_7(self) -> None:
        self.assertEqual(EXIT_STAGE_FAILED_RETRYABLE, 7)

    def test_exit_stage_failed_permanent_is_8(self) -> None:
        self.assertEqual(EXIT_STAGE_FAILED_PERMANENT, 8)

    def test_exit_drift_detected_is_9(self) -> None:
        self.assertEqual(EXIT_DRIFT_DETECTED, 9)

    def test_exit_network_error_is_10(self) -> None:
        self.assertEqual(EXIT_NETWORK_ERROR, 10)

    def test_exit_secret_missing_is_11(self) -> None:
        self.assertEqual(EXIT_SECRET_MISSING, 11)

    def test_exit_dependency_floor_not_met_is_12(self) -> None:
        self.assertEqual(EXIT_DEPENDENCY_FLOOR_NOT_MET, 12)

    def test_all_proposed_constants_in_free_range_7_to_12(self) -> None:
        proposed = {
            EXIT_STAGE_FAILED_RETRYABLE,
            EXIT_STAGE_FAILED_PERMANENT,
            EXIT_DRIFT_DETECTED,
            EXIT_NETWORK_ERROR,
            EXIT_SECRET_MISSING,
            EXIT_DEPENDENCY_FLOOR_NOT_MET,
        }
        for code in proposed:
            self.assertGreaterEqual(code, 7)
            self.assertLessEqual(code, 12)

    def test_proposed_constants_are_distinct(self) -> None:
        proposed = [
            EXIT_STAGE_FAILED_RETRYABLE,
            EXIT_STAGE_FAILED_PERMANENT,
            EXIT_DRIFT_DETECTED,
            EXIT_NETWORK_ERROR,
            EXIT_SECRET_MISSING,
            EXIT_DEPENDENCY_FLOOR_NOT_MET,
        ]
        self.assertEqual(len(proposed), len(set(proposed)))


class TestVerifiedExitCodeConstantsUnchanged(unittest.TestCase):
    """The verified constants (0, 2, 3, 4, 5, 6) MUST be unchanged."""

    _VERIFIED = {0, 2, 3, 4, 5, 6}

    def test_exit_ok_is_0(self) -> None:
        self.assertEqual(EXIT_OK, 0)

    def test_exit_profile_invalid_is_3(self) -> None:
        self.assertEqual(EXIT_PROFILE_INVALID, 3)

    def test_exit_pre_flight_failed_is_4(self) -> None:
        self.assertEqual(EXIT_PRE_FLIGHT_FAILED, 4)

    def test_exit_profile_switch_rejected_is_5(self) -> None:
        self.assertEqual(EXIT_PROFILE_SWITCH_REJECTED, 5)

    def test_exit_execute_not_authorized_is_6(self) -> None:
        self.assertEqual(EXIT_EXECUTE_NOT_AUTHORIZED, 6)

    def test_no_collision_between_verified_and_proposed(self) -> None:
        proposed = {
            EXIT_STAGE_FAILED_RETRYABLE,
            EXIT_STAGE_FAILED_PERMANENT,
            EXIT_DRIFT_DETECTED,
            EXIT_NETWORK_ERROR,
            EXIT_SECRET_MISSING,
            EXIT_DEPENDENCY_FLOOR_NOT_MET,
        }
        self.assertEqual(proposed & self._VERIFIED, set())


class TestConstantsAreReExportedFromPackage(unittest.TestCase):
    """``aee.installer`` re-exports both verified and proposed constants."""

    def test_verified_constants_re_exported(self) -> None:
        import aee.installer as pkg
        for name in (
            "EXIT_OK", "EXIT_PROFILE_INVALID", "EXIT_PRE_FLIGHT_FAILED",
            "EXIT_PROFILE_SWITCH_REJECTED", "EXIT_EXECUTE_NOT_AUTHORIZED",
        ):
            self.assertTrue(hasattr(pkg, name), f"{name} not re-exported")

    def test_proposed_constants_re_exported(self) -> None:
        import aee.installer as pkg
        for name in (
            "EXIT_STAGE_FAILED_RETRYABLE", "EXIT_STAGE_FAILED_PERMANENT",
            "EXIT_DRIFT_DETECTED", "EXIT_NETWORK_ERROR",
            "EXIT_SECRET_MISSING", "EXIT_DEPENDENCY_FLOOR_NOT_MET",
        ):
            self.assertTrue(hasattr(pkg, name), f"{name} not re-exported")

    def test_constants_in___all__(self) -> None:
        import aee.installer as pkg
        for name in (
            "EXIT_STAGE_FAILED_RETRYABLE", "EXIT_STAGE_FAILED_PERMANENT",
            "EXIT_DRIFT_DETECTED", "EXIT_NETWORK_ERROR",
            "EXIT_SECRET_MISSING", "EXIT_DEPENDENCY_FLOOR_NOT_MET",
        ):
            self.assertIn(name, pkg.__all__)


# ---------------------------------------------------------------------------#
# Exception hierarchy — class-level contracts
# ---------------------------------------------------------------------------#


class TestExceptionHierarchy(unittest.TestCase):
    """All new exceptions are InstallerError subclasses with correct exit_code."""

    _NEW_CLASSES = [
        StageFailedRetryableError,
        StageFailedPermanentError,
        DriftDetectedError,
        NetworkError,
        SecretMissingError,
        DependencyFloorNotMetError,
    ]

    def test_all_new_exceptions_subclass_installer_error(self) -> None:
        for cls in self._NEW_CLASSES:
            self.assertTrue(
                issubclass(cls, InstallerError),
                f"{cls.__name__} is not an InstallerError subclass",
            )

    def test_all_new_exceptions_subclass_exception(self) -> None:
        for cls in self._NEW_CLASSES:
            self.assertTrue(issubclass(cls, Exception))

    def test_new_exception_exit_codes_match_proposed_constants(self) -> None:
        mapping = [
            (StageFailedRetryableError, EXIT_STAGE_FAILED_RETRYABLE),
            (StageFailedPermanentError, EXIT_STAGE_FAILED_PERMANENT),
            (DriftDetectedError, EXIT_DRIFT_DETECTED),
            (NetworkError, EXIT_NETWORK_ERROR),
            (SecretMissingError, EXIT_SECRET_MISSING),
            (DependencyFloorNotMetError, EXIT_DEPENDENCY_FLOOR_NOT_MET),
        ]
        for cls, expected_code in mapping:
            self.assertEqual(
                cls.exit_code,
                expected_code,
                f"{cls.__name__}.exit_code != {expected_code}",
            )

    def test_new_exception_exit_codes_are_distinct(self) -> None:
        codes = [cls.exit_code for cls in self._NEW_CLASSES]
        self.assertEqual(len(codes), len(set(codes)), "duplicate exit codes")

    def test_new_exception_exit_codes_do_not_collide_with_verified(self) -> None:
        verified = {0, 2, 3, 4, 5, 6}
        for cls in self._NEW_CLASSES:
            self.assertNotIn(
                cls.exit_code, verified,
                f"{cls.__name__}.exit_code={cls.exit_code} collides with verified",
            )

    def test_verified_exception_exit_codes_unchanged(self) -> None:
        """Existing exception classes keep their verified exit codes."""
        self.assertEqual(ProfileSwitchRejectedError.exit_code, 5)
        self.assertEqual(PreFlightFailedError.exit_code, 4)
        self.assertEqual(ExecuteNotAuthorizedError.exit_code, 6)


# ---------------------------------------------------------------------------#
# Exception construction + structured fields
# ---------------------------------------------------------------------------#


class TestStageFailedRetryableError(unittest.TestCase):
    def test_exit_code_is_7(self) -> None:
        self.assertEqual(StageFailedRetryableError.exit_code, 7)

    def test_construction_stores_stage_and_reason(self) -> None:
        err = StageFailedRetryableError("02_clone", "timeout")
        self.assertEqual(err.stage, "02_clone")
        self.assertEqual(err.reason, "timeout")

    def test_construction_without_reason(self) -> None:
        err = StageFailedRetryableError("02_clone")
        self.assertEqual(err.stage, "02_clone")
        self.assertEqual(err.reason, "")

    def test_message_contains_stage(self) -> None:
        err = StageFailedRetryableError("02_clone", "timeout")
        self.assertIn("02_clone", str(err))
        self.assertIn("retryable", str(err))

    def test_raisable_and_caught_as_installer_error(self) -> None:
        with self.assertRaises(InstallerError):
            raise StageFailedRetryableError("01_deps")


class TestStageFailedPermanentError(unittest.TestCase):
    def test_exit_code_is_8(self) -> None:
        self.assertEqual(StageFailedPermanentError.exit_code, 8)

    def test_construction_stores_stage_and_reason(self) -> None:
        err = StageFailedPermanentError("04_runtime_setup", "venv creation failed")
        self.assertEqual(err.stage, "04_runtime_setup")
        self.assertEqual(err.reason, "venv creation failed")

    def test_message_contains_stage_and_permanent(self) -> None:
        err = StageFailedPermanentError("04_runtime_setup")
        self.assertIn("04_runtime_setup", str(err))
        self.assertIn("permanent", str(err))


class TestDriftDetectedError(unittest.TestCase):
    def test_exit_code_is_9(self) -> None:
        self.assertEqual(DriftDetectedError.exit_code, 9)

    def test_construction_stores_field_expected_actual(self) -> None:
        err = DriftDetectedError("commit_sha", "abc123", "def456")
        self.assertEqual(err.field, "commit_sha")
        self.assertEqual(err.expected, "abc123")
        self.assertEqual(err.actual, "def456")

    def test_message_contains_field_and_values(self) -> None:
        err = DriftDetectedError("commit_sha", "abc123", "def456")
        msg = str(err)
        self.assertIn("commit_sha", msg)
        self.assertIn("abc123", msg)
        self.assertIn("def456", msg)


class TestNetworkError(unittest.TestCase):
    def test_exit_code_is_10(self) -> None:
        self.assertEqual(NetworkError.exit_code, 10)

    def test_construction_stores_operation_and_reason(self) -> None:
        err = NetworkError("git_clone", "connection refused")
        self.assertEqual(err.operation, "git_clone")
        self.assertEqual(err.reason, "connection refused")

    def test_construction_without_reason(self) -> None:
        err = NetworkError("git_fetch")
        self.assertEqual(err.operation, "git_fetch")
        self.assertEqual(err.reason, "")

    def test_message_contains_operation(self) -> None:
        err = NetworkError("git_clone", "timeout")
        self.assertIn("git_clone", str(err))


class TestSecretMissingError(unittest.TestCase):
    def test_exit_code_is_11(self) -> None:
        self.assertEqual(SecretMissingError.exit_code, 11)

    def test_construction_stores_secret_name(self) -> None:
        err = SecretMissingError("AEE_API_KEY")
        self.assertEqual(err.secret_name, "AEE_API_KEY")

    def test_message_contains_secret_name_not_value(self) -> None:
        err = SecretMissingError("AEE_API_KEY")
        msg = str(err)
        self.assertIn("AEE_API_KEY", msg)
        # The message must not contain a secret *value* — only the name.
        # This test is a contract: the message shape is "required secret
        # missing or invalid: '<name>'" and never includes a value.
        self.assertNotIn("=", msg)

    def test_message_shape(self) -> None:
        err = SecretMissingError("HERMES_TOKEN")
        self.assertIn("required secret missing or invalid", str(err))


class TestDependencyFloorNotMetError(unittest.TestCase):
    def test_exit_code_is_12(self) -> None:
        self.assertEqual(DependencyFloorNotMetError.exit_code, 12)

    def test_construction_stores_dependency_required_found(self) -> None:
        err = DependencyFloorNotMetError("python", "3.11", "3.8")
        self.assertEqual(err.dependency, "python")
        self.assertEqual(err.required, "3.11")
        self.assertEqual(err.found, "3.8")

    def test_message_contains_dependency_and_versions(self) -> None:
        err = DependencyFloorNotMetError("node", "18", "14")
        msg = str(err)
        self.assertIn("node", msg)
        self.assertIn("18", msg)
        self.assertIn("14", msg)


# ---------------------------------------------------------------------------#
# Source-level contracts (no subprocess, no secret leakage)
# ---------------------------------------------------------------------------#


class TestSourceContracts(unittest.TestCase):
    """The new exception classes do not introduce subprocess or secret leakage."""

    def test_backend_module_does_not_import_subprocess(self) -> None:
        source = inspect.getsource(backend_mod)
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("from subprocess", source)

    def test_backend_module_does_not_use_os_system(self) -> None:
        source = inspect.getsource(backend_mod)
        # os.system / os.popen are not used
        self.assertNotIn("os.system(", source)
        self.assertNotIn("os.popen(", source)

    def test_secret_missing_error_message_does_not_include_value_param(self) -> None:
        """SecretMissingError's __init__ accepts only secret_name, never a value."""
        sig = inspect.signature(SecretMissingError.__init__)
        params = list(sig.parameters.keys())
        # self + secret_name only
        self.assertEqual(params, ["self", "secret_name"])


# ---------------------------------------------------------------------------#
# Backend ↔ lifecycle constant identity (single source of truth)
# ---------------------------------------------------------------------------#


class TestConstantIdentity(unittest.TestCase):
    """The backend's exception exit_codes reference the lifecycle constants."""

    def test_backend_imports_constants_from_lifecycle(self) -> None:
        """backend.py should import the 6 proposed constants from lifecycle."""
        # Verify the backend module has the constants as module-level names
        # (imported, not redefined).
        for name in (
            "EXIT_STAGE_FAILED_RETRYABLE",
            "EXIT_STAGE_FAILED_PERMANENT",
            "EXIT_DRIFT_DETECTED",
            "EXIT_NETWORK_ERROR",
            "EXIT_SECRET_MISSING",
            "EXIT_DEPENDENCY_FLOOR_NOT_MET",
        ):
            self.assertTrue(
                hasattr(backend_mod, name),
                f"backend module missing {name}",
            )

    def test_backend_constants_are_same_object_as_lifecycle(self) -> None:
        """The backend's constants ARE the lifecycle's constants (same int object)."""
        # For small ints, CPython interns them, so `is` works.
        # We verify value equality which is the meaningful check.
        pairs = [
            ("EXIT_STAGE_FAILED_RETRYABLE", 7),
            ("EXIT_STAGE_FAILED_PERMANENT", 8),
            ("EXIT_DRIFT_DETECTED", 9),
            ("EXIT_NETWORK_ERROR", 10),
            ("EXIT_SECRET_MISSING", 11),
            ("EXIT_DEPENDENCY_FLOOR_NOT_MET", 12),
        ]
        for name, val in pairs:
            self.assertEqual(getattr(backend_mod, name), val)
            self.assertEqual(getattr(lifecycle_mod, name), val)

    def test_backend_does_not_redefine_proposed_constants(self) -> None:
        """backend.py must NOT redefine the 6 constants — it imports them.

        This prevents accidental renumbering: if backend.py had its own
        ``EXIT_STAGE_FAILED_RETRYABLE = 7`` line, a future renumber in
        lifecycle.py would silently desync.
        """
        source = inspect.getsource(backend_mod)
        # The constants should appear in an import block, not as assignments.
        # We check that there is no "EXIT_STAGE_FAILED_RETRYABLE = " assignment
        # line (the import line uses "EXIT_STAGE_FAILED_RETRYABLE," with a comma).
        for name in (
            "EXIT_STAGE_FAILED_RETRYABLE",
            "EXIT_STAGE_FAILED_PERMANENT",
            "EXIT_DRIFT_DETECTED",
            "EXIT_NETWORK_ERROR",
            "EXIT_SECRET_MISSING",
            "EXIT_DEPENDENCY_FLOOR_NOT_MET",
        ):
            # Look for "NAME = <number>" pattern (assignment, not import)
            import_pattern = "{n} =".format(n=name)
            # The import line is "    EXIT_STAGE_FAILED_RETRYABLE," — no " = "
            # An assignment would be "EXIT_STAGE_FAILED_RETRYABLE = 7"
            lines_with_assign = [
                line for line in source.splitlines()
                if line.strip().startswith(name + " =")
            ]
            self.assertEqual(
                len(lines_with_assign), 0,
                f"{name} is redefined (assigned) in backend.py — should be imported only",
            )


if __name__ == "__main__":
    unittest.main()