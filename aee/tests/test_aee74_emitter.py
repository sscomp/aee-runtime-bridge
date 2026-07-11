"""AEE-7.4 slice 2 — Emitter protocol + emitters test suite.

This module pins the contract for the new
``aee.observability_runtime`` sub-package.  It is the
regression home for the slice; if a future session changes
any of the five invariants below without updating this
file, the tests will catch it.

Invariants pinned
-----------------

1. **Event validation** — passing an unknown kind raises
   ``ValueError`` at construction time.  Only
   ``bypass_sot_check=True`` may skip the check, and that
   flag is reserved for the K+2 wire-up slice.
2. **Event frozen-ness** — once constructed, neither the
   field values nor the payload may be mutated.  This
   protects the post-emission audit trail.
3. **Event severity auto-lookup** — when ``severity`` is
   ``None``, ``effective_severity`` reads from the SOT;
   ``intent_mismatch`` resolves to ``"high"``,
   ``delivery_unverified`` to ``"warn"``, etc.
4. **EventEmitter Protocol** — duck typing works.  Any
   class with ``emit(event)`` and ``close()`` is an
   ``EventEmitter`` even without inheritance.
5. **Process-wide registry idempotency** —
   ``default_emitter()`` returns a fresh ``NullEmitter``
   on first call; ``set_default_emitter()`` swaps it
   in-place.  Tests that swap must call the previous
   emitter's ``close()`` themselves; the registry does
   not auto-close.

Test groups (stdlib ``unittest`` only, no pytest):

* Group A — :class:`Event` shape and validation (12 cases)
* Group B — :class:`Event` frozen + payload immutability (5 cases)
* Group C — :class:`Event` severity lookup (6 cases)
* Group D — :class:`Event` to_dict round-trip (4 cases)
* Group E — :class:`EventEmitter` Protocol (5 cases)
* Group F — :class:`NullEmitter` (4 cases)
* Group G — :class:`BufferingEmitter` (8 cases)
* Group H — :class:`StdoutJsonEmitter` (8 cases)
* Group I — registry (4 cases)
* Group J — SOT integration (3 cases)

Total: 59 tests.  Target runtime: < 1 second.
"""
from __future__ import annotations

import io
import json
import unittest
from typing import Any, Mapping

from aee.observability import EventCategory, EventKind, EventSeverity
from aee.observability_runtime import (
    BufferingEmitter,
    Event,
    EventEmitter,
    NullEmitter,
    StdoutJsonEmitter,
    default_emitter,
    set_default_emitter,
)


# ---------------------------------------------------------------------------
# Group A — Event shape and validation
# ---------------------------------------------------------------------------


class TestEventValidation(unittest.TestCase):
    """Unknown kind must raise at construction time."""

    def test_known_kind_succeeds(self) -> None:
        """A kind that is in ``EventKind`` constructs cleanly."""
        evt = Event(kind="completed", source="test")
        self.assertEqual(evt.kind, "completed")

    def test_all_seventeen_kinds_construct(self) -> None:
        """Every SOT kind is a valid ``Event.kind``.

        The SOT count grew from 17 (slice 1) to 22 (slice
        3) when we added the 5 ORCHESTRATOR kinds.  AEE-7.4
        finalization grew it to 23 by adding CLAIMED to
        LIFECYCLE.  This test pins the *current* count;
        a future shrink or unplanned growth fails the
        assertion.
        """
        from aee.observability import EventKind as _EK
        self.assertEqual(len(_EK.all()), 23)
        for kind in _EK.all():
            Event(kind=kind, source="test")

    def test_unknown_kind_raises(self) -> None:
        """An unknown kind raises ``ValueError``."""
        with self.assertRaises(ValueError) as cm:
            Event(kind="not_a_real_kind", source="test")
        self.assertIn("not in the AEE-7.4 SOT", str(cm.exception))

    def test_empty_kind_raises(self) -> None:
        """An empty-string kind raises ``ValueError`` (truthiness check)."""
        with self.assertRaises(ValueError):
            Event(kind="", source="test")

    def test_bypass_sot_check_skips_validation(self) -> None:
        """``bypass_sot_check=True`` permits unknown kinds (K+2 reserved)."""
        # This is the only path to construct a non-SOT
        # kind; reserved for the wire-up slice.
        evt = Event(
            kind="future_kind", source="test", bypass_sot_check=True
        )
        self.assertEqual(evt.kind, "future_kind")

    def test_bypass_sot_check_does_not_skip_empty(self) -> None:
        """``bypass_sot_check=True`` does NOT skip the empty-string check.

        The check is ``if not self.kind`` — falsy strings
        (including empty) still raise.  This is intentional
        because the SOT cannot help classify an empty kind.
        """
        with self.assertRaises(ValueError):
            Event(kind="", source="test", bypass_sot_check=True)

    def test_source_defaults_to_unknown(self) -> None:
        """No source → ``"unknown"`` (defensive default)."""
        evt = Event(kind="completed")
        self.assertEqual(evt.source, "unknown")

    def test_task_id_and_run_id_default_none(self) -> None:
        """Both ids default to ``None``."""
        evt = Event(kind="completed", source="test")
        self.assertIsNone(evt.task_id)
        self.assertIsNone(evt.run_id)

    def test_payload_default_is_empty(self) -> None:
        """No payload → empty ``MappingProxyType``."""
        evt = Event(kind="completed", source="test")
        # MappingProxyType behaves like a dict for reads.
        self.assertEqual(len(evt.payload), 0)

    def test_explicit_payload_preserved(self) -> None:
        """A dict payload survives construction."""
        payload = {"duration_sec": 12.3, "tokens": 4096}
        evt = Event(kind="completed", source="test", payload=payload)
        self.assertEqual(dict(evt.payload), payload)

    def test_kind_is_intent_mismatch_for_high(self) -> None:
        """``intent_mismatch`` is the SOT's only HIGH kind today.

        Future additions of HIGH kinds are tracked by
        ``test_aee74_observability.py::TestSeverityShape``
        in slice 1; this test pins the *count* of HIGH
        kinds so the contract is explicit.
        """
        # 17 kinds total; 1 is HIGH (intent_mismatch).
        severities = {
            k: EventSeverity(sev)
            for k, sev in __import__(
                "aee.observability.events", fromlist=["_SEVERITY_FOR_KIND"]
            )._SEVERITY_FOR_KIND.items()
        }
        high_kinds = sorted(k for k, s in severities.items() if s == EventSeverity.HIGH)
        self.assertEqual(high_kinds, ["intent_mismatch"])

    def test_kind_is_delivery_unverified_for_warn(self) -> None:
        """``delivery_unverified`` is WARN — the SOT's only DELIVERY kind."""
        from aee.observability.events import _CATEGORY_FOR_KIND

        delivery_kinds = sorted(
            k for k, c in _CATEGORY_FOR_KIND.items()
            if c == EventCategory.DELIVERY.value
        )
        self.assertEqual(delivery_kinds, ["delivery_unverified"])


# ---------------------------------------------------------------------------
# Group B — Event frozen + payload immutability
# ---------------------------------------------------------------------------


class TestEventFrozen(unittest.TestCase):
    """``Event`` is a frozen dataclass; the payload is a MappingProxyType."""

    def test_setattr_kind_raises(self) -> None:
        """Assigning to ``kind`` after construction raises ``FrozenInstanceError``."""
        evt = Event(kind="completed", source="test")
        with self.assertRaises(Exception):
            # ``FrozenInstanceError`` is a subclass of
            # ``AttributeError``; we catch the broader
            # exception to stay defensive against the
            # exact class name across dataclass versions.
            evt.kind = "failed"  # type: ignore[misc]

    def test_setattr_source_raises(self) -> None:
        """Assigning to ``source`` after construction raises."""
        evt = Event(kind="completed", source="test")
        with self.assertRaises(Exception):
            evt.source = "other"  # type: ignore[misc]

    def test_payload_mutation_raises(self) -> None:
        """Mutating ``payload`` (a ``MappingProxyType``) raises ``TypeError``."""
        evt = Event(kind="completed", source="test", payload={"a": 1})
        with self.assertRaises(TypeError):
            evt.payload["a"] = 2  # type: ignore[index]

    def test_payload_does_not_alias_input_dict(self) -> None:
        """Mutating the input dict after construction does NOT affect the event."""
        original = {"a": 1}
        evt = Event(kind="completed", source="test", payload=original)
        original["a"] = 999
        # The event captured the *value* at construction time.
        self.assertEqual(dict(evt.payload), {"a": 1})

    def test_event_is_hashable(self) -> None:
        """Frozen dataclass + MappingProxyType → Event is hashable.

        Two events with the same fields hash equal.  This
        is a regression for "the audit trail breaks when an
        Event ends up in a set or as a dict key".  Slice 3
        added event_id and timestamp_iso; we pass them
        explicitly so the two events hash equal.
        """
        a = Event(
            kind="completed", source="t", task_id="T-1",
            event_id="abc123def456", timestamp_iso="2026-01-01T00:00:00Z",
        )
        b = Event(
            kind="completed", source="t", task_id="T-1",
            event_id="abc123def456", timestamp_iso="2026-01-01T00:00:00Z",
        )
        self.assertEqual(hash(a), hash(b))
        self.assertIn(a, {a, b})  # set membership check


# ---------------------------------------------------------------------------
# Group C — Event severity lookup
# ---------------------------------------------------------------------------


class TestEventSeverityLookup(unittest.TestCase):
    """``effective_severity`` reads from the SOT when ``severity`` is None."""

    def test_intent_mismatch_is_high(self) -> None:
        """``intent_mismatch`` resolves to ``EventSeverity.HIGH``."""
        evt = Event(kind="intent_mismatch", source="test")
        self.assertEqual(evt.effective_severity, "high")

    def test_delivery_unverified_is_warn(self) -> None:
        """``delivery_unverified`` resolves to ``EventSeverity.WARN``."""
        evt = Event(kind="delivery_unverified", source="test")
        self.assertEqual(evt.effective_severity, "warn")

    def test_completed_is_info(self) -> None:
        """``completed`` resolves to ``EventSeverity.INFO``."""
        evt = Event(kind="completed", source="test")
        self.assertEqual(evt.effective_severity, "info")

    def test_explicit_severity_overrides(self) -> None:
        """An explicit ``severity`` field wins over the SOT default."""
        # ``intent_mismatch`` would default to "high", but
        # we override to "info" (legal use case: a low-risk
        # intent mismatch flagged for review).
        evt = Event(
            kind="intent_mismatch",
            source="test",
            severity="info",
        )
        self.assertEqual(evt.effective_severity, "info")

    def test_bypassed_unknown_kind_falls_back_to_info(self) -> None:
        """``bypass_sot_check=True`` + unknown kind → ``effective_severity`` falls back to ``"info"``.

        The SOT cannot classify a non-SOT kind, so the
        fallback is the safe default.
        """
        evt = Event(
            kind="future_kind", source="test", bypass_sot_check=True
        )
        self.assertEqual(evt.effective_severity, "info")

    def test_traversal_is_info(self) -> None:
        """``traversal`` is INFO (observe-only audit row)."""
        evt = Event(kind="traversal", source="test")
        self.assertEqual(evt.effective_severity, "info")


# ---------------------------------------------------------------------------
# Group D — Event to_dict round-trip
# ---------------------------------------------------------------------------


class TestEventToDict(unittest.TestCase):
    """``Event.to_dict()`` produces a JSON-friendly dict."""

    def test_to_dict_keys(self) -> None:
        """The dict has the 8 expected keys (slice 3 added event_id + timestamp_iso)."""
        evt = Event(kind="completed", source="dispatcher", task_id="T-1")
        d = evt.to_dict()
        self.assertEqual(
            set(d.keys()),
            {
                "kind", "payload", "source", "task_id", "run_id", "severity",
                "event_id", "timestamp_iso",
            },
        )

    def test_to_dict_payload_is_mutable_copy(self) -> None:
        """``payload`` in the dict is a fresh mutable copy, not a view."""
        evt = Event(kind="completed", source="test", payload={"a": 1})
        d = evt.to_dict()
        # Mutating the copy does not affect the event.
        d["payload"]["a"] = 999
        self.assertEqual(dict(evt.payload), {"a": 1})

    def test_to_dict_serializes_to_json(self) -> None:
        """The dict round-trips through ``json.dumps`` cleanly."""
        evt = Event(
            kind="completed",
            source="dispatcher",
            task_id="T-1",
            payload={"duration_sec": 12.3, "ok": True},
        )
        text = json.dumps(evt.to_dict(), ensure_ascii=False)
        parsed = json.loads(text)
        self.assertEqual(parsed["kind"], "completed")
        self.assertEqual(parsed["task_id"], "T-1")
        self.assertEqual(parsed["payload"]["duration_sec"], 12.3)

    def test_to_dict_with_none_ids(self) -> None:
        """``None`` task_id / run_id survive round-trip."""
        evt = Event(kind="completed", source="test")
        d = evt.to_dict()
        self.assertIsNone(d["task_id"])
        self.assertIsNone(d["run_id"])


# ---------------------------------------------------------------------------
# Group E — EventEmitter Protocol
# ---------------------------------------------------------------------------


class TestEventEmitterProtocol(unittest.TestCase):
    """The Protocol is structural (duck typing)."""

    def test_null_emitter_is_emitter(self) -> None:
        """``NullEmitter`` is an ``EventEmitter`` (runtime checkable)."""
        self.assertIsInstance(NullEmitter(), EventEmitter)

    def test_buffering_emitter_is_emitter(self) -> None:
        """``BufferingEmitter`` is an ``EventEmitter``."""
        self.assertIsInstance(BufferingEmitter(), EventEmitter)

    def test_stdout_emitter_is_emitter(self) -> None:
        """``StdoutJsonEmitter`` is an ``EventEmitter``."""
        stream = io.StringIO()
        self.assertIsInstance(StdoutJsonEmitter(stream=stream), EventEmitter)

    def test_custom_class_with_emit_and_close_is_emitter(self) -> None:
        """A custom class with ``emit`` + ``close`` is structurally an emitter (no inheritance needed)."""

        class MyCustom:
            def emit(self, event: Event) -> None:
                pass

            def close(self) -> None:
                pass

        self.assertIsInstance(MyCustom(), EventEmitter)

    def test_class_without_close_is_not_emitter(self) -> None:
        """A class missing ``close`` is NOT an ``EventEmitter``."""

        class MissingClose:
            def emit(self, event: Event) -> None:
                pass

        self.assertNotIsInstance(MissingClose(), EventEmitter)


# ---------------------------------------------------------------------------
# Group F — NullEmitter
# ---------------------------------------------------------------------------


class TestNullEmitter(unittest.TestCase):
    """``NullEmitter`` is a no-op."""

    def test_emit_returns_none(self) -> None:
        """``emit`` returns ``None`` and does nothing visible."""
        e = NullEmitter()
        result = e.emit(Event(kind="completed", source="t"))
        self.assertIsNone(result)

    def test_close_returns_none(self) -> None:
        """``close`` returns ``None``."""
        e = NullEmitter()
        self.assertIsNone(e.close())

    def test_close_idempotent(self) -> None:
        """``close`` called twice does not raise."""
        e = NullEmitter()
        e.close()
        e.close()  # second call must not raise

    def test_emit_does_not_raise_for_high_severity(self) -> None:
        """A HIGH-severity event is silently dropped (no escalation side-effect)."""
        e = NullEmitter()
        # If NullEmitter did anything, it would be a
        # regression — the bridge relies on "no emitter
        # configured → silent no-op" as a safety net.
        e.emit(Event(kind="intent_mismatch", source="t", task_id="T-1"))


# ---------------------------------------------------------------------------
# Group G — BufferingEmitter
# ---------------------------------------------------------------------------


class TestBufferingEmitter(unittest.TestCase):
    """In-memory buffer; FIFO; bounded by maxlen."""

    def test_empty_buffer_has_len_zero(self) -> None:
        e = BufferingEmitter()
        self.assertEqual(len(e), 0)
        self.assertEqual(e.events, [])

    def test_emit_appends(self) -> None:
        """``emit`` appends to the buffer in order."""
        e = BufferingEmitter()
        e.emit(Event(kind="created", source="t"))
        e.emit(Event(kind="completed", source="t"))
        self.assertEqual(len(e), 2)
        self.assertEqual([ev.kind for ev in e.events], ["created", "completed"])

    def test_count_by_kind(self) -> None:
        """``count_by_kind`` returns the number of events with that kind."""
        e = BufferingEmitter()
        e.emit(Event(kind="created", source="t"))
        e.emit(Event(kind="completed", source="t"))
        e.emit(Event(kind="completed", source="t"))
        self.assertEqual(e.count_by_kind("created"), 1)
        self.assertEqual(e.count_by_kind("completed"), 2)
        self.assertEqual(e.count_by_kind("failed"), 0)

    def test_clear_empties_buffer(self) -> None:
        """``clear`` removes all events."""
        e = BufferingEmitter()
        e.emit(Event(kind="created", source="t"))
        e.clear()
        self.assertEqual(len(e), 0)

    def test_maxlen_drops_oldest(self) -> None:
        """When the buffer is full, the oldest event is dropped (FIFO)."""
        e = BufferingEmitter(maxlen=2)
        # Use SOT kinds so the Event constructor's
        # SOT-validation step does not reject them.
        e.emit(Event(kind="created", source="t", task_id="T-1"))
        e.emit(Event(kind="started", source="t", task_id="T-1"))
        e.emit(Event(kind="completed", source="t", task_id="T-1"))  # drops "created"
        kinds = [ev.kind for ev in e.events]
        self.assertEqual(kinds, ["started", "completed"])

    def test_maxlen_zero_raises(self) -> None:
        """``maxlen <= 0`` raises ``ValueError`` at construction time."""
        with self.assertRaises(ValueError):
            BufferingEmitter(maxlen=0)
        with self.assertRaises(ValueError):
            BufferingEmitter(maxlen=-5)

    def test_maxlen_none_is_unbounded(self) -> None:
        """``maxlen=None`` does not drop events."""
        e = BufferingEmitter(maxlen=None)
        for i in range(100):
            e.emit(Event(kind="created", source="t", task_id=f"T-{i}"))
        self.assertEqual(len(e), 100)

    def test_events_property_returns_fresh_list(self) -> None:
        """``events`` returns a fresh list, not a live view."""
        e = BufferingEmitter()
        e.emit(Event(kind="created", source="t"))
        snapshot = e.events
        snapshot.append(Event(kind="completed", source="t"))
        # Mutating the snapshot does not affect the buffer.
        self.assertEqual(len(e), 1)


# ---------------------------------------------------------------------------
# Group H — StdoutJsonEmitter
# ---------------------------------------------------------------------------


class TestStdoutJsonEmitter(unittest.TestCase):
    """Writes one JSON object per line to the stream."""

    def setUp(self) -> None:
        self.stream = io.StringIO()
        self.emitter = StdoutJsonEmitter(
            stream=self.stream, add_timestamp=False
        )

    def test_emit_writes_one_line(self) -> None:
        """``emit`` writes exactly one line per event."""
        self.emitter.emit(Event(kind="completed", source="t"))
        self.assertEqual(self.stream.getvalue().count("\n"), 1)

    def test_emit_line_is_valid_json(self) -> None:
        """The written line parses as JSON."""
        self.emitter.emit(
            Event(
                kind="completed",
                source="dispatcher",
                task_id="T-1",
                payload={"duration_sec": 12.3},
            )
        )
        line = self.stream.getvalue().strip()
        parsed = json.loads(line)
        self.assertEqual(parsed["kind"], "completed")
        self.assertEqual(parsed["source"], "dispatcher")
        self.assertEqual(parsed["task_id"], "T-1")
        self.assertEqual(parsed["payload"]["duration_sec"], 12.3)

    def test_emit_includes_category(self) -> None:
        """The line includes ``category`` resolved from the SOT."""
        self.emitter.emit(Event(kind="intent_mismatch", source="t"))
        parsed = json.loads(self.stream.getvalue().strip())
        self.assertEqual(parsed["category"], "intent")

    def test_emit_includes_severity(self) -> None:
        """The line includes ``severity`` from the auto-lookup."""
        self.emitter.emit(Event(kind="intent_mismatch", source="t"))
        parsed = json.loads(self.stream.getvalue().strip())
        self.assertEqual(parsed["severity"], "high")

    def test_multiple_events_write_multiple_lines(self) -> None:
        """Three events produce three newline-terminated lines."""
        for kind in ("created", "started", "completed"):
            self.emitter.emit(Event(kind=kind, source="t"))
        lines = self.stream.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        # Each line is independently parseable.
        for line, kind in zip(lines, ("created", "started", "completed")):
            self.assertEqual(json.loads(line)["kind"], kind)

    def test_add_timestamp_true_writes_ts(self) -> None:
        """When ``add_timestamp=True``, the line has a ``ts`` key."""
        e = StdoutJsonEmitter(stream=self.stream, add_timestamp=True)
        e.emit(Event(kind="completed", source="t"))
        parsed = json.loads(self.stream.getvalue().strip())
        self.assertIn("ts", parsed)
        # ISO-8601 with timezone (Z or +00:00).
        self.assertRegex(parsed["ts"], r"^\d{4}-\d{2}-\d{2}T")

    def test_close_idempotent(self) -> None:
        """``close`` is idempotent (no error on second call)."""
        self.emitter.close()
        self.emitter.close()

    def test_emit_after_close_raises(self) -> None:
        """``emit`` after ``close`` raises ``RuntimeError``."""
        self.emitter.close()
        with self.assertRaises(RuntimeError):
            self.emitter.emit(Event(kind="completed", source="t"))


# ---------------------------------------------------------------------------
# Group I — process-wide registry
# ---------------------------------------------------------------------------


class TestRegistry(unittest.TestCase):
    """The default-emitter registry is process-wide and swappable."""

    def setUp(self) -> None:
        # Snapshot + restore so tests don't leak state.
        self._previous = default_emitter()

    def tearDown(self) -> None:
        # Restore the original (call close on the test's
        # emitter, then put the original back).
        current = default_emitter()
        if current is not self._previous:
            try:
                current.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
            set_default_emitter(self._previous)

    def test_default_is_null_emitter_on_first_call(self) -> None:
        """A fresh process sees ``NullEmitter`` as the default.

        The test runner may have side-effects from other
        tests, so we force-reset to ``NullEmitter`` first.
        """
        set_default_emitter(NullEmitter())
        self.assertIsInstance(default_emitter(), NullEmitter)

    def test_set_default_emitter_swaps(self) -> None:
        """``set_default_emitter`` replaces the registry entry in place."""
        buf = BufferingEmitter()
        set_default_emitter(buf)
        self.assertIs(default_emitter(), buf)

    def test_set_default_emitter_to_null(self) -> None:
        """``set_default_emitter(NullEmitter())`` is the canonical reset."""
        buf = BufferingEmitter()
        set_default_emitter(buf)
        set_default_emitter(NullEmitter())
        self.assertIsInstance(default_emitter(), NullEmitter)
        # The previous BufferingEmitter still works (the
        # registry does not auto-close; the test that
        # swaps is responsible for that — see tearDown).
        buf.emit(Event(kind="completed", source="t"))
        self.assertEqual(len(buf), 1)

    def test_default_emitter_returns_same_instance(self) -> None:
        """``default_emitter()`` returns the same object on every call."""
        buf = BufferingEmitter()
        set_default_emitter(buf)
        self.assertIs(default_emitter(), default_emitter())


# ---------------------------------------------------------------------------
# Group J — SOT integration
# ---------------------------------------------------------------------------


class TestSOTIntegration(unittest.TestCase):
    """The new sub-package interoperates with the slice 1 SOT."""

    def test_every_sot_kind_can_be_constructed_as_event(self) -> None:
        """All SOT kinds produce a valid ``Event``.

        Slice 3 grew the SOT from 17 → 22 by adding
        the ORCHESTRATOR category's 5 kinds.  AEE-7.4
        finalization grew it 22 → 23 by adding CLAIMED
        to LIFECYCLE.  This test is the lock-step
        assertion that the SOT and ``Event`` agree on
        the inventory.
        """
        from aee.observability import EventKind as _EK
        self.assertEqual(len(_EK.all()), 23)
        for kind in _EK.all():
            evt = Event(kind=kind, source="t")
            self.assertEqual(evt.kind, kind)

    def test_buffering_emitter_preserves_severity(self) -> None:
        """A buffered ``intent_mismatch`` event still has ``effective_severity == "high"``.

        This guards against a future refactor that drops
        the severity at emit time.
        """
        buf = BufferingEmitter()
        buf.emit(Event(kind="intent_mismatch", source="t", task_id="T-1"))
        self.assertEqual(len(buf), 1)
        self.assertEqual(buf.events[0].effective_severity, "high")

    def test_stdout_emitter_resolves_category_for_all_kinds(self) -> None:
        """Every SOT kind round-trips through ``StdoutJsonEmitter`` with a non-null category.

        This is the end-to-end "the consumer is reading
        from the SOT correctly" check — if a future SOT
        addition forgets to update ``_CATEGORY_FOR_KIND``,
        this test fails before the SOT-side tripwire does.
        """
        stream = io.StringIO()
        e = StdoutJsonEmitter(stream=stream, add_timestamp=False)
        from aee.observability import EventKind as _EK
        for kind in _EK.all():
            e.emit(Event(kind=kind, source="t"))
        lines = stream.getvalue().splitlines()
        # AEE-7.4 finalization: 22 → 23 (CLAIMED added to LIFECYCLE).
        self.assertEqual(len(lines), 23)
        for line, kind in zip(lines, _EK.all()):
            parsed = json.loads(line)
            self.assertEqual(parsed["kind"], kind)
            self.assertIsNotNone(
                parsed["category"],
                f"kind={kind!r} produced category=None — SOT "
                "_CATEGORY_FOR_KIND is missing an entry",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
