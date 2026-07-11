"""AEE-7.4 slice 2 — StdoutJsonEmitter for local-dev observability.

Why stdout, not a file
----------------------
``stdout`` is the universal sink for daemon processes:
journald, docker logs, ``nohup foo > log 2>&1``, and
``hermes send --to telegram:...`` all read from it.  By
writing one JSON object per line (the "ndjson" format),
the output is:

* grep-able (``grep '"kind":"intent_mismatch"' bridge.log``)
* parseable (``jq 'select(.severity=="high")'``)
* appendable (no header / no truncation hazard)

A future slice can add a :class:`FileJsonEmitter` that
shares the same JSON shape — the *format* is the contract,
not the *sink*.

Why ``TextIOBase`` not ``sys.stdout``
-------------------------------------
Tests need to capture the output.  Passing a writable
``io.StringIO`` lets a test assert exact bytes without
monkey-patching ``sys.stdout``.  The default is
``sys.stdout`` for production use.
"""
from __future__ import annotations

import json
import sys
from typing import IO, Optional

from .emitter import Event, EventEmitter


class StdoutJsonEmitter:
    """Emit one JSON object per event to a writable.

    Parameters
    ----------
    stream
        Anything with a ``.write(str) -> int`` method.
        Defaults to :data:`sys.stdout`.  Pass
        ``io.StringIO()`` in tests to capture.
    add_timestamp
        When ``True`` (the default), every line has a
        top-level ``"ts"`` field with an ISO-8601
        timestamp.  Tests that compare exact output pass
        ``add_timestamp=False`` to avoid flaky
        second-by-second drift.

    Output format (one line per event)
    ----------------------------------
    ``{"ts": "2026-07-11T08:00:00.000+00:00", "kind":
    "completed", "category": "lifecycle", "severity":
    "info", "source": "dispatcher", "task_id": "...", "run_id":
    null, "payload": {...}}``

    ``category`` is included for orchestrator-side filter
    convenience (the SOT exposes :func:`category_for`).
    """

    def __init__(
        self,
        stream: Optional[IO[str]] = None,
        add_timestamp: bool = True,
    ) -> None:
        self._stream: Optional[IO[str]] = (
            stream if stream is not None else sys.stdout
        )
        self._add_timestamp = add_timestamp

    def emit(self, event: Event) -> None:
        """Serialize ``event`` to JSON and write one line to the stream.

        The line is flushed (no buffering) so a crashed
        process does not lose the last event.  JSON encoding
        is the default ``json.dumps`` (no custom encoder)
        because the payload is constrained to JSON-friendly
        primitives by the upstream call sites.
        """
        from aee.observability import category_for  # noqa: WPS433

        if self._stream is None:
            raise RuntimeError(
                "StdoutJsonEmitter.emit called after close(); "
                "this is a programming error in the caller"
            )
        category = category_for(event.kind)
        line = {
            "kind": event.kind,
            "category": category.value if category is not None else None,
            "severity": event.effective_severity,
            "source": event.source,
            "task_id": event.task_id,
            "run_id": event.run_id,
            "payload": dict(event.payload),
        }
        if self._add_timestamp:
            from datetime import datetime, timezone

            line["ts"] = datetime.now(timezone.utc).isoformat()
        text = json.dumps(line, ensure_ascii=False, sort_keys=True)
        self._stream.write(text + "\n")
        # Flush so a SIGKILL does not lose the last event.
        flush = getattr(self._stream, "flush", None)
        if callable(flush):
            flush()

    def close(self) -> None:
        """Flush and detach the stream.

        Does **not** close the underlying stream (we may
        not own it — e.g. ``sys.stdout``).  Idempotent.
        After ``close()`` returns, any subsequent
        :meth:`emit` raises :class:`RuntimeError`.
        """
        if self._stream is not None:
            flush = getattr(self._stream, "flush", None)
            if callable(flush):
                flush()
            self._stream = None
