"""AEE-7.1 — TRAVERSAL audit-row query contract.

AEE-7.1 added a *secondary* audit row to
``ArtifactPipeline.collect()`` for every path whose
``PolicyDecision.traversal_hint`` is True. The contract is:

* The primary row's ``code`` reflects the underlying verdict
  (``OK`` if the normalised path is still in-bounds,
  ``OUTSIDE_ROOTS`` if the normalisation pushed the path out).
* The secondary row always has ``code="traversal"`` (a literal
  string, not the enum), ``traversal_hint=True``, and a
  ``linked_decision_id`` pointing at the primary row's
  ``decision_id``.
* The secondary row is emitted in *both* the accepted and
  rejected cases, so ops can ``WHERE code='traversal'`` to find
  every literal ``..`` attempt regardless of whether the
  destination was allowed.

This file pins the contract with three focused tests:

* ``test_secondary_row_on_traversal_hint_in_bounds`` — path
  uses ``..`` but lands inside the allowed root → primary
  code is ``OK``; secondary row is still emitted.
* ``test_secondary_row_on_traversal_hint_out_of_bounds`` —
  path uses ``..`` and lands outside the allowed root →
  primary code is ``OUTSIDE_ROOTS``; secondary row is still
  emitted.
* ``test_no_secondary_row_when_no_traversal_hint`` — path
  with no ``..`` segments → no ``code="traversal"`` row at
  all.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from aee.artifacts.collect import ArtifactPipeline
from aee.artifacts.policy import ArtifactPolicy, PolicyViolationCode
from aee.artifacts.repository import InMemoryArtifactRepository


def _events_by_code(events, code: str):
    return [e for e in events if e.get("code") == code]


def _traversal_events(events):
    return _events_by_code(events, "traversal")


class TestTraversalAuditContract(unittest.TestCase):
    """AEE-7.1 TRAVERSAL secondary audit row contract."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="aee71-traversal-")
        self.addCleanup(self._rm_tmp)
        # Permissive-ish policy: the root is the tmp dir, but
        # the test passes paths that *try* to traverse out.
        self.policy = ArtifactPolicy(
            allowed_roots=(self.tmp,),
            description="aee71_traversal_test",
        )
        self.repo = InMemoryArtifactRepository()
        self.pipeline = ArtifactPipeline(
            repo=self.repo,
            policy=self.policy,
            on_policy_violation="skip_and_warn",
            policy_source="TestTraversalAuditContract",
        )
        # Put a real file inside the root so the in-bounds case
        # has something to actually collect.
        self._inside = os.path.join(self.tmp, "real.txt")
        Path(self._inside).write_text("hello\n")

    def _rm_tmp(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    # 1) In-bounds traversal: primary OK, secondary row still emitted
    # ------------------------------------------------------------------
    def test_secondary_row_on_traversal_hint_in_bounds(self) -> None:
        # ``subdir/../real.txt`` collapses to ``real.txt`` which
        # is inside the root. The primary code is OK; the
        # secondary row must still be emitted.
        candidate = os.path.join(self.tmp, "subdir", "..", "real.txt")
        # Sanity: the policy itself flags this as a traversal hint
        decision = self.policy.check(candidate)
        self.assertTrue(decision.traversal_hint, "policy must flag `..`")
        self.assertEqual(decision.code, PolicyViolationCode.OK)
        self.assertEqual(decision.accepted, True)

        # Run the pipeline.
        results = self.pipeline.collect("T-IN", [candidate])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].exists)

        events = self.repo.policy_events
        primary = [e for e in events if e.get("code") == "ok"]
        secondary = _traversal_events(events)
        self.assertEqual(
            len(primary), 1,
            f"expected exactly one primary OK row, got {primary!r}",
        )
        self.assertEqual(
            len(secondary), 1,
            f"expected exactly one traversal secondary row, got {secondary!r}",
        )
        # The secondary row's linked_decision_id must match the
        # primary row's decision_id.
        self.assertEqual(
            secondary[0]["linked_decision_id"],
            primary[0]["decision_id"],
        )
        # And the secondary row must carry the primary code in detail
        # so the final outcome is recoverable.
        self.assertIn("primary_code=ok", secondary[0]["detail"])
        # The traversal_hint flag is true on both rows.
        self.assertTrue(secondary[0]["traversal_hint"])

    # ------------------------------------------------------------------
    # 2) Out-of-bounds traversal: primary OUTSIDE_ROOTS, secondary emitted
    # ------------------------------------------------------------------
    def test_secondary_row_on_traversal_hint_out_of_bounds(self) -> None:
        # A path that *starts* inside the root but normalises to
        # something outside (here we use the absolute
        # ``/etc/passwd`` masquerading via a literal-`..` chain).
        # We can't go up from /tmp/aee71... to /etc without a
        # very long chain — easier: pre-construct the candidate
        # the policy module will see.
        candidate = os.path.join(self.tmp, "..", "..", "..", "etc", "passwd")
        decision = self.policy.check(candidate)
        self.assertTrue(decision.traversal_hint)
        self.assertEqual(decision.code, PolicyViolationCode.OUTSIDE_ROOTS)
        self.assertEqual(decision.accepted, False)

        results = self.pipeline.collect("T-OUT", [candidate])
        # The path is rejected; the pipeline still returns a
        # placeholder Artifact with exists=False so callers can
        # record it.
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].exists)
        self.assertIn("outside", results[0].classification_source)

        events = self.repo.policy_events
        primary = [
            e for e in events
            if e.get("code") == PolicyViolationCode.OUTSIDE_ROOTS.value
        ]
        secondary = _traversal_events(events)
        self.assertEqual(len(primary), 1, f"got {primary!r}")
        self.assertEqual(len(secondary), 1, f"got {secondary!r}")
        self.assertEqual(
            secondary[0]["linked_decision_id"],
            primary[0]["decision_id"],
        )
        self.assertIn("primary_code=outside_allowed_roots", secondary[0]["detail"])

    # ------------------------------------------------------------------
    # 3) No traversal: only the primary row, no secondary
    # ------------------------------------------------------------------
    def test_no_secondary_row_when_no_traversal_hint(self) -> None:
        candidate = self._inside  # plain absolute path, no `..`
        decision = self.policy.check(candidate)
        self.assertFalse(decision.traversal_hint, "policy must NOT flag clean path")

        results = self.pipeline.collect("T-CLEAN", [candidate])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].exists)

        events = self.repo.policy_events
        primary = [e for e in events if e.get("code") == "ok"]
        secondary = _traversal_events(events)
        self.assertEqual(len(primary), 1)
        self.assertEqual(
            secondary, [],
            "no traversal_hint → no secondary row",
        )


class TestTraversalAuditQueryability(unittest.TestCase):
    """The TRAVERSAL contract is *queryable*: a single
    ``WHERE code='traversal'`` returns the secondary rows in
    isolation. This test runs a *batch* with both kinds of
    paths mixed in and asserts the query returns just the
    traversal rows, in order, with the right ``linked_decision_id``.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="aee71-traversal-q-")
        self.addCleanup(self._rm_tmp)
        self.policy = ArtifactPolicy(
            allowed_roots=(self.tmp,),
            description="aee71_traversal_query_test",
        )
        self.repo = InMemoryArtifactRepository()
        self.pipeline = ArtifactPipeline(
            repo=self.repo,
            policy=self.policy,
        )
        # Real file inside the root.
        self._real = os.path.join(self.tmp, "real.txt")
        Path(self._real).write_text("hi")
        # Make a subdir so the `subdir/../real.txt` traversal
        # has a real segment to work with.
        os.makedirs(os.path.join(self.tmp, "subdir"), exist_ok=True)

    def _rm_tmp(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_batch_query_filters_to_traversal_rows(self) -> None:
        paths = [
            self._real,                                              # clean
            os.path.join(self.tmp, "subdir", "..", "real.txt"),      # traversal in-bounds
            os.path.join(self.tmp, "..", "..", "..", "etc", "passwd"),  # traversal out-of-bounds
            os.path.join(self.tmp, "another.txt"),                   # clean (will be MISSING)
        ]
        self.pipeline.collect("T-BATCH", paths)

        # Query contract: ``WHERE code='traversal'`` returns
        # exactly the two traversal rows (in-bounds and out).
        events = self.repo.policy_events
        traversal_rows = _traversal_events(events)
        self.assertEqual(
            len(traversal_rows), 2,
            f"expected 2 traversal rows, got {traversal_rows!r}",
        )
        # Each traversal row has a distinct linked_decision_id
        # pointing at the primary row that triggered it.
        linked_ids = {row["linked_decision_id"] for row in traversal_rows}
        self.assertEqual(len(linked_ids), 2)
        # Every primary decision_id referenced is also in the
        # event log (no orphan linked rows).
        all_decision_ids = {e.get("decision_id") for e in events}
        for lid in linked_ids:
            self.assertIn(lid, all_decision_ids)

        # Sanity: the two clean paths generated no traversal row.
        clean_rows = [
            e for e in events
            if e.get("code") == "ok" and not e.get("traversal_hint")
        ]
        # ``self._real`` is one clean path. The other "another.txt"
        # is MISSING and produces a primary row with code="missing_path"
        # — also not a traversal row.
        self.assertEqual(len(clean_rows), 1)


if __name__ == "__main__":
    unittest.main()
