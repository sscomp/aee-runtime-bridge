"""AEE-5 Runtime Selector — unit tests.

Pure selector tests. Build small sets of
RuntimeDescriptors in memory and exercise the
9-step selection order from the AEE-5 task spec §4.4.
"""
from __future__ import annotations

import unittest

from aee.runtimes.errors import RuntimeNotFoundError
from aee.runtimes.models import (
    RuntimeCapabilities,
    RuntimeDescriptor,
    RuntimeHealth,
    RuntimeHealthStatus,
    RuntimeLimits,
    TaskRuntimeRequirements,
)
from aee.runtimes.selector import RuntimeSelector, select_runtime


def _mk(
    *,
    runtime_id: str,
    runtime_type: str = "aee_lightweight",
    caps=("task.shell", "task.python"),
    labels=None,
    enabled: bool = True,
    health: str = "healthy",
) -> RuntimeDescriptor:
    return RuntimeDescriptor(
        runtime_id=runtime_id,
        runtime_type=runtime_type,
        display_name=runtime_id,
        capabilities=RuntimeCapabilities(list(caps)),
        labels=dict(labels or {}),
        limits=RuntimeLimits(),
        health=RuntimeHealth(status=health),
        enabled=enabled,
    )


class TestSelectorBasics(unittest.TestCase):
    def setUp(self):
        self.sel = RuntimeSelector(allow_unknown_health=True)
        self.rts = [
            _mk(runtime_id="aee-lightweight-local", caps=["task.shell"], labels={"env": "local"}),
            _mk(runtime_id="shell-rt", runtime_type="shell", caps=["task.shell", "task.git"], labels={"env": "sandbox"}),
            _mk(runtime_id="claude-rt", runtime_type="claude_code", caps=["task.shell", "task.claude"], labels={"env": "sandbox"}),
        ]

    def test_no_requirements_picks_first_dispatchable(self):
        res = self.sel.select(task=None, available_runtimes=self.rts)
        self.assertEqual(res.selected_runtime_id, "aee-lightweight-local")
        self.assertEqual(res.candidate_count, 3)
        self.assertEqual(len(res.evaluated_runtime_ids), 3)

    def test_empty_requirements_picks_first_dispatchable(self):
        res = self.sel.select(
            task=TaskRuntimeRequirements(),
            available_runtimes=self.rts,
        )
        self.assertEqual(res.selected_runtime_id, "aee-lightweight-local")

    def test_required_capability_subset(self):
        res = self.sel.select(
            task=TaskRuntimeRequirements(required_capabilities=["task.shell"]),
            available_runtimes=self.rts,
        )
        self.assertIn(res.selected_runtime_id, {"aee-lightweight-local", "shell-rt", "claude-rt"})

    def test_required_capability_not_held_returns_404(self):
        with self.assertRaises(RuntimeNotFoundError) as ctx:
            self.sel.select(
                task=TaskRuntimeRequirements(required_capabilities=["task.browser"]),
                available_runtimes=self.rts,
            )
        self.assertEqual(ctx.exception.code, "AEE_RUNTIME_NOT_FOUND")

    def test_disabled_runtime_not_selected(self):
        rts = [
            _mk(runtime_id="disabled-rt", enabled=False, health="healthy"),
            _mk(runtime_id="enabled-rt", health="healthy"),
        ]
        res = self.sel.select(task=None, available_runtimes=rts)
        self.assertEqual(res.selected_runtime_id, "enabled-rt")

    def test_excluded_runtime_ids_excluded(self):
        res = self.sel.select(
            task=TaskRuntimeRequirements(excluded_runtime_ids=["aee-lightweight-local"]),
            available_runtimes=self.rts,
        )
        self.assertNotEqual(res.selected_runtime_id, "aee-lightweight-local")

    def test_runtime_type_exact_match(self):
        res = self.sel.select(
            task=TaskRuntimeRequirements(runtime_type="shell"),
            available_runtimes=self.rts,
        )
        self.assertEqual(res.selected_runtime_id, "shell-rt")

    def test_runtime_type_mismatch(self):
        with self.assertRaises(RuntimeNotFoundError) as ctx:
            self.sel.select(
                task=TaskRuntimeRequirements(runtime_type="kubernetes"),
                available_runtimes=self.rts,
            )
        d = ctx.exception.to_dict()
        self.assertEqual(d["details"]["required_runtime_type"], "kubernetes")

    def test_required_labels_subset(self):
        res = self.sel.select(
            task=TaskRuntimeRequirements(required_labels={"env": "sandbox"}),
            available_runtimes=self.rts,
        )
        # The two sandbox Runtimes have env=sandbox; we
        # have 2 candidates. Tie-break by runtime_id.
        self.assertIn(res.selected_runtime_id, {"shell-rt", "claude-rt"})

    def test_required_labels_mismatch_excluded(self):
        with self.assertRaises(RuntimeNotFoundError):
            self.sel.select(
                task=TaskRuntimeRequirements(required_labels={"env": "nowhere"}),
                available_runtimes=self.rts,
            )

    def test_unhealthy_not_dispatchable(self):
        rts = [
            _mk(runtime_id="unhealthy", health="unhealthy"),
            _mk(runtime_id="healthy", health="healthy"),
        ]
        res = self.sel.select(task=None, available_runtimes=rts)
        self.assertEqual(res.selected_runtime_id, "healthy")

    def test_offline_not_dispatchable(self):
        rts = [
            _mk(runtime_id="offline", health="offline"),
            _mk(runtime_id="healthy", health="healthy"),
        ]
        res = self.sel.select(task=None, available_runtimes=rts)
        self.assertEqual(res.selected_runtime_id, "healthy")

    def test_unknown_dispatchable_by_default(self):
        rts = [
            _mk(runtime_id="unknown", health="unknown"),
            _mk(runtime_id="healthy", health="healthy"),
        ]
        res = self.sel.select(task=None, available_runtimes=rts)
        # Both dispatchable; healthy ranks higher (lower score).
        self.assertEqual(res.selected_runtime_id, "healthy")

    def test_unknown_not_dispatchable_in_strict_mode(self):
        rts = [_mk(runtime_id="unknown", health="unknown")]
        sel = RuntimeSelector(allow_unknown_health=False)
        with self.assertRaises(RuntimeNotFoundError):
            sel.select(task=None, available_runtimes=rts)

    def test_degraded_ranks_below_healthy(self):
        rts = [
            _mk(runtime_id="degraded", health="degraded"),
            _mk(runtime_id="healthy", health="healthy"),
        ]
        res = self.sel.select(task=None, available_runtimes=rts)
        self.assertEqual(res.selected_runtime_id, "healthy")


class TestSelectorPreferences(unittest.TestCase):
    def setUp(self):
        self.rts = [
            _mk(runtime_id="rt-a", caps=["task.shell"], health="healthy"),
            _mk(runtime_id="rt-b", caps=["task.shell"], health="healthy"),
            _mk(runtime_id="rt-c", caps=["task.shell", "task.git"], health="healthy"),
        ]

    def test_preferred_runtime_id_ranks_first(self):
        res = select_runtime(
            task=TaskRuntimeRequirements(
                required_capabilities=["task.shell"],
                preferred_runtime_ids=["rt-c"],
            ),
            available_runtimes=self.rts,
        )
        self.assertEqual(res.selected_runtime_id, "rt-c")

    def test_preferred_capability_weight(self):
        res = select_runtime(
            task=TaskRuntimeRequirements(
                required_capabilities=["task.shell"],
                preferred_capabilities=["task.git"],
            ),
            available_runtimes=self.rts,
        )
        # rt-c has the preferred cap; rt-a and rt-b don't.
        self.assertEqual(res.selected_runtime_id, "rt-c")

    def test_deterministic_tie_break_by_runtime_id(self):
        res = select_runtime(
            task=TaskRuntimeRequirements(
                required_capabilities=["task.shell"],
            ),
            available_runtimes=self.rts,
        )
        # rt-a and rt-b have identical scores; tie-break
        # by runtime_id ascending -> rt-a wins.
        self.assertEqual(res.selected_runtime_id, "rt-a")

    def test_determinism_same_input_same_output(self):
        req = TaskRuntimeRequirements(required_capabilities=["task.shell"])
        out1 = select_runtime(task=req, available_runtimes=self.rts).to_dict()
        out2 = select_runtime(task=req, available_runtimes=self.rts).to_dict()
        self.assertEqual(out1, out2)


class TestSelectorErrorContract(unittest.TestCase):
    def test_no_match_includes_evaluated_runtimes(self):
        rts = [
            _mk(runtime_id="rt1", caps=["task.x"]),
            _mk(runtime_id="rt2", caps=["task.y"], enabled=False),
        ]
        with self.assertRaises(RuntimeNotFoundError) as ctx:
            select_runtime(
                task=TaskRuntimeRequirements(required_capabilities=["task.z"]),
                available_runtimes=rts,
            )
        d = ctx.exception.to_dict()
        er = d["details"]["evaluated_runtimes"]
        ids = {e["runtime_id"] for e in er}
        self.assertEqual(ids, {"rt1", "rt2"})

    def test_no_match_includes_rejection_reasons(self):
        rts = [
            _mk(runtime_id="rt1", caps=["task.x"], health="unhealthy"),
        ]
        with self.assertRaises(RuntimeNotFoundError) as ctx:
            select_runtime(task=None, available_runtimes=rts)
        d = ctx.exception.to_dict()
        reasons = d["details"]["evaluated_runtimes"][0]["rejected_reasons"]
        # rt1 was rejected; reasons should mention health.
        self.assertTrue(
            any("health" in r.lower() for r in reasons),
            f"expected 'health' in rejection reasons, got {reasons}",
        )

    def test_required_capability_message_format(self):
        rts = [
            _mk(runtime_id="rt1", caps=["task.x"]),
        ]
        with self.assertRaises(RuntimeNotFoundError) as ctx:
            select_runtime(
                task=TaskRuntimeRequirements(
                    required_capabilities=["task.shell", "task.git"],
                ),
                available_runtimes=rts,
            )
        reasons = ctx.exception.to_dict()["details"]["evaluated_runtimes"][0][
            "rejected_reasons"
        ]
        # Either of the two missing caps should appear.
        joined = " ".join(reasons).lower()
        self.assertIn("missing required capabilit", joined)


if __name__ == "__main__":
    unittest.main()
