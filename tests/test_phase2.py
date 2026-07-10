"""Tests for the Phase 2 modules: reaper, notifier, safety, usage.

Run:
    cd ~/hermes-runtime-bridge
    .venv/bin/python tests/test_phase2.py
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

# Use a tmp DB so we don't pollute the real one.
os.environ.setdefault("DISPATCHER_DB_PATH", "/tmp/_phase2_test_dispatcher.db")

# Make imports work.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dispatcher.db import get_conn
import dispatcher.db as _db
from dispatcher.manager import TaskManager
from dispatcher.models import (
    LEGAL_TRANSITIONS,
)


def _fresh_db():
    """Wipe + re-init the test DB so each test is independent.

    We monkeypatch the module-level DB_PATH so the singleton connection
    points at a tmp file.
    """
    import pathlib
    p = pathlib.Path("/tmp/_phase2_test_dispatcher.db")
    if p.exists():
        p.unlink()
    _db.DB_PATH = p
    _db._initialized = False
    # Force any cached thread-local connection closed.
    if hasattr(_db._local, "conn"):
        try:
            _db._local.conn.close()
        except Exception:
            pass
        delattr(_db._local, "conn")
    get_conn()  # triggers schema init


# ===========================================================================
# Reaper
# ===========================================================================


class TestReaper(unittest.TestCase):
    def setUp(self):
        _fresh_db()
        self.mgr = TaskManager()
        self.cfg = type("Cfg", (), {
            "stale_running_sec": 60,
            "stale_queued_sec": 30,
            "max_total_age_sec": 300,
            "grace_period_sec": 0,  # no grace in tests
            "enabled": True,
        })()

    def test_no_stale_when_healthy(self):
        from dispatcher.reaper import reap_once, ReaperConfig
        cfg = ReaperConfig(
            stale_running_sec=60, stale_queued_sec=30,
            max_total_age_sec=300, grace_period_sec=0, enabled=True,
        )
        # Create a fresh queued task; should be skipped.
        t = self.mgr.create(title="fresh", type="normal", input_text="hi", session_id="x")
        res = reap_once(self.mgr, cfg)
        self.assertEqual(res.scanned, 1)
        self.assertEqual(res.reaped, [])
        self.assertEqual(len(res.skipped), 1)

    def test_queued_task_reaped_after_stale_queued_sec(self):
        from dispatcher.reaper import reap_once, ReaperConfig
        cfg = ReaperConfig(
            stale_running_sec=60, stale_queued_sec=1,
            max_total_age_sec=300, grace_period_sec=0, enabled=True,
        )
        t = self.mgr.create(title="stale queued", type="normal", input_text="hi", session_id="x")
        time.sleep(1.2)
        res = reap_once(self.mgr, cfg)
        self.assertIn(t.task_id, res.reaped)
        # And the task is now in `timeout` state.
        t2 = self.mgr.get(t.task_id)
        self.assertEqual(t2.status, "timeout")

    def test_total_age_reaps_old_task(self):
        from dispatcher.reaper import reap_once, ReaperConfig
        cfg = ReaperConfig(
            stale_running_sec=10000, stale_queued_sec=10000,
            max_total_age_sec=1, grace_period_sec=0, enabled=True,
        )
        t = self.mgr.create(title="old", type="normal", input_text="hi", session_id="x")
        time.sleep(1.2)
        res = reap_once(self.mgr, cfg)
        self.assertIn(t.task_id, res.reaped)

    def test_grace_period_protects_new_task(self):
        from dispatcher.reaper import reap_once, ReaperConfig
        cfg = ReaperConfig(
            stale_running_sec=1, stale_queued_sec=1,
            max_total_age_sec=1, grace_period_sec=300, enabled=True,
        )
        t = self.mgr.create(title="fresh", type="normal", input_text="hi", session_id="x")
        time.sleep(1.2)
        res = reap_once(self.mgr, cfg)
        # Grace is 5 minutes, so even with 1s thresholds, it shouldn't reap.
        self.assertEqual(res.reaped, [])
        self.assertEqual(len(res.skipped), 1)

    def test_disabled_config_does_nothing(self):
        from dispatcher.reaper import reap_once, ReaperConfig
        cfg = ReaperConfig(
            stale_running_sec=1, stale_queued_sec=1,
            max_total_age_sec=1, grace_period_sec=0, enabled=False,
        )
        t = self.mgr.create(title="old", type="normal", input_text="hi", session_id="x")
        time.sleep(1.2)
        res = reap_once(self.mgr, cfg)
        self.assertEqual(res.reaped, [])
        self.assertEqual(res.scanned, 0)

    def test_stale_count_summary(self):
        from dispatcher.reaper import reap_once, stale_count, ReaperConfig
        cfg = ReaperConfig(
            stale_running_sec=1, stale_queued_sec=1,
            max_total_age_sec=1, grace_period_sec=0, enabled=True,
        )
        for i in range(3):
            self.mgr.create(title=f"t{i}", type="normal", input_text="hi", session_id="x")
        time.sleep(1.2)
        cnt = stale_count(self.mgr, cfg)
        self.assertEqual(cnt["queued"], 3)
        self.assertEqual(cnt["would_reap"], 3)


# ===========================================================================
# Safety
# ===========================================================================


class TestSafety(unittest.TestCase):
    def test_hard_blocklist_rejects(self):
        from dispatcher.safety import evaluate
        d = evaluate("rm -rf /")
        self.assertEqual(d.action, "block")
        d = evaluate("please run :(){:|:&};: now")
        self.assertEqual(d.action, "block")

    def test_allowlist_in_normal_mode_is_permissive(self):
        from dispatcher.safety import evaluate
        # Normal mode: only blocklist is enforced. Anything else allowed.
        d = evaluate("ls /home/ubuntu", mode="normal")
        self.assertEqual(d.action, "allow")

    def test_allowlist_in_ops_mode_blocks_unknown_commands(self):
        from dispatcher.safety import evaluate
        d = evaluate("evilcmd --destroy", mode="ops")
        # P3 loosening (2026-07-08): unknown binary names are now
        # allowed with audit_warn, NOT require_approval. The real
        # risk surface (sudo, apt install, pip install, etc.) is
        # still gated by the approval substrings check.
        self.assertEqual(d.action, "allow")
        self.assertFalse(d.needs_human)
        self.assertTrue(d.meta.get("audit_warn"))
        self.assertEqual(d.meta.get("first_token"), "evilcmd")

    def test_allowlist_in_coding_mode_allows_known(self):
        from dispatcher.safety import evaluate
        d = evaluate("ls -la /home/ubuntu", mode="ops")
        self.assertEqual(d.action, "allow")

    def test_approval_gate_for_sudo(self):
        from dispatcher.safety import evaluate
        d = evaluate("ls -la", mode="ops")
        self.assertEqual(d.action, "allow")
        d = evaluate("sudo apt install nginx", mode="ops")
        self.assertEqual(d.action, "require_approval")
        self.assertIn("sudo", d.matched)

    def test_path_safety_blocks_etc(self):
        from dispatcher.safety import evaluate
        d = evaluate("cat /etc/passwd", mode="ops")
        self.assertEqual(d.action, "block")
        d = evaluate("cat /home/ubuntu/secret", mode="ops")
        self.assertEqual(d.action, "allow")

    def test_decision_to_dict(self):
        from dispatcher.safety import evaluate
        d = evaluate("ls -la", mode="ops")
        d2 = d.to_dict()
        self.assertEqual(d2["action"], "allow")
        self.assertIn("reason", d2)


# ===========================================================================
# Notifier
# ===========================================================================


class TestNotifier(unittest.TestCase):
    def test_disabled_when_no_token(self):
        # Make sure env vars are unset.
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)
        from dispatcher.notifier import _telegram_config
        cfg = _telegram_config()
        self.assertFalse(cfg["enabled"])

    def test_local_log_written_when_alert(self):
        for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            os.environ.pop(k, None)
        from dispatcher.notifier import _append_local_log
        line = '{"event":"test"}'
        _append_local_log(line)
        from dispatcher.manager import _BRIDGE_ROOT
        log_path = _BRIDGE_ROOT / "logs" / "notifier.log"
        self.assertTrue(log_path.exists())
        self.assertIn(line, log_path.read_text(encoding="utf-8"))


# ===========================================================================
# Usage
# ===========================================================================


class TestUsage(unittest.TestCase):
    def setUp(self):
        _fresh_db()
        self.mgr = TaskManager()

    def test_aggregate_openai_style(self):
        from dispatcher.usage import aggregate
        t = self.mgr.create(title="a", type="normal", input_text="hi", session_id="x")
        self.mgr.start(t.task_id, hermes_run_id="r1")
        self.mgr.complete(
            t.task_id,
            output_text="ok",
            usage={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
            raw={"model": "claude-sonnet-4-6"},
        )
        agg = aggregate(period="all")
        self.assertEqual(agg["totals"]["task_count"], 1)
        self.assertEqual(agg["totals"]["input_tokens"], 100)
        self.assertEqual(agg["totals"]["output_tokens"], 50)
        self.assertGreater(agg["totals"]["estimated_cost_usd"], 0)
        # by_type[0].type should be 'normal'.
        types = [d["type"] for d in agg["by_type"]]
        self.assertIn("normal", types)
        # by_model[0].model should be 'claude-sonnet-4-6' (now picked up from raw).
        models = [d["model"] for d in agg["by_model"]]
        self.assertIn("claude-sonnet-4-6", models)

    def test_aggregate_hermes_short_form(self):
        from dispatcher.usage import aggregate
        t = self.mgr.create(title="b", type="research", input_text="hi", session_id="x")
        self.mgr.start(t.task_id, hermes_run_id="r2")
        self.mgr.complete(
            t.task_id, output_text="ok",
            usage={"p": 10, "c": 5, "t": 15},
            raw={"model": "kimi-k2.6:cloud"},
        )
        agg = aggregate(period="all")
        self.assertEqual(agg["totals"]["input_tokens"], 10)
        self.assertEqual(agg["totals"]["output_tokens"], 5)

    def test_aggregate_per_task(self):
        from dispatcher.usage import aggregate
        t = self.mgr.create(title="c", type="normal", input_text="hi", session_id="x")
        self.mgr.start(t.task_id, hermes_run_id="r3")
        self.mgr.complete(
            t.task_id, output_text="ok",
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        agg = aggregate(period="all", task_id=t.task_id)
        self.assertEqual(agg["task_id"], t.task_id)
        self.assertEqual(agg["totals"]["task_count"], 1)


# ===========================================================================
# State machine includes timeout
# ===========================================================================


class TestStateMachine(unittest.TestCase):
    def test_timeout_is_legal_from_inflight(self):
        for s in ("pending", "queued", "running", "waiting"):
            self.assertIn("timeout", LEGAL_TRANSITIONS[s],
                          f"timeout should be legal from {s}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
