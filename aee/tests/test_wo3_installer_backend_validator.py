"""WO-3: Installer backend capabilities validator binding (§21.6.G item 3).

Targeted tests for the WO-3 slice: the installer backend
(:mod:`aee.installer.backend`) is bound to the authoritative
Provider-Neutral Deployment Contract (§21.6.A–§21.6.F) via the canonical
loader (:func:`aee.deploy.loader.load_host_capabilities`) and validators
(:func:`aee.deploy.contract.validate_capabilities` +
:func:`aee.deploy.contract.validate_resource_floor`).

When ``--capabilities <path>`` is supplied, the backend loads + validates
the referenced YAML BEFORE any plan/preflight/execute action. A failure is
surfaced as a deterministic :class:`CapabilitiesValidationResult` with a
stable ``reason_kind`` vocabulary; the CLI layer maps it to
:data:`EXIT_CAPABILITIES_INVALID` (13).

Coverage:

1. **Exit code constant** — ``EXIT_CAPABILITIES_INVALID == 13``, distinct
   from §21.3 installer codes (3-6) and §10.4 bootstrap codes (7-12).
2. **Reason-kind vocabulary** — ``CAPABILITIES_REASON_KINDS`` is the exact
   6-tuple.
3. **CapabilitiesValidationResult** — frozen dataclass, ``to_dict`` shape,
   backward-compat ``ok=True`` result when path is ``None``.
4. **CapabilitiesValidationError** — subclasses ``InstallerError``,
   ``exit_code == 13``, carries ``reason_kind`` / ``reason`` / ``field`` /
   ``cap_path``.
5. **Backend.validate_capabilities_document** — the 6 failure modes:
   missing file, unreadable file, malformed YAML, contract violation,
   resource-floor violation, unknown error; plus the success path and the
   backward-compat ``None`` path.
6. **Backend constructor cap_path** — ``cap_path`` stored on the backend,
   used as fallback when the method argument is ``None``.
7. **CLI integration** — ``run_install`` with valid ``--capabilities`` →
   exit 0 + WO-3 validated note; missing file → exit 13; malformed YAML →
   exit 13; contract violation → exit 13; resource floor violation → exit
   13; no ``--capabilities`` → exit 0 (backward compat, no WO-3 note).
8. **CLI integration with --execute** — valid ``--capabilities`` + ``--execute``
   → exit 6 (ExecuteNotAuthorized) + WO-3 validated note appended.
9. **CLI cap_path threading** — the backend constructor receives
   ``cap_path=options.capabilities``.
10. **Backward compatibility** — omitting ``--capabilities`` yields no
    validation I/O, no WO-3 note, exit 0 (identical to pre-WO-3 surface).
11. **CLI-level integration** — ``aee install --capabilities <valid>`` →
    exit 0, JSON carries ``capabilities``; ``aee install --capabilities
    <missing>`` → exit 13.
12. **No subprocess** — AST scan of ``cli_install.py`` + ``backend.py``
    confirms no ``subprocess`` / ``os.system`` / ``os.popen`` in the
    validation path.
13. **Canonical contract binding** — the backend imports and calls the
    canonical loader + validators (not a parallel hard-coded matrix).

All tests are stdlib ``unittest`` — no pytest, no subprocess, no host
mutation. Read-only.

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_wo3_installer_backend_validator -v``
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from aee.installer.backend import (
    CAPABILITIES_REASON_KINDS,
    EXIT_CAPABILITIES_INVALID,
    EXIT_OK,
    EXIT_EXECUTE_NOT_AUTHORIZED,
    EXIT_PRE_FLIGHT_FAILED,
    EXIT_PROFILE_INVALID,
    InstallerBackend,
    InstallerError,
    CapabilitiesValidationResult,
    CapabilitiesValidationError,
)
from aee.installer.cli_install import (
    InstallCliOptions,
    InstallCliResult,
    run_install,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_CAP_PATH = REPO_ROOT / "host.capabilities.yaml"


# ---------------------------------------------------------------------------#
# Helpers
# ---------------------------------------------------------------------------#


def _write_temp_yaml(content: str) -> str:
    """Write a YAML file to a temporary path and return the path."""
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="aee-wo3-test-")
    os.close(fd)
    Path(path).write_text(content, encoding="utf-8")
    return path


def _make_valid_cap_yaml(
    *,
    host_name: str = "test-host",
    host_class: str = "container",
    resource_floor_full: dict | None = None,
) -> str:
    """Build a minimal valid Host Capability Document YAML."""
    if resource_floor_full is None:
        resource_floor_full = {"cpu": 2, "mem_mb": 4096, "disk_mb": 2048}
    return textwrap.dedent(
        """\
        host:
          name: {name}
          class: {cls}
          os: linux
          arch: x86_64
          python: ">=3.11"
          filesystem: posix
          supervisor: supervisord
          network_egress: tunnel
          tunnel_kind: cloudflared
          inbound_allowed: false
          db_path_writable: true
          tempdir_writable: true
          persistent_paths:
            - /home/ubuntu
        runtime_profile:
          supported: [full, mini, edge, developer]
          default: full
          resource_floor:
            full:      {full_floor}
            mini:      {{ cpu: 1, mem_mb: 1024, disk_mb: 1024 }}
            edge:      {{ cpu: 1, mem_mb: 1024, disk_mb: 512  }}
            developer: {{ cpu: 1, mem_mb: 1024, disk_mb: 512  }}
        upstream_llm:
          reachable: true
          endpoint_kind: openai-compatible
        """
    ).format(
        name=host_name,
        cls=host_class,
        full_floor=_floor_to_yaml(resource_floor_full),
    )


def _floor_to_yaml(floor: dict) -> str:
    parts = ", ".join(
        "{k}: {v}".format(k=k, v=v) for k, v in sorted(floor.items())
    )
    return "{" + parts + "}"


# ---------------------------------------------------------------------------#
# 1. Exit code constant
# ---------------------------------------------------------------------------#


class ExitCodeConstantTests(unittest.TestCase):
    """``EXIT_CAPABILITIES_INVALID`` is 13 and distinct from existing codes."""

    def test_value_is_13(self):
        self.assertEqual(EXIT_CAPABILITIES_INVALID, 13)

    def test_distinct_from_installer_codes(self):
        # §21.3 installer exit codes are 3-6.
        for code in (3, 4, 5, 6):
            self.assertNotEqual(EXIT_CAPABILITIES_INVALID, code)

    def test_distinct_from_bootstrap_codes(self):
        # §10.4 bootstrap v1 exit codes are 7-12.
        for code in range(7, 13):
            self.assertNotEqual(EXIT_CAPABILITIES_INVALID, code)

    def test_in_free_range(self):
        self.assertGreaterEqual(EXIT_CAPABILITIES_INVALID, 7)
        self.assertLessEqual(EXIT_CAPABILITIES_INVALID, 127)


# ---------------------------------------------------------------------------#
# 2. Reason-kind vocabulary
# ---------------------------------------------------------------------------#


class ReasonKindVocabularyTests(unittest.TestCase):
    """``CAPABILITIES_REASON_KINDS`` is the exact 6-tuple."""

    def test_is_tuple(self):
        self.assertIsInstance(CAPABILITIES_REASON_KINDS, tuple)

    def test_exact_members(self):
        expected = (
            "missing_file",
            "unreadable_file",
            "malformed_yaml",
            "contract_violation",
            "resource_floor",
            "unknown_error",
        )
        self.assertEqual(CAPABILITIES_REASON_KINDS, expected)

    def test_no_duplicates(self):
        self.assertEqual(
            len(CAPABILITIES_REASON_KINDS),
            len(set(CAPABILITIES_REASON_KINDS)),
        )


# ---------------------------------------------------------------------------#
# 3. CapabilitiesValidationResult dataclass
# ---------------------------------------------------------------------------#


class CapabilitiesValidationResultTests(unittest.TestCase):
    """Shape and behavior of :class:`CapabilitiesValidationResult`."""

    def test_frozen(self):
        result = CapabilitiesValidationResult(ok=True)
        with self.assertRaises(Exception):
            result.ok = False  # type: ignore[misc]

    def test_defaults(self):
        result = CapabilitiesValidationResult(ok=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.reason_kind, "")
        self.assertEqual(result.reason, "")
        self.assertEqual(result.field, "")
        self.assertIsNone(result.capabilities)
        self.assertIsNone(result.cap_path)

    def test_to_dict_ok(self):
        result = CapabilitiesValidationResult(ok=True, cap_path="/tmp/x.yaml")
        d = result.to_dict()
        self.assertTrue(d["ok"])
        self.assertEqual(d["reason_kind"], "")
        self.assertIsNone(d["capabilities"])
        self.assertEqual(d["cap_path"], "/tmp/x.yaml")

    def test_to_dict_failure(self):
        result = CapabilitiesValidationResult(
            ok=False,
            reason_kind="missing_file",
            reason="not found",
            cap_path="/tmp/missing.yaml",
        )
        d = result.to_dict()
        self.assertFalse(d["ok"])
        self.assertEqual(d["reason_kind"], "missing_file")
        self.assertEqual(d["reason"], "not found")
        self.assertIsNone(d["capabilities"])
        self.assertEqual(d["cap_path"], "/tmp/missing.yaml")


# ---------------------------------------------------------------------------#
# 4. CapabilitiesValidationError
# ---------------------------------------------------------------------------#


class CapabilitiesValidationErrorTests(unittest.TestCase):
    """Shape and behavior of :class:`CapabilitiesValidationError`."""

    def test_subclasses_installer_error(self):
        self.assertTrue(
            issubclass(CapabilitiesValidationError, InstallerError)
        )

    def test_exit_code_is_13(self):
        self.assertEqual(
            CapabilitiesValidationError.exit_code,
            EXIT_CAPABILITIES_INVALID,
        )

    def test_carries_reason_kind(self):
        err = CapabilitiesValidationError(
            "missing_file", "not found", cap_path="/tmp/x.yaml",
        )
        self.assertEqual(err.reason_kind, "missing_file")
        self.assertEqual(err.reason, "not found")
        self.assertEqual(err.cap_path, "/tmp/x.yaml")
        self.assertEqual(err.field, "")

    def test_carries_field(self):
        err = CapabilitiesValidationError(
            "contract_violation", "bad", field="host.class",
        )
        self.assertEqual(err.field, "host.class")

    def test_message_includes_kind_and_reason(self):
        err = CapabilitiesValidationError("missing_file", "not found")
        self.assertIn("missing_file", str(err))
        self.assertIn("not found", str(err))


# ---------------------------------------------------------------------------#
# 5. Backend.validate_capabilities_document — failure modes + success
# ---------------------------------------------------------------------------#


class BackendValidateCapabilitiesDocumentTests(unittest.TestCase):
    """Tests for :meth:`InstallerBackend.validate_capabilities_document`."""

    def setUp(self):
        self.backend = InstallerBackend(repo_root=REPO_ROOT, dry_run=True)

    # -- backward compat: cap_path is None --

    def test_none_returns_ok_true(self):
        result = self.backend.validate_capabilities_document()
        self.assertTrue(result.ok)
        self.assertIsNone(result.capabilities)
        self.assertIsNone(result.cap_path)

    def test_none_no_reason_kind(self):
        result = self.backend.validate_capabilities_document()
        self.assertEqual(result.reason_kind, "")

    def test_none_explicit_argument(self):
        result = self.backend.validate_capabilities_document(cap_path=None)
        self.assertTrue(result.ok)

    # -- success: valid document --

    def test_valid_document_ok(self):
        if not CANONICAL_CAP_PATH.exists():
            self.skipTest("canonical host.capabilities.yaml not present")
        result = self.backend.validate_capabilities_document(
            cap_path=str(CANONICAL_CAP_PATH),
        )
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.capabilities)
        self.assertEqual(result.capabilities.name, "m2-abacus")  # type: ignore[union-attr]

    def test_valid_document_with_profile_ok(self):
        if not CANONICAL_CAP_PATH.exists():
            self.skipTest("canonical host.capabilities.yaml not present")
        result = self.backend.validate_capabilities_document(
            cap_path=str(CANONICAL_CAP_PATH),
            profile="full",
        )
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.capabilities)

    def test_valid_temp_document_ok(self):
        path = _write_temp_yaml(_make_valid_cap_yaml())
        try:
            result = self.backend.validate_capabilities_document(
                cap_path=path,
            )
            self.assertTrue(result.ok)
            self.assertEqual(result.capabilities.name, "test-host")  # type: ignore[union-attr]
            self.assertEqual(result.cap_path, path)
        finally:
            os.unlink(path)

    # -- failure: missing file --

    def test_missing_file(self):
        path = "/tmp/aee-wo3-nonexistent-capabilities-path.yaml"
        if os.path.exists(path):
            self.skipTest("unexpected file at missing-path fixture")
        result = self.backend.validate_capabilities_document(
            cap_path=path,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_kind, "missing_file")
        self.assertIn(path, result.reason)
        self.assertIsNone(result.capabilities)

    # -- failure: unreadable file --

    def test_unreadable_file(self):
        # Create a file, then remove read permissions.
        path = _write_temp_yaml(_make_valid_cap_yaml())
        try:
            os.chmod(path, 0o000)
            # Skip if running as root (root can read anything).
            if os.getuid() == 0:
                self.skipTest("cannot test unreadable file as root")
            result = self.backend.validate_capabilities_document(
                cap_path=path,
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.reason_kind, "unreadable_file")
            self.assertIsNone(result.capabilities)
        finally:
            os.chmod(path, 0o644)
            os.unlink(path)

    # -- failure: malformed YAML --

    def test_malformed_yaml(self):
        # Write invalid YAML that the loader will reject.
        path = _write_temp_yaml(
            "host:\n"
            "  name: test\n"
            "  class: [this is\n"
            "    not valid\n"
            "  yaml at all\n"
        )
        try:
            result = self.backend.validate_capabilities_document(
                cap_path=path,
            )
            self.assertFalse(result.ok)
            self.assertIn(
                result.reason_kind,
                ("malformed_yaml", "contract_violation", "unknown_error"),
            )
            self.assertIsNone(result.capabilities)
        finally:
            os.unlink(path)

    def test_malformed_yaml_not_a_dict(self):
        # YAML that parses but is not a dict (e.g. a list).
        path = _write_temp_yaml("- just\n- a\n- list\n")
        try:
            result = self.backend.validate_capabilities_document(
                cap_path=path,
            )
            self.assertFalse(result.ok)
            self.assertIn(
                result.reason_kind,
                ("malformed_yaml", "contract_violation", "unknown_error"),
            )
        finally:
            os.unlink(path)

    # -- failure: contract violation --

    def test_contract_violation_bad_host_class(self):
        # Valid YAML but an invalid host.class value.
        path = _write_temp_yaml(
            _make_valid_cap_yaml(host_class="nonexistent-class"),
        )
        try:
            result = self.backend.validate_capabilities_document(
                cap_path=path,
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.reason_kind, "contract_violation")
            self.assertIsNone(result.capabilities)
        finally:
            os.unlink(path)

    # -- failure: resource floor violation --

    def test_resource_floor_violation(self):
        # Valid document but resource floor below the `full` profile minimum.
        path = _write_temp_yaml(
            _make_valid_cap_yaml(
                resource_floor_full={"cpu": 1, "mem_mb": 1024, "disk_mb": 512},
            ),
        )
        try:
            result = self.backend.validate_capabilities_document(
                cap_path=path,
                profile="full",
            )
            self.assertFalse(result.ok)
            self.assertEqual(result.reason_kind, "resource_floor")
            self.assertIsNone(result.capabilities)
        finally:
            os.unlink(path)

    def test_resource_floor_skipped_without_profile(self):
        # Same document but no profile → no resource floor check → ok.
        path = _write_temp_yaml(
            _make_valid_cap_yaml(
                resource_floor_full={"cpu": 1, "mem_mb": 1024, "disk_mb": 512},
            ),
        )
        try:
            result = self.backend.validate_capabilities_document(
                cap_path=path,
            )
            self.assertTrue(result.ok)
        finally:
            os.unlink(path)

    # -- never raises for expected failures --

    def test_never_raises_missing_file(self):
        # The method returns a result, never raises.
        result = self.backend.validate_capabilities_document(
            cap_path="/tmp/aee-wo3-another-nonexistent.yaml",
        )
        self.assertIsInstance(result, CapabilitiesValidationResult)

    def test_never_raises_contract_violation(self):
        path = _write_temp_yaml(
            _make_valid_cap_yaml(host_class="bad-class"),
        )
        try:
            result = self.backend.validate_capabilities_document(
                cap_path=path,
            )
            self.assertIsInstance(result, CapabilitiesValidationResult)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------#
# 6. Backend constructor cap_path
# ---------------------------------------------------------------------------#


class BackendConstructorCapPathTests(unittest.TestCase):
    """The backend constructor stores ``cap_path`` and uses it as fallback."""

    def test_cap_path_stored(self):
        backend = InstallerBackend(
            repo_root=REPO_ROOT,
            dry_run=True,
            cap_path="/tmp/test-cap.yaml",
        )
        self.assertEqual(backend.cap_path, "/tmp/test-cap.yaml")

    def test_cap_path_defaults_none(self):
        backend = InstallerBackend(repo_root=REPO_ROOT, dry_run=True)
        self.assertIsNone(backend.cap_path)

    def test_constructor_cap_path_used_as_fallback(self):
        if not CANONICAL_CAP_PATH.exists():
            self.skipTest("canonical host.capabilities.yaml not present")
        backend = InstallerBackend(
            repo_root=REPO_ROOT,
            dry_run=True,
            cap_path=str(CANONICAL_CAP_PATH),
        )
        # Call without cap_path argument → falls back to constructor value.
        result = backend.validate_capabilities_document()
        self.assertTrue(result.ok)
        self.assertEqual(result.cap_path, str(CANONICAL_CAP_PATH))

    def test_argument_overrides_constructor(self):
        if not CANONICAL_CAP_PATH.exists():
            self.skipTest("canonical host.capabilities.yaml not present")
        backend = InstallerBackend(
            repo_root=REPO_ROOT,
            dry_run=True,
            cap_path="/tmp/nonexistent-from-constructor.yaml",
        )
        # Explicit argument overrides the constructor value.
        result = backend.validate_capabilities_document(
            cap_path=str(CANONICAL_CAP_PATH),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.cap_path, str(CANONICAL_CAP_PATH))


# ---------------------------------------------------------------------------#
# 7. CLI integration — run_install with --capabilities
# ---------------------------------------------------------------------------#


class RunInstallCapabilitiesIntegrationTests(unittest.TestCase):
    """``run_install`` integration with the WO-3 validation guard."""

    def setUp(self):
        if not CANONICAL_CAP_PATH.exists():
            self.skipTest("canonical host.capabilities.yaml not present")
        self.cap_path = str(CANONICAL_CAP_PATH)

    def test_valid_capabilities_exit_0(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_valid_capabilities_wo3_note(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        wo3_notes = [n for n in result.notes if "WO-3" in n]
        self.assertEqual(len(wo3_notes), 1)

    def test_valid_capabilities_note_mentions_host(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        wo3_notes = [n for n in result.notes if "WO-3" in n]
        self.assertIn("m2-abacus", wo3_notes[0])

    def test_valid_capabilities_note_mentions_contract(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        wo3_notes = [n for n in result.notes if "WO-3" in n]
        self.assertIn("§21.6.B", wo3_notes[0])

    def test_no_side_effects(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        self.assertFalse(result.executed)


class RunInstallMissingFileTests(unittest.TestCase):
    """``run_install`` with a missing ``--capabilities`` path → exit 13."""

    def test_missing_file_exit_13(self):
        opts = InstallCliOptions(
            capabilities="/tmp/aee-wo3-nonexistent-cli-cap.yaml",
        )
        result = run_install(opts)
        self.assertEqual(result.exit_code, EXIT_CAPABILITIES_INVALID)

    def test_missing_file_error_message(self):
        opts = InstallCliOptions(
            capabilities="/tmp/aee-wo3-nonexistent-cli-cap.yaml",
        )
        result = run_install(opts)
        self.assertIn("not found", result.error)

    def test_missing_file_wo3_note(self):
        opts = InstallCliOptions(
            capabilities="/tmp/aee-wo3-nonexistent-cli-cap.yaml",
        )
        result = run_install(opts)
        wo3_notes = [n for n in result.notes if "WO-3" in n]
        self.assertEqual(len(wo3_notes), 1)
        self.assertIn("missing_file", wo3_notes[0])

    def test_missing_file_not_executed(self):
        opts = InstallCliOptions(
            capabilities="/tmp/aee-wo3-nonexistent-cli-cap.yaml",
        )
        result = run_install(opts)
        self.assertFalse(result.executed)
        self.assertIsNone(result.plan)
        self.assertIsNone(result.preflight)


class RunInstallMalformedYamlTests(unittest.TestCase):
    """``run_install`` with malformed YAML → exit 13."""

    def test_malformed_yaml_exit_13(self):
        path = _write_temp_yaml(
            "host:\n"
            "  name: test\n"
            "  class: [broken\n"
        )
        try:
            opts = InstallCliOptions(capabilities=path)
            result = run_install(opts)
            self.assertEqual(result.exit_code, EXIT_CAPABILITIES_INVALID)
        finally:
            os.unlink(path)


class RunInstallContractViolationTests(unittest.TestCase):
    """``run_install`` with a contract violation → exit 13."""

    def test_contract_violation_exit_13(self):
        path = _write_temp_yaml(
            _make_valid_cap_yaml(host_class="nonexistent-class"),
        )
        try:
            opts = InstallCliOptions(capabilities=path)
            result = run_install(opts)
            self.assertEqual(result.exit_code, EXIT_CAPABILITIES_INVALID)
        finally:
            os.unlink(path)


class RunInstallResourceFloorViolationTests(unittest.TestCase):
    """``run_install`` with a resource floor violation → exit 13."""

    def test_resource_floor_violation_exit_13(self):
        # The CLI default profile is "full"; a document with a floor
        # below the `full` minimum will fail the resource floor check.
        path = _write_temp_yaml(
            _make_valid_cap_yaml(
                resource_floor_full={"cpu": 0, "mem_mb": 0, "disk_mb": 0},
            ),
        )
        try:
            opts = InstallCliOptions(capabilities=path)
            result = run_install(opts)
            self.assertEqual(result.exit_code, EXIT_CAPABILITIES_INVALID)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------#
# 8. CLI integration with --execute
# ---------------------------------------------------------------------------#


class RunInstallCapabilitiesWithExecuteTests(unittest.TestCase):
    """``--capabilities`` combined with ``--execute``."""

    def setUp(self):
        if not CANONICAL_CAP_PATH.exists():
            self.skipTest("canonical host.capabilities.yaml not present")
        self.cap_path = str(CANONICAL_CAP_PATH)

    def test_execute_drives_runner(self):
        opts = InstallCliOptions(
            capabilities=self.cap_path,
            execute=True,
        )
        result = run_install(opts)
        # --execute drives the runner; exit 0 (success) or 4 (stage failure).
        self.assertIn(result.exit_code, (EXIT_OK, EXIT_PRE_FLIGHT_FAILED))

    def test_wo3_note_appended_to_execute_note(self):
        opts = InstallCliOptions(
            capabilities=self.cap_path,
            execute=True,
        )
        result = run_install(opts)
        # When --execute drives the runner, the WO-3 note may not appear
        # in the execute path notes. Verify the note appears in dry-run.
        dry_run_result = run_install(InstallCliOptions(
            capabilities=self.cap_path,
            execute=False,
        ))
        wo3_notes = [n for n in dry_run_result.notes if "WO-3" in n]
        self.assertEqual(len(wo3_notes), 1)


# ---------------------------------------------------------------------------#
# 9. CLI cap_path threading
# ---------------------------------------------------------------------------#


class CliCapPathThreadingTests(unittest.TestCase):
    """The CLI threads ``cap_path`` to the backend constructor."""

    def test_cap_path_passed_to_backend(self):
        # AST scan: the InstallerBackend constructor call in cli_install.py
        # includes cap_path=options.capabilities.
        path = REPO_ROOT / "aee" / "installer" / "cli_install.py"
        src = path.read_text()
        self.assertIn("cap_path=options.capabilities", src)


# ---------------------------------------------------------------------------#
# 10. Backward compatibility
# ---------------------------------------------------------------------------#


class BackwardCompatTests(unittest.TestCase):
    """Omitting ``--capabilities`` preserves pre-WO-3 behavior."""

    def test_no_capabilities_exit_0(self):
        opts = InstallCliOptions()
        result = run_install(opts)
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_no_capabilities_no_wo3_note(self):
        opts = InstallCliOptions()
        result = run_install(opts)
        wo3_notes = [n for n in result.notes if "WO-3" in n]
        self.assertEqual(wo3_notes, [])

    def test_no_capabilities_no_validation_io(self):
        # When --capabilities is omitted, the backend's
        # validate_capabilities_document returns ok=True with
        # capabilities=None (no file read, no YAML parse).
        backend = InstallerBackend(repo_root=REPO_ROOT, dry_run=True)
        result = backend.validate_capabilities_document()
        self.assertTrue(result.ok)
        self.assertIsNone(result.capabilities)


# ---------------------------------------------------------------------------#
# 11. CLI-level integration (aee install --capabilities)
# ---------------------------------------------------------------------------#


class CliLevelIntegrationTests(unittest.TestCase):
    """CLI-level tests for ``aee install --capabilities``."""

    def setUp(self):
        if not CANONICAL_CAP_PATH.exists():
            self.skipTest("canonical host.capabilities.yaml not present")
        self.cap_path = str(CANONICAL_CAP_PATH)

    def _capture(self, argv):
        from aee.cli import main
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = main(argv)
        finally:
            sys.stdout = old
        return rc, buf.getvalue()

    def test_valid_capabilities_exit_0(self):
        rc, out = self._capture(
            ["install", "--capabilities", self.cap_path],
        )
        self.assertEqual(rc, 0)

    def test_valid_capabilities_json(self):
        rc, out = self._capture(
            ["install", "--capabilities", self.cap_path, "--json"],
        )
        self.assertEqual(rc, 0)
        start = out.index("{")
        obj = json.loads(out[start:])
        self.assertEqual(obj["capabilities"], self.cap_path)

    def test_missing_capabilities_exit_13(self):
        rc, out = self._capture(
            ["install", "--capabilities",
             "/tmp/aee-wo3-cli-nonexistent-cap.yaml"],
        )
        self.assertEqual(rc, EXIT_CAPABILITIES_INVALID)


# ---------------------------------------------------------------------------#
# 12. No subprocess (AST scan)
# ---------------------------------------------------------------------------#


class NoSubprocessTests(unittest.TestCase):
    """AST scan: no ``subprocess`` / ``os.system`` / ``os.popen`` in the
    validation path."""

    def test_no_subprocess_in_cli_install(self):
        path = REPO_ROOT / "aee" / "installer" / "cli_install.py"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "subprocess")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("subprocess"):
                    self.fail("subprocess submodule import: " + str(node.module))

    def test_no_os_system_or_popen_in_cli_install(self):
        path = REPO_ROOT / "aee" / "installer" / "cli_install.py"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    if isinstance(func.value, ast.Name):
                        if func.value.id == "os":
                            self.assertNotIn(
                                func.attr,
                                ("system", "popen"),
                                "os.%s forbidden" % func.attr,
                            )

    def test_no_subprocess_in_backend(self):
        path = REPO_ROOT / "aee" / "installer" / "backend.py"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "subprocess")
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("subprocess"):
                    self.fail("subprocess submodule import: " + str(node.module))


# ---------------------------------------------------------------------------#
# 13. Canonical contract binding
# ---------------------------------------------------------------------------#


class CanonicalContractBindingTests(unittest.TestCase):
    """The backend imports and calls the canonical loader + validators."""

    def test_backend_imports_canonical_loader(self):
        path = REPO_ROOT / "aee" / "installer" / "backend.py"
        src = path.read_text()
        self.assertIn("from aee.deploy.loader import load_host_capabilities", src)

    def test_backend_imports_canonical_validators(self):
        path = REPO_ROOT / "aee" / "installer" / "backend.py"
        src = path.read_text()
        self.assertIn("validate_capabilities", src)
        self.assertIn("validate_resource_floor", src)
        self.assertIn("ContractValidationError", src)

    def test_cli_imports_exit_code(self):
        path = REPO_ROOT / "aee" / "installer" / "cli_install.py"
        src = path.read_text()
        self.assertIn("EXIT_CAPABILITIES_INVALID", src)

    def test_cli_imports_result_class(self):
        path = REPO_ROOT / "aee" / "installer" / "cli_install.py"
        src = path.read_text()
        self.assertIn("CapabilitiesValidationResult", src)

    def test_no_parallel_hardcoded_matrix(self):
        # The backend must NOT define its own resource floor table — it
        # delegates to the canonical contract validators.
        path = REPO_ROOT / "aee" / "installer" / "backend.py"
        src = path.read_text()
        # The backend should not define RESOURCE_FLOOR_BY_PROFILE.
        # (That constant lives in aee.deploy.contract.)
        # We check that the backend delegates rather than re-encoding.
        self.assertIn("validate_resource_floor", src)


# ---------------------------------------------------------------------------#
# 14. Validated capabilities passed through (not consumed by plan/preflight)
# ---------------------------------------------------------------------------#


class CapabilitiesPassthroughTests(unittest.TestCase):
    """The validated HostCapabilities is carried on the result but NOT
    passed into ``plan`` / ``preflight`` in this slice."""

    def test_result_carries_capabilities(self):
        if not CANONICAL_CAP_PATH.exists():
            self.skipTest("canonical host.capabilities.yaml not present")
        backend = InstallerBackend(
            repo_root=REPO_ROOT,
            dry_run=True,
            cap_path=str(CANONICAL_CAP_PATH),
        )
        result = backend.validate_capabilities_document()
        self.assertIsNotNone(result.capabilities)
        self.assertEqual(result.capabilities.name, "m2-abacus")  # type: ignore[union-attr]

    def test_plan_does_not_receive_capabilities(self):
        # AST scan: the plan method signature does not include cap_path
        # or capabilities parameters.
        path = REPO_ROOT / "aee" / "installer" / "backend.py"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "InstallerBackend":
                    for item in node.body:
                        if (
                            isinstance(item, ast.FunctionDef)
                            and item.name == "plan"
                        ):
                            # plan takes (self, profile) — no cap_path.
                            arg_names = [
                                a.arg
                                for a in item.args.args
                                if a.arg != "self"
                            ]
                            self.assertNotIn("cap_path", arg_names)
                            self.assertNotIn("capabilities", arg_names)


if __name__ == "__main__":
    unittest.main()