"""AEE-7.7a — Live-Report Audit (read-only).

This package runs a one-shot audit over a ``reports/`` directory
hierarchy.  It is the read-only counterpart to the AEE-7.11
``aee/reporting/identity.classify_and_persist`` helper:

* ``classify_and_persist`` writes a ``identity.json`` sidecar
  next to every FIXTURE/UNKNOWN ``task.json``.  It is the
  per-record persistent form.
* :func:`aee.audit.run_audit` walks a reports root, classifies
  every record *in memory* via :func:`classify_record`, runs
  :func:`validate_task_identity` for consistency, aggregates
  the verdicts, and writes **one** JSON + **one** Markdown
  summary to the caller-supplied output directory.  It never
  writes inside the audited ``reports/`` directory.  It is the
  bulk one-shot form, suitable for an AEE-7.7 next-slice
  delivery.

The audit is intentionally **read-only** with respect to the
dispatcher write path:

* No row is written to ``data/dispatcher.db``.
* No file is written under ``reports/``.
* The audit output is a single JSON + a single Markdown written
  to the caller-supplied output path (defaults to
  ``/tmp/aee77a-audit-<UTC>.{json,md}``).

Why this lives in its own package
---------------------------------
``aee/reporting/`` is the *per-record* home (Identity, sidecar
writer, consistency validator).  ``aee/audit/`` is the
*corpus-level* home (walk a directory, aggregate verdicts,
write a summary report).  Separating the two keeps the
``aee/reporting/`` surface small (no I/O helpers, no directory
walkers) and makes it easy to extend the audit (more findings
types, more aggregation buckets) without bloating the
read-side identity model.

Public surface
--------------

* :func:`run_audit` — the entry point.  Takes a reports root
  and an output path, returns a :class:`AuditSummary`.
* :class:`AuditSummary` — the in-memory aggregate, serializable
  via :meth:`to_dict` / :meth:`to_markdown`.
* :class:`PerTaskVerdict` — a single record's verdict
  (classification + consistency findings).

Excluded by design
------------------

* No network calls.  No HTTP.  No ``requests``.
* No subprocess.  No shelling out to ``git`` or ``sqlite3``.
* No environment variable reads.  The audit is deterministic
  given the same on-disk corpus + the same policy version.
* No logging of secrets.  The audit never reads
  ``dispatcher/.env`` or any secret-bearing file.
"""
from __future__ import annotations

from .live_audit import (
    AuditSummary,
    PerTaskVerdict,
    run_audit,
)

__all__ = [
    "AuditSummary",
    "PerTaskVerdict",
    "run_audit",
]
