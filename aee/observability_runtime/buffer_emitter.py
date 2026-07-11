"""AEE-7.4 slice 2 — in-memory BufferingEmitter for tests.

Why a test-only emitter in production source
--------------------------------------------
Putting :class:`BufferingEmitter` in the production
package (rather than ``aee/tests/``) lets *future* code
paths use it as a *real* sink — e.g. a debug flag in
production that records the last 1000 events for a
post-mortem.  The class is small, the API is stable, and
the cost of having it on the import path is one Python
file.

Why bounded
-----------
Unbounded growth is a memory leak waiting to happen.  If
``maxlen`` is set, the oldest event is dropped (FIFO) when
the buffer is full.  ``maxlen=None`` means unbounded, used
only when the caller is sure of the event volume.
"""
from __future__ import annotations

from collections import deque
from typing import Deque, Optional

from .emitter import Event, EventEmitter


class BufferingEmitter:
    """In-memory emitter.  Append :class:`Event` to a deque.

    Parameters
    ----------
    maxlen
        Maximum number of events to retain.  When the
        buffer is full, the *oldest* event is dropped.
        ``None`` (the default) means unbounded.

    Notes
    -----
    The implementation is **not** thread-safe.  The bridge
    runs each task in a single thread, so this is fine
    today.  If a future slice needs concurrent emission,
    wrap the buffer in a ``threading.Lock``.
    """

    def __init__(self, maxlen: Optional[int] = None) -> None:
        if maxlen is not None and maxlen <= 0:
            raise ValueError(
                f"BufferingEmitter maxlen must be positive or None, "
                f"got {maxlen!r}"
            )
        self._maxlen = maxlen
        self._events: Deque[Event] = deque(maxlen=maxlen)

    def emit(self, event: Event) -> None:
        """Append ``event`` to the buffer.

        ``event`` is stored as-is (frozen dataclass), not
        copied.  Callers MUST NOT mutate the event after
        handing it over; doing so would corrupt the
        post-emission audit trail.
        """
        self._events.append(event)

    def close(self) -> None:
        """Idempotent no-op (no resources to release)."""
        return None

    @property
    def events(self) -> list:
        """Snapshot of the current buffer, oldest-first.

        Returns a fresh ``list`` each call (not a view) so
        callers can iterate without worrying about the
        deque mutating underneath them.
        """
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def clear(self) -> None:
        """Empty the buffer."""
        self._events.clear()

    def count_by_kind(self, kind: str) -> int:
        """How many events in the buffer have ``kind`` equal to ``kind``.

        Used by tests to assert e.g. ``count_by_kind("completed") == 1``
        without scanning the buffer manually.
        """
        return sum(1 for e in self._events if e.kind == kind)
