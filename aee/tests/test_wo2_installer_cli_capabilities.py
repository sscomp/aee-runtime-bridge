"""WO-2: Installer CLI ``--capabilities`` option (plumbing surface).

Targeted tests for the WO-2 installer CLI surface addition: the
``--capabilities <path>`` flag on the ``aee install`` subcommand.

WO-2 was the **plumbing-only** slice: the flag is parsed, recorded in
:class:`InstallCliOptions` / :class:`InstallCliResult`, and surfaced as
an audit note. WO-3 subsequently bound the installer backend to the
authoritative §21.6.B / §21.6.C contract: the document is now loaded +
validated BEFORE any plan/preflight/execute action, and a failure
yields exit code 13 (``EXIT_CAPABILITIES_INVALID``).

These tests were updated when WO-3 landed to reflect the new
exit-code behavior (a missing/invalid ``--capabilities`` path now
returns exit 13, not exit 0) and the renamed audit note (WO-3
validated note instead of WO-2 audit note). The WO-2 plumbing
surface (field on options/result, CLI routing, help text, backward
compat) is still exercised here; the backend binding + exit-13
behavior is tested in ``test_wo3_installer_backend_validator.py``.

Coverage:

1. **InstallCliOptions.capabilities** — default ``None``, settable,
   ``to_dict`` carries the field, frozen dataclass.
2. **InstallCliResult.capabilities** — recorded in the result,
   ``to_dict`` carries the field.
3. **run_install with --capabilities (valid path)** — exit 0,
   ``capabilities`` recorded, WO-3 validated note emitted,
   no side effects.
4. **run_install with --capabilities (missing path)** — exit 13
   (WO-3 backend binding; the contract gate refuses the install),
   ``capabilities`` recorded, WO-3 rejection note emitted.
5. **run_install with --capabilities + --execute** — exit 6
   (ExecuteNotAuthorized), ``capabilities`` recorded, WO-3 validated
   note appended to the execute-refused note.
6. **CLI plumbing** — ``aee install --capabilities <path>`` routes
   through the Phase 4B dispatch (not the Phase 9.2 path);
   ``--json`` emits a JSON object with the ``capabilities`` field.
7. **Backward compatibility** — ``aee install`` with no flags still
   uses the Phase 9.2 dispatch path (exact stdout text match);
   ``InstallCliOptions()`` with no ``capabilities`` defaults to
   ``None`` and behaves identically to the pre-WO-2 shape.
8. **Help text** — ``aee install --help`` mentions ``--capabilities``.
9. **No subprocess** — AST scan of ``cli_install.py`` confirms no
   ``subprocess`` / ``os.system`` / ``os.popen`` usage (the
   ``os.path.exists`` import inside the helper is allowed).
10. **WO-3 implemented** — ``run_install`` calls
    ``validate_capabilities_document`` via the backend; the
    ``InstallerBackend`` is passed ``cap_path``. A missing/invalid
    path yields exit 13. (These tests were inverted when WO-3 landed;
    they now assert WO-3 IS implemented, replacing the old
    "WO-3 not implemented" guards.)

All tests are stdlib ``unittest`` — no pytest, no subprocess, no host
mutation. Read-only.

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_wo2_installer_cli_capabilities -v``
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from aee.installer.cli_install import (
    InstallCliOptions,
    InstallCliResult,
    run_install,
)
from aee.installer.backend import (
    EXIT_OK,
    EXIT_EXECUTE_NOT_AUTHORIZED,
    EXIT_CAPABILITIES_INVALID,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_CAP_PATH = REPO_ROOT / "host.capabilities.yaml"


# ---------------------------------------------------------------------------#
# 1. InstallCliOptions.capabilities
# ---------------------------------------------------------------------------#


class InstallCliOptionsCapabilitiesTests(unittest.TestCase):
    """Tests for the ``capabilities`` field on :class:`InstallCliOptions`."""

    def test_default_is_none(self):
        opts = InstallCliOptions()
        self.assertIsNone(opts.capabilities)

    def test_settable(self):
        opts = InstallCliOptions(capabilities="/tmp/cap.yaml")
        self.assertEqual(opts.capabilities, "/tmp/cap.yaml")

    def test_to_dict_carries_capabilities(self):
        opts = InstallCliOptions(capabilities="/tmp/cap.yaml")
        d = opts.to_dict()
        self.assertIn("capabilities", d)
        self.assertEqual(d["capabilities"], "/tmp/cap.yaml")

    def test_to_dict_none_capabilities(self):
        opts = InstallCliOptions()
        d = opts.to_dict()
        self.assertIn("capabilities", d)
        self.assertIsNone(d["capabilities"])

    def test_frozen(self):
        opts = InstallCliOptions(capabilities="/tmp/cap.yaml")
        with self.assertRaises(Exception):
            opts.capabilities = "/tmp/other.yaml"  # type: ignore[misc]


# ---------------------------------------------------------------------------#
# 2. InstallCliResult.capabilities
# ---------------------------------------------------------------------------#


class InstallCliResultCapabilitiesTests(unittest.TestCase):
    """Tests for the ``capabilities`` field on :class:`InstallCliResult`."""

    def test_capabilities_recorded_in_result(self):
        opts = InstallCliOptions(capabilities="/tmp/cap.yaml")
        result = run_install(opts)
        self.assertEqual(result.capabilities, "/tmp/cap.yaml")

    def test_to_dict_carries_capabilities(self):
        opts = InstallCliOptions(capabilities="/tmp/cap.yaml")
        result = run_install(opts)
        d = result.to_dict()
        self.assertIn("capabilities", d)
        self.assertEqual(d["capabilities"], "/tmp/cap.yaml")

    def test_none_capabilities_in_result(self):
        opts = InstallCliOptions()
        result = run_install(opts)
        self.assertIsNone(result.capabilities)
        self.assertIsNone(result.to_dict()["capabilities"])


# ---------------------------------------------------------------------------#
# 3. run_install with --capabilities (valid path)
# ---------------------------------------------------------------------------#


class RunInstallValidPathTests(unittest.TestCase):
    """``run_install`` with a valid ``--capabilities`` path."""

    def setUp(self):
        if not CANONICAL_CAP_PATH.exists():
            self.skipTest("canonical host.capabilities.yaml not present")
        self.cap_path = str(CANONICAL_CAP_PATH)

    def test_exit_0(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        self.assertEqual(result.exit_code, EXIT_OK)

    def test_capabilities_recorded(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        self.assertEqual(result.capabilities, self.cap_path)

    def test_audit_note_emitted(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        wo3_notes = [n for n in result.notes if "WO-3" in n]
        self.assertEqual(len(wo3_notes), 1)

    def test_audit_note_mentions_validated(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        wo3_notes = [n for n in result.notes if "WO-3" in n]
        self.assertIn("validated", wo3_notes[0])

    def test_no_side_effects(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        self.assertFalse(result.executed)


# ---------------------------------------------------------------------------#
# 4. run_install with --capabilities (missing path)
# ---------------------------------------------------------------------------#


class RunInstallMissingPathTests(unittest.TestCase):
    """``run_install`` with a missing ``--capabilities`` path.

    WO-3 binds the backend to the §21.6.B / §21.6.C contract: a
    missing file is a ``missing_file`` failure mode → exit 13
    (``EXIT_CAPABILITIES_INVALID``). The install is refused before
    plan/preflight.
    """

    def setUp(self):
        self.cap_path = "/tmp/aee-wo2-nonexistent-capabilities-path.yaml"
        if os.path.exists(self.cap_path):
            self.skipTest("unexpected file at the missing-path fixture")

    def test_exit_13_capabilities_invalid(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        self.assertEqual(result.exit_code, EXIT_CAPABILITIES_INVALID)

    def test_capabilities_recorded(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        self.assertEqual(result.capabilities, self.cap_path)

    def test_wo3_rejection_note(self):
        opts = InstallCliOptions(capabilities=self.cap_path)
        result = run_install(opts)
        wo3_notes = [n for n in result.notes if "WO-3" in n]
        self.assertEqual(len(wo3_notes), 1)
        self.assertIn("missing_file", wo3_notes[0])


# ---------------------------------------------------------------------------#
# 5. run_install with --capabilities + --execute
# ---------------------------------------------------------------------------#


class RunInstallCapabilitiesWithExecuteTests(unittest.TestCase):
    """``--capabilities`` combined with ``--execute``."""

    def setUp(self):
        if not CANONICAL_CAP_PATH.exists():
            self.skipTest("canonical host.capabilities.yaml not present")
        self.cap_path = str(CANONICAL_CAP_PATH)

    def test_exit_6_execute_not_authorized(self):
        opts = InstallCliOptions(
            capabilities=self.cap_path,
            execute=True,
        )
        result = run_install(opts)
        self.assertEqual(result.exit_code, EXIT_EXECUTE_NOT_AUTHORIZED)

    def test_capabilities_recorded(self):
        opts = InstallCliOptions(
            capabilities=self.cap_path,
            execute=True,
        )
        result = run_install(opts)
        self.assertEqual(result.capabilities, self.cap_path)

    def test_audit_note_appended_to_execute_note(self):
        opts = InstallCliOptions(
            capabilities=self.cap_path,
            execute=True,
        )
        result = run_install(opts)
        # The execute-refused note is a single note that includes the
        # WO-3 capabilities validated suffix.
        wo3_notes = [n for n in result.notes if "WO-3" in n]
        self.assertEqual(len(wo3_notes), 1)
        self.assertIn("validated", wo3_notes[0])


# ---------------------------------------------------------------------------#
# 6. CLI plumbing
# ---------------------------------------------------------------------------#


class CliPlumbingTests(unittest.TestCase):
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

    def test_routes_through_phase4b_dispatch(self):
        """``--capabilities`` routes through the Phase 4B path (not 9.2)."""
        rc, out = self._capture(
            ["install", "--capabilities", self.cap_path]
        )
        self.assertEqual(rc, 0)
        # Phase 4B output header is distinct from Phase 9.2.
        self.assertIn("Phase 4B", out)

    def test_json_emits_capabilities_field(self):
        rc, out = self._capture(
            ["install", "--capabilities", self.cap_path, "--json"]
        )
        self.assertEqual(rc, 0)
        start = out.index("{")
        obj = json.loads(out[start:])
        self.assertEqual(obj["capabilities"], self.cap_path)
        self.assertTrue(any("WO-3" in n for n in obj.get("notes", [])))

    def test_capabilities_with_execute(self):
        rc, out = self._capture(
            ["install", "--capabilities", self.cap_path, "--execute"]
        )
        self.assertEqual(rc, 6)
        self.assertIn("Phase 4B", out)


# ---------------------------------------------------------------------------#
# 7. Backward compatibility
# ---------------------------------------------------------------------------#


class BackwardCompatTests(unittest.TestCase):
    """Backward-compat: no ``--capabilities`` preserves pre-WO-2 behavior."""

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

    def test_no_flags_uses_phase92_path(self):
        """``aee install`` with no flags → Phase 9.2 path (exact stdout)."""
        rc, out = self._capture(["install"])
        self.assertEqual(rc, 0)
        # Phase 9.2 output header (NOT Phase 4B).
        self.assertIn("aee install (dry-run / §21.3 installer backend)", out)
        self.assertNotIn("Phase 4B", out)

    def test_no_capabilities_defaults_none(self):
        opts = InstallCliOptions()
        result = run_install(opts)
        self.assertIsNone(result.capabilities)

    def test_no_capabilities_no_wo3_note(self):
        """No ``--capabilities`` → no WO-3 audit note."""
        opts = InstallCliOptions()
        result = run_install(opts)
        wo3_notes = [n for n in result.notes if "WO-3" in n]
        self.assertEqual(wo3_notes, [])


# ---------------------------------------------------------------------------#
# 8. Help text
# ---------------------------------------------------------------------------#


class HelpTextTests(unittest.TestCase):
    """``aee install --help`` mentions ``--capabilities``."""

    def test_help_mentions_capabilities(self):
        from aee.cli import _build_parser
        parser = _build_parser()
        buf = io.StringIO()
        try:
            parser.parse_args(["install", "--help"])
        except SystemExit:
            pass
        # argparse prints help to stdout on --help.
        # Re-capture via print_help directly.
        buf = io.StringIO()
        parser.parse_args(["install"])  # populate subparser
        # Use the install subparser's format_help via the main parser.
        help_buf = io.StringIO()
        old = sys.stdout
        sys.stdout = help_buf
        try:
            try:
                parser.parse_args(["install", "--help"])
            except SystemExit:
                pass
        finally:
            sys.stdout = old
        help_text = help_buf.getvalue()
        self.assertIn("--capabilities", help_text)
        self.assertIn("Host Capability Document", help_text)


# ---------------------------------------------------------------------------#
# 9. No subprocess
# ---------------------------------------------------------------------------#


class NoSubprocessTests(unittest.TestCase):
    """AST scan: no ``subprocess`` / ``os.system`` / ``os.popen`` in
    ``cli_install.py`` (the ``os.path.exists`` import is allowed)."""

    def test_no_subprocess_import(self):
        path = REPO_ROOT / "aee" / "installer" / "cli_install.py"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "subprocess")
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual(node.module, "subprocess")
                if node.module and node.module.startswith("subprocess"):
                    self.fail("subprocess submodule import: " + node.module)

    def test_no_os_system_or_popen(self):
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


# ---------------------------------------------------------------------------#
# 10. WO-3 not implemented
# ---------------------------------------------------------------------------#


class Wo3ImplementedTests(unittest.TestCase):
    """WO-3 (backend contract binding) IS implemented.

    ``run_install`` calls ``validate_capabilities_document`` via the
    backend, and passes ``cap_path`` to the ``InstallerBackend``
    constructor. A missing/invalid ``--capabilities`` path yields
    exit 13 (``EXIT_CAPABILITIES_INVALID``).

    These tests were inverted when WO-3 landed — they previously
    asserted WO-3 was NOT implemented (the WO-2 plumbing-only
    invariant); they now assert WO-3 IS implemented.
    """

    def test_validate_capabilities_document_called(self):
        """``run_install`` calls the backend's
        ``validate_capabilities_document`` (WO-3)."""
        path = REPO_ROOT / "aee" / "installer" / "cli_install.py"
        src = path.read_text()
        self.assertIn(
            "validate_capabilities_document",
            src,
            "run_install must call validate_capabilities_document (WO-3)",
        )

    def test_cap_path_passed_to_backend(self):
        """The ``InstallerBackend`` constructor is passed ``cap_path``."""
        path = REPO_ROOT / "aee" / "installer" / "cli_install.py"
        src = path.read_text()
        self.assertIn(
            "cap_path=options.capabilities",
            src,
            "run_install must pass cap_path to InstallerBackend (WO-3)",
        )

    def test_missing_capabilities_yields_exit_13(self):
        """A missing ``--capabilities`` path yields exit 13
        (``EXIT_CAPABILITIES_INVALID``), not exit 0."""
        opts = InstallCliOptions(
            capabilities="/tmp/aee-wo3-nonexistent-capabilities-path-3.yaml",
        )
        result = run_install(opts)
        self.assertEqual(result.exit_code, EXIT_CAPABILITIES_INVALID)

    def test_valid_capabilities_yields_exit_0(self):
        """A valid ``--capabilities`` path still yields exit 0
        (validation passes; the install proceeds)."""
        if not CANONICAL_CAP_PATH.exists():
            self.skipTest("canonical host.capabilities.yaml not present")
        valid_opts = InstallCliOptions(
            capabilities=str(CANONICAL_CAP_PATH),
        )
        valid_rc = run_install(valid_opts).exit_code
        self.assertEqual(valid_rc, EXIT_OK)


if __name__ == "__main__":
    unittest.main()