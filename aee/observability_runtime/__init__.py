"""AEE-7.4 slice 2 — event emission protocol and emitter implementations.

This sub-package is the *consumer* of the SOT in
:mod:`aee.observability`.  The SOT defines what an event
*is* (the vocabulary of kinds, categories, severities).
This sub-package defines *how* an event leaves the
process.

Why a separate sub-package
--------------------------
The SOT (``aee/observability/``) was shipped in slice 1 and
is untracked in the working tree.  Slice 2 deliberately does
*not* touch any file in ``aee/observability/`` — that is the
working tree isolation rule (AEE iteration pattern, rule 9).
The seam is the *consume* boundary, not the *mutate*
boundary: this sub-package reads the SOT but does not write
to it.

Public surface (re-exported below for convenience):

* :class:`Event` — frozen dataclass, the unit of emission.
* :class:`EventEmitter` — :class:`typing.Protocol` for any
  sink that can receive an :class:`Event`.
* :class:`NullEmitter` — no-op default, drop-in safe.
* :class:`BufferingEmitter` — in-memory list, test-only.
* :class:`StdoutJsonEmitter` — one JSON object per line, for
  local-dev observability.
* :func:`default_emitter` / :func:`set_default_emitter` —
  process-wide registry.  Starts at :class:`NullEmitter`.

The SOT (kinds, categories, severities) is consumed via
``aee.observability`` — :func:`aee.observability.is_known`
and :func:`aee.observability.severity_for`.  No reverse
imports.
"""
from __future__ import annotations

from .emitter import (
    Event,
    EventEmitter,
    NullEmitter,
    default_emitter,
    set_default_emitter,
)
from .buffer_emitter import BufferingEmitter
from .stdout_emitter import StdoutJsonEmitter
from .serialization import (
    SCHEMA_VERSION,
    SECRET_CANARY,
    serialize_event,
    to_json_line,
)
from .env_config import ENV_VAR_NAME, emitter_from_env
from .wireup import (
    EMITTER_SOURCE_DISPATCHER,
    EMITTER_SOURCE_ORCHESTRATOR,
    install,
    uninstall,
    is_installed,
)

__all__ = [
    "BufferingEmitter",
    "EMITTER_SOURCE_DISPATCHER",
    "EMITTER_SOURCE_ORCHESTRATOR",
    "ENV_VAR_NAME",
    "Event",
    "EventEmitter",
    "NullEmitter",
    "SCHEMA_VERSION",
    "SECRET_CANARY",
    "StdoutJsonEmitter",
    "default_emitter",
    "emitter_from_env",
    "install",
    "is_installed",
    "serialize_event",
    "set_default_emitter",
    "to_json_line",
    "uninstall",
]
