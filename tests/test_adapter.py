"""AEE-1 contract tests for the RuntimeAdapter Protocol.

The tests in this file use `FakeAdapter` as the canonical reference
implementation; the same tests must pass for any new adapter
(HermesAdapter, future Pi / Claude Code adapters) once they
implement the Protocol. Use this file as the regression harness
when adding a new adapter.

Coverage:
    1. Protocol shape (name, runtime_type, methods)
    2. submit returns a RuntimeSubmitResult with a non-empty id
    3. poll reports state transitions
    4. cancel transitions to cancelled
    5. Unknown ids raise UnknownExternalRunError
    6. AdapterRegistry get/lookup/replace semantics
    7. submit() payload translation (input, session_id, mode)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aee.adapters import (  # noqa: E402
    AdapterNotFoundError,
    FakeAdapter,
    HermesAdapter,
    RuntimeAdapter,
    RuntimeCancelResult,
    RuntimePollResult,
    RuntimeSubmitResult,
    UnknownExternalRunError,
)
from aee.adapters.base import RuntimeError as AdapterRuntimeError  # noqa: E402
from aee.core import (  # noqa: E402
    AdapterRegistry,
    Job,
    JobStatus,
    can_transition,
)
from aee.core.registry import (  # noqa: E402
    adapter_registry,
    bootstrap_defaults,
)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# 1. Protocol shape
# ---------------------------------------------------------------------------


def test_protocol_shape():
    a = FakeAdapter()
    assert isinstance(a, RuntimeAdapter)
    assert a.name == "fake"
    assert a.runtime_type == "fake"
    for attr in ("submit", "poll", "cancel"):
        assert callable(getattr(a, attr)), f"RuntimeAdapter missing {attr}"
    print("  OK   protocol shape")


# ---------------------------------------------------------------------------
# 2. submit
# ---------------------------------------------------------------------------


def test_submit_returns_submit_result():
    a = FakeAdapter()
    job = Job(title="t", input="echo hi", session_id="s1", mode="ops")
    res = run(a.submit(job))
    assert isinstance(res, RuntimeSubmitResult)
    assert res.external_run_id.startswith("FAKE-")
    assert res.status == "queued"
    assert a.submitted_jobs and a.submitted_jobs[0]["input"] == "echo hi"
    assert a.submitted_jobs[0]["session_id"] == "s1"
    assert a.submitted_jobs[0]["mode"] == "ops"
    print("  OK   submit returns RuntimeSubmitResult with non-empty id")


# ---------------------------------------------------------------------------
# 3. poll — state transitions
# ---------------------------------------------------------------------------


def test_poll_queued_running_completed():
    a = FakeAdapter()
    job = Job(title="t", input="x")
    res = run(a.submit(job))
    rid = res.external_run_id

    p = run(a.poll(rid))
    assert isinstance(p, RuntimePollResult)
    assert p.status == "queued"
    assert p.is_terminal is False
    assert p.output is None

    run(a.mark_running(rid))
    p = run(a.poll(rid))
    assert p.status == "running"
    assert p.is_terminal is False

    run(a.mark_completed(rid, output="hello", usage={"tokens": 7}))
    p = run(a.poll(rid))
    assert p.status == "completed"
    assert p.is_terminal is True
    assert p.output == "hello"
    assert p.usage == {"tokens": 7}
    print("  OK   poll observes queued -> running -> completed")


# ---------------------------------------------------------------------------
# 4. cancel
# ---------------------------------------------------------------------------


def test_cancel_running():
    a = FakeAdapter()
    job = Job(title="t", input="x")
    rid = run(a.submit(job)).external_run_id
    run(a.mark_running(rid))
    res = run(a.cancel(rid))
    assert isinstance(res, RuntimeCancelResult)
    assert res.cancelled is True
    p = run(a.poll(rid))
    assert p.status == "cancelled"
    assert p.is_terminal is True
    print("  OK   cancel transitions running -> cancelled")


def test_cancel_unknown_returns_cancelled():
    a = FakeAdapter()
    res = run(a.cancel("FAKE-doesnotexist"))
    # We treat 404 as "already gone" = cancelled from our side.
    assert res.cancelled is True
    assert "not found" in res.reason
    print("  OK   cancel of unknown id is treated as already gone")


# ---------------------------------------------------------------------------
# 5. Unknown ids
# ---------------------------------------------------------------------------


def test_poll_unknown_raises():
    a = FakeAdapter()
    try:
        run(a.poll("FAKE-nope"))
    except UnknownExternalRunError as exc:
        assert "FAKE-nope" in str(exc)
    else:
        raise AssertionError("expected UnknownExternalRunError")
    print("  OK   poll of unknown id raises UnknownExternalRunError")


def test_submit_hook_can_raise_runtime_error():
    def hook(_job, _kind):
        return AdapterRuntimeError("synthetic transport failure")
    a = FakeAdapter()
    a.hook = hook
    job = Job(title="t", input="x")
    try:
        run(a.submit(job))
    except AdapterRuntimeError as exc:
        assert "synthetic" in str(exc)
    else:
        raise AssertionError("expected AdapterRuntimeError")
    print("  OK   submit() hook can raise RuntimeError to simulate failure")


# ---------------------------------------------------------------------------
# 6. AdapterRegistry
# ---------------------------------------------------------------------------


def test_registry_get_and_notfound():
    reg = AdapterRegistry()
    reg.register(FakeAdapter(), replace=True)
    assert reg.get("fake").name == "fake"
    try:
        reg.get("nope")
    except AdapterNotFoundError as exc:
        assert "nope" in str(exc)
    else:
        raise AssertionError("expected AdapterNotFoundError")
    # duplicate register without replace raises
    try:
        reg.register(FakeAdapter(), replace=False)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on duplicate register")
    # replace=True ok
    reg.register(FakeAdapter(), replace=True)
    print("  OK   AdapterRegistry get / not-found / replace")


def test_bootstrap_defaults_installs_hermes():
    # Ensure adapter_registry contains "hermes" after bootstrap.
    before = "hermes" in adapter_registry.names()
    bootstrap_defaults(force=True)
    assert "hermes" in adapter_registry.names()
    hermes = adapter_registry.get("hermes")
    assert isinstance(hermes, HermesAdapter)
    # Force=False is a no-op (doesn't raise).
    bootstrap_defaults(force=False)
    print("  OK   bootstrap_defaults installs HermesAdapter")


# ---------------------------------------------------------------------------
# 7. HermesAdapter payload translation (no network)
# ---------------------------------------------------------------------------


def test_hermes_payload_uses_legacy_instructions():
    """HermesAdapter._build_submit_payload should include the
    legacy `instructions` text verbatim so existing behaviour is
    preserved. This is a regression test against accidental
    rewording of the prompt header.
    """
    h = HermesAdapter(base_url="http://example.invalid", api_key="dummy")
    job = Job(
        title="t",
        input="hello",
        session_id="s",
        client_source="gpt",
        model_name="claude-sonnet-4-6",
        mode="ops",
    )
    payload = h._build_submit_payload(job)
    assert payload["input"] == "hello"
    assert payload["session_id"] == "s"
    assert "Hermes M2" in payload["instructions"]
    assert "Never echo API keys" in payload["instructions"]
    md = payload.get("metadata", {})
    assert md.get("client_source") == "gpt"
    assert md.get("model_name") == "claude-sonnet-4-6"
    assert md.get("mode") == "ops"
    # mode=normal should NOT set a `mode` key in metadata (matches legacy)
    job2 = Job(title="t", input="x", mode="normal", client_source="gpt", model_name="m")
    payload2 = h._build_submit_payload(job2)
    assert "mode" not in payload2.get("metadata", {})
    print("  OK   HermesAdapter payload translation preserves legacy fields")


# ---------------------------------------------------------------------------
# 8. State machine cross-checks
# ---------------------------------------------------------------------------


def test_state_machine_terminal_is_sink():
    for s in JobStatus.TERMINAL:
        for t in JobStatus.ALL:
            assert not can_transition(s, t), f"terminal {s} should not transition to {t}"
    # queued -> running, cancelled, failed, timeout all ok
    for t in ("running", "cancelled", "failed", "timeout"):
        assert can_transition("queued", t)
    # running -> all four non-queued states
    for t in ("completed", "cancelled", "failed", "timeout"):
        assert can_transition("running", t)
    print("  OK   JobStatus.TERMINAL is a sink; legal transitions documented")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    print("=== AEE-1 adapter contract ===")
    tests = [
        test_protocol_shape,
        test_submit_returns_submit_result,
        test_poll_queued_running_completed,
        test_cancel_running,
        test_cancel_unknown_returns_cancelled,
        test_poll_unknown_raises,
        test_submit_hook_can_raise_runtime_error,
        test_registry_get_and_notfound,
        test_bootstrap_defaults_installs_hermes,
        test_hermes_payload_uses_legacy_instructions,
        test_state_machine_terminal_is_sink,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {t.__name__}: {type(exc).__name__}: {exc}")
            return 1
    print()
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
