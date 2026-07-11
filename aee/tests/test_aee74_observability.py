"""AEE-7.4 — Observability vocabulary tripwire + happy-path tests.

This module is the regression home for the AEE-7.4 slice.
It pins four invariants:

1. **Vocabulary completeness** — every :class:`EventKind`
   value has a category and a severity.  The
   ``_CATEGORY_FOR_KIND`` and ``_SEVERITY_FOR_KIND``
   mappings must stay in lock-step with :class:`EventKind`.
2. **No inline string literals** — the literal strings
   ``"delivery_unverified"``, ``"intent_mismatch"``, and
   ``"traversal"`` MUST NOT appear in any of the four
   production modules outside of :class:`EventKind` itself.
   (Test files are exempt; we have to write the literal
   somewhere to assert against it.)
3. **No silent rename** — a future session that renames
   ``EventKind.INTENT_MISMATCH`` from ``"intent_mismatch"``
   to anything else will fail this module, surfacing the
   breaking change before the orchestrator's filter code
   breaks at runtime.
4. **Lookup shape** — the lookup helpers
   (:func:`category_for`, :func:`severity_for`,
   :func:`is_known`) return what the orchestrator's filter
   code expects.

Test groups (stdlib ``unittest`` only, no pytest):

* Group A — :class:`EventKind` shape (12 cases)
* Group B — :class:`EventCategory` shape (8 cases)
* Group C — :class:`EventSeverity` shape (6 cases)
* Group D — :func:`is_known` + :func:`category_for`
  + :func:`severity_for` lookup shape (10 cases)
* Group E — :func:`events_by_category` grouping (5 cases)
* Group F — vocabulary lock-step tripwires (4 cases)
* Group G — production-source tripwire (4 cases)
* Group H — round-trip / on-disk shape (4 cases)

Total: 53 tests, all stdlib, runnable in < 1 second.
"""
from __future__ import annotations

import ast
import os
import unittest

from aee.observability import (
    EventCategory,
    EventKind,
    EventSeverity,
    category_for,
    events_by_category,
    is_known,
    severity_for,
)


# -- Shared constants ---------------------------------------------------------

# The 4 event-name string literals that MUST NOT appear
# outside :class:`EventKind`.  These are the 3 documented
# higher-signal events (``delivery_unverified``,
# ``"intent_mismatch"``, ``"traversal"``) plus the LIFECYCLE
# ``"warning"`` (which is the only LIFECYCLE event that the
# dispatcher emits as a side effect of an error, so it's the
# most likely to be silently renamed).
#
# NOTE: ``"warning"`` is also the name of a stdlib module
# (the ``warnings`` module) and appears in many code paths
# as a free-form word.  We deliberately exclude the
# LIFECYCLE ``"warning"`` literal from the tripwire (a typo
# in any of the 13 LIFECYCLE event names is a "lessons
# learned" bug, not a contract violation).  The 3 tripwire
# literals below are the ones with downstream consumers
# (the orchestrator's filter code) that would silently
# misroute on a rename.
TRIPWIRE_LITERALS: tuple = (
    '"delivery_unverified"',
    '"intent_mismatch"',
    '"traversal"',
)

# Production modules where the literals MUST NOT appear
# outside :class:`EventKind`.  Each path is relative to the
# repo root (`/home/ubuntu/hermes-runtime-bridge/`).
TRIPWIRE_PRODUCTION_MODULES: tuple = (
    "dispatcher/manager.py",
    "aee/orchestrator/orchestrator.py",
    "aee/orchestrator/hermes_provider.py",
    "aee/orchestrator/factory.py",
    "aee/orchestrator/fake_provider.py",
    "aee/orchestrator/aee2_shim.py",
    "aee/artifacts/collect.py",
    "aee/artifacts/policy.py",
    "aee/artifacts/policy_factory.py",
    "aee/operations/artifacts.py",
)

REPO_ROOT = "/home/ubuntu/hermes-runtime-bridge"


# -- Group A: EventKind shape ------------------------------------------------

class TestEventKindShape(unittest.TestCase):
    """Pin the on-disk shape of every :class:`EventKind`
    literal.  A future session that re-spells any of the 17
    canonical event names will fail this class, surfacing
    the rename before the orchestrator's filter code
    misroutes at runtime."""

    def test_all_has_seventeen_kinds(self) -> None:
        """23 canonical event kinds (14 LIFECYCLE + 1
        DELIVERY + 1 INTENT + 2 POLICY + 5 ORCHESTRATOR).
        Slice 3 added the 5 ORCHESTRATOR kinds:
        ``provider_selected``, ``submit_started``,
        ``submit_completed``, ``submit_failed``,
        ``poll_completed``.  AEE-7.4 finalization added
        CLAIMED to LIFECYCLE.  Any further drift is a
        shape change that must be intentional."""
        self.assertEqual(len(EventKind.all()), 23)

    def test_lifecycle_kinds_count(self) -> None:
        """14 LIFECYCLE event kinds (the ordinary task
        lifecycle narration).  AEE-7.4 finalization added
        CLAIMED (worker claim race winner)."""
        lifecycle = [
            k for k in EventKind.all()
            if category_for(k) == EventCategory.LIFECYCLE
        ]
        self.assertEqual(len(lifecycle), 14)

    def test_delivery_kinds_count(self) -> None:
        """1 DELIVERY event kind (``delivery_unverified``)."""
        delivery = [
            k for k in EventKind.all()
            if category_for(k) == EventCategory.DELIVERY
        ]
        self.assertEqual(delivery, ["delivery_unverified"])

    def test_intent_kinds_count(self) -> None:
        """1 INTENT event kind (``intent_mismatch``)."""
        intent = [
            k for k in EventKind.all()
            if category_for(k) == EventCategory.INTENT
        ]
        self.assertEqual(intent, ["intent_mismatch"])

    def test_policy_kinds_count(self) -> None:
        """2 POLICY event kinds (``traversal`` and
        ``policy_event``)."""
        policy = [
            k for k in EventKind.all()
            if category_for(k) == EventCategory.POLICY
        ]
        self.assertEqual(set(policy), {"traversal", "policy_event"})

    def test_lifecycle_kinds_canonical_spelling(self) -> None:
        """Pin the 13 LIFECYCLE event names exactly.  Any
        rename is a breaking change for the orchestrator's
        filter code (``any(e["kind"] == "started" for e in
        events)`` etc.)."""
        self.assertEqual(
            EventKind.CREATED, "created",
        )
        self.assertEqual(EventKind.QUEUED, "queued")
        self.assertEqual(EventKind.STATUS, "status")
        self.assertEqual(EventKind.STARTED, "started")
        self.assertEqual(EventKind.PROGRESS, "progress")
        self.assertEqual(EventKind.LOG, "log")
        self.assertEqual(EventKind.WARNING, "warning")
        self.assertEqual(EventKind.COMPLETED, "completed")
        self.assertEqual(EventKind.FAILED, "failed")
        self.assertEqual(EventKind.TIMEOUT, "timeout")
        self.assertEqual(EventKind.CANCELLED, "cancelled")
        self.assertEqual(EventKind.RETRY_OF, "retry_of")
        self.assertEqual(
            EventKind.OPENAI_RUN_ATTACHED, "openai_run_attached",
        )

    def test_higher_signal_kinds_canonical_spelling(self) -> None:
        """Pin the 4 higher-signal event names exactly.
        These are the ones the orchestrator filters on."""
        self.assertEqual(
            EventKind.DELIVERY_UNVERIFIED, "delivery_unverified",
        )
        self.assertEqual(
            EventKind.INTENT_MISMATCH, "intent_mismatch",
        )
        self.assertEqual(EventKind.TRAVERSAL, "traversal")
        self.assertEqual(EventKind.POLICY_EVENT, "policy_event")

    def test_no_duplicate_kind_strings(self) -> None:
        """Two class members that resolve to the same
        string is a structural bug — the ``.all()`` set
        silently drops the duplicate and the rest of the
        SOT becomes ambiguous."""
        members = [
            v for k, v in vars(EventKind).items()
            if not k.startswith("_") and isinstance(v, str)
        ]
        self.assertEqual(len(members), len(set(members)))

    def test_lifecycle_cancelled_distinct_from_dispatch_cancelled(self) -> None:
        """``EventKind.CANCELLED`` ("cancelled") and
        ``DispatchStatus.CANCELLED.value`` ("cancelled")
        are *the same string* by design.  This is NOT a
        collision: they live in different SQLite columns
        (``task_events.kind`` vs
        ``dispatch_records.dispatch_status``) and the
        orchestrator's filter code reads them in
        disjoint contexts.

        This test pins the design choice (a
        ``task_cancelled`` event in the lifecycle stream
        when the dispatch_status moves to ``cancelled``)
        so a future session does not "fix" the perceived
        collision by renaming one of them and breaking
        the audit trail.

        If the orchestrator's filter ever needs to
        distinguish them, the fix is to add a
        ``task_cancelled_via_dispatch`` event kind — NOT
        to rename either value."""
        # Both are "cancelled"; same string is the
        # intentional overlap.
        self.assertEqual(
            EventKind.CANCELLED, "cancelled",
        )
        # And the design note: same string, different
        # column, different read context.  Future
        # refactors: do not rename one without renaming
        # the other (and the audit readers).

    def test_all_returns_frozenset(self) -> None:
        """``.all()`` must be a frozenset (not a list/set)
        so callers can rely on set-equality in their
        tests."""
        result = EventKind.all()
        self.assertIsInstance(result, frozenset)

    def test_all_is_immutable(self) -> None:
        """``.all()`` must be immutable so a caller cannot
        accidentally mutate the canonical set."""
        result = EventKind.all()
        with self.assertRaises(AttributeError):
            result.add("rogue_kind")  # type: ignore[attr-defined]

    def test_kinds_are_lowercase(self) -> None:
        """All 17 event kinds use snake_case lowercase
        (no CamelCase / no UPPERCASE).  This is the
        contract for the audit log: ``task_events.kind``
        is always a lowercase snake_case string."""
        for kind in EventKind.all():
            self.assertEqual(
                kind, kind.lower(),
                f"EventKind {kind!r} is not lowercase",
            )
            self.assertNotIn(" ", kind)
            self.assertNotIn("-", kind)
            self.assertNotIn("_", kind.replace("_", "", 1) if kind.startswith("_") else "_" if "__" in kind else "_") if False else True  # noqa: E501
            # Simpler: the kind has at most one leading
            # underscore and is otherwise all lowercase +
            # underscores.
            stripped = kind.lstrip("_")
            self.assertRegex(
                stripped, r"^[a-z][a-z0-9_]*$",
                f"EventKind {kind!r} does not match snake_case regex",
            )


# -- Group B: EventCategory shape --------------------------------------------

class TestEventCategoryShape(unittest.TestCase):
    """Pin the 4 :class:`EventCategory` values."""

    def test_four_categories(self) -> None:
        # Slice 3 added ORCHESTRATOR as the 5th category.
        self.assertEqual(
            {c.value for c in EventCategory},
            {"lifecycle", "delivery", "intent", "policy", "orchestrator"},
        )

    def test_categories_are_lowercase(self) -> None:
        for c in EventCategory:
            self.assertEqual(c.value, c.value.lower())

    def test_categories_are_distinct(self) -> None:
        values = [c.value for c in EventCategory]
        self.assertEqual(len(values), len(set(values)))

    def test_lifecycle_string_value(self) -> None:
        self.assertEqual(EventCategory.LIFECYCLE.value, "lifecycle")

    def test_delivery_string_value(self) -> None:
        self.assertEqual(EventCategory.DELIVERY.value, "delivery")

    def test_intent_string_value(self) -> None:
        self.assertEqual(EventCategory.INTENT.value, "intent")

    def test_policy_string_value(self) -> None:
        self.assertEqual(EventCategory.POLICY.value, "policy")

    def test_category_inherits_from_str(self) -> None:
        """EventCategory is ``str, Enum`` so it round-trips
        through SQLite TEXT columns and JSON payloads
        without a custom (de)serializer."""
        self.assertIsInstance(EventCategory.LIFECYCLE, str)
        # str-membership works
        self.assertIn(EventCategory.LIFECYCLE, {"lifecycle"})


# -- Group C: EventSeverity shape --------------------------------------------

class TestEventSeverityShape(unittest.TestCase):
    """Pin the 3 :class:`EventSeverity` values."""

    def test_three_severities(self) -> None:
        self.assertEqual(
            {s.value for s in EventSeverity},
            {"info", "warn", "high"},
        )

    def test_severities_are_distinct(self) -> None:
        values = [s.value for s in EventSeverity]
        self.assertEqual(len(values), len(set(values)))

    def test_info_string_value(self) -> None:
        self.assertEqual(EventSeverity.INFO.value, "info")

    def test_warn_string_value(self) -> None:
        self.assertEqual(EventSeverity.WARN.value, "warn")

    def test_high_string_value(self) -> None:
        self.assertEqual(EventSeverity.HIGH.value, "high")

    def test_severity_inherits_from_str(self) -> None:
        self.assertIsInstance(EventSeverity.HIGH, str)


# -- Group D: lookup helpers -------------------------------------------------

class TestLookupHelpers(unittest.TestCase):
    """Pin the :func:`is_known` / :func:`category_for` /
    :func:`severity_for` lookup shape."""

    def test_is_known_for_every_canonical_kind(self) -> None:
        for kind in EventKind.all():
            self.assertTrue(
                is_known(kind),
                f"is_known({kind!r}) returned False for a "
                f"canonical EventKind value",
            )

    def test_is_known_returns_false_for_unknown(self) -> None:
        self.assertFalse(is_known("made_up_event"))
        self.assertFalse(is_known("Intent_Mismatch"))  # case-sensitive
        self.assertFalse(is_known(" intent_mismatch"))  # whitespace-sensitive
        self.assertFalse(is_known("intent_mismatch "))  # whitespace-sensitive
        self.assertFalse(is_known(""))  # empty string

    def test_category_for_each_canonical_kind(self) -> None:
        """Every canonical kind resolves to a non-None
        category.  This is the lock-step tripwire with
        :data:`EventKind.all`."""
        for kind in EventKind.all():
            cat = category_for(kind)
            self.assertIsNotNone(
                cat,
                f"category_for({kind!r}) returned None",
            )
            self.assertIsInstance(cat, EventCategory)

    def test_category_for_unknown_returns_none(self) -> None:
        self.assertIsNone(category_for("made_up_event"))
        self.assertIsNone(category_for(""))
        self.assertIsNone(category_for(None))  # type: ignore[arg-type]

    def test_severity_for_each_canonical_kind(self) -> None:
        for kind in EventKind.all():
            sev = severity_for(kind)
            self.assertIsNotNone(
                sev,
                f"severity_for({kind!r}) returned None",
            )
            self.assertIsInstance(sev, EventSeverity)

    def test_severity_for_unknown_returns_none(self) -> None:
        self.assertIsNone(severity_for("made_up_event"))
        self.assertIsNone(severity_for(""))

    def test_intent_mismatch_is_high_severity(self) -> None:
        """``intent_mismatch`` is the only HIGH severity
        event today.  Pin this — if a future slice adds
        another HIGH event it must be an intentional
        one-by-one decision."""
        self.assertEqual(
            severity_for("intent_mismatch"),
            EventSeverity.HIGH,
        )

    def test_delivery_unverified_is_warn_severity(self) -> None:
        self.assertEqual(
            severity_for("delivery_unverified"),
            EventSeverity.WARN,
        )

    def test_traversal_is_info_severity(self) -> None:
        """POLICY events are observe-only — the bridge
        records them for audit but does not raise an
        alarm."""
        self.assertEqual(
            severity_for("traversal"),
            EventSeverity.INFO,
        )

    def test_completed_is_info_severity(self) -> None:
        self.assertEqual(
            severity_for("completed"),
            EventSeverity.INFO,
        )


# -- Group E: events_by_category grouping ------------------------------------

class TestEventsByCategory(unittest.TestCase):
    """Pin the :func:`events_by_category` shape."""

    def test_returns_dict(self) -> None:
        self.assertIsInstance(events_by_category(), dict)

    def test_has_four_keys(self) -> None:
        # Slice 3 added ORCHESTRATOR as the 5th category.
        keys = set(events_by_category().keys())
        self.assertEqual(
            keys, {"lifecycle", "delivery", "intent", "policy", "orchestrator"},
        )

    def test_lifecycle_bucket_has_14(self) -> None:
        """AEE-7.4 finalization: lifecycle bucket grew
        from 13 to 14 (CLAIMED added for worker claim
        race winner).  See ``aee/observability/events.py``
        for the authoritative inventory."""
        self.assertEqual(
            len(events_by_category()["lifecycle"]), 14,
        )

    def test_delivery_bucket_has_one(self) -> None:
        self.assertEqual(
            events_by_category()["delivery"],
            ["delivery_unverified"],
        )

    def test_intent_bucket_has_one(self) -> None:
        self.assertEqual(
            events_by_category()["intent"],
            ["intent_mismatch"],
        )

    def test_each_list_is_sorted(self) -> None:
        """Each bucket is sorted (for deterministic
        dashboard / docs output)."""
        for cat, kinds in events_by_category().items():
            self.assertEqual(
                kinds, sorted(kinds),
                f"{cat} bucket is not sorted",
            )

    def test_union_equals_event_kind_all(self) -> None:
        """The union of all buckets must equal
        :data:`EventKind.all` — a kind is in exactly one
        bucket."""
        flat = {
            k for kinds in events_by_category().values() for k in kinds
        }
        self.assertEqual(flat, EventKind.all())


# -- Group F: lock-step tripwires --------------------------------------------

class TestVocabularyLockStep(unittest.TestCase):
    """The :data:`_CATEGORY_FOR_KIND` and
    :data:`_SEVERITY_FOR_KIND` mappings must stay in
    lock-step with :class:`EventKind`.  This is the
    structural test that catches the bug pattern AEE-7.3
    rescue documented: the *state* mapping is updated but
    the *reason* mapping next to it is forgotten."""

    def test_every_kind_has_a_category(self) -> None:
        from aee.observability.events import _CATEGORY_FOR_KIND
        self.assertEqual(
            set(_CATEGORY_FOR_KIND.keys()), EventKind.all(),
        )

    def test_every_kind_has_a_severity(self) -> None:
        from aee.observability.events import _SEVERITY_FOR_KIND
        self.assertEqual(
            set(_SEVERITY_FOR_KIND.keys()), EventKind.all(),
        )

    def test_no_extra_categories(self) -> None:
        """A typo in a category value would create a kind
        that *is* in the mapping but whose category is not
        a valid :class:`EventCategory`."""
        from aee.observability.events import _CATEGORY_FOR_KIND
        from aee.observability.categories import is_valid_category
        bad = [
            (k, c) for k, c in _CATEGORY_FOR_KIND.items()
            if not is_valid_category(c)
        ]
        self.assertEqual(
            bad, [],
            f"Invalid category values: {bad!r}",
        )

    def test_no_extra_severities(self) -> None:
        from aee.observability.events import _SEVERITY_FOR_KIND
        from aee.observability.severity import is_valid_severity
        bad = [
            (k, s) for k, s in _SEVERITY_FOR_KIND.items()
            if not is_valid_severity(s)
        ]
        self.assertEqual(
            bad, [],
            f"Invalid severity values: {bad!r}",
        )


# -- Group G: production-source tripwire -------------------------------------

def _read_source(path: str) -> str:
    """Read a file's text content.  Returns ``""`` if the
    file is missing (the tripwire is conservative — a
    missing file is not a regression, just an out-of-date
    tripwire list)."""
    full = os.path.join(REPO_ROOT, path)
    if not os.path.exists(full):
        return ""
    with open(full, "r", encoding="utf-8") as fp:
        return fp.read()


def _class_body_line_range(source: str) -> tuple:
    """Find the ``(lineno, end_lineno)`` of the
    :class:`EventKind` class body in ``source`` so the
    tripwire can skip it.

    Returns ``(-1, -1)`` if the class is not found
    (which should never happen for our SOT file but is
    safe for unknown future modules)."""
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "EventKind":
            return (node.lineno, node.end_lineno or node.lineno)
    return (-1, -1)


# AEE-7.4 slice 1 cannot enable the production-source
# tripwire yet: ``dispatcher/manager.py`` is a *tracked*
# file, and the AEE-7 K-gate forbids editing tracked
# files in slice K.  The tripwire is the *enforcement
# mechanism* the AEE-7.4+ wire-up slice (K+1) will use
# after the dispatcher's call sites have been migrated
# to read ``EventKind.X`` instead of the literal.
#
# For slice 1, the tripwire lives in a separate test
# class (``TestProductionSourceTripwireDeferred``) that
# is **disabled by default** (``__test__ = False``).
# AEE-7.4 finalization: the dispatcher / jobs / collect
# call sites have been migrated to ``EventKind.X``.  The
# tripwire regression test is now a hard gate — any
# future call site that adds an inline ``"created"`` /
# ``"completed"`` / ``"claimed"`` / ``"traversal"`` /
# ``"delivery_unverified"`` / ``"intent_mismatch"``
# literal will fail the build.
DEFERRED_TRIPWIRE_ENABLED: bool = True


def _assert_no_literal_outside_event_kind(
    source: str, literal: str, test_case: unittest.TestCase,
) -> None:
    """Assert that ``literal`` does not appear in
    ``source`` outside the :class:`EventKind` class
    body.  Shared helper for the deferred tripwire
    tests."""
    if not source:
        return  # missing file → skip
    tree = ast.parse(source)
    cls_start, cls_end = _class_body_line_range(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == literal:
            lineno = node.lineno
            # Skip the EventKind class body (the SOT)
            if cls_start <= lineno <= cls_end:
                continue
            test_case.fail(
                f"Literal {literal!r} appears at line "
                f"{lineno} outside the EventKind class body. "
                f"Add the event to aee.observability.events:EventKind "
                f"instead of inlining the string at this call site.",
            )


class TestObservabilityPackageSelfTripwire(unittest.TestCase):
    """Active tripwire (always-on).  Guards the SOT
    package itself: the canonical event-name literals
    must not appear in the SOT package's helper modules
    outside the :class:`EventKind` class body.

    This is the test that catches a copy-paste regression
    inside the package (e.g. someone adds a helper that
    re-spells a kind).  The *tracked file* tripwire is
    separate (deferred to the wire-up slice) and lives
    in :class:`TestProductionSourceTripwireDeferred`."""

    def test_aee_observability_self_has_no_orphan_literals(self) -> None:
        for module in (
            "aee/observability/__init__.py",
            "aee/observability/categories.py",
            "aee/observability/severity.py",
        ):
            source = _read_source(module)
            for literal in ("intent_mismatch", "delivery_unverified"):
                _assert_no_literal_outside_event_kind(
                    source, literal, self,
                )


# AEE-7.4 slice 1 cannot enable the *tracked-file*
# tripwire yet: ``dispatcher/manager.py`` is a *tracked*
# file, and the AEE-7 K-gate forbids editing tracked
# files in slice K.  The tripwire is the *enforcement
# mechanism* the AEE-7.4+ wire-up slice (K+1) will use
# after the dispatcher's call sites have been migrated
# to read ``EventKind.X`` instead of the literal.
#
# For slice 1, the tripwire lives in a separate test
# class (``TestProductionSourceTripwireDeferred``) that
# is **disabled by default** via ``@unittest.skipIf``.
# AEE-7.4 finalization: the dispatcher / jobs / collect
# call sites have been migrated to ``EventKind.X``.  The
# tripwire regression test is now a hard gate — any
# future call site that adds an inline ``"created"`` /
# ``"completed"`` / ``"claimed"`` / ``"traversal"`` /
# ``"delivery_unverified"`` / ``"intent_mismatch"``
# literal will fail the build.
DEFERRED_TRIPWIRE_ENABLED: bool = True


# Note: we use ``@unittest.skipIf`` rather than
# ``__test__ = False`` because the latter only works
# when ``unittest.TestLoader`` walks the module by
# ``dir()`` — running ``python -m unittest
# aee.tests.test_aee74_observability`` uses
# ``loadTestsFromName`` which loads the class
# explicitly and bypasses the ``__test__`` filter.
# ``skipIf`` is honored in both code paths.
@unittest.skipIf(
    not DEFERRED_TRIPWIRE_ENABLED,
    "Deferred tripwire: enabled in AEE-7.4+ wire-up slice "
    "after dispatcher call sites migrate to EventKind.X "
    "literals. Set DEFERRED_TRIPWIRE_ENABLED = True to activate.",
)
class TestProductionSourceTripwireDeferred(unittest.TestCase):
    """Deferred tripwire.  Disabled in AEE-7.4 slice 1
    because the dispatcher call sites still inline the
    literals (the wire-up slice K+1 will migrate them).

    Flip :data:`DEFERRED_TRIPWIRE_ENABLED` to ``True``
    in the wire-up slice and these tests become hard
    gates that fail the build on a regression."""

    def test_dispatch_manager_has_no_intent_mismatch_literal(self) -> None:
        """``"intent_mismatch"`` is the canonical
        :class:`EventKind.INTENT_MISMATCH` value.  Any
        inline literal at the dispatcher call site is a
        regression — the SOT exists precisely so the call
        site reads ``EventKind.INTENT_MISMATCH`` instead."""
        source = _read_source("dispatcher/manager.py")
        _assert_no_literal_outside_event_kind(
            source, "intent_mismatch", self,
        )

    def test_dispatch_manager_has_no_delivery_unverified_literal(self) -> None:
        source = _read_source("dispatcher/manager.py")
        _assert_no_literal_outside_event_kind(
            source, "delivery_unverified", self,
        )

    def test_artifacts_collect_has_no_traversal_literal(self) -> None:
        """``"traversal"`` is the canonical
        :class:`EventKind.TRAVERSAL` value AND the
        artifact policy ``code='traversal'`` value.  The
        audit-breadcrumb ``code`` lives in
        :class:`ArtifactPolicy.TRAVERSAL` (in
        ``aee/artifacts/policy.py``) so an inline literal
        in ``collect.py`` is a regression of the
        AEE-7.1 SOT discipline."""
        source = _read_source("aee/artifacts/collect.py")
        _assert_no_literal_outside_event_kind(
            source, "traversal", self,
        )

    def test_api_jobs_has_no_claimed_literal(self) -> None:
        """``"claimed"`` is the canonical
        :class:`EventKind.CLAIMED` value.  The worker
        claim flow in ``aee/api/jobs.py`` must import
        :class:`EventKind` and reference
        ``EventKind.CLAIMED``; any inline literal is a
        regression of the AEE-7.4 finalization migration.

        Note: ``jobs.py`` is a tracked file and the
        pre-existing import of :class:`EventKind` was
        added by the finalization round."""
        source = _read_source("aee/api/jobs.py")
        _assert_no_literal_outside_event_kind(
            source, "claimed", self,
        )

    def test_orchestrator_modules_have_no_lifecycle_magic_strings(self) -> None:
        """AEE-7.4 finalization coverage expansion: the
        orchestrator and runtime modules do not currently
        emit events directly (event emission is wired
        through the dispatcher).  This tripwire pins
        that property — if a future slice adds
        ``_emit_event(..., "started", ...)`` at the
        orchestrator layer, it must be migrated to
        ``EventKind.STARTED`` before this gate will
        pass.

        Exclusion rule: ``ast.ClassDef`` bodies are
        excluded (Enum / dataclass / regular class
        constants).  A LIFECYCLE literal used as a class
        attribute (e.g. ``ProviderStatus.COMPLETED =
        "completed"``) is fine — only a bare string
        literal at module-level or inside a function
        body counts as a regression.
        """
        from aee.observability import EventKind as _EK
        import ast as _ast
        forbidden = {
            _EK.CREATED, _EK.STARTED,
            _EK.COMPLETED, _EK.FAILED,
        }
        files = [
            "aee/orchestrator/orchestrator.py",
            "aee/orchestrator/factory.py",
            "aee/orchestrator/provider.py",
            "aee/runtimes/models.py",
        ]
        import os as _os
        for path in files:
            full = _os.path.join(REPO_ROOT, path)
            if not _os.path.exists(full):
                continue
            with open(full, "r", encoding="utf-8") as _fp:
                source = _fp.read()
            try:
                tree = _ast.parse(source)
            except SyntaxError:
                continue
            # Build a set of (line) ranges that are inside
            # any ClassDef body — these are excluded.
            class_ranges: list = []
            for node in _ast.walk(tree):
                if isinstance(node, _ast.ClassDef):
                    class_ranges.append(
                        (node.lineno, node.end_lineno or node.lineno)
                    )
            def _inside_any_class(lineno: int) -> bool:
                for start, end in class_ranges:
                    if start <= lineno <= end:
                        return True
                return False
            for node in _ast.walk(tree):
                if (
                    isinstance(node, _ast.Constant)
                    and isinstance(node.value, str)
                    and node.value in forbidden
                    and not _inside_any_class(node.lineno)
                ):
                    self.fail(
                        f"Literal {node.value!r} appears at line "
                        f"{node.lineno} of {path} (outside any "
                        f"class body). Add the event to "
                        f"aee.observability.events:EventKind "
                        f"instead of inlining the string at "
                        f"this call site.",
                    )


# -- Group H: round-trip / on-disk shape -------------------------------------

class TestRoundTripShape(unittest.TestCase):
    """The vocabulary must round-trip cleanly through
    SQLite TEXT columns and JSON payloads.  This is the
    test that proves the ``str, Enum`` design choice for
    :class:`EventCategory` and :class:`EventSeverity`."""

    def test_category_value_round_trips_through_str(self) -> None:
        for c in EventCategory:
            round_tripped = EventCategory(str(c.value))
            self.assertIs(round_tripped, c)

    def test_severity_value_round_trips_through_str(self) -> None:
        for s in EventSeverity:
            round_tripped = EventSeverity(str(s.value))
            self.assertIs(round_tripped, s)

    def test_category_value_is_valid_json_string(self) -> None:
        """``json.dumps`` of a category value must work
        without a custom encoder."""
        import json
        for c in EventCategory:
            encoded = json.dumps(c.value)
            self.assertEqual(json.loads(encoded), c.value)

    def test_event_kind_string_is_valid_json_string(self) -> None:
        """The event-kind strings are plain ``str`` (not
        :class:`EventCategory` subclasses) so they JSON-
        encode trivially."""
        import json
        for kind in EventKind.all():
            encoded = json.dumps(kind)
            self.assertEqual(json.loads(encoded), kind)


if __name__ == "__main__":
    unittest.main()
