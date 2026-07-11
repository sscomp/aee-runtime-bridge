"""AEE-7.6 Task ID identity consistency validator.

Closes the §20.9 attribution-drift class of bug observed in
TASK-20260711-0043/0044/0048 audits: a single physical run can
be referenced by 4 distinct identifiers (runtime task_id,
executor_session_id, runtime_run_id, hermes_run_id), and any
mismatch between them surfaces as a confusing "which one is
canonical?" question downstream.

The validator is a *pure function* over a ``Task`` row (or a
plain dict with the same field names). It returns a structured
:class:`IdentityConsistencyReport` that flags:

* **Mismatched runtime anchors** — the same record referenced
  under multiple ids that don't agree.
* **Missing anchors** — a record that says it's a RUNTIME
  record but has no runtime anchor.
* **Record-kind collision** — a record classified as
  FIXTURE that nonetheless has a real runtime anchor.
* **Legacy null fields** — a record that has only some
  anchors populated. This is *not* an error: legacy tasks
  pre-AEE-7.5 have null executor_session_id and null
  runtime_run_id, and the read-side validator must keep
  working for them. The report flags these as
  ``LEGACY_NULL_FIELD`` (informational, not failing).

Tripwire
--------
The tripwire form is :func:`tripwire_violations`: returns a
list of human-readable strings, empty list = pass. Used in
the test suite and in the audit pipeline to catch silent
identity drift early.

The validator does NOT consult any external system. It is a
pure schema-and-content check, suitable for use in
unit tests, in CI, and in the AEE-7.5 read-side
identity pipeline as an additional layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Public enums
# ---------------------------------------------------------------------------


class ConsistencySeverity(str, Enum):
    """The severity of a consistency finding.

    * ``ERROR`` — the record is internally inconsistent and
      must NOT be cited as a runtime record. Examples:
      mismatched runtime anchors, runtime record with no
      runtime anchor, fixutre record with a real anchor.
    * ``WARNING`` — the record is suspicious but the
      inconsistency is recoverable. Example: a runtime
      record with only one of the two runtime anchors.
    * ``LEGACY_NULL_FIELD`` — informational only. The
      record has null fields because it predates AEE-7.5
      and was created before write-side metadata was
      captured. NOT an error.
    """

    ERROR = "error"
    WARNING = "warning"
    LEGACY_NULL_FIELD = "legacy_null_field"


# Match the dispatcher's real run_id shape. 32 lowercase hex chars
# after ``run_``. Sentinel ids (``hr-1``, ``run-traversal``) do
# NOT match. (Mirrors `aee/reporting/identity.py::_RUN_ID_HEX_RE`.)
_RUN_ID_HEX_RE = re.compile(r"^run_[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsistencyFinding:
    """A single identity-consistency finding."""

    code: str
    severity: ConsistencySeverity
    message: str
    # Optional: the field(s) that triggered the finding.
    field_path: str = ""


@dataclass(frozen=True)
class IdentityConsistencyReport:
    """The full consistency verdict for a single record.

    ``is_consistent`` is True iff no ERROR-severity finding
    was produced. WARNING and LEGACY_NULL_FIELD findings do
    NOT flip it to False (they're informational).
    """

    task_id: str
    findings: List[ConsistencyFinding] = field(default_factory=list)
    is_consistent: bool = True

    def errors(self) -> List[ConsistencyFinding]:
        return [f for f in self.findings if f.severity == ConsistencySeverity.ERROR]

    def warnings(self) -> List[ConsistencyFinding]:
        return [f for f in self.findings if f.severity == ConsistencySeverity.WARNING]

    def legacy_hints(self) -> List[ConsistencyFinding]:
        return [f for f in self.findings if f.severity == ConsistencySeverity.LEGACY_NULL_FIELD]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "is_consistent": self.is_consistent,
            "findings": [
                {
                    "code": f.code,
                    "severity": f.severity.value,
                    "message": f.message,
                    "field_path": f.field_path,
                }
                for f in self.findings
            ],
        }


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def validate_task_identity(
    task: Dict[str, Any],
    *,
    record_kind: Optional[str] = None,
) -> IdentityConsistencyReport:
    """Validate the identity consistency of a single task record.

    Parameters
    ----------
    task
        A dict with at least the keys: ``task_id``,
        ``executor_session_id``, ``runtime_run_id``,
        ``hermes_run_id``, ``status``. ``runtime_run_id`` and
        ``executor_session_id`` may be None (legacy default).
        ``record_kind`` (RUNTIME / FIXTURE / UNKNOWN) may be
        passed separately or read from ``task["record_kind"]``.
    record_kind
        Optional override for the record kind. When provided,
        takes precedence over ``task["record_kind"]``. When
        None, the function falls back to ``task["record_kind"]``
        and then to "unknown".

    Returns
    -------
    IdentityConsistencyReport
        The full verdict, including all findings. Callers
        should check ``is_consistent`` to decide whether the
        record is citeable.
    """
    task_id = str(task.get("task_id") or "<missing>")
    findings: List[ConsistencyFinding] = []
    executor_session_id = _normalize_optional(task.get("executor_session_id"))
    runtime_run_id = _normalize_optional(task.get("runtime_run_id"))
    hermes_run_id = _normalize_optional(task.get("hermes_run_id"))
    status = str(task.get("status") or "")
    kind = (
        record_kind
        or task.get("record_kind")
        or "unknown"
    )
    kind = str(kind).lower() if kind else "unknown"

    # --- 1. Runtime anchor shape ---
    if runtime_run_id and not _RUN_ID_HEX_RE.match(runtime_run_id):
        findings.append(
            ConsistencyFinding(
                code="RUNTIME_ANCHOR_SHAPE",
                severity=ConsistencySeverity.WARNING,
                message=(
                    f"runtime_run_id {runtime_run_id!r} does not match "
                    f"the canonical run_<32hex> shape"
                ),
                field_path="runtime_run_id",
            )
        )

    # --- 2. Mismatched runtime anchors ---
    # If both hermes_run_id and runtime_run_id are populated and
    # one looks like a real run id and the other doesn't agree,
    # that's an attribution-drift signal.
    if (
        hermes_run_id
        and runtime_run_id
        and hermes_run_id != runtime_run_id
    ):
        findings.append(
            ConsistencyFinding(
                code="MISMATCHED_RUNTIME_ANCHORS",
                severity=ConsistencySeverity.ERROR,
                message=(
                    f"hermes_run_id={hermes_run_id!r} and "
                    f"runtime_run_id={runtime_run_id!r} disagree"
                ),
                field_path="hermes_run_id / runtime_run_id",
            )
        )

    # --- 3. Record-kind vs anchor consistency ---
    if kind == "runtime":
        if not hermes_run_id and not runtime_run_id:
            findings.append(
                ConsistencyFinding(
                    code="RUNTIME_RECORD_WITHOUT_ANCHOR",
                    severity=ConsistencySeverity.ERROR,
                    message=(
                        "record_kind=RUNTIME but neither hermes_run_id "
                        "nor runtime_run_id is populated"
                    ),
                    field_path="record_kind",
                )
            )
    elif kind == "fixture":
        if (
            hermes_run_id
            and _RUN_ID_HEX_RE.match(hermes_run_id)
        ) or (
            runtime_run_id
            and _RUN_ID_HEX_RE.match(runtime_run_id)
        ):
            findings.append(
                ConsistencyFinding(
                    code="FIXTURE_RECORD_WITH_REAL_ANCHOR",
                    severity=ConsistencySeverity.WARNING,
                    message=(
                        "record_kind=FIXTURE but a real run_<32hex> "
                        f"anchor is present (hermes_run_id="
                        f"{hermes_run_id!r}, runtime_run_id="
                        f"{runtime_run_id!r})"
                    ),
                    field_path="record_kind",
                )
            )

    # --- 4. Status vs anchor consistency ---
    if status == "running" and not hermes_run_id and not runtime_run_id:
        findings.append(
            ConsistencyFinding(
                code="RUNNING_TASK_WITHOUT_ANCHOR",
                severity=ConsistencySeverity.ERROR,
                message=(
                    "status='running' but no runtime anchor is "
                    "populated — cannot cite this task as a real run"
                ),
                field_path="status",
            )
        )

    # --- 5. Legacy null fields (informational) ---
    if not executor_session_id and not runtime_run_id:
        # Both write-side fields are null. This is the legacy
        # pre-AEE-7.5 shape. The dispatcher's
        # `find_by_hermes_run_id` lookup still works, so the
        # task is citeable. We flag it as LEGACY_NULL_FIELD
        # so the audit knows it's a pre-write-side-metadata
        # record.
        findings.append(
            ConsistencyFinding(
                code="LEGACY_NULL_WRITE_SIDE_METADATA",
                severity=ConsistencySeverity.LEGACY_NULL_FIELD,
                message=(
                    "executor_session_id and runtime_run_id are both "
                    "NULL — this task predates AEE-7.5 write-side "
                    "metadata; hermes_run_id is the only available "
                    "runtime anchor"
                ),
                field_path="executor_session_id / runtime_run_id",
            )
        )

    # --- 6. Empty task_id ---
    if not task_id or task_id == "<missing>":
        findings.append(
            ConsistencyFinding(
                code="EMPTY_TASK_ID",
                severity=ConsistencySeverity.ERROR,
                message="task_id is missing or empty",
                field_path="task_id",
            )
        )

    is_consistent = not any(
        f.severity == ConsistencySeverity.ERROR for f in findings
    )
    return IdentityConsistencyReport(
        task_id=task_id,
        findings=findings,
        is_consistent=is_consistent,
    )


def tripwire_violations(
    task: Dict[str, Any],
    *,
    record_kind: Optional[str] = None,
) -> List[str]:
    """Tripwire form: return a list of ERROR-severity messages.

    Empty list = pass. Used in the test suite and audit
    pipeline. Does not include WARNING or LEGACY_NULL_FIELD
    findings — those are informational and would create
    noise if surfaced as "violations".
    """
    report = validate_task_identity(task, record_kind=record_kind)
    return [f"{f.code}: {f.message}" for f in report.errors()]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalize_optional(value: Any) -> Optional[str]:
    """Return None for empty/whitespace, else the trimmed str.

    Mirrors the dispatcher's wire-boundary normalization in
    `manager.create` (line 241-242 of dispatcher/manager.py):
    empty string and whitespace collapse to None so the
    validator never sees a sentinel ``""`` value.
    """
    if value is None:
        return None
    s = str(value)
    s = s.strip()
    return s or None
