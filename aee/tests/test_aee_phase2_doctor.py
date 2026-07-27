"""AEE Phase 2 — ``aee doctor`` targeted tests.

These tests verify the doctor module (:mod:`aee.doctor`) and the
``aee doctor`` CLI subcommand (wired in :mod:`aee.cli`). Coverage:

1. **Status vocabulary** — PASS / CAVEAT / FAIL fold correctly.
2. **CheckResult / DoctorReport DTOs** — frozen, to_dict / to_text.
3. **Python version check** — passes on the host's py3.11+.
4. **Git check** — PASS when ``.git`` present, CAVEAT when missing,
   FAIL when git binary absent.
5. **Dependencies check** — PASS when all required deps importable;
   FAIL when one is missing (verified by simulating ImportError via
   monkeypatch of ``__import__``).
6. **Config files** — PASS / CAVEAT / FAIL based on ``.env`` and
   ``requirements.lock`` presence.
7. **Env vars** — PASS / CAVEAT / FAIL based on required/optional
   presence (values never read or echoed).
8. **Directory permissions** — PASS on writable dir, FAIL on
   read-only dir (via monkeypatch of ``os.access``).
9. **Hermes connectivity** — PASS on 2xx/4xx, FAIL on connection
   error; skipped when ``network=False``.
10. **Docker** — CAVEAT when absent, PASS when present (via
    ``shutil.which`` monkeypatch).
11. **Profile validation** — PASS on known profile, FAIL on unknown.
12. **Verdict folding** — single FAIL sinks the whole report; single
    CAVEAT yields CAVEAT; all PASS yields PASS.
13. **CLI plumbing** — ``aee doctor`` parses, dispatches, returns the
    expected exit code (0 / 7 / 8) for PASS / CAVEAT / FAIL scenarios.
14. **No-secret-exposure** — env-var detail never contains the value,
    only the variable name.
15. **Machine-readable output** — ``--json`` emits valid JSON with
    the expected top-level keys.

Run: ``PYTHONPATH=. python3 -m unittest aee.tests.test_aee_phase2_doctor -v``
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

from aee import cli as aee_cli
from aee.cli import (
    EXIT_DOCTOR_CAVEATS,
    EXIT_DOCTOR_FAILED,
    EXIT_OK,
    main as aee_main,
)
from aee.doctor import (
    CheckResult,
    DoctorReport,
    DoctorRunner,
    EXIT_DOCTOR_CAVEATS as D_CAVEATS,
    EXIT_DOCTOR_FAILED as D_FAILED,
    EXIT_DOCTOR_OK as D_OK,
    OPTIONAL_ENV_VARS,
    REQUIRED_DEPS,
    REQUIRED_DIRS,
    REQUIRED_ENV_VARS,
    _check_config_files,
    _check_dependencies,
    _check_directory_permissions,
    _check_docker,
    _check_env_vars,
    _check_git,
    _check_hermes_connectivity,
    _check_platform_info,
    _check_profile,
    _check_python_version,
    _fold,
    run_doctor,
)
from aee.profiles.descriptor import DEFAULT_PROFILE, KNOWN_PROFILES


# ---------------------------------------------------------------------------#
# Helpers
# ---------------------------------------------------------------------------#


def _full_env() -> dict:
    """Build an environ mapping with all required + optional vars set."""
    env = {v: "set" for v in REQUIRED_ENV_VARS}
    # HERMES_BASE_URL must be a real URL so urllib.request.Request
    # can parse it when the connectivity check is exercised.
    env["HERMES_BASE_URL"] = "http://127.0.0.1:8642"
    env.update({v: "set" for v in OPTIONAL_ENV_VARS})
    return env


class _FakeUrllibResponse:
    """Minimal stand-in for an HTTPResponse for the connectivity check."""

    def __init__(self, code: int) -> None:
        self._code = code

    def __enter__(self) -> "_FakeUrllibResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def getcode(self) -> int:
        return self._code


# ---------------------------------------------------------------------------#
# Status fold
# ---------------------------------------------------------------------------#


class StatusFoldTests(unittest.TestCase):
    def test_pass_pass_yields_pass(self) -> None:
        self.assertEqual(_fold("PASS", "PASS"), "PASS")

    def test_pass_caveat_yields_caveat(self) -> None:
        self.assertEqual(_fold("PASS", "CAVEAT"), "CAVEAT")

    def test_pass_fail_yields_fail(self) -> None:
        self.assertEqual(_fold("PASS", "FAIL"), "FAIL")

    def test_caveat_fail_yields_fail(self) -> None:
        self.assertEqual(_fold("CAVEAT", "FAIL"), "FAIL")

    def test_fail_fail_yields_fail(self) -> None:
        self.assertEqual(_fold("FAIL", "FAIL"), "FAIL")

    def test_fold_symmetric(self) -> None:
        self.assertEqual(_fold("CAVEAT", "PASS"), _fold("PASS", "CAVEAT"))


# ---------------------------------------------------------------------------#
# DTOs
# ---------------------------------------------------------------------------#


class CheckResultTests(unittest.TestCase):
    def test_frozen(self) -> None:
        cr = CheckResult("x", "PASS", "d")
        with self.assertRaises(Exception):
            cr.name = "y"  # type: ignore[misc]

    def test_to_dict_keys(self) -> None:
        cr = CheckResult("x", "CAVEAT", "d", caveat="cv")
        d = cr.to_dict()
        self.assertEqual(
            set(d.keys()), {"name", "status", "detail", "caveat"}
        )

    def test_to_dict_caveat_defaults_empty(self) -> None:
        cr = CheckResult("x", "PASS", "d")
        self.assertEqual(cr.to_dict()["caveat"], "")


class DoctorReportTests(unittest.TestCase):
    def test_to_dict_keys(self) -> None:
        r = DoctorReport(
            verdict="PASS",
            profile="full",
            checks=(CheckResult("a", "PASS", "d"),),
            summary={"PASS": 1, "CAVEAT": 0, "FAIL": 0},
        )
        d = r.to_dict()
        self.assertEqual(set(d.keys()), {"verdict", "profile", "checks", "summary"})

    def test_to_text_contains_verdict_and_summary(self) -> None:
        r = DoctorReport(
            verdict="PASS",
            profile="full",
            checks=(CheckResult("a", "PASS", "d"),),
            summary={"PASS": 1, "CAVEAT": 0, "FAIL": 0},
        )
        txt = r.to_text()
        self.assertIn("verdict : PASS", txt)
        self.assertIn("PASS=1", txt)

    def test_to_text_lists_caveats_section_when_present(self) -> None:
        r = DoctorReport(
            verdict="CAVEAT",
            profile="full",
            checks=(
                CheckResult("a", "PASS", "d"),
                CheckResult("b", "CAVEAT", "d2", caveat="watch out"),
            ),
            summary={"PASS": 1, "CAVEAT": 1, "FAIL": 0},
        )
        txt = r.to_text()
        self.assertIn("caveats:", txt)
        self.assertIn("watch out", txt)

    def test_to_text_omits_caveats_section_when_absent(self) -> None:
        r = DoctorReport(
            verdict="PASS",
            profile="full",
            checks=(CheckResult("a", "PASS", "d"),),
            summary={"PASS": 1, "CAVEAT": 0, "FAIL": 0},
        )
        self.assertNotIn("caveats:", r.to_text())


def _make_pass_dependencies_result() -> "CheckResult":
    """Build a hermetic PASS :class:`CheckResult` for the dependencies
    check, decoupling runner/CLI integration tests from host-installed
    packages.

    The runner and CLI tests exercise verdict folding and CLI plumbing
    (exit codes, JSON shape, profile propagation). They are NOT about
    the dependencies check itself — that is covered by
    :class:`DependenciesCheckTests` hermetically via ``__import__``
    monkeypatching. Letting the real ``_check_dependencies()`` run in
    these tests would make them fail on any host missing
    ``uvicorn`` / ``pyyaml`` / etc., which is exactly the
    non-hermeticity this patch fixes.

    The returned result mirrors what ``_check_dependencies`` would
    return on a fully provisioned host (all required modules
    importable), preserving the runner's verdict-folding arithmetic.
    """
    return CheckResult(
        "required_dependencies",
        "PASS",
        "all {n} required modules importable".format(
            n=len(REQUIRED_DEPS)
        ),
    )


def _patch_dependencies_pass():
    """``patch`` context manager that forces ``_check_dependencies``
    to return a hermetic PASS inside a ``with`` block."""
    return patch(
        "aee.doctor._check_dependencies",
        return_value=_make_pass_dependencies_result(),
    )


# ---------------------------------------------------------------------------#
# Individual checks
# ---------------------------------------------------------------------------#


class PythonVersionTests(unittest.TestCase):
    def test_passes_on_host(self) -> None:
        cr = _check_python_version()
        self.assertEqual(cr.status, "PASS")
        self.assertIn("Python", cr.detail)

    def test_fails_on_old_version(self) -> None:
        with patch("aee.doctor.sys") as mock_sys:
            mock_sys.version_info = (3, 8, 0, "final", 0)
            cr = _check_python_version()
        self.assertEqual(cr.status, "FAIL")
        self.assertIn("need", cr.detail)


class GitCheckTests(unittest.TestCase):
    def test_pass_when_git_dir_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            with patch("aee.doctor.shutil.which", return_value="/usr/bin/git"):
                cr = _check_git(Path(tmp))
        self.assertEqual(cr.status, "PASS")
        self.assertIn(".git present", cr.detail)

    def test_caveat_when_git_dir_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("aee.doctor.shutil.which", return_value="/usr/bin/git"):
                cr = _check_git(Path(tmp))
        self.assertEqual(cr.status, "CAVEAT")
        self.assertIn("MISSING", cr.detail)
        self.assertTrue(cr.caveat)

    def test_fail_when_git_binary_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("aee.doctor.shutil.which", return_value=None):
                cr = _check_git(Path(tmp))
        self.assertEqual(cr.status, "FAIL")
        self.assertIn("not found", cr.detail)


def _hermetic_all_importable_import():
    """Build a fake ``__import__`` that simulates every module in
    :data:`aee.doctor.REQUIRED_DEPS` being importable, regardless of
    whether the packages are actually installed on the host.

    This isolates ``_check_dependencies`` from the host's pip state so
    the test exercises the function's PASS branch hermetically. The
    existing ``test_fails_when_one_missing`` test already proved the
    FAIL branch by monkeypatching ``__import__`` to raise
    ``ImportError`` for ``fastapi``; this helper mirrors that pattern
    but for the PASS branch.

    The returned callable delegates to the real ``__import__`` for any
    module name *not* in ``REQUIRED_DEPS`` (preserving production
    import behavior for stdlib and any other transitive imports).
    """
    import builtins

    real_import = builtins.__import__
    required_mod_names = {mod for mod, _pkg in REQUIRED_DEPS}

    def fake_import(name: str, *args: object, **kwargs: object):
        # ``name`` may be a dotted submodule (e.g. ``uvicorn``); we only
        # intercept the top-level package names listed in REQUIRED_DEPS.
        top = name.split(".", 1)[0]
        if top in required_mod_names:
            # Return a lightweight stand-in module stub so ``__import__``
            # succeeds without touching the real import system. We do
            # not need a real module object — _check_dependencies only
            # cares that ImportError is NOT raised.
            return real_import("os", *args, **kwargs)  # any stdlib module
        return real_import(name, *args, **kwargs)

    return fake_import


class DependenciesCheckTests(unittest.TestCase):
    def test_passes_when_all_importable(self) -> None:
        # Hermetic: simulate all REQUIRED_DEPS being importable so the
        # test does not depend on host-installed packages. The previous
        # implementation called ``_check_dependencies()`` directly, which
        # relied on the host having ``uvicorn`` / ``pyyaml`` / etc.
        # installed — that made the test non-hermetic and it broke on
        # hosts missing any of those packages.
        with patch("builtins.__import__",
                   side_effect=_hermetic_all_importable_import()):
            cr = _check_dependencies()
        self.assertEqual(cr.status, "PASS")

    def test_fails_when_one_missing(self) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: object, **kwargs: object):
            if name == "fastapi":
                raise ImportError("simulated missing fastapi")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            cr = _check_dependencies()
        self.assertEqual(cr.status, "FAIL")
        self.assertIn("fastapi", cr.detail)

    def test_required_deps_nonempty(self) -> None:
        self.assertGreater(len(REQUIRED_DEPS), 0)


class ConfigFilesTests(unittest.TestCase):
    def test_pass_when_both_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("X=1\n")
            (Path(tmp) / "requirements.lock").write_text("fastapi\n")
            cr = _check_config_files(Path(tmp))
        self.assertEqual(cr.status, "PASS")

    def test_caveat_when_lock_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("X=1\n")
            cr = _check_config_files(Path(tmp))
        self.assertEqual(cr.status, "CAVEAT")
        self.assertIn("requirements.lock missing", cr.detail)
        self.assertTrue(cr.caveat)

    def test_fail_when_env_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "requirements.lock").write_text("fastapi\n")
            cr = _check_config_files(Path(tmp))
        self.assertEqual(cr.status, "FAIL")
        self.assertIn(".env missing", cr.detail)

    def test_fail_when_both_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cr = _check_config_files(Path(tmp))
        self.assertEqual(cr.status, "FAIL")


class EnvVarsCheckTests(unittest.TestCase):
    def test_pass_when_all_present(self) -> None:
        cr = _check_env_vars(_full_env())
        self.assertEqual(cr.status, "PASS")

    def test_caveat_when_optional_missing(self) -> None:
        env = {v: "set" for v in REQUIRED_ENV_VARS}
        cr = _check_env_vars(env)
        self.assertEqual(cr.status, "CAVEAT")
        self.assertTrue(cr.caveat)

    def test_fail_when_required_missing(self) -> None:
        env = _full_env()
        del env["HERMES_BASE_URL"]
        cr = _check_env_vars(env)
        self.assertEqual(cr.status, "FAIL")
        self.assertIn("HERMES_BASE_URL", cr.detail)

    def test_never_exposes_values(self) -> None:
        env = _full_env()
        env["HERMES_API_KEY"] = "SUPER-SECRET-TOKEN-VALUE"
        cr = _check_env_vars(env)
        # The detail must contain the variable NAME but never the value.
        self.assertNotIn("SUPER-SECRET-TOKEN-VALUE", cr.detail)
        self.assertNotIn("SUPER-SECRET-TOKEN-VALUE", cr.caveat)


class DirectoryPermissionsTests(unittest.TestCase):
    def test_pass_when_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cr = _check_directory_permissions(Path(tmp))
        self.assertEqual(cr.status, "PASS")

    def test_fail_when_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch("aee.doctor.os.access", return_value=False):
                cr = _check_directory_permissions(Path(tmp))
        self.assertEqual(cr.status, "FAIL")
        self.assertIn("not writable", cr.detail)

    def test_fail_when_cannot_create(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "aee.doctor.Path.mkdir",
                side_effect=OSError("simulated"),
            ):
                cr = _check_directory_permissions(Path(tmp))
        self.assertEqual(cr.status, "FAIL")
        self.assertIn("cannot create", cr.detail)


class HermesConnectivityTests(unittest.TestCase):
    def test_pass_on_2xx(self) -> None:
        env = {"HERMES_BASE_URL": "http://127.0.0.1:8642"}
        with patch("aee.doctor.urllib.request.urlopen") as mock:
            mock.return_value = _FakeUrllibResponse(200)
            cr = _check_hermes_connectivity(env, connect_timeout=1.0)
        self.assertEqual(cr.status, "PASS")
        self.assertIn("HTTP 200", cr.detail)

    def test_pass_on_4xx(self) -> None:
        # 4xx means upstream is reachable, just rejected the request.
        env = {"HERMES_BASE_URL": "http://127.0.0.1:8642"}
        import urllib.error as ue

        def fake_urlopen(*args: object, **kwargs: object):
            raise ue.HTTPError(
                url="http://127.0.0.1:8642/",
                code=404,
                msg="Not Found",
                hdrs=None,
                fp=None,
            )

        with patch("aee.doctor.urllib.request.urlopen", side_effect=fake_urlopen):
            cr = _check_hermes_connectivity(env, connect_timeout=1.0)
        self.assertEqual(cr.status, "PASS")
        self.assertIn("HTTP 404", cr.detail)

    def test_fail_on_connection_error(self) -> None:
        env = {"HERMES_BASE_URL": "http://127.0.0.1:8642"}
        import urllib.error as ue

        with patch(
            "aee.doctor.urllib.request.urlopen",
            side_effect=ue.URLError("refused"),
        ):
            cr = _check_hermes_connectivity(env, connect_timeout=1.0)
        self.assertEqual(cr.status, "FAIL")
        self.assertIn("cannot reach", cr.detail)

    def test_fail_when_base_url_missing(self) -> None:
        cr = _check_hermes_connectivity({}, connect_timeout=1.0)
        self.assertEqual(cr.status, "FAIL")
        self.assertIn("HERMES_BASE_URL not set", cr.detail)


class DockerCheckTests(unittest.TestCase):
    def test_pass_when_present(self) -> None:
        with patch("aee.doctor.shutil.which", return_value="/usr/bin/docker"):
            cr = _check_docker()
        self.assertEqual(cr.status, "PASS")

    def test_caveat_when_absent(self) -> None:
        with patch("aee.doctor.shutil.which", return_value=None):
            cr = _check_docker()
        self.assertEqual(cr.status, "CAVEAT")
        self.assertTrue(cr.caveat)


class ProfileCheckTests(unittest.TestCase):
    def test_pass_on_known_profile(self) -> None:
        for p in KNOWN_PROFILES:
            cr = _check_profile(p)
            self.assertEqual(cr.status, "PASS", p)
            self.assertIn(p, cr.detail)

    def test_fail_on_unknown_profile(self) -> None:
        cr = _check_profile("bogus")
        self.assertEqual(cr.status, "FAIL")
        self.assertIn("bogus", cr.detail)


class PlatformInfoTests(unittest.TestCase):
    def test_always_pass(self) -> None:
        cr = _check_platform_info()
        self.assertEqual(cr.status, "PASS")
        self.assertIn("python=", cr.detail)


# ---------------------------------------------------------------------------#
# Runner / verdict folding
# ---------------------------------------------------------------------------#


class DoctorRunnerVerdictTests(unittest.TestCase):
    def _make_runner(
        self,
        tmp: str,
        env: Optional[dict] = None,
        network: bool = False,
    ) -> DoctorRunner:
        if env is None:
            env = _full_env()
        # Create the .env and requirements.lock so config check passes.
        (Path(tmp) / ".env").write_text("X=1\n")
        (Path(tmp) / "requirements.lock").write_text("fastapi\n")
        (Path(tmp) / ".git").mkdir()
        return DoctorRunner(
            repo_root=Path(tmp),
            environ=env,
            network=network,
        )

    def test_verdict_pass_when_everything_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._make_runner(tmp)
            with patch("aee.doctor.shutil.which", return_value="/usr/bin/git"):
                with _patch_dependencies_pass():
                    report = runner.run()
        self.assertEqual(report.verdict, "PASS")
        self.assertEqual(report.summary["FAIL"], 0)
        self.assertEqual(report.summary["CAVEAT"], 0)

    def test_verdict_caveat_when_docker_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._make_runner(tmp)
            with patch(
                "aee.doctor.shutil.which",
                side_effect=lambda name: None if name == "docker" else "/usr/bin/" + name,
            ):
                with _patch_dependencies_pass():
                    report = runner.run()
        self.assertEqual(report.verdict, "CAVEAT")
        self.assertEqual(report.summary["CAVEAT"], 1)
        self.assertEqual(report.summary["FAIL"], 0)

    def test_verdict_fail_when_required_env_missing(self) -> None:
        env = _full_env()
        del env["HERMES_API_KEY"]
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._make_runner(tmp, env=env)
            with patch("aee.doctor.shutil.which", return_value="/usr/bin/git"):
                with _patch_dependencies_pass():
                    report = runner.run()
        self.assertEqual(report.verdict, "FAIL")
        self.assertGreaterEqual(report.summary["FAIL"], 1)

    def test_verdict_fail_when_unknown_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = DoctorRunner(
                repo_root=Path(tmp),
                environ=_full_env(),
                profile="bogus",
                network=False,
            )
            (Path(tmp) / ".env").write_text("X=1\n")
            (Path(tmp) / "requirements.lock").write_text("fastapi\n")
            (Path(tmp) / ".git").mkdir()
            with patch("aee.doctor.shutil.which", return_value="/usr/bin/git"):
                report = runner.run()
        self.assertEqual(report.verdict, "FAIL")

    def test_network_skipped_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._make_runner(tmp, network=False)
            report = runner.run()
        # No hermes_connectivity check in the report.
        names = [c.name for c in report.checks]
        self.assertNotIn("hermes_connectivity", names)

    def test_network_included_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._make_runner(tmp, network=True)
            with patch("aee.doctor.urllib.request.urlopen") as mock:
                mock.return_value = _FakeUrllibResponse(200)
                with _patch_dependencies_pass():
                    report = runner.run()
        names = [c.name for c in report.checks]
        self.assertIn("hermes_connectivity", names)

    def test_run_doctor_convenience(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("X=1\n")
            (Path(tmp) / "requirements.lock").write_text("fastapi\n")
            (Path(tmp) / ".git").mkdir()
            with patch("aee.doctor.shutil.which", return_value="/usr/bin/git"):
                with _patch_dependencies_pass():
                    report = run_doctor(
                        repo_root=Path(tmp),
                        environ=_full_env(),
                        network=False,
                    )
        self.assertEqual(report.verdict, "PASS")


# ---------------------------------------------------------------------------#
# CLI plumbing
# ---------------------------------------------------------------------------#


class CliDoctorTests(unittest.TestCase):
    def _run_doctor_cli(self, argv: list) -> tuple:
        """Capture ``aee doctor`` stdout/stderr and exit code."""
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with patch("sys.stdout", buf_out), patch("sys.stderr", buf_err):
            rc = aee_main(argv)
        return rc, buf_out.getvalue(), buf_err.getvalue()

    def test_doctor_help_lists_subcommand(self) -> None:
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            with self.assertRaises(SystemExit):
                aee_main(["--help"])
        self.assertIn("doctor", buf.getvalue())

    def test_doctor_subcommand_returns_zero_on_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("X=1\n")
            (Path(tmp) / "requirements.lock").write_text("fastapi\n")
            (Path(tmp) / ".git").mkdir()
            with patch("aee.doctor.shutil.which", return_value="/usr/bin/git"):
                with patch("aee.doctor.os.environ", _full_env()):
                    with _patch_dependencies_pass():
                        rc, out, err = self._run_doctor_cli(
                            ["doctor", "--no-network", "--repo-root", tmp]
                        )
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("verdict : PASS", out)

    def test_doctor_returns_7_on_caveat(self) -> None:
        # Force a CAVEAT by removing Docker from PATH.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("X=1\n")
            (Path(tmp) / "requirements.lock").write_text("fastapi\n")
            (Path(tmp) / ".git").mkdir()
            with patch(
                "aee.doctor.shutil.which",
                side_effect=lambda name: None if name == "docker" else "/usr/bin/" + name,
            ):
                with patch("aee.doctor.os.environ", _full_env()):
                    with _patch_dependencies_pass():
                        rc, out, err = self._run_doctor_cli(
                            ["doctor", "--no-network", "--repo-root", tmp]
                        )
        self.assertEqual(rc, EXIT_DOCTOR_CAVEATS)
        self.assertIn("verdict : CAVEAT", out)

    def test_doctor_returns_8_on_fail(self) -> None:
        # Force a FAIL by dropping a required env var.
        env = _full_env()
        del env["HERMES_API_KEY"]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("X=1\n")
            (Path(tmp) / "requirements.lock").write_text("fastapi\n")
            (Path(tmp) / ".git").mkdir()
            with patch("aee.doctor.shutil.which", return_value="/usr/bin/git"):
                with patch("aee.doctor.os.environ", env):
                    with _patch_dependencies_pass():
                        rc, out, err = self._run_doctor_cli(
                            ["doctor", "--no-network", "--repo-root", tmp]
                        )
        self.assertEqual(rc, EXIT_DOCTOR_FAILED)
        self.assertIn("verdict : FAIL", out)

    def test_doctor_json_emits_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("X=1\n")
            (Path(tmp) / "requirements.lock").write_text("fastapi\n")
            (Path(tmp) / ".git").mkdir()
            with patch("aee.doctor.shutil.which", return_value="/usr/bin/git"):
                with patch("aee.doctor.os.environ", _full_env()):
                    with _patch_dependencies_pass():
                        rc, out, err = self._run_doctor_cli(
                            ["doctor", "--json", "--no-network", "--repo-root", tmp]
                        )
        self.assertEqual(rc, EXIT_OK)
        payload = json.loads(out)
        for key in ("verdict", "profile", "checks", "summary"):
            self.assertIn(key, payload)
        self.assertEqual(payload["verdict"], "PASS")
        self.assertIsInstance(payload["checks"], list)
        self.assertIsInstance(payload["summary"], dict)

    def test_doctor_profile_flag_propagated(self) -> None:
        # ``aee --profile mini doctor`` should report profile=mini.
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".env").write_text("X=1\n")
            (Path(tmp) / "requirements.lock").write_text("fastapi\n")
            (Path(tmp) / ".git").mkdir()
            with patch("aee.doctor.shutil.which", return_value="/usr/bin/git"):
                with patch("aee.doctor.os.environ", _full_env()):
                    with _patch_dependencies_pass():
                        rc, out, err = self._run_doctor_cli(
                            ["--profile", "mini", "doctor", "--no-network", "--repo-root", tmp]
                        )
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("profile : mini", out)

    def test_doctor_unknown_profile_returns_fail(self) -> None:
        # Unknown profile is rejected by argparse choices → exit 2.
        with self.assertRaises(SystemExit) as ctx:
            aee_main(["--profile", "bogus", "doctor", "--no-network"])
        self.assertEqual(ctx.exception.code, 2)

    def test_doctor_exit_code_constants_distinct(self) -> None:
        # Doctor exit codes must not collide with the installer's.
        installer_codes = {0, 2, 3, 4, 5, 6}
        doctor_codes = {D_OK, D_CAVEATS, D_FAILED}
        # Only EXIT_OK (0) is shared (success). The two non-zero doctor
        # codes must be distinct and outside the installer's set.
        self.assertEqual(D_OK, 0)
        self.assertNotIn(D_CAVEATS, installer_codes)
        self.assertNotIn(D_FAILED, installer_codes)
        self.assertNotEqual(D_CAVEATS, D_FAILED)


# ---------------------------------------------------------------------------#
# Backward compat — install subcommand still works
# ---------------------------------------------------------------------------#


class BackwardCompatTests(unittest.TestCase):
    def test_install_subcommand_still_dispatches(self) -> None:
        # The doctor subcommand must not break ``aee install``.
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            rc = aee_main(["install", "--dry-run"])
        self.assertEqual(rc, EXIT_OK)
        self.assertIn("aee install", buf.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()