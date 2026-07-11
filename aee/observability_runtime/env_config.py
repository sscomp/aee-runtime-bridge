"""AEE-7.4 slice 3 — env-driven emitter selection.

The bridge can be configured to install a non-default emitter
via the ``AEE_EVENT_EMITTER`` environment variable.  This module
parses the env var and returns the matching emitter instance.

Supported values
----------------
* ``"null"`` (default) — :class:`NullEmitter` (no-op).
* ``"buffer"`` — :class:`BufferingEmitter` (in-memory list,
  test-only).
* ``"stdout_json"`` — :class:`StdoutJsonEmitter` (one JSON
  object per line on stdout).

Anything else is treated as a fail-safe: a warning is logged
and :class:`NullEmitter` is returned.  This means a typo in
the env var (e.g. ``"stdout"``) never blocks the bridge from
starting; observability silently degrades to off.

Why env-var config
------------------
Slice 2 ships a process-wide :func:`default_emitter` registry
that any module can call.  For production wiring, the *easiest*
configuration surface is an env var: no code change, no API
call, no startup-order coupling.  An alternative (a config
file) would mean parsing YAML, which is out of scope for slice 3
— slice 2 deliberately kept the protocol config-free.

Usage
-----
The recommended wiring is at module load time of the
application entry point::

    # app.py at import time
    from aee.observability_runtime.env_config import emitter_from_env
    from aee.observability_runtime import set_default_emitter
    set_default_emitter(emitter_from_env())

This is *safe at import time* because every code path that
emits an event calls :func:`default_emitter`, and the
registry is mutable.  Tests call
:func:`set_default_emitter` in ``setUp`` to override.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from .emitter import EventEmitter, NullEmitter
from .buffer_emitter import BufferingEmitter
from .stdout_emitter import StdoutJsonEmitter


log = logging.getLogger("aee.observability_runtime.env_config")


#: Environment variable name.  Pinned here so it appears in
#: one place and the tripwire test can refer to it.
ENV_VAR_NAME = "AEE_EVENT_EMITTER"


#: Mapping of string -> emitter factory.  Factories are
#: callables (no-arg) so the emitter is fresh per call —
#: important for :class:`StdoutJsonEmitter` which holds
#: mutable state (the bound stdout stream).
_FACTORIES = {
    "null": lambda: NullEmitter(),
    "buffer": lambda: BufferingEmitter(),
    "stdout_json": lambda: StdoutJsonEmitter(),
}


def _normalize(name: str) -> str:
    """Normalize an env-var string to the canonical form.

    Lowercase, strip whitespace, drop empty.  A user-set
    ``"  Stdout_JSON  "`` becomes ``"stdout_json"``.
    """
    return (name or "").strip().lower()


def emitter_from_env(env_var: Optional[str] = None) -> EventEmitter:
    """Return the emitter named by the env var, or NullEmitter.

    ``env_var`` defaults to :data:`ENV_VAR_NAME`.  Callers
    may pass a different name for testing.

    Fail-safe behavior: unknown names log a warning and
    return :class:`NullEmitter`.  The bridge MUST NOT fail
    to start because of a bad emitter config.
    """
    raw = os.environ.get(env_var if env_var is not None else ENV_VAR_NAME, "")
    name = _normalize(raw)
    if not name:
        # Unset or empty -> default to null (silent).
        return NullEmitter()
    factory = _FACTORIES.get(name)
    if factory is None:
        # Unknown -> warn + fall back to null (fail-safe).
        log.warning(
            "AEE-7.4 env_config: unknown emitter name %r (env=%s); "
            "valid names: %s; falling back to NullEmitter",
            name,
            ENV_VAR_NAME,
            sorted(_FACTORIES),
        )
        return NullEmitter()
    return factory()


__all__ = [
    "ENV_VAR_NAME",
    "emitter_from_env",
]
