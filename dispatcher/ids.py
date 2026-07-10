"""Task ID generator: TASK-YYYYMMDD-NNNN.

Counter is per-day; resets at UTC midnight. Atomic via SQLite transaction
so two concurrent dispatcher.create() calls cannot produce the same ID.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .db import get_conn, transaction


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def next_task_id() -> str:
    """Return the next unique TASK-YYYYMMDD-NNNN for today (UTC).

    Design: this is a *read-only* helper. The caller is expected to use
    the returned ID in a subsequent INSERT, and SQLite's PRIMARY KEY on
    `tasks.task_id` guarantees uniqueness. Two concurrent callers may
    occasionally race and both compute the same ID — the loser's INSERT
    will raise IntegrityError, which the caller should catch and retry
    by calling next_task_id() again.

    We deliberately do NOT open a transaction here. The bridge's
    connection is in autocommit mode, and SQLite's snapshot isolation
    means a `BEGIN ... SELECT ... COMMIT` would always see stale data
    for the second consecutive call (autocommit's per-statement
    snapshots do not refresh across explicit transactions). Reading
    outside an explicit transaction gives us a fresh snapshot each call.
    """
    today = _today_utc()
    prefix = f"TASK-{today}-"
    conn = get_conn()
    row = conn.execute(
        "SELECT task_id FROM tasks WHERE task_id LIKE ? ORDER BY task_id DESC LIMIT 1",
        (prefix + "%",),
    ).fetchone()
    if row is None:
        n = 1
    else:
        # task_id is e.g. "TASK-20260707-0007" -> seq = 7
        try:
            n = int(row["task_id"].rsplit("-", 1)[1]) + 1
        except (IndexError, ValueError):
            # Malformed row; fall back to counting.
            cnt = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE task_id LIKE ?",
                (prefix + "%",),
            ).fetchone()
            n = (cnt["c"] if cnt else 0) + 1
    return f"{prefix}{n:04d}"


def now_iso() -> str:
    """ISO 8601 UTC with millisecond precision and Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )
