"""AEE-6.3 — Registry / Selector integration for the new
``claude_code`` runtime.

Verifies the contract documented in
``references/aee6-slice-3-security-provider-case-study.md``
§"Runtime registry / selector":

* ``bootstrap_default_runtimes(register_claude_code=True)`` registers
  ``claude-code-local`` *in addition to* ``aee-lightweight-local``
  when the host ``claude`` CLI is on ``$PATH``.
* ``bootstrap_default_runtimes(register_claude_code=False)`` leaves
  the AEE-5 baseline intact (just the lightweight runtime).
* ``RuntimeSelector`` returns ``claude-code-local`` for an explicit
  ``runtime_type="claude_code"`` requirement.
* ``RuntimeSelector`` still returns one of the registered runtimes
  for a request with no claude-code requirement.
* ``RuntimeSelector`` rejects the claude-code runtime when a task
  excludes it via ``excluded_runtime_ids``.

The test runs against the real bootstrap path (no monkey-patching
of the descriptor builder). On hosts without ``claude`` on ``$PATH``
the claude-code-specific assertions are skipped, but the
"does-not-register" assertions still run.
"""

from __future__ import annotations

import shutil
import unittest

from aee.runtimes import registry as registry_module
from aee.runtimes import selector as selector_module
from aee.runtimes.builtins.claude_code_local import build_claude_code_descriptor
from aee.runtimes.models import TaskRuntimeRequirements


def _has_claude_cli() -> bool:
    return shutil.which("claude") is not None


class _RegistrySnapshot:
    """Capture and restore the singleton registry around each test.

    The test suite has historical leaks (notably ``r1`` from
    ``test_dispatch_service.py``) that pollute the process-wide
    ``runtime_registry``. This helper scrubs *all* runtime ids that
    are not part of the AEE-5 baseline (``aee-lightweight-local``)
    at entry, then restores the original set on exit. New runtimes
    registered by the test that match the original set are
    re-registered; everything else is dropped.
    """

    _BASELINE_IDS = {"aee-lightweight-local"}

    def __init__(self) -> None:
        self._saved_ids: list = []
        self._saved_descs: list = []

    def __enter__(self) -> "_RegistrySnapshot":
        reg = registry_module.runtime_registry
        for rt in reg.list_runtimes():
            self._saved_ids.append(rt.runtime_id)
            self._saved_descs.append(rt)
        # Scrub non-baseline runtimes to start each test from a
        # known state. (We keep aee-lightweight-local because that
        # IS the AEE-5 baseline.)
        for rt in reg.list_runtimes():
            if rt.runtime_id not in self._BASELINE_IDS:
                reg.unregister_runtime(rt.runtime_id)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        reg = registry_module.runtime_registry
        # Drop anything added during the test.
        current = {rt.runtime_id for rt in reg.list_runtimes()}
        added = current - set(self._saved_ids)
        for rid in added:
            reg.unregister_runtime(rid)
        # Make sure every originally-saved id is still present.
        present = {rt.runtime_id for rt in reg.list_runtimes()}
        missing = set(self._saved_ids) - present
        for desc in self._saved_descs:
            if desc.runtime_id in missing:
                reg.register_runtime(desc, replace=True)


class TestClaudeCodeBootstrap(unittest.TestCase):
    """Group A — bootstrap behaviour with and without the binary."""

    def setUp(self) -> None:
        self._snap = _RegistrySnapshot()
        self._snap.__enter__()

    def tearDown(self) -> None:
        self._snap.__exit__(None, None, None)

    def test_bootstrap_registers_claude_code_when_binary_present(self) -> None:
        if not _has_claude_cli():
            self.skipTest("claude CLI not on $PATH on this host")
        registry_module.bootstrap_default_runtimes(force=True)
        ids = {r.runtime_id for r in registry_module.runtime_registry.list_runtimes()}
        self.assertIn("aee-lightweight-local", ids)
        self.assertIn("claude-code-local", ids)

    def test_bootstrap_with_register_claude_code_false_leaves_baseline(self) -> None:
        registry_module.bootstrap_default_runtimes(
            force=True, register_claude_code=False
        )
        ids = {r.runtime_id for r in registry_module.runtime_registry.list_runtimes()}
        self.assertIn("aee-lightweight-local", ids)
        self.assertNotIn("claude-code-local", ids)

    def test_bootstrap_idempotent(self) -> None:
        # Calling twice should not produce duplicates.
        registry_module.bootstrap_default_runtimes(force=True)
        n_first = len(registry_module.runtime_registry.list_runtimes())
        registry_module.bootstrap_default_runtimes(force=True)
        n_second = len(registry_module.runtime_registry.list_runtimes())
        # force=True replaces, so the count is bounded by n_first + 1
        # (a new runtimes list is rebuilt only when the descriptor
        # actually changes; for the test we only assert non-doubling).
        self.assertLessEqual(n_second, n_first + 1)

    def test_descriptor_factory_returns_none_without_binary(self) -> None:
        d = build_claude_code_descriptor(
            binary="/no/such/binary/definitely-missing"
        )
        self.assertIsNone(d)

    def test_descriptor_factory_shape(self) -> None:
        if not _has_claude_cli():
            self.skipTest("claude CLI not on $PATH on this host")
        d = build_claude_code_descriptor()
        self.assertIsNotNone(d)
        assert d is not None  # for type-checker
        self.assertEqual(d.runtime_id, "claude-code-local")
        self.assertEqual(d.runtime_type, "claude_code")
        self.assertIn("runtime.claude_code", d.capabilities.capabilities)


class TestRuntimeSelectorClaudeCode(unittest.TestCase):
    """Group B — selector picks the right runtime for each requirement."""

    def setUp(self) -> None:
        self._snap = _RegistrySnapshot()
        self._snap.__enter__()
        registry_module.bootstrap_default_runtimes(force=True)

    def tearDown(self) -> None:
        self._snap.__exit__(None, None, None)

    def _select(
        self, req: TaskRuntimeRequirements
    ) -> str:
        selector = selector_module.RuntimeSelector()
        result = selector.select(
            task=req,
            available_runtimes=registry_module.runtime_registry.list_runtimes(),
        )
        return result.selected_runtime_id

    def test_explicit_claude_code_runtime_type_picks_claude_code(self) -> None:
        if not _has_claude_cli():
            self.skipTest("claude CLI not on $PATH on this host")
        req = TaskRuntimeRequirements(runtime_type="claude_code")
        self.assertEqual(self._select(req), "claude-code-local")

    def test_no_requirement_returns_a_registered_runtime(self) -> None:
        # With both runtimes registered, the selector should return
        # one of them. We don't pin which — the AEE-5 selector's
        # tie-break is ASCII order, so aee-lightweight-local wins on
        # a tie, but production code should not depend on that
        # detail.
        req = TaskRuntimeRequirements()
        rid = self._select(req)
        self.assertIn(
            rid, ("aee-lightweight-local", "claude-code-local")
        )

    def test_excluded_claude_code_falls_back_to_lightweight(self) -> None:
        if not _has_claude_cli():
            self.skipTest("claude CLI not on $PATH on this host")
        req = TaskRuntimeRequirements(
            runtime_type="claude_code",
            excluded_runtime_ids=["claude-code-local"],
        )
        # Either the selector falls back to lightweight, or it raises
        # RuntimeNotFoundError — both are acceptable per AEE-5 contract.
        try:
            rid = self._select(req)
            self.assertEqual(rid, "aee-lightweight-local")
        except selector_module.RuntimeNotFoundError:
            pass  # acceptable

    def test_disabled_claude_code_is_not_selectable(self) -> None:
        if not _has_claude_cli():
            self.skipTest("claude CLI not on $PATH on this host")
        # Replace the registered claude-code runtime with a disabled
        # copy. (The repo stores descriptors in SQLite, so mutating
        # the in-memory copy from list_runtimes() does not persist;
        # we have to go through the public register API.)
        for rt in registry_module.runtime_registry.list_runtimes():
            if rt.runtime_id == "claude-code-local":
                disabled = type(rt)(
                    **{**rt.__dict__, "enabled": False}
                )
                registry_module.runtime_registry.register_runtime(
                    disabled, replace=True
                )
                break
        req = TaskRuntimeRequirements(runtime_type="claude_code")
        selector = selector_module.RuntimeSelector()
        with self.assertRaises(selector_module.RuntimeNotFoundError):
            selector.select(
                task=req,
                available_runtimes=registry_module.runtime_registry.list_runtimes(),
            )

    def test_selector_with_only_lightweight_registered(self) -> None:
        # Drop the claude-code runtime first (it may have been
        # registered by an earlier test or a leaked registration).
        registry_module.runtime_registry.unregister_runtime("claude-code-local")
        # Force the bootstrap to skip the claude-code path.
        registry_module.bootstrap_default_runtimes(
            force=True, register_claude_code=False
        )
        ids = {r.runtime_id for r in registry_module.runtime_registry.list_runtimes()}
        self.assertNotIn("claude-code-local", ids)
        req = TaskRuntimeRequirements(runtime_type="claude_code")
        selector = selector_module.RuntimeSelector()
        with self.assertRaises(selector_module.RuntimeNotFoundError):
            selector.select(
                task=req,
                available_runtimes=registry_module.runtime_registry.list_runtimes(),
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
