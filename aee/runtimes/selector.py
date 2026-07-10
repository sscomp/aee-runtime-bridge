"""AEE-5 Runtime Selector — deterministic matching.

Implements the 9-step selection order from the AEE-5
task spec §4.4. Pure function: given a `TaskRuntimeRequirements`
and a list of `RuntimeDescriptor`s, return either a
`RuntimeSelectionResult` (with `selected_runtime_id` set)
or raise `RuntimeNotFoundError` (the structured error the
dispatch service logs and the API surfaces as a 422).

Determinism
-----------
The selector never reads time, randomness, or external
state. Score ties are broken by `runtime_id` ASCII order
(ascending). This is the contract the AEE-5 task spec
requires: a job's selection result is reproducible given
the same `requirements` and the same list of
`available_runtimes`.

Score (lower = better)
----------------------
For each candidate that passes the required filters, we
compute a score that reflects preference and health:

    0 (best)    — Runtime in `preferred_runtime_ids`
    +1          — `preferred_capabilities` count
    +health_score(status)   — see aee.runtimes.health

Candidates with the lowest total score rank first; the
selector returns the first one.

Exclusion rules
---------------
1. `Runtime.enabled == False` is rejected with reason
   "runtime disabled".
2. `Runtime.runtime_id in excluded_runtime_ids` is
   rejected with reason "runtime excluded by task".
3. Health-dispatchable check is applied (configurable
   `allow_unknown_health`). Rejection reason:
   "runtime health <status> not dispatchable".
4. `runtime_type` exact match.
5. `required_capabilities` subset match.
6. `required_labels` subset match (every required k=v
   must be present on the Runtime).

The selector also captures the *full* evaluated set
(enabled + disabled) for the `AEE_RUNTIME_NOT_FOUND`
error's `evaluated_runtimes` list, so an operator can
debug "why didn't this job dispatch" without re-running
the matching manually.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .errors import RuntimeNotFoundError
from .health import health_score, is_dispatchable
from .models import (
    RuntimeDescriptor,
    RuntimeHealthStatus,
    RuntimeSelectionResult,
    TaskRuntimeRequirements,
)


# Regex used to *normalize* a capability string for
# subset matching. The matcher's rule is the same as
# the dispatcher's existing `normalize_capabilities`:
# lowercase, trim, drop empties.
_CAP_NORMALIZE_RE = re.compile(r"\s+")


def _norm_caps(values) -> List[str]:
    if not values:
        return []
    out: List[str] = []
    seen = set()
    for v in values:
        if not isinstance(v, str):
            continue
        n = _CAP_NORMALIZE_RE.sub(" ", v).strip().lower()
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _norm_labels(d: Optional[Dict[str, str]]) -> Dict[str, str]:
    if not d:
        return {}
    return {str(k).strip().lower(): str(v).strip().lower() for k, v in d.items()}


class RuntimeSelector:
    """Deterministic Runtime selector.

    Stateless; can be instantiated once and reused. The
    module-level `select_runtime()` helper below is the
    thin convenience wrapper the rest of AEE uses.
    """

    def __init__(self, *, allow_unknown_health: bool = True) -> None:
        self._allow_unknown_health = bool(allow_unknown_health)

    def select(
        self,
        *,
        task: Optional[TaskRuntimeRequirements],
        available_runtimes: List[RuntimeDescriptor],
    ) -> RuntimeSelectionResult:
        """Pick a Runtime for `task` from `available_runtimes`.

        Returns a `RuntimeSelectionResult` on success.
        Raises `RuntimeNotFoundError` when no Runtime
        fits; the exception's structured details capture
        every evaluated Runtime + the reason it was
        rejected.
        """
        # `None` requirements == "no constraints" (AEE-4
        # compat path). The selector still returns the
        # first enabled, dispatchable Runtime.
        if task is None:
            task = TaskRuntimeRequirements()

        evaluated: List[str] = []
        rejected: Dict[str, List[str]] = {}
        candidates: List[RuntimeDescriptor] = []

        req_caps = set(_norm_caps(task.required_capabilities))
        pref_caps = set(_norm_caps(task.preferred_capabilities))
        req_labels = _norm_labels(task.required_labels)
        excluded = {
            str(x).strip() for x in (task.excluded_runtime_ids or []) if x
        }
        preferred_ids = {
            str(x).strip() for x in (task.preferred_runtime_ids or []) if x
        }
        target_type = (task.runtime_type or "").strip()

        # 1. Walk the whole list to build the diagnostic
        #    `evaluated_runtimes` BEFORE we filter. This is
        #    what the AEE-5 task spec §4.5 wants in the
        #    `AEE_RUNTIME_NOT_FOUND` payload.
        for r in available_runtimes:
            evaluated.append(r.runtime_id)
            reasons: List[str] = []
            if not r.enabled:
                reasons.append("runtime disabled")
            if r.runtime_id in excluded:
                reasons.append("runtime excluded by task")
            if not is_dispatchable(
                r.health.status, allow_unknown_health=self._allow_unknown_health
            ):
                reasons.append(
                    f"runtime health {r.health.status!r} not dispatchable"
                )
            if target_type and r.runtime_type != target_type:
                reasons.append(
                    f"runtime_type mismatch: required {target_type!r}, "
                    f"got {r.runtime_type!r}"
                )
            r_caps = set(_norm_caps(r.capabilities.to_list()))
            missing = sorted(req_caps - r_caps)
            if missing:
                reasons.append(
                    f"missing required capability: {missing[0]}"
                    if len(missing) == 1
                    else f"missing required capabilities: {missing}"
                )
            r_labels = _norm_labels(r.labels)
            missing_labels = [
                f"{k}={v}" for k, v in req_labels.items() if r_labels.get(k) != v
            ]
            if missing_labels:
                reasons.append(
                    f"missing required label: {missing_labels[0]}"
                    if len(missing_labels) == 1
                    else f"missing required labels: {missing_labels}"
                )

            if reasons:
                rejected[r.runtime_id] = reasons
            else:
                candidates.append(r)

        if not candidates:
            # AEE-5 §4.5: structured error. Build the
            # `evaluated_runtimes` list with the rejected
            # reasons so the operator can see *why* nothing
            # matched.
            evaluated_runtimes = [
                {
                    "runtime_id": rid,
                    "rejected_reasons": list(reasons),
                }
                for rid, reasons in rejected.items()
            ]
            raise RuntimeNotFoundError(
                message="No enabled runtime satisfies the task requirements.",
                required_runtime_type=target_type or None,
                required_capabilities=sorted(req_caps),
                required_labels=req_labels,
                evaluated_runtimes=evaluated_runtimes,
            )

        # 2. Score the candidates. Lower = better.
        scored: List[tuple] = []
        # We invert the preferred-capability weight: a
        # Runtime that has more of the preferred caps
        # gets a *lower* score (i.e. ranks *higher*).
        # To keep the "lower = better" invariant while
        # still expressing the preference, we use a
        # negative weight for preferred-cap match.
        for r in candidates:
            score = 0
            # Preferred-id first: a Runtime NOT in
            # the preferred list gets a +1 penalty.
            if r.runtime_id not in preferred_ids:
                score += 1
            # Preferred-capability weight: a Runtime
            # with more preferred caps ranks better
            # (lower score). Subtracting a small
            # number per match keeps the ordering
            # stable without dominating the health
            # score.
            r_caps = set(_norm_caps(r.capabilities.to_list()))
            score -= len(pref_caps & r_caps)
            # Health
            score += health_score(r.health.status)
            scored.append((score, r.runtime_id, r))
        # Sort by (score, runtime_id) for stable tie-break.
        scored.sort(key=lambda t: (t[0], t[1]))
        _score, _rid, chosen = scored[0]

        # 3. Compose the human-readable reason. Stable
        #    across identical inputs.
        reason_bits = []
        if task.is_empty():
            reason_bits.append("no requirements; picked first dispatchable runtime")
        else:
            if target_type:
                reason_bits.append(f"runtime_type={target_type!r} matched")
            if req_caps:
                reason_bits.append(
                    f"required capabilities satisfied: {sorted(req_caps)}"
                )
            if req_labels:
                reason_bits.append(
                    f"required labels satisfied: {sorted(req_labels.items())}"
                )
            if chosen.runtime_id in preferred_ids:
                reason_bits.append("runtime_id is preferred")
            if pref_caps & set(_norm_caps(chosen.capabilities.to_list())):
                reason_bits.append(
                    f"preferred capabilities match: "
                    f"{sorted(pref_caps & set(_norm_caps(chosen.capabilities.to_list())))}"
                )
            reason_bits.append(f"health={chosen.health.status}")
        reason = "; ".join(reason_bits) or "matched"

        return RuntimeSelectionResult(
            selected_runtime_id=chosen.runtime_id,
            selection_reason=reason,
            candidate_count=len(candidates),
            evaluated_runtime_ids=list(evaluated),
            rejected_reasons=dict(rejected),
        )


# Module-level convenience.
_default_selector = RuntimeSelector()


def select_runtime(
    *,
    task: Optional[TaskRuntimeRequirements],
    available_runtimes: List[RuntimeDescriptor],
    allow_unknown_health: bool = True,
) -> RuntimeSelectionResult:
    """Functional entry point used by the dispatch service."""
    sel = RuntimeSelector(allow_unknown_health=allow_unknown_health)
    return sel.select(task=task, available_runtimes=available_runtimes)


__all__ = ["RuntimeSelector", "select_runtime"]
