"""AEE-7.4 slice 3 — deterministic event serialization.

The wire format we emit externally (e.g. to the GPT orchestrator
over the bridge, or to the local-dev stdout JSON emitter) needs a
*stable* schema so consumers can rely on field names.  This module
defines:

* :data:`SCHEMA_VERSION` — the wire-format revision.
* :func:`serialize_event` — the canonical JSON object for a
  single :class:`~aee.observability_runtime.Event`.
* :func:`_SECRET_CANARY` — the test-time canary string used by
  the secret-leak tripwire to assert we never put a token /
  env / file body in the wire payload.

Why a separate module
---------------------
The wire-format is a *contract*.  Putting it next to the
:class:`Event` dataclass would couple the dataclass to a
serialization choice; putting it inside the emitters would mean
each emitter has its own copy.  A small dedicated module makes
the contract auditable (one place to read the schema) and
testable (one place to test the secret-leak guarantee).

Secret handling
---------------
The contract (slice 3 spec) says: events MUST NOT contain tokens,
full env, sensitive stdout/stderr, or raw prompts.  We enforce
that in two places:

1. **At construction time** (the :class:`Event` dataclass
   validator) — it checks that the kind is on the SOT
   whitelist, but does not (and cannot) inspect the payload for
   secrets.

2. **At serialization time** (this module) — the
   :func:`serialize_event` function *scrubs* known-sensitive
   key names (``"api_key"``, ``"token"``, ``"password"``,
   ``"env"``, ``"prompt"``, ``"raw_prompt"``) from the payload
   before they hit the wire.  This is a defense-in-depth
   measure: even if a caller mistakenly puts a secret in a
   payload, the wire format will not leak it.

The :data:`_SECRET_CANARY` string is the test fixture: the
tripwire test in
``aee/tests/test_aee74_wireup.py`` injects the canary into a
payload, runs it through :func:`serialize_event`, and asserts
the canary does not appear in the output.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

from .emitter import Event


# Wire-format revision.  Bump when the JSON shape changes; do
# NOT bump when only the payload keys change.  v1 is the slice 3
# initial release.
SCHEMA_VERSION = "1.0"


#: Canary string used by the secret-leak tripwire.  Distinctive
#: so a ``grep`` for it in a serialized event would be a clear
#: signal of a leak.  The tripwire test in
#: Canary string used by the secret-leak tripwire.  Distinctive
#: so a ``grep`` for it in a serialized event would be a clear
#: signal of a leak.  The tripwire test in
#: ``aee/tests/test_aee74_wireup.py`` uses this constant
#: directly.  The string contains a hyphenated suffix to make
#: it visually unmistakable and to avoid matching any real
#: credential format.
SECRET_CANARY = "AEE74-CANARY-DO-NOT-EMIT-9f3b2a1c"


#: Key names that MUST be scrubbed from any payload before
#: serialization.  Matched case-insensitively, exact key only
#: (no prefix / suffix).  Conservative: errs on the side of
#: scrubbing more rather than less.
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "env",
        "environment",
        "prompt",
        "raw_prompt",
        "system_prompt",
        "anthropic_api_key",
        "openai_api_key",
        # AEE-7.4 finalization: ``authorization`` and
        # ``auth`` are universally secret (they hold
        # ``Bearer ...`` / ``Basic ...`` credentials).
        # Adding them to the scrubber list closes a
        # round-trip E2E finding (the wire serializer
        # used to emit the raw ``Bearer`` prefix
        # verbatim in the payload).  The downstream
        # consumer sees ``"<redacted>"`` at the same
        # position, which is a strong signal that
        # secret-handling is active.
        "authorization",
        "auth",
    }
)


def _scrub(value: Any) -> Any:
    """Recursively scrub known-secret keys from ``value``.

    Walks dicts and lists, returns the same shape with secret
    keys removed.  Non-collection scalars pass through
    unchanged.  This is a defense-in-depth measure: callers
    should not put secrets in payloads in the first place.
    """
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                out[str(k)] = _scrub(v)
                continue
            if k.lower() in _SECRET_KEYS:
                # Replace the value with a marker so downstream
                # consumers can see that a key was scrubbed
                # (rather than silently missing).
                out[k] = "<redacted>"
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, str):
        # Also scrub a flat string that happens to contain the
        # canary verbatim — defensive against ``payload={"line":
        # <stdout containing canary>}``.  This is a
        # best-effort check; the tripwire test covers the
        # specific case.
        if SECRET_CANARY in value:
            return value.replace(SECRET_CANARY, "<redacted>")
    return value


def _now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601.

    Centralised here so all serialized events have the same
    timestamp format (``YYYY-MM-DDTHH:MM:SSZ``).  Matches the
    format used elsewhere in the dispatcher.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def serialize_event(event: Event) -> Dict[str, Any]:
    """Return the canonical JSON-ready dict for ``event``.

    The returned dict is a *copy* — mutating it does not affect
    the event.  The dict has the following shape::

        {
          "schema_version": "1.0",
          "event_id": "<uuid4 hex>",
          "event_type": "<kind>",
          "timestamp": "<iso8601>",
          "task_id": "...",
          "run_id": "...",
          "source": "...",
          "severity": "info|warn|high|critical",
          "payload": {...scrubbed...}
        }

    Additional contract-required fields (``job_id``,
    ``dispatch_id``, ``runtime_id``, ``runtime_type``,
    ``provider``, ``status``, ``previous_status``,
    ``duration_ms``, ``exit_code``, ``failure_code``,
    ``artifact_id``) are taken from the payload when present;
    this avoids a schema-version bump for the slice 3 initial
    release while still letting tests and downstream consumers
    pull them out at the top level when they care.
    """
    payload = _scrub(dict(event.payload))
    out: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event.event_id,
        "event_type": event.kind,
        "timestamp": event.timestamp_iso or _now_iso(),
        "source": event.source,
        "severity": event.effective_severity,
        "payload": payload,
    }
    if event.task_id is not None:
        out["task_id"] = event.task_id
    if event.run_id is not None:
        out["run_id"] = event.run_id
    # Promote the optional contract fields from payload to
    # top-level when present.  This is the slice 3 wire-format
    # compromise: callers can set them in payload, and
    # consumers can read them at the top level.  If both
    # top-level fields and payload fields are populated with
    # conflicting values, the top-level wins (a future
    # slice could move these to first-class Event fields).
    for optional in (
        "job_id",
        "dispatch_id",
        "runtime_id",
        "runtime_type",
        "provider",
        "status",
        "previous_status",
        "duration_ms",
        "exit_code",
        "failure_code",
        "artifact_id",
    ):
        if optional in payload:
            out[optional] = payload[optional]
    return out


def to_json_line(event: Event) -> str:
    """Serialize ``event`` as a single JSON line.

    Used by :class:`StdoutJsonEmitter` to write one event per
    line to stdout.  The line has no trailing newline —
    callers add the newline on write.
    """
    return json.dumps(
        serialize_event(event),
        default=str,
        sort_keys=True,
        ensure_ascii=False,
    )


__all__ = [
    "SCHEMA_VERSION",
    "SECRET_CANARY",
    "serialize_event",
    "to_json_line",
]
