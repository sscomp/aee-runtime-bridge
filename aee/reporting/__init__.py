"""AEE Audit Namespace Hardening — read-side identity model.

This package is the canonical home for the **read-side** identity
model that decides whether a ``reports/TASK-YYYYMMDD-NNNN/task.json``
record is a real Runtime execution or a fixture (injection probe,
test sentinel, or path-traversal sample).

It is **intentionally** read-only with respect to the dispatcher's
write path:

* The dispatcher (``dispatcher/manager.py``) keeps writing the
  same ``task.json`` shape it has written since AEE-1.
* R1–R4 atomic commits stay byte-identical.
* The validator reads ``task.json``, classifies it, and writes a
  companion ``identity.json`` next to it (additive).

Public surface
--------------

* :class:`RecordKind` — the 3-way classification.
* :class:`Identity` — the structured verdict for a single report.
* :class:`SentinelPolicy` — the configurable sentinel fixture rules.
* :func:`classify_record` — the entry point.
* :func:`iter_reports` — the report iterator (sorted, lazy).
* :func:`write_identity_sidecar` — atomic, idempotent sidecar write.
* :func:`read_identity_sidecar` — companion read.
* :func:`load_task_json` — robust loader (handles missing / malformed).

Why this exists (2026-07-11, Audit Namespace Hardening slice)
-------------------------------------------------------------

The ``reports/`` directory layout is keyed by
``TASK-YYYYMMDD-NNNN`` where ``NNNN`` is a sequence number
allocated by ``dispatcher.manager.create()``. The same shape is
shared by:

1. **Real Runtime executions** — sequence numbers issued for actual
   orchestrator-driven tasks, with a real ``run_<32-char-hex>``
   Hermes run_id.
2. **Fixture / injection probe reports** — sequence numbers issued
   for path-traversal samples (``input_text = "read
   /tmp/../etc/whatever now"``), test sentinels (``hermes_run_id =
   "hr-1"`` / ``"r3"`` / ``"hr"`` / ``"run-traversal"``), and
   placeholder titles (``"aee6-traversal"`` / ``"t"`` / ``"c"``).

An audit or future orchestrator that only reads ``task.json`` and
trusts ``progress_pct`` / ``hermes_run_id`` will misidentify a
fixture as a real task. The prior AEE-7.1.1 reconciliation audit
(2026-07-11 14:15 Asia-Taipei) hit this trap and labelled fixture
``TASK-20260711-0018`` as the executor before re-deriving the real
executor ``TASK-20260711-0015`` from cross-namespace evidence.

This module is the structural fix: every consumer of ``reports/``
MUST call :func:`classify_record` first and refuse to act on
:class:`RecordKind.FIXTURE` records as if they were real
executions.

Backward compatibility
----------------------

* ``Identity`` is a **pure read**; it never modifies the
  ``task.json`` file. Existing audit code that reads
  ``task.json`` directly keeps working — they just lose the
  fixture guard.
* The companion ``identity.json`` is an **additive** file. It
  can be deleted at any time and the validator will recompute it
  from ``task.json``.
* Old ``task.json`` files (no ``record_kind`` /
  ``is_fixture`` / ``executor_session_id`` fields) classify
  correctly via the heuristic-only path.

Naming
------

* ``canonical_task_id`` — the real ``TASK-...`` value a Runtime
  execution's task.json carries. Same as ``task.json["task_id"]``
  in 99% of cases.
* ``executor_session_id`` — the executor session that produced
  the task (e.g. ``AEE-R1-R4-ATOMIC-COMMITS-20260711``). This is
  the strongest cross-namespace anchor.
* ``runtime_run_id`` — the executor's real run_id (e.g.
  ``r1-r4-atomic-20260711-1258``), NOT the dispatcher's
  ``hermes_run_id`` field which carries sentinels.
* ``user_provided_alias`` — when the user (or a prior audit)
  referred to a report by a different name (e.g. audit session
  labelled 0018 = TASK-20260711-0015), the alias is preserved
  here so the mapping is auditable.
"""
from __future__ import annotations

from .identity import (
    Identity,
    RecordKind,
    SentinelPolicy,
    classify_and_persist,
    classify_record,
    iter_reports,
    load_task_json,
    read_identity_sidecar,
    write_identity_sidecar,
)
# AEE-7.6: identity consistency tripwire. Re-exported so any
# audit / reporting consumer can import the validator alongside
# the read-side identity model:
#
#   from aee.reporting import (
#       validate_task_identity,
#       tripwire_violations,
#       ConsistencySeverity,
#   )
from .identity_consistency import (
    ConsistencySeverity,
    IdentityConsistencyReport,
    validate_task_identity,
    tripwire_violations,
)

__all__ = [
    "ConsistencySeverity",
    "Identity",
    "IdentityConsistencyReport",
    "RecordKind",
    "SentinelPolicy",
    "classify_and_persist",
    "classify_record",
    "iter_reports",
    "load_task_json",
    "read_identity_sidecar",
    "tripwire_violations",
    "validate_task_identity",
    "write_identity_sidecar",
]
