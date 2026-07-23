"""AEE v3 Telegram Completion Enforcement Gate — 4-stage completion state model.

The v3 gate refines the dispatcher's terminal state from a single
``status='completed'`` boolean into a 4-stage chain so the
orchestrator can tell *how far* a task actually got before it
stopped:

    execution_completed
        -> evidence_completed
            -> notification_completed
                -> final_completed

Rationale
---------
Pre-v3, ``TaskManager.complete()`` set ``status='completed'`` and
emitted a single ``COMPLETED`` event, but it never called
``notifier.notify_completed`` (Gap A — dead hook) and
``config/notify.json`` excluded ``completed`` from ``notify_on``
(Gap B). The result was a task that the dispatcher reported as
"done" while no Telegram alert ever fired — the orchestrator had
to poll the task row to learn the outcome, defeating the point of
the notifier.

The v3 gate fixes both gaps:

* ``complete()`` now calls ``notify_completed_with_fallback`` (the
  Hermes Telegram Gateway path with the legacy notifier as
  fallback) and persists the gate's result into a new
  ``task_outputs.notification_json`` column.
* The 3 new ``EventKind`` members (``notification_pending`` /
  ``notification_completed`` / ``notification_failed``) narrate
  the gate's outcome into the existing ``task_events`` audit log
  so the orchestrator can filter on them.

The 4-stage model below is the **read-side** projection of that
gate: given a task row (+ its ``task_outputs`` row), what is the
highest stage the task reached? The state machine itself is
*observability-only* in this iteration — ``status='completed'``
stays the terminal state for backward compatibility, and the
gate never blocks the existing ``complete()`` return value. A
future iteration can flip the gate to *blocking* (i.e. refuse to
report ``final_completed`` until the notification has a
``message_id``) once the 7-day shadow run is green.

Reference
---------
The full gap analysis (Gap A dead hook + Gap B config exclusion +
the 4-stage design contract) lives at:

    ~/.hermes/skills/software-development/aee-iteration-pattern/references/aee-v3-telegram-completion-enforcement-gate-analysis.md

See that doc for the original incident timeline, the decision
record for "observability-only first, blocking later", and the
shadow-run promotion criteria.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# 4-stage completion state constants (mirror the EventKind pattern — plain
# class-level string constants, NOT Enum, because the on-disk shape is TEXT
# and the orchestrator's filter code compares against the string value).
# ---------------------------------------------------------------------------


class CompletionState:
    """The 4-stage completion state vocabulary.

    Members are class-level string constants (not ``Enum`` members)
    because the on-disk shape is ``TEXT`` (the
    ``task_outputs.notification_json`` blob, plus the
    ``compute_completion_state`` return value) and the
    orchestrator's filter code compares against the string value.
    Using ``str`` constants keeps the round-trip trivial.
    """

    # Task entered a terminal status (completed / failed / cancelled /
    # timeout). Execution itself is done; evidence + notification are
    # still pending.
    EXECUTION_COMPLETED = "execution_completed"
    # Delivery verification has run and recorded its evidence into
    # ``task_outputs.delivery_json`` (the artifact stat/hash/classify
    # results). Execution + evidence are done; notification is still
    # pending.
    EVIDENCE_COMPLETED = "evidence_completed"
    # The notification gate has fired and recorded its result into
    # ``task_outputs.notification_json``. The blob is present but the
    # send itself has not been confirmed (no ``message_id``). This is
    # the "queued but not delivered" state.
    NOTIFICATION_COMPLETED = "notification_completed"
    # The notification gate returned a confirmed ``message_id``. This
    # is the only terminal completion state under the v3 model — the
    # task is fully done AND the operator has been notified.
    FINAL_COMPLETED = "final_completed"


# Aliases re-exported at module top level for convenience; the task spec
# references them as module-level names (``EXECUTION_COMPLETED`` etc.).
EXECUTION_COMPLETED = CompletionState.EXECUTION_COMPLETED
EVIDENCE_COMPLETED = CompletionState.EVIDENCE_COMPLETED
NOTIFICATION_COMPLETED = CompletionState.NOTIFICATION_COMPLETED
FINAL_COMPLETED = CompletionState.FINAL_COMPLETED


def is_terminal_completion_state(state: str) -> bool:
    """``True`` iff ``state`` is the terminal completion state
    (``FINAL_COMPLETED``). Under the v3 model only
    ``final_completed`` is fully terminal — the three earlier
    stages are non-terminal because the gate has not yet confirmed
    delivery."""
    return state == FINAL_COMPLETED


def legal_completion_transitions() -> Dict[str, List[str]]:
    """Return the 3-step forward chain of the v3 completion model:

        execution_completed -> [evidence_completed]
        evidence_completed  -> [notification_completed]
        notification_completed -> [final_completed]

    Returned as a dict so callers can look up the legal next
    stages for a given current stage. The chain is strictly
    linear in this iteration — no skip / fork / retry edges —
    but the dict shape leaves room for a future iteration to add
    a ``notification_failed -> [notification_completed]`` retry
    edge without changing the call signature."""
    return {
        EXECUTION_COMPLETED: [EVIDENCE_COMPLETED],
        EVIDENCE_COMPLETED: [NOTIFICATION_COMPLETED],
        NOTIFICATION_COMPLETED: [FINAL_COMPLETED],
    }


def compute_completion_state(task_row: Dict[str, object]) -> str:
    """Inspect a SQLite row dict and return the highest completion
    stage the task has reached.

    Required keys on ``task_row`` (mirroring the
    ``tasks`` + ``task_outputs`` join the manager builds for
    ``TaskManager.completion_state``):

    * ``status``       — the dispatcher status string (unused by the
                         v3 stage logic itself; kept in the contract
                         for callers that want to gate on
                         ``status == 'completed'`` separately).
    * ``finished_at``  — non-NULL once the task entered a terminal
                         status (set by ``complete()`` / ``fail()`` /
                         ``timeout()`` / ``cancel()``).
    * ``delivery_json`` — non-NULL once ``_verify_expected_delivery``
                          has recorded artifact evidence.
    * ``notification_json`` — non-NULL once the v3 gate has fired.

    Stage resolution (highest reached wins):

    1. If ``notification_json`` is non-NULL AND JSON-decodes to a
       dict with ``sent == True`` AND a non-NULL ``message_id`` ->
       ``FINAL_COMPLETED`` (the gate confirmed delivery).
    2. Elif ``delivery_json`` is non-NULL (evidence recorded) ->
       ``NOTIFICATION_COMPLETED`` (evidence done, notification
       pending). NOTE: this stage name is a deliberate alias of
       the ``EventKind.NOTIFICATION_COMPLETED`` literal but in the
       v3 stage model it means "evidence done, notification
       pending" — the naming asymmetry is documented in the
       reference analysis; the value strings are kept distinct
       from the ``EventKind`` namespace by the
       ``final_completed`` capstone.
    3. Elif ``finished_at`` is non-NULL (execution completed) ->
       ``EVIDENCE_COMPLETED`` (execution done, evidence pending).
    4. Else ``EXECUTION_COMPLETED`` (just entered terminal).

    Defensive: this function MUST NOT raise on malformed JSON or
    missing keys. Per the v3 contract, **any decode error on
    ``notification_json`` collapses to ``EXECUTION_COMPLETED``**
    (the safest non-terminal stage) — a corrupt blob must never
    be misread as a higher stage, and the orchestrator's read
    path must never break on a corrupt ``notification_json``
    blob. This short-circuit takes precedence over the
    ``delivery_json`` / ``finished_at`` cascade below.
    """
    if not isinstance(task_row, dict):
        return EXECUTION_COMPLETED

    notification_json = task_row.get("notification_json")
    if notification_json:
        # Decide whether the blob is a confirmed-delivery record.
        # A malformed blob collapses to EXECUTION_COMPLETED per the
        # v3 defensive contract (never raise, never misread).
        try:
            decoded = json.loads(notification_json) if isinstance(
                notification_json, str
            ) else notification_json
        except (json.JSONDecodeError, TypeError, ValueError):
            # Malformed blob — collapse to the safest stage and
            # short-circuit (do NOT fall through to the
            # delivery_json / finished_at cascade; a corrupt
            # notification_json is itself a signal that the gate
            # did not produce a trustworthy result).
            return EXECUTION_COMPLETED
        if not isinstance(decoded, dict):
            # Decoded but not a dict (e.g. a JSON list / string /
            # number) — same defensive collapse.
            return EXECUTION_COMPLETED
        sent = decoded.get("sent")
        message_id = decoded.get("message_id")
        if sent is True and message_id is not None:
            return FINAL_COMPLETED
        # The blob is a dict but not a confirmed delivery (sent
        # False, or message_id None). Fall through to the
        # delivery_json / finished_at cascade so the stage
        # reflects whatever evidence *is* present.

    delivery_json = task_row.get("delivery_json")
    if delivery_json:
        return NOTIFICATION_COMPLETED

    finished_at = task_row.get("finished_at")
    if finished_at:
        return EVIDENCE_COMPLETED

    return EXECUTION_COMPLETED