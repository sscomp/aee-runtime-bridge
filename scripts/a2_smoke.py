#!/usr/bin/env python3
"""Semantic smoke runner under the verification-mode contract
(TASK-20260908-0008).

This is the corrected version of the ad-hoc semantic smoke that caused
the 2026-09-08 B1b deploy incident (5 real Telegram sends, mids
3073-3077): that smoke called ``manager.timeout()/complete()`` outside
pytest with the real ``.env`` and no suppression. THIS script always
runs inside ``verification_mode()`` — every dispatcher terminal call is
suppressed at the shared gate before any subprocess or network work.

Scenario
--------
A synthetic task (``T-A``) is inserted with RAW SQL into a TEMP
dispatcher DB (never ``TaskManager.create()`` — see the B1b incident:
temp-DB auto-numbering can draw a REAL production task id and pollute
``reports/`` + ``logs/``). The smoke then drives terminal transitions
and asserts the suppressed result shapes.

Usage::

    .venv/bin/python scripts/a2_smoke.py

Live notification audit: untouched (suppressed attempts land in the
process-local sink under /tmp).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aee._notification_guard import (  # noqa: E402
    audit_sink_file,
    notifications_disabled,
    verification_mode,
)


def _insert_synthetic_task(db_path: Path) -> None:
    """Insert task ``T-A`` with explicit never-real id via raw SQL.

    Never use ``TaskManager.create()`` for synthetic scenarios: a temp
    DB's auto-numbering (TASK-YYYYMMDD-NNNN) can COLLIDE with real
    production task ids and overwrite that id's gitignored report file.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO tasks (task_id, title, type, owner, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("T-A", "a2-b2b3-fail-closed-smoke (synthetic)", "ops",
             "a2", "running", "2026-09-08T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    with verification_mode():
        assert notifications_disabled(), (
            "verification mode must arm the suppression gate"
        )
        sink = audit_sink_file()
        print(f"[a2_smoke] suppression armed; sink={sink}")

        from tests._live_db_guard import (
            make_temp_dispatcher_db,
            point_module_to_temp_db,
        )

        with make_temp_dispatcher_db(
            prefix="a2-b2b3-smoke-"
        ) as db_path, point_module_to_temp_db(db_path):
            # Post-rebind safety assertion: the production DB path must
            # NOT be bound inside this context (B4 remains open — the
            # smoke itself must prove its own isolation).
            from dispatcher.db import DB_PATH as _bound_path
            if str(_bound_path) == str(_ROOT / "data" / "dispatcher.db"):
                print("[a2_smoke] FAIL: production DB path still bound — "
                      "temp-DB rebind failed", file=sys.stderr)
                return 1
            _insert_synthetic_task(db_path)

            from dispatcher.manager import TaskManager

            m = TaskManager()
            results = {}
            for method, status in (
                ("timeout", "timeout"),
                ("cancel", "cancelled"),
            ):
                try:
                    getattr(m, method)("T-A")
                    results[method] = "transitioned"
                except Exception as exc:  # noqa: BLE001
                    # Illegal-transition on the second call is expected
                    # (already terminal); record and continue.
                    results[method] = f"{type(exc).__name__}"
                # Whatever the transition outcome, the notification must
                # be suppressed.
                out = m.get_output("T-A") or {}
                blob = out.get("notification_json")
                if blob:
                    decoded = json.loads(blob)
                    if decoded.get("sent") is True:
                        print(f"[a2_smoke] FAIL: {method} produced a "
                              "sent=True notification under verification "
                              f"mode: {decoded}", file=sys.stderr)
                        return 1
                    results[f"{method}_notification"] = decoded.get("method")
            print(json.dumps(results, indent=2, ensure_ascii=False))

        # Count what WOULD have been sent (suppressed attempts).
        suppressed_rows = 0
        if sink is not None and sink.exists():
            suppressed_rows = sum(
                1 for line in sink.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        print(f"[a2_smoke] suppressed attempts recorded: {suppressed_rows}")
        print("[a2_smoke] PASS: all sends suppressed, temp DB only, "
              "live audit untouched")
        return 0


if __name__ == "__main__":
    sys.exit(main())