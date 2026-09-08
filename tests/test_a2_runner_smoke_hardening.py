"""A2 runner/smoke hardening regression tests (2026-09-08).

Two deploy-verify findings (a2_b2_b3_b4_production_deploy_verify_20260908.md
caveats 1+2), fixed and pinned here:

1. ``scripts/a2_runner.py`` printed a hardcoded ``unlink_guard=True``
   banner WITHOUT calling ``install_unlink_guard()`` — in a bare runner
   process the last-ditch unlink wrapper was never armed (only pytest
   conftest Layer 0 installed it). Now the runner installs the guard
   through the SAME entry point conftest uses and the banner prints the
   guard's own ``unlink_guard_installed()`` truth.

2. ``scripts/a2_smoke.py`` called ``manager.timeout("T-A")`` without the
   required ``reason`` kwarg (dispatcher/manager.py: timeout(self,
   task_id, reason)) — the TypeError was swallowed by the smoke's own
   handler and recorded as a bare ``"TypeError"`` result string. Now the
   smoke passes the reaper-semantics reason.

Isolation discipline (mirrors tests/test_b4_db_guard_fail_closed.py):

* every DB-facing in-process test binds a TEMP dispatcher DB via the
  shared ``tests._live_db_guard`` helpers — the production DB is never
  opened/unlinked;
* subprocess tests run through the bare ``scripts/a2_runner.py`` /
  ``scripts/a2_smoke.py`` contract with ``_MINIMAL_CHILD_ENV`` (no
  pytest markers, no Telegram credentials) so the child must arm every
  guard itself;
* unlink-refusal probes aim at a PRODUCTION-SHAPED identity faked in a
  tempdir (``_set_identity_override_for_testing``), never at the real
  production file;
* no test in this module performs a live Telegram send; the live
  notification audit must gain ZERO rows across the whole module.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aee import _db_guard as b4  # noqa: E402
from tests._live_db_guard import (  # noqa: E402
    make_temp_dispatcher_db,
    point_module_to_temp_db,
)

_LIVE_AUDIT = ROOT / "logs" / "notification_audit.jsonl"

#: Bare child env: deliberately NO pytest/notification/DB markers — the
#: a2_runner contract must arm everything itself (that is the contract
#: under test).
_MINIMAL_CHILD_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
    "LANG": "C.UTF-8",
    "PYTHONPATH": str(ROOT),
}


def _live_audit_rows() -> int:
    if not _LIVE_AUDIT.exists():
        return 0
    return sum(1 for line in _LIVE_AUDIT.read_text(
        encoding="utf-8").splitlines() if line.strip())


def _insert_synthetic_task(db_path: Path, task_id: str = "T-A") -> None:
    """Raw-SQL synthetic task with an explicit never-real id (never
    TaskManager.create(): temp-DB auto-numbering can draw a REAL
    production task id — the 2026-09-08 B1b incident lesson)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, owner, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, "a2-runner-smoke-hardening (synthetic)", "ops",
             "a2", "running", "2026-09-08T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# (a) bare a2_runner REALLY installs the unlink guard (not just reports it)
# ---------------------------------------------------------------------------


_RUNNER_GUARD_STATE_CHILD = (
    "import os\n"
    "from aee._db_guard import unlink_guard_installed\n"
    "# The runner must have ARMED the wrapper, not merely claimed it:\n"
    "assert unlink_guard_installed() is True, (\n"
    "    'a2_runner did not actually install the unlink guard')\n"
    "import os as _os\n"
    "import pathlib as _pl\n"
    "assert _os.unlink.__name__ == '_guarded_os_unlink', _os.unlink\n"
    "assert _os.remove.__name__ == '_guarded_os_remove', _os.remove\n"
    "assert _pl.Path.unlink.__name__ == '_guarded_path_unlink', (\n"
    "    _pl.Path.unlink)\n"
    "print('CHILD_UNLINK_GUARD_ARMED')\n"
)


class TestBareRunnerInstallsUnlinkGuard:
    def test_banner_and_callables_actually_wrapped(self):
        """A bare ``scripts/a2_runner.py`` process must end up with
        os.unlink/os.remove/Path.unlink WRAPPED (qualname proof — not a
        boolean report) and its banner must print the guard's own truth
        (unlink_guard=True from unlink_guard_installed())."""
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "a2_runner.py"), "-"],
            input=_RUNNER_GUARD_STATE_CHILD, capture_output=True,
            text=True, timeout=120, env=dict(_MINIMAL_CHILD_ENV),
            cwd=str(ROOT),
        )
        assert "CHILD_UNLINK_GUARD_ARMED" in out.stdout, (
            f"a2_runner unlink-guard contract failed:\n"
            f"stdout={out.stdout}\nstderr={out.stderr}"
        )
        # The banner reports the guard's own installed state.
        assert "[a2_runner] DB guard armed" in out.stderr
        assert "unlink_guard=True)" in out.stderr

    def test_fresh_process_reports_uninstalled_before_runner(self):
        """Control: a plain interpreter (no runner) has the wrapper
        UNinstalled — proving the qualname assertions above detect the
        runner's installation rather than a pre-armed interpreter."""
        script = (
            "import sys; sys.path.insert(0, %r); "
            "import os as _os, pathlib as _pl; "
            "print(_os.unlink.__qualname__, "
            "_pl.Path.unlink.__qualname__)" % str(ROOT)
        )
        out = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=60, env=dict(_MINIMAL_CHILD_ENV), cwd=str(ROOT),
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.split() == ["unlink", "Path.unlink"], out.stdout


# ---------------------------------------------------------------------------
# (b) the production-DB unlink/remove alias protection WORKS in the bare
#     runner verification context (production-shaped temp identity only)
# ---------------------------------------------------------------------------


_RUNNER_ALIAS_REFUSAL_CHILD = (
    "import os, sys, tempfile\n"
    "sys.path.insert(0, {root!r})\n"
    "from aee import _db_guard as g\n"
    "assert g.unlink_guard_installed() is True\n"
    "# Production-SHAPED identity faked in a tempdir — the real\n"
    "# production DB is never the target (B4 4c/4f test discipline).\n"
    "d = tempfile.mkdtemp(prefix='a2-runner-alias-probe-')\n"
    "fake = os.path.join(d, 'dispatcher.db')\n"
    "open(fake, 'wb').close()\n"
    "g._set_identity_override_for_testing({{\n"
    "    'realpath': fake,\n"
    "    'device': os.stat(fake).st_dev,\n"
    "    'inode': os.stat(fake).st_ino,\n"
    "}})\n"
    "# The alias resolves to the production identity; the LAST-DITCH\n"
    "# process-level wrapper must refuse all three unlink shapes.\n"
    "alias = os.path.join(d, 'alias-to-prod.db')\n"
    "os.symlink(fake, alias)\n"
    "refused = []\n"
    "for op in ('os.unlink', 'os.remove', 'Path.unlink'):\n"
    "    try:\n"
    "        if op == 'os.unlink':\n"
    "            os.unlink(alias)\n"
    "        elif op == 'os.remove':\n"
    "            os.remove(alias)\n"
    "        else:\n"
    "            from pathlib import Path\n"
    "            Path(alias).unlink()\n"
    "        refused.append(op + '=NOT-REFUSED')\n"
    "    except g.ProductionDBWriteAttemptError:\n"
    "        refused.append(op + '=refused')\n"
    "assert refused == [\n"
    "    'os.unlink=refused', 'os.remove=refused',\n"
    "    'Path.unlink=refused'], refused\n"
    "# The refused unlink must not have removed the symlink alias.\n"
    "assert os.path.lexists(alias), (\n"
    "    'refused unlink must leave the alias in place')\n"
    "print('CHILD_ALIAS_REFUSED_OK')\n"
).format(root=str(ROOT))


class TestBareRunnerAliasProtectionEffective:
    def test_unlink_remove_path_unlink_all_refused_under_runner(self):
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "a2_runner.py"), "-"],
            input=_RUNNER_ALIAS_REFUSAL_CHILD, capture_output=True,
            text=True, timeout=120, env=dict(_MINIMAL_CHILD_ENV),
            cwd=str(ROOT),
        )
        assert "CHILD_ALIAS_REFUSED_OK" in out.stdout, (
            f"bare-runner alias protection failed:\n"
            f"stdout={out.stdout}\nstderr={out.stderr}"
        )


# ---------------------------------------------------------------------------
# (c) a2_smoke T-A timeout path: no TypeError, reason recorded
# ---------------------------------------------------------------------------


class TestA2SmokeTimeoutReason:
    _REASON = "smoke: no progress, reaper timeout"

    def test_timeout_transition_records_reason_in_db(self):
        """Direct manager-level proof under the pytest session guard:
        timeout(T-A, reason=...) transitions the synthetic task and the
        reason lands in BOTH tasks.error_message and the TIMEOUT
        event payload (task_events.payload_json)."""
        with make_temp_dispatcher_db(
            prefix="a2-smoke-hardening-"
        ) as db_path, point_module_to_temp_db(db_path):
            _insert_synthetic_task(db_path)
            from dispatcher.manager import TaskManager

            m = TaskManager()
            task = m.timeout("T-A", reason=self._REASON)
            assert task.status == "timeout"
            # Suppressed under the session gate: no persisted fake
            # notification bookkeeping (reviewed contract shape).
            assert (m.get_output("T-A") or {}).get(
                "notification_json") is None
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    "SELECT status, error_message FROM tasks "
                    "WHERE task_id = 'T-A'"
                ).fetchone()
                assert row[0] == "timeout"
                assert row[1] == self._REASON
                ev = conn.execute(
                    "SELECT payload_json FROM task_events "
                    "WHERE task_id = 'T-A' AND kind = 'timeout'"
                ).fetchone()
                payload = json.loads(ev[0])
                assert payload["reason"] == self._REASON
            finally:
                conn.close()

    def test_smoke_script_reports_no_type_error_and_reason_recorded(self):
        """End-to-end: the corrected scripts/a2_smoke.py exits 0 with
        timeout=transitioned (NOT TypeError), cancel=IllegalTransition
        (expected second-call shape), PASS banner, and zero live-audit
        delta."""
        live_before = _live_audit_rows()
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "a2_smoke.py")],
            capture_output=True, text=True, timeout=180,
            env=dict(_MINIMAL_CHILD_ENV), cwd=str(ROOT),
        )
        assert out.returncode == 0, (
            f"a2_smoke failed:\nstdout={out.stdout}\nstderr={out.stderr}"
        )
        assert '"timeout": "transitioned"' in out.stdout, out.stdout
        assert '"cancel": "IllegalTransition"' in out.stdout, out.stdout
        assert "TypeError" not in out.stdout
        assert "TypeError" not in out.stderr
        assert "[a2_smoke] PASS" in out.stdout
        assert _live_audit_rows() == live_before, (
            "a2_smoke must not touch the live notification audit"
        )


# ---------------------------------------------------------------------------
# (d) the runner fix must not regress the notification contract
# ---------------------------------------------------------------------------


class TestRunnerContractStillSuppressed:
    def test_runner_child_still_default_suppressed(self):
        """Re-run the B2/B3 contract shape through the hardened runner:
        with NO env, the gate must still default-arm and a suppressed
        direct notifier call must do zero network work."""
        child_code = (
            "import os\n"
            "from aee._notification_guard import notifications_disabled\n"
            "assert notifications_disabled(), 'gate must default-arm'\n"
            "from unittest import mock\n"
            "from dispatcher.notifier import _send_telegram\n"
            "with mock.patch('urllib.request.urlopen') as m:\n"
            "    assert _send_telegram('123456:ABC-fake', '000000000', "
            "'nope') is False\n"
            "    m.assert_not_called()\n"
            "print('CHILD_OK')\n"
        )
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "a2_runner.py"), "-"],
            input=child_code, capture_output=True, text=True, timeout=120,
            env=dict(_MINIMAL_CHILD_ENV), cwd=str(ROOT),
        )
        assert "CHILD_OK" in out.stdout, (
            f"a2_runner notification contract failed:\n"
            f"stdout={out.stdout}\nstderr={out.stderr}"
        )


@pytest.fixture(autouse=True)
def _audit_freeze_guard():
    """Whole-module teardown assertion: the live audit gains zero rows
    from anything in this module (stub or real)."""
    before = _live_audit_rows()
    yield
    assert _live_audit_rows() == before, (
        "notification_audit.jsonl grew during the hardening tests — "
        "live-send leak or stub pollution"
    )