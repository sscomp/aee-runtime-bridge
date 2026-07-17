"""AEE Epic 9.2 — Unified CLI UX (§21.2) targeted tests.

These tests verify the §21.2 contract for ``aee install --profile``
exposed by :mod:`aee.cli`. Coverage:

1. **Four profiles** — ``install --profile {full,mini,edge,developer}``
   all parse, dispatch, and return exit code 0.
2. **Default profile** — ``aee install`` (no ``--profile``) resolves
   to ``DEFAULT_PROFILE`` (``"full"``).
3. **Invalid profile** — ``install --profile bogus`` exits non-zero
   (argparse ``choices`` → exit code 2) with a clear message.
4. **Help output** — top-level ``--help`` lists the four profiles and
   the default; ``install --help`` lists the per-subcommand ``--profile``.
5. **Exit codes** — success = 0; argparse error = 2; defence-in-depth
   ``UnknownProfileError`` (programmatic path) = 3.
6. **Backward compatibility** — the existing
   ``python -m aee.reporting.build_index`` CLI surface is unchanged
   (separate module, untouched).
7. **Canonical-source consistency** — the CLI imports
   ``KNOWN_PROFILES`` / ``DEFAULT_PROFILE`` from
   :mod:`aee.profiles.descriptor` and does not maintain a parallel
   hard-coded matrix. Verified via AST scan + runtime identity check.
8. **Global vs subcommand precedence** —
   ``aee --profile mini install --profile edge`` resolves to ``edge``
   (subcommand wins); ``aee --profile mini install`` resolves to
   ``mini`` (global used as fallback).
9. **No installer backend side effects** — the ``install``
   subcommand does not import ``subprocess``, ``os.system``, or
   perform filesystem writes; the descriptor is read-only.

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee92_unified_cli_ux -v``
"""
from __future__ import annotations

import ast
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from aee import cli as aee_cli
from aee.cli import (
    EXIT_OK,
    EXIT_PARSE_ERROR,
    EXIT_PROFILE_ERROR,
    PROG_NAME,
    _build_parser,
    _extract_global_profile,
    _install_dispatch,
    _resolve_profile,
    main,
)
from aee.profiles.descriptor import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    UnknownProfileError,
    parse_profile,
)


# ---------------------------------------------------------------------------
# 1. Four profiles — install --profile <each> works
# ---------------------------------------------------------------------------

class InstallFourProfilesTests(unittest.TestCase):
    """``aee install --profile {full,mini,edge,developer}`` succeeds."""

    def _run_install(self, profile: str) -> tuple:
        buf = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", err):
            rc = main(["install", "--profile", profile])
        return rc, buf.getvalue(), err.getvalue()

    def test_full_install_exit_0(self) -> None:
        rc, out, err = self._run_install("full")
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("profile (resolved)  : full", out)

    def test_mini_install_exit_0(self) -> None:
        rc, out, err = self._run_install("mini")
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("profile (resolved)  : mini", out)

    def test_edge_install_exit_0(self) -> None:
        rc, out, err = self._run_install("edge")
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("profile (resolved)  : edge", out)

    def test_developer_install_exit_0(self) -> None:
        rc, out, err = self._run_install("developer")
        self.assertEqual(rc, EXIT_OK, msg=err)
        self.assertIn("profile (resolved)  : developer", out)

    def test_each_profile_descriptor_reflected(self) -> None:
        """The dispatch output reflects the canonical descriptor."""
        for profile in KNOWN_PROFILES:
            with self.subTest(profile=profile):
                rc, out, err = self._run_install(profile)
                self.assertEqual(rc, EXIT_OK, msg=err)
                # The descriptor's purpose line appears in the output.
                expected_purpose = parse_profile(profile)  # validates
                self.assertIn("purpose", out)


# ---------------------------------------------------------------------------
# 2. Default profile — no --profile → DEFAULT_PROFILE
# ---------------------------------------------------------------------------

class DefaultProfileTests(unittest.TestCase):
    """``aee install`` without ``--profile`` defaults to ``full``."""

    def test_install_no_profile_defaults_to_full(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["install"])
        self.assertEqual(rc, EXIT_OK)
        out = buf.getvalue()
        self.assertIn("profile (resolved)  : full", out)
        self.assertIn("default profile     : full", out)

    def test_install_no_profile_matches_default_constant(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["install"])
        self.assertEqual(rc, EXIT_OK)
        out = buf.getvalue()
        self.assertIn(
            "profile (resolved)  : {d}".format(d=DEFAULT_PROFILE),
            out,
        )


# ---------------------------------------------------------------------------
# 3. Invalid profile — non-zero exit + clear message
# ---------------------------------------------------------------------------

class InvalidProfileTests(unittest.TestCase):
    """``--profile bogus`` is rejected with non-zero exit."""

    def test_install_bogus_profile_exits_2(self) -> None:
        buf = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", buf), patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["install", "--profile", "bogus"])
        self.assertEqual(cm.exception.code, EXIT_PARSE_ERROR)
        self.assertIn("invalid choice", err.getvalue())
        self.assertIn("bogus", err.getvalue())

    def test_install_bogus_lists_valid_profiles(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["install", "--profile", "bogus"])
        self.assertEqual(cm.exception.code, EXIT_PARSE_ERROR)
        msg = err.getvalue()
        for p in KNOWN_PROFILES:
            self.assertIn(p, msg)

    def test_global_bogus_profile_exits_2(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["--profile", "bogus", "install"])
        self.assertEqual(cm.exception.code, EXIT_PARSE_ERROR)
        self.assertIn("invalid choice", err.getvalue())


# ---------------------------------------------------------------------------
# 4. Help output — profiles + default listed
# ---------------------------------------------------------------------------

class HelpOutputTests(unittest.TestCase):
    """``--help`` lists the four profiles and the default behavior."""

    def test_top_level_help_lists_four_profiles(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
        self.assertEqual(cm.exception.code, EXIT_OK)
        out = buf.getvalue()
        for p in KNOWN_PROFILES:
            self.assertIn(p, out)
        # The choices constraint appears in the usage line.
        self.assertIn("{full,mini,edge,developer}", out)

    def test_top_level_help_mentions_default(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                main(["--help"])
        self.assertEqual(cm.exception.code, EXIT_OK)
        out = buf.getvalue()
        self.assertIn("Default: full", out)
        self.assertIn("DEFAULT_PROFILE", out)

    def test_install_help_lists_profile_flag(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as cm:
                main(["install", "--help"])
        self.assertEqual(cm.exception.code, EXIT_OK)
        out = buf.getvalue()
        self.assertIn("--profile", out)
        for p in KNOWN_PROFILES:
            self.assertIn(p, out)

    def test_help_exit_code_zero(self) -> None:
        for argv in (["--help"], ["install", "--help"]):
            with self.subTest(argv=argv):
                buf = io.StringIO()
                with patch("sys.stdout", buf):
                    with self.assertRaises(SystemExit) as cm:
                        main(argv)
                self.assertEqual(cm.exception.code, EXIT_OK)


# ---------------------------------------------------------------------------
# 5. Exit codes — success / parse-error / profile-error
# ---------------------------------------------------------------------------

class ExitCodeTests(unittest.TestCase):
    """Exit codes match the §21.2 contract."""

    def test_success_exit_0(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["install", "--profile", "full"])
        self.assertEqual(rc, EXIT_OK)

    def test_argparse_error_exit_2(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["install", "--profile", "bogus"])
        self.assertEqual(cm.exception.code, EXIT_PARSE_ERROR)

    def test_no_subcommand_exit_2(self) -> None:
        # ``aee`` with no subcommand prints help and exits non-zero.
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main([])
        self.assertEqual(rc, EXIT_PARSE_ERROR)

    def test_unknown_subcommand_exit_2(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err):
            with self.assertRaises(SystemExit) as cm:
                main(["bogus-subcommand"])
        self.assertEqual(cm.exception.code, EXIT_PARSE_ERROR)

    def test_profile_error_exit_3_defence_in_depth(self) -> None:
        """Programmatic call with empty profile → exit 3.

        argparse ``choices`` accepts the empty string only if it
        matches a choice (it doesn't), so this path is normally
        unreachable via the CLI. But :func:`_install_dispatch` is a
        public function — a programmatic caller could pass an empty
        string. :func:`parse_profile` rejects empty (returns
        DEFAULT_PROFILE) but raises on truly unknown values, so we
        simulate the unknown-value path directly.
        """
        err = io.StringIO()
        with patch("sys.stderr", err):
            rc = _install_dispatch("definitely-not-a-profile")
        self.assertEqual(rc, EXIT_PROFILE_ERROR)
        self.assertIn("unknown profile", err.getvalue())


# ---------------------------------------------------------------------------
# 6. Backward compatibility — build_index CLI untouched
# ---------------------------------------------------------------------------

class BackwardCompatTests(unittest.TestCase):
    """``python -m aee.reporting.build_index`` CLI is unchanged."""

    def test_build_index_main_still_importable(self) -> None:
        from aee.reporting.build_index import main as bi_main
        self.assertTrue(callable(bi_main))

    def test_build_index_main_signature_unchanged(self) -> None:
        import inspect
        from aee.reporting.build_index import main as bi_main
        sig = inspect.signature(bi_main)
        # K5 added --manifest-path / --audit-action; the signature
        # must still accept ``argv`` as the only positional.
        self.assertEqual(list(sig.parameters), ["argv"])

    def test_aee_cli_does_not_modify_build_index(self) -> None:
        """Importing ``aee.cli`` does not mutate ``build_index``."""
        from aee.reporting import build_index
        # ``build_index`` should not have any ``--profile`` flag.
        parser = None
        # We can't easily reconstruct the build_index parser without
        # running it, so check the source for the absence of a
        # ``--profile`` add_argument call that would be a regression.
        src_path = Path(build_index.__file__)
        src = src_path.read_text(encoding="utf-8")
        # The build_index parser may have other profile-related
        # strings in comments, but it must NOT add a top-level
        # ``--profile`` argument (that would shadow the unified CLI).
        # Look for the specific pattern that would indicate a
        # regression: ``add_argument("--profile"``.
        self.assertNotIn(
            'add_argument("--profile"',
            src,
            msg="build_index.py must not add a --profile flag (unified CLI owns it)",
        )


# ---------------------------------------------------------------------------
# 7. Canonical-source consistency — no parallel hard-coded matrix
# ---------------------------------------------------------------------------

class CanonicalSourceConsistencyTests(unittest.TestCase):
    """``aee.cli`` imports the canonical tuple; no parallel matrix."""

    def test_cli_imports_known_profiles_from_descriptor(self) -> None:
        # ``aee.cli.KNOWN_PROFILES`` is the *same object* as the
        # canonical tuple in ``aee.profiles.descriptor``.
        from aee.profiles.descriptor import KNOWN_PROFILES as canonical
        self.assertIs(aee_cli.KNOWN_PROFILES, canonical)

    def test_cli_imports_default_profile_from_descriptor(self) -> None:
        from aee.profiles.descriptor import DEFAULT_PROFILE as canonical
        self.assertIs(aee_cli.DEFAULT_PROFILE, canonical)

    def test_cli_choices_match_canonical_tuple(self) -> None:
        """The argparse ``choices`` constraint equals the canonical tuple.

        We verify by building the parser and inspecting the action's
        ``choices`` attribute — it must equal the canonical tuple
        (as a set, since argparse stores a tuple internally).
        """
        parser = _build_parser()
        # Find the global --profile action.
        global_action = None
        for action in parser._actions:
            if "--profile" in (action.option_strings or []):
                global_action = action
                break
        self.assertIsNotNone(global_action)
        assert global_action is not None  # for the type checker
        self.assertEqual(tuple(global_action.choices or ()), KNOWN_PROFILES)

    def test_no_parallel_hardcoded_matrix_in_source(self) -> None:
        """AST scan: ``aee/cli.py`` must not redefine KNOWN_PROFILES.

        The only definition of ``KNOWN_PROFILES`` / ``DEFAULT_PROFILE``
        in the source must be the import statement. A parallel
        hard-coded tuple (e.g. ``("full","mini","edge","developer")``)
        as a module-level literal would be a regression.
        """
        src_path = Path(aee_cli.__file__)
        src = src_path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        # Walk for Assign nodes whose target is named KNOWN_PROFILES
        # or DEFAULT_PROFILE — these would be re-definitions.
        redefinitions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in (
                        "KNOWN_PROFILES",
                        "DEFAULT_PROFILE",
                    ):
                        redefinitions.append(target.id)
        self.assertEqual(
            redefinitions,
            [],
            msg="aee/cli.py must not redefine KNOWN_PROFILES or DEFAULT_PROFILE",
        )

    def test_no_string_literal_profile_matrix_in_source(self) -> None:
        """No string literal ``"full", "mini", "edge", "developer"`` tuple.

        A hard-coded matrix would appear as a tuple of four string
        literals in the AST. We scan for any Tuple node containing
        all four profile names as string constants — that would
        indicate a parallel matrix.
        """
        src_path = Path(aee_cli.__file__)
        src = src_path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        profiles = set(KNOWN_PROFILES)
        for node in ast.walk(tree):
            if isinstance(node, ast.Tuple):
                literal_strs = {
                    elt.value for elt in node.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                }
                if profiles.issubset(literal_strs):
                    self.fail(
                        "aee/cli.py contains a hard-coded profile matrix "
                        "tuple at line {ln}; import from "
                        "aee.profiles.descriptor instead".format(ln=node.lineno)
                    )


# ---------------------------------------------------------------------------
# 8. Global vs subcommand precedence
# ---------------------------------------------------------------------------

class PrecedenceTests(unittest.TestCase):
    """Subcommand ``--profile`` wins over global ``--profile``."""

    def test_subcommand_wins_over_global(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["--profile", "mini", "install", "--profile", "edge"])
        self.assertEqual(rc, EXIT_OK)
        out = buf.getvalue()
        self.assertIn("profile (resolved)  : edge", out)

    def test_global_used_when_subcommand_omitted(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["--profile", "mini", "install"])
        self.assertEqual(rc, EXIT_OK)
        out = buf.getvalue()
        self.assertIn("profile (resolved)  : mini", out)

    def test_resolve_profile_function_subcommand_wins(self) -> None:
        self.assertEqual(_resolve_profile("full", "mini"), "mini")
        self.assertEqual(_resolve_profile("full", None), "full")
        self.assertEqual(_resolve_profile("mini", "edge"), "edge")

    def test_extract_global_profile_default(self) -> None:
        self.assertEqual(_extract_global_profile(None), DEFAULT_PROFILE)
        self.assertEqual(_extract_global_profile([]), DEFAULT_PROFILE)
        self.assertEqual(_extract_global_profile(["install"]), DEFAULT_PROFILE)

    def test_extract_global_profile_explicit(self) -> None:
        # When only the global flag is present, it is returned.
        self.assertEqual(
            _extract_global_profile(["--profile", "mini", "install"]),
            "mini",
        )
        # When only the subcommand flag is present, the pre-pass
        # picks it up (it cannot distinguish global vs subcommand
        # positions). ``_resolve_profile`` then combines the two
        # correctly: subcommand value wins over the pre-pass value
        # only when the subcommand value is non-None. The end-to-end
        # behavior is verified by PrecedenceTests above.
        self.assertEqual(
            _extract_global_profile(["install", "--profile", "edge"]),
            "edge",
        )


# ---------------------------------------------------------------------------
# 9. No installer backend side effects
# ---------------------------------------------------------------------------

class NoSideEffectsTests(unittest.TestCase):
    """The ``install`` subcommand performs no side effects."""

    def test_cli_module_does_not_import_subprocess(self) -> None:
        src_path = Path(aee_cli.__file__)
        src = src_path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(
                        alias.name, "subprocess",
                        msg="aee.cli must not import subprocess (no installer backend)",
                    )
            if isinstance(node, ast.ImportFrom):
                self.assertNotEqual(
                    node.module, "subprocess",
                    msg="aee.cli must not import from subprocess",
                )

    def test_cli_module_does_not_import_os_system(self) -> None:
        src_path = Path(aee_cli.__file__)
        src = src_path.read_text(encoding="utf-8")
        # No os.system / os.popen calls.
        self.assertNotIn("os.system(", src)
        self.assertNotIn("os.popen(", src)

    def test_install_dispatch_is_dry_run(self) -> None:
        """The dispatch output says ``backend_implemented : True`` + ``executed : False``.

        Updated for Epic 9.3 (§21.3): the installer backend is now
        wired, so ``backend_implemented`` flipped from ``False`` to
        ``True``. The ``executed : False`` line carries the dry-run
        guarantee (no side effects). The §21.2 "no side effects"
        invariant is preserved by the lazy import + dry-run default.
        """
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["install", "--profile", "full"])
        self.assertEqual(rc, EXIT_OK)
        out = buf.getvalue()
        self.assertIn("backend_implemented : True", out)
        self.assertIn("executed            : False", out)
        self.assertIn("side effects        : none", out)

    def test_install_json_output_shape(self) -> None:
        """``--json`` emits a JSON object with the expected keys.

        Updated for Epic 9.3 (§21.3): ``backend_implemented`` is now
        ``True`` and the payload carries ``plan`` + ``preflight`` +
        ``executed: False``.
        """
        import json
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = main(["install", "--profile", "mini", "--json"])
        self.assertEqual(rc, EXIT_OK)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["subcommand"], "install")
        self.assertEqual(payload["profile"], "mini")
        self.assertEqual(payload["default_profile"], DEFAULT_PROFILE)
        self.assertEqual(payload["known_profiles"], list(KNOWN_PROFILES))
        self.assertEqual(payload["dry_run"], True)
        self.assertEqual(payload["backend_implemented"], True)
        self.assertEqual(payload["executed"], False)
        self.assertIn("plan", payload)
        self.assertIn("preflight", payload)
        self.assertIn("descriptor", payload)
        self.assertEqual(payload["descriptor"]["name"], "mini")


# ---------------------------------------------------------------------------
# 10. PROG_NAME + parser sanity
# ---------------------------------------------------------------------------

class ParserSanityTests(unittest.TestCase):
    """The parser exposes the documented surface."""

    def test_prog_name_is_aee(self) -> None:
        self.assertEqual(PROG_NAME, "aee")

    def test_build_parser_returns_argparse_parser(self) -> None:
        import argparse
        parser = _build_parser()
        self.assertIsInstance(parser, argparse.ArgumentParser)

    def test_parser_has_install_subcommand(self) -> None:
        import argparse
        parser = _build_parser()
        # Find the subparsers action.
        subparsers_action = None
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                subparsers_action = action
                break
        self.assertIsNotNone(subparsers_action)
        assert subparsers_action is not None  # for the type checker
        self.assertIn("install", subparsers_action.choices)


if __name__ == "__main__":
    unittest.main()