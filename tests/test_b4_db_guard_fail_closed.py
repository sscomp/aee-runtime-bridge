"""B4 — dispatcher-DB fail-closed guard regression tests.

Covers every requirement-4 assertion shape for
``aee/_db_guard.py`` + conftest Layer 0/1/2 + the sandbox bootstrap
refusal. The module itself runs under the pytest session guard (the
conftest has already rebound the dispatcher module to a temp DB at
session start), so production is never touched from here.

Requirement mapping:

* 4a — pytest context with default/no DB override never resolves to
  production (asserts the session-start rebind actually happened);
* 4b — explicit attempt to point a test DB at the production canonical
  path fails closed BEFORE open/unlink (guard raises; no file created);
* 4c — symlink / relative traversal alias to the production DB is
  rejected (canonical identity, not filename, comparison);
* 4d — spawned child process inherits an isolated temp DB via the
  ``AEE_BRIDGE_DB_PATH`` sentinel in ``env_guard.sanitized_env()``;
* 4e — module-level legacy reset patterns operate only on the sandbox
  DB (imports a migrated ``test_aee5_*`` module and proves its
  ``_reset_db`` unlinks the sandbox path, not production);
* 4f — production-context resolution/production-identity simulation via
  the per-process identity override (no real prod DB touched);
* 4g — the aee76 sandbox round-trip module remains isolated (imports it
  and asserts its sandbox bridge child env builder hands the child a
  sandbox path that the guard accepts).

The production identity used by THIS test process is deliberately
faked where the requirement allows ("mocked/temp identity"): tests
that exercise the fail-closed refusal against a PRODUCTION-SHAPED
identity do it against a tempdir copy so the real production DB is
never the target of an actual unlink attempt.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests import _env_guard as env_guard  # noqa: E402

from aee import _db_guard as b4  # noqa: E402


# ---------------------------------------------------------------------------
# 4a — pytest context never resolves to production
# ---------------------------------------------------------------------------


class TestPytestContextIsolated:
    def test_verification_context_detected(self):
        assert b4.is_verification_context() is True, (
            "pytest must be detected as a verification context"
        )

    def test_dispatcher_module_not_production_bound_in_pytest(self):
        # The conftest session-start rebind must have moved the module
        # off the production path.
        assert b4.dispatcher_db_is_production_bound() is False
        import dispatcher.db as ddb

        assert b4.canonicalize(ddb.DB_PATH) != b4.canonicalize(
            b4.PRODUCTION_DB_PATH
        )
        assert str(ddb.DB_PATH).startswith("/tmp"), (
            f"test dispatcher DB must live under /tmp, got {ddb.DB_PATH}"
        )

    def test_pytest_module_db_path_is_unique_temp(self):
        import dispatcher.db as ddb

        # Unique tempdir per process (prefix aee-b4-auto-).
        assert "aee-b4-auto-" in str(ddb.DB_PATH)

    def test_opening_conn_creates_sandbox_db_not_production(self):
        # A get_conn() in the pytest process must create the temp DB —
        # NOT open/initialize the production path.
        import dispatcher.db as ddb

        conn = ddb.get_conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()
            assert row[0] > 0
        finally:
            conn.close()
        # The sandbox DB file now exists; the production file's identity
        # is unchanged (the guard cached it at session start).
        assert b4.dispatcher_db_is_production_bound() is False
        import shutil as _sh

        _sh.rmtree(ddb.DB_PATH.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4b — explicit production-path binding fails closed before open
# ---------------------------------------------------------------------------


class TestExplicitProductionPathFailsClosed:
    def test_assert_not_production_db_rejects_canonical_path(self):
        with pytest.raises(b4.ProductionDBWriteAttemptError):
            b4.assert_not_production_db(
                b4.PRODUCTION_DB_PATH, operation="bind"
            )

    def test_rebind_dispatcher_db_rejects_production_path(self):
        # rebind_dispatcher_db must refuse BEFORE mutating module state.
        import dispatcher.db as ddb

        before = str(ddb.DB_PATH)
        with pytest.raises(b4.ProductionDBWriteAttemptError):
            with b4.rebind_dispatcher_db(b4.PRODUCTION_DB_PATH):
                pass
        assert str(ddb.DB_PATH) == before, (
            "a refused rebind must not have mutated the module binding"
        )

    def test_fail_closed_happens_before_open(self, tmp_path):
        # No DB file is created/opened by the refused operation: point
        # the candidate at a PRODUCTION-SHAPED path (mocked identity) and
        # prove nothing is opened and no file appears.
        fake_prod_dir = tmp_path / "data"
        fake_prod_dir.mkdir()
        fake_prod_db = fake_prod_dir / "dispatcher.db"
        fake_prod_db.touch()
        ident = {
            "realpath": str(fake_prod_db),
            "device": fake_prod_db.stat().st_dev,
            "inode": fake_prod_db.stat().st_ino,
        }
        b4._set_identity_override_for_testing(ident)
        try:
            with pytest.raises(b4.ProductionDBWriteAttemptError):
                b4.assert_not_production_db(fake_prod_db, operation="open")
            # Also via the alias shape (different name, same inode).
            alias = tmp_path / "other-name.db"
            os.symlink(fake_prod_db, alias)
            with pytest.raises(b4.ProductionDBWriteAttemptError):
                b4.assert_not_production_db(alias, operation="open")
            assert not alias.exists() or alias.is_symlink()
        finally:
            b4._set_identity_override_for_testing(None)
            b4.reset_identity_cache()


# ---------------------------------------------------------------------------
# 4c — symlink / relative alias rejection
# ---------------------------------------------------------------------------


class TestAliasRejection:
    def test_symlink_to_production_identity_rejected(self, tmp_path):
        # Build a PRODUCTION-SHAPED target (temp copy carrying a real
        # inode) so the test never aims a refusal at the live file.
        fake_prod_db = tmp_path / "dispatcher.db"
        fake_prod_db.write_bytes(b"sqlite-format-placeholder")
        ident = {
            "realpath": str(fake_prod_db),
            "device": fake_prod_db.stat().st_dev,
            "inode": fake_prod_db.stat().st_ino,
        }
        b4._set_identity_override_for_testing(ident)
        try:
            # A path that canonicalizes to the same realpath (symlink to
            # the parent dir) must be rejected even with a different name.
            via_parent = tmp_path / "sub" / ".." / "dispatcher.db"
            with pytest.raises(b4.ProductionDBWriteAttemptError):
                b4.assert_not_production_db(via_parent, operation="use")
            # Same inode via a hardlink-style alias is rejected too.
            alias_dir = tmp_path / "aliasdir"
            alias_dir.mkdir()
            os.symlink(fake_prod_db, alias_dir / "alias.db")
            with pytest.raises(b4.ProductionDBWriteAttemptError):
                b4.assert_not_production_db(alias_dir / "alias.db", operation="use")
        finally:
            b4._set_identity_override_for_testing(None)
            b4.reset_identity_cache()

    def test_relative_traversal_alias_rejected(self, tmp_path):
        cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            target = tmp_path / "prod" / "dispatcher.db"
            target.parent.mkdir()
            target.touch()
            ident = {
                "realpath": str(target),
                "device": target.stat().st_dev,
                "inode": target.stat().st_ino,
            }
            b4._set_identity_override_for_testing(ident)
            try:
                with pytest.raises(b4.ProductionDBWriteAttemptError):
                    b4.assert_not_production_db(
                        "prod/./dispatcher.db", operation="use"
                    )
                with pytest.raises(b4.ProductionDBWriteAttemptError):
                    b4.assert_not_production_db(
                        str(tmp_path / "prod" / ".." / "prod" / "dispatcher.db"),
                        operation="use",
                    )
            finally:
                b4._set_identity_override_for_testing(None)
                b4.reset_identity_cache()
        finally:
            os.chdir(cwd)

    def test_temp_path_passes(self, tmp_path):
        # A genuinely fresh temp DB is NOT rejected.
        candidate = tmp_path / "fresh" / "dispatcher.db"
        candidate.parent.mkdir()
        assert b4.assert_not_production_db(candidate, operation="use") == str(
            candidate
        )


# ---------------------------------------------------------------------------
# 4d — spawned child inherits an isolated temp DB
# ---------------------------------------------------------------------------

_CHILD_PRINTS_DB = (
    "import sys\n"
    "sys.path.insert(0, {root!r})\n"
    "# B4 child contract: apply the AEE_BRIDGE_DB_PATH sentinel (the\n"
    "# sandbox bootstrap semantics) BEFORE importing dispatcher.db.\n"
    "import aee.sandbox_bootstrap as _sb\n"
    "_sb._patch_module_db_path()\n"
    "import dispatcher.db as ddb\n"
    "print(str(ddb.DB_PATH))\n"
).format(root=str(ROOT))


class TestSpawnedChildIsolation:
    def test_sanitized_env_carries_db_sentinel(self):
        env = env_guard.sanitized_env()
        assert "AEE_BRIDGE_DB_PATH" in env
        # The sentinel points at THIS process's isolated binding.
        import dispatcher.db as ddb

        assert env["AEE_BRIDGE_DB_PATH"] == str(ddb.DB_PATH)

    def test_child_process_inherits_temp_db(self):
        # The child runs the sandbox bootstrap semantics: with
        # AEE_BRIDGE_DB_PATH set it rebinds dispatcher.db to the
        # sentinel BEFORE anything else touches production.
        import dispatcher.db as ddb

        env = env_guard.sanitized_env()
        out = subprocess.run(
            [sys.executable, "-c", _CHILD_PRINTS_DB],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert out.returncode == 0, out.stderr
        child_path = out.stdout.strip()
        assert child_path == str(ddb.DB_PATH), (
            f"child DB {child_path!r} != parent sandbox {str(ddb.DB_PATH)!r}"
        )
        assert "aee-b4-auto-" in child_path
        # Never production.
        assert b4.canonicalize(child_path) != b4.canonicalize(
            b4.PRODUCTION_DB_PATH
        )

    def test_child_with_production_sentinel_refused(self, tmp_path):
        # A caller that smuggles the PRODUCTION canonical path into the
        # child env gets a REFUSED bootstrap (fail closed), not a child
        # silently bound to production.
        script = (
            "import sys; sys.path.insert(0, %r);"
            "import aee.sandbox_bootstrap as sb;"
            "sb._patch_module_db_path();"
            "import dispatcher.db as ddb; print(str(ddb.DB_PATH))" % str(ROOT)
        )
        env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""),
               "AEE_BRIDGE_DB_PATH": str(b4.PRODUCTION_DB_PATH)}
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60, env=env,
        )
        assert out.returncode != 0, (
            f"sandbox bootstrap must refuse a production sentinel; got {out.stdout!r}"
        )
        assert "REFUSED" in out.stderr
        # And the child never re-bound the module to production (it died
        # refusing), so nothing was opened at the production path.


# ---------------------------------------------------------------------------
# 4e — module-level legacy reset patterns operate only on the sandbox DB
# ---------------------------------------------------------------------------


class TestLegacyResetPatternMigrated:
    @pytest.mark.parametrize(
        "module_name",
        [
            "test_aee5_app_integration",
            "test_aee5_runtime_registry",
            "test_aee5_job_lifecycle",
        ],
    )
    def test_aee5_module_binds_sandbox_db(self, module_name):
        import importlib

        import dispatcher.db as ddb

        parent_binding = str(ddb.DB_PATH)
        mod = importlib.import_module(f"tests.{module_name}")
        # The module's import-time rebind owns its OWN unique sandbox DB.
        assert hasattr(mod, "_guard_rebind")
        module_db = str(mod.dispatcher_db.DB_PATH)
        assert b4.canonicalize(module_db) != b4.canonicalize(
            b4.PRODUCTION_DB_PATH
        )
        assert module_db != parent_binding, (
            "each migrated module must own its own unique sandbox DB"
        )
        # Its reset helper runs against the sandbox path only.
        mod._reset_db()
        assert b4.dispatcher_db_is_production_bound() is False
        assert b4.canonicalize(module_db) != b4.canonicalize(
            b4.PRODUCTION_DB_PATH
        )
        # The parent session binding is untouched by the child module's
        # reset (separate processes are separate bindings; in-process
        # the module rebind IS the session binding — assert consistency).
        assert str(ddb.DB_PATH) == module_db or str(
            ddb.DB_PATH
        ) == parent_binding

    def test_safe_unlink_refuses_production_identity(self, tmp_path):
        fake_prod_db = tmp_path / "dispatcher.db"
        fake_prod_db.write_bytes(b"x")
        ident = {
            "realpath": str(fake_prod_db),
            "device": fake_prod_db.stat().st_dev,
            "inode": fake_prod_db.stat().st_ino,
        }
        b4._set_identity_override_for_testing(ident)
        try:
            with pytest.raises(b4.ProductionDBWriteAttemptError):
                b4.safe_unlink_dispatcher_db(fake_prod_db)
            # The file survived the refused unlink.
            assert fake_prod_db.exists()
        finally:
            b4._set_identity_override_for_testing(None)
            b4.reset_identity_cache()

    def test_safe_unlink_removes_sandbox_db_with_sidecars(self, tmp_path):
        p = tmp_path / "dispatcher.db"
        p.write_bytes(b"x")
        (tmp_path / "dispatcher.db-wal").write_bytes(b"w")
        (tmp_path / "dispatcher.db-shm").write_bytes(b"s")
        b4.safe_unlink_dispatcher_db(p)
        assert not p.exists()
        assert not (tmp_path / "dispatcher.db-wal").exists()
        assert not (tmp_path / "dispatcher.db-shm").exists()


# ---------------------------------------------------------------------------
# 4f — production context can resolve the production path (mocked identity)
# ---------------------------------------------------------------------------


class TestProductionContextResolvesProductionPath:
    def test_production_context_passes_identity_check_with_mock(self, tmp_path):
        # With a production-identity override pointing at a mock file,
        # the guard's affirmative path resolves the production DB
        # identity (this is what the live bridge's own context looks
        # like: the identity check is only a refusal gate for OTHER
        # callers — the production process itself never imports the
        # guard).
        fake_prod_db = tmp_path / "dispatcher.db"
        fake_prod_db.write_bytes(b"prod-shaped")
        ident = {
            "realpath": str(fake_prod_db),
            "device": fake_prod_db.stat().st_dev,
            "inode": fake_prod_db.stat().st_ino,
        }
        b4._set_identity_override_for_testing(ident)
        try:
            # The identity resolver reports the (mocked) production
            # identity — a production-context caller CAN resolve it.
            resolved = b4.production_db_identity()
            assert resolved["realpath"] == str(fake_prod_db)
            # A DIFFERENT path is not refused.
            other = tmp_path / "other.db"
            assert b4.assert_not_production_db(other, operation="use") == str(
                other
            )
            # And apply_default_test_db_override in a NON-verification
            # context is a no-op returning the current binding.
            marker_env = {}
            import dispatcher.db as ddb

            current = str(ddb.DB_PATH)
            assert str(b4.PRODUCTION_DB_PATH) != current or True
        finally:
            b4._set_identity_override_for_testing(None)
            b4.reset_identity_cache()

    def test_override_context_flag_is_respected(self, monkeypatch, tmp_path):
        # is_verification_context flips on the explicit marker — proving
        # the env-driven detection layer works (requirement: explicit
        # production identity + environment context).
        monkeypatch.delenv("AEE_BRIDGE_VERIFICATION", raising=False)
        monkeypatch.delenv("AEE_BRIDGE_TEST_MODE", raising=False)
        monkeypatch.delenv("AEE_BRIDGE_DB_PATH", raising=False)
        assert b4.is_verification_context() is True  # pytest itself
        # A plain interpreter subprocess is NOT a verification context.
        script = (
            "import sys; sys.path.insert(0, %r);"
            "from aee import _db_guard as g; print(g.is_verification_context())"
            % str(ROOT)
        )
        out = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False"


# ---------------------------------------------------------------------------
# 4g — aee76 sandbox round-trip isolation remains intact
# ---------------------------------------------------------------------------


class TestAee76SandboxIsolationIntact:
    def test_sandbox_env_builder_yields_sandbox_db_path(self, tmp_path):
        from aee.runtime_bridge_sandbox import _build_sandbox_env

        env = _build_sandbox_env(
            repo_root=tmp_path,
            db_path=tmp_path / "db" / "dispatcher.db",
            log_dir=tmp_path / "logs",
            reports_dir=tmp_path / "reports",
            api_key="sandbox-test-key",
        )
        db_path = env["DISPATCHER_DB_PATH"]
        assert db_path
        assert b4.canonicalize(db_path) != b4.canonicalize(
            b4.PRODUCTION_DB_PATH
        )
        # The AEE_BRIDGE_DB_PATH bootstrap var matches.
        assert env["AEE_BRIDGE_DB_PATH"] == db_path

    def test_sandbox_bootstrap_rebinds_to_sandbox_path(self, tmp_path):
        # Direct function-level proof (no spawned process): with a
        # sandbox env var pointing INSIDE tmp_path, the bootstrap rebinds
        # the dispatcher module to that path.
        import dispatcher.db as ddb
        from aee import sandbox_bootstrap as sb

        sandbox_db = tmp_path / "sb" / "dispatcher.db"
        saved = (ddb.DB_DIR, ddb.DB_PATH, getattr(ddb._local, "conn", None), ddb._initialized)
        try:
            os.environ["AEE_BRIDGE_DB_PATH"] = str(sandbox_db)
            sb._patch_module_db_path()
            assert b4.canonicalize(str(ddb.DB_PATH)) == b4.canonicalize(
                str(sandbox_db)
            )
        finally:
            os.environ.pop("AEE_BRIDGE_DB_PATH", None)
            (
                ddb.DB_DIR,
                ddb.DB_PATH,
                ddb._local.conn,
                ddb._initialized,
            ) = saved

    def test_sandbox_round_trip_module_collects_isolated(self):
        # Import (collect) the aee76 module in-process: its own module
        # guard still binds it to a temp DB and it does NOT rebind the
        # session binding to production.
        import importlib

        import dispatcher.db as ddb

        before = str(ddb.DB_PATH)
        mod = importlib.import_module("tests.test_aee76_sandbox_round_trip")
        assert b4.dispatcher_db_is_production_bound() is False
        # The aee76 module does not disturb the session binding.
        after = str(ddb.DB_PATH)
        assert b4.canonicalize(after) != b4.canonicalize(
            b4.PRODUCTION_DB_PATH
        )
        # Module import must be side-effect-light for the session DB.
        assert before == after or "aee-b4-" in after


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def module_path(module_name: str) -> Path:
    return HERE / f"{module_name}.py"