"""Targeted tests for ``aee.adapters.dsh_headless.DshHeadlessAdapter``.

Scope: confirm the adapter satisfies the ``RuntimeAdapter`` Protocol,
constructs argv safely (no shell, no string interpolation), and maps
DSH subprocess outcomes into the existing AEE result dataclasses.

These tests do NOT require a live DSH installation — they mock
``asyncio.create_subprocess_exec`` so the contract is exercised
deterministically. The headless smoke test that actually invokes the
DSH CLI is documented separately in the implementation report.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from aee.adapters import (  # noqa: E402
    DshHeadlessAdapter,
    RuntimeAdapter,
    UnknownExternalRunError,
)
from aee.adapters.base import RuntimeError as AdapterRuntimeError  # noqa: E402
from aee.adapters.dsh_headless import (  # noqa: E402
    _safe_tail as _real_safe_tail,
)
from aee.adapters.dsh_headless import _safe_text as _real_safe_text  # noqa: E402
from aee.core.job_models import Job  # noqa: E402
from aee.core.registry import (  # noqa: E402
    AdapterRegistry,
    adapter_registry,
    register_dsh_headless,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fake subprocess harness
# ---------------------------------------------------------------------------


@dataclass
class _FakeProc:
    """Stand-in for the object returned by ``create_subprocess_exec``.

    The adapter only reads ``returncode`` and ``pid`` and awaits
    ``communicate()``; we provide just enough surface area for the
    adapter's success, failure, timeout, and credential paths.
    """

    pid: int
    returncode: int
    stdout: bytes
    stderr: bytes
    # If non-None, ``communicate`` raises ``asyncio.TimeoutError`` once
    # and then on the second await returns empty bytes (mirrors the
    # adapter's recovery path).
    timeout_once: bool = False
    killed: bool = False

    async def communicate(self) -> Tuple[bytes, bytes]:
        if self.timeout_once:
            self.timeout_once = False
            raise asyncio.TimeoutError()
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _install_subprocess_mock(
    monkeypatch,
    *,
    queue: List[_FakeProc],
) -> List[List[str]]:
    """Patch ``asyncio.create_subprocess_exec`` to pop from ``queue``.

    Returns a list that captures the ``argv`` each call received so the
    test can assert on command construction.
    """
    captured: List[List[str]] = []

    async def _fake_create_subprocess_exec(*argv, **_kwargs):
        captured.append(list(argv))
        if not queue:
            raise AssertionError("subprocess queue empty — adapter called create_subprocess_exec more times than the test staged")
        return queue.pop(0)

    if hasattr(monkeypatch, "setattr"):
        monkeypatch.setattr(
            "aee.adapters.dsh_headless.asyncio.create_subprocess_exec",
            _fake_create_subprocess_exec,
        )
    else:  # standalone runner stub
        import aee.adapters.dsh_headless as _mod
        monkeypatch.install(_mod, "asyncio", _fake_create_subprocess_exec)
    return captured


# ---------------------------------------------------------------------------
# 14.1 Adapter construction
# ---------------------------------------------------------------------------


def test_adapter_construction_and_protocol_shape():
    a = DshHeadlessAdapter()
    assert isinstance(a, RuntimeAdapter)
    assert a.name == "dsh-headless"
    assert a.runtime_type == "dsh_headless"
    for attr in ("submit", "poll", "cancel", "health"):
        assert callable(getattr(a, attr)), f"DshHeadlessAdapter missing {attr}"
    print("  OK   protocol shape + name/runtime_type")


def test_adapter_resolves_via_explicit_opt_in_registry(monkeypatch):
    # Bootstrap does NOT register dsh-headless (it must remain
    # explicit-opt-in only).
    from aee.core.registry import bootstrap_defaults
    bootstrap_defaults(force=True)
    assert "dsh-headless" not in adapter_registry.names()

    # Explicit opt-in adds it; idempotent on second call.
    reg = AdapterRegistry()
    assert register_dsh_headless.__name__ == "register_dsh_headless"
    # The module-level singleton gets the registration; clear it
    # first to keep the assertion independent of other test runs.
    adapter_registry.unregister("dsh-headless")
    added_first = register_dsh_headless()
    added_second = register_dsh_headless()
    assert added_first is True
    assert added_second is False
    assert "dsh-headless" in adapter_registry.names()
    assert isinstance(adapter_registry.get("dsh-headless"), DshHeadlessAdapter)

    # Cleanup so this test does not leak state into sibling tests.
    adapter_registry.unregister("dsh-headless")
    # Restore the bootstrap defaults so other tests keep their expected
    # adapter set.
    bootstrap_defaults(force=True)
    print("  OK   explicit opt-in registry: not in bootstrap; register_dsh_headless idempotent")


# ---------------------------------------------------------------------------
# 14.2 Command construction (argv safety)
# ---------------------------------------------------------------------------


def test_argv_construction_no_shell(monkeypatch):
    """Argv shape: ``[node, cli, --profile, headless, <task>]``.

    No ``shell=True`` interpolation; task is a single argv entry even
    when it contains characters that would be dangerous in a shell
    context.
    """
    a = DshHeadlessAdapter()
    argv = a._build_argv("hello world; rm -rf /tmp/should-not-run")
    assert argv == [
        "node",
        "/workspace/deepseek-harness/apps/cli/lib/bin.js",
        "--profile",
        "headless",
        "hello world; rm -rf /tmp/should-not-run",
    ]
    # Verify env override knobs are honored.
    a2 = DshHeadlessAdapter(
        node_bin="/custom/node",
        cli_path="/custom/bin.js",
        profile="custom",
    )
    argv2 = a2._build_argv("task")
    assert argv2 == ["/custom/node", "/custom/bin.js", "--profile", "custom", "task"]
    print("  OK   argv: no shell, single argv entry for task, env overrides honored")


def test_submit_invokes_subprocess_with_expected_argv(monkeypatch):
    a = DshHeadlessAdapter()
    proc = _FakeProc(pid=1234, returncode=0, stdout=b"assistant text\n", stderr=b"")
    captured = _install_subprocess_mock(monkeypatch, queue=[proc])

    job = Job(title="t", input="say hi")
    res = _run(a.submit(job))

    # submit() waited for the child and returned a terminal result.
    assert res.status == "completed"
    assert captured, "create_subprocess_exec was not invoked"
    argv = captured[0]
    assert argv[0] == "node"
    assert argv[1] == "/workspace/deepseek-harness/apps/cli/lib/bin.js"
    assert argv[2:4] == ["--profile", "headless"]
    assert argv[4] == "# t\n\nsay hi"
    print("  OK   submit() argv contains node + cli + --profile headless + task")


# ---------------------------------------------------------------------------
# 14.3 Successful execution mapping
# ---------------------------------------------------------------------------


def test_successful_run_maps_to_terminal_submit_and_idempotent_poll(monkeypatch):
    a = DshHeadlessAdapter()
    proc = _FakeProc(
        pid=4242,
        returncode=0,
        stdout=b"the assistant said hi\n",
        stderr=b"",
    )
    _install_subprocess_mock(monkeypatch, queue=[proc])

    job = Job(title="t", input="say hi")
    submit = _run(a.submit(job))
    assert submit.external_run_id.startswith("dsh-headless-")
    assert submit.status == "completed"
    assert submit.raw["exit_code"] == 0
    # Credentials reported as present/absent (P0 bridge §9: this host
    # routes to ollama-cloud, so the diagnostic includes OLLAMA_API_KEY
    # in addition to the DeepSeek forward-compat keys).
    assert submit.raw["credentials"]["OLLAMA_API_KEY"] in {"present", "absent"}
    assert submit.raw["credentials"]["DEEPSEEK_API_KEY"] in {"present", "absent"}
    # No credential value is exposed.
    raw_str = repr(submit.raw)
    assert "sk-" not in raw_str  # no secret prefix
    assert "OLLAMA_API_KEY=" not in raw_str
    assert "DEEPSEEK_API_KEY=" not in raw_str

    poll1 = _run(a.poll(submit.external_run_id))
    poll2 = _run(a.poll(submit.external_run_id))
    for p in (poll1, poll2):
        assert p.is_terminal is True
        assert p.status == "completed"
        assert p.output == "the assistant said hi\n"
        assert p.error is None
    print("  OK   success path: terminal submit + idempotent terminal poll")


# ---------------------------------------------------------------------------
# 14.4 Failure mapping
# ---------------------------------------------------------------------------


def test_non_zero_exit_maps_to_failed_submit(monkeypatch):
    a = DshHeadlessAdapter()
    proc = _FakeProc(
        pid=99,
        returncode=1,
        stdout=b"",
        stderr=b"some unrelated dsh error\n",
    )
    _install_subprocess_mock(monkeypatch, queue=[proc])

    job = Job(title="t", input="x")
    res = _run(a.submit(job))
    assert res.status == "failed"
    assert res.raw["failure_kind"] == "execution_failed"
    # Stderr tail is reported (last 64 KiB, trimmed), but the task text
    # is never echoed back.
    assert "some unrelated dsh error" in res.raw["stderr_tail"]
    print("  OK   non-zero exit -> RuntimeSubmitResult status=failed, failure_kind=execution_failed")


def test_missing_credential_maps_to_deterministic_failure(monkeypatch):
    a = DshHeadlessAdapter()
    # Real DSH stderr shape when OLLAMA_API_KEY is absent (the
    # adapter is intentionally forgiving — multiple substring
    # sentinels so future DSH versions that rephrase the error
    # still land on this branch).
    proc = _FakeProc(
        pid=100,
        returncode=1,
        stdout=b"",
        stderr=b"Error: OLLAMA_API_KEY is required (MISSING_CREDENTIAL)\n",
    )
    _install_subprocess_mock(monkeypatch, queue=[proc])

    job = Job(title="t", input="x")
    res = _run(a.submit(job))
    # Must NOT be a silent PASS.
    assert res.status == "failed"
    assert res.raw["failure_kind"] == "missing_credential"
    # Diagnostic reports VARIABLE_NAME: present/absent only — keys
    # now include OLLAMA_API_KEY (P0 bridge §9) alongside the
    # DeepSeek forward-compat entries.
    creds = res.raw["credentials"]
    assert "OLLAMA_API_KEY" in creds
    assert "DEEPSEEK_API_KEY" in creds
    assert "DEEPSEEK_BASE_URL" in creds
    assert all(v in {"present", "absent"} for v in creds.values())
    # The actual credential value (if any) must not appear anywhere
    # in the returned mapping.
    assert "OLLAMA_API_KEY=" not in repr(res.raw)
    assert "DEEPSEEK_API_KEY=" not in repr(res.raw)
    print("  OK   MISSING_CREDENTIAL -> status=failed, failure_kind=missing_credential (never PASS)")


def test_missing_executable_raises_runtime_error(monkeypatch, tmp_path):
    # Point the adapter at a CLI path that does not exist.
    missing = tmp_path / "does-not-exist.js"
    a = DshHeadlessAdapter(cli_path=str(missing))

    job = Job(title="t", input="x")
    try:
        _run(a.submit(job))
    except AdapterRuntimeError as exc:
        assert "not found" in str(exc).lower()
    else:
        raise AssertionError("expected AdapterRuntimeError when CLI is missing")
    print("  OK   missing CLI -> AdapterRuntimeError (transport-level)")


def test_timeout_returns_timeout_submit(monkeypatch):
    a = DshHeadlessAdapter(timeout_seconds=5)
    proc = _FakeProc(
        pid=7,
        returncode=0,
        stdout=b"",
        stderr=b"",
        timeout_once=True,
    )
    _install_subprocess_mock(monkeypatch, queue=[proc])

    job = Job(title="t", input="x")
    res = _run(a.submit(job))
    # Adapter maps timeout to ``status=timeout`` (terminal) on the
    # submit result so the watcher can short-circuit polling.
    assert res.status == "timeout"
    assert proc.killed is True
    print("  OK   timeout -> status=timeout (terminal) + child killed")


def test_poll_unknown_raises():
    a = DshHeadlessAdapter()
    try:
        _run(a.poll("dsh-headless-nope"))
    except UnknownExternalRunError as exc:
        assert "dsh-headless-nope" in str(exc)
    else:
        raise AssertionError("expected UnknownExternalRunError")
    print("  OK   poll(unknown) -> UnknownExternalRunError")


# ---------------------------------------------------------------------------
# Helpers — directly exercise private helpers to lock in behaviour
# ---------------------------------------------------------------------------


def test_safe_text_bounded_decode():
    huge = b"x" * (300 * 1024)
    out = _real_safe_text(huge)
    assert isinstance(out, str)
    assert len(out) <= 256 * 1024
    print("  OK   _safe_text caps at 256 KiB")


def test_safe_tail_trims():
    tail = _real_safe_tail(b"y" * 100, limit=10)
    assert tail == "y" * 10
    print("  OK   _safe_tail honours limit")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    """Standalone runner — works with or without pytest.

    When invoked via pytest, the ``monkeypatch`` fixture is injected
    into each test. When invoked via ``python tests/test_*.py``, we
    supply a tiny stub that supports the same ``setattr`` /
    ``install`` surface used by ``_install_subprocess_mock``.
    """
    if "pytest" in sys.modules:
        # pytest will discover and run the test_* functions directly;
        # we still expose this entry point so ``python tests/test_*.py``
        # produces a sensible message rather than a stack trace.
        print("=== AEE DshHeadlessAdapter targeted tests ===")
        print("(run via pytest for full coverage; standalone runner skipped)")
        return 0

    class _StandaloneMocker:
        def __init__(self) -> None:
            self._restore: list = []

        def setattr(self, target: str, value: Any) -> None:
            import importlib
            module_name, _, attr = target.rpartition(".")
            module = importlib.import_module(module_name)
            self._restore.append((module, attr, getattr(module, attr, None)))
            setattr(module, attr, value)

        def install(self, module: Any, attr: str, value: Any) -> None:
            self._restore.append((module, attr, getattr(module, attr, None)))
            setattr(module, attr, value)

        def undo(self) -> None:
            while self._restore:
                module, attr, previous = self._restore.pop()
                if previous is None:
                    try:
                        delattr(module, attr)
                    except AttributeError:
                        pass
                else:
                    setattr(module, attr, previous)

    print("=== AEE DshHeadlessAdapter targeted tests ===")
    tests = [
        test_adapter_construction_and_protocol_shape,
        test_adapter_resolves_via_explicit_opt_in_registry,
        test_argv_construction_no_shell,
        test_submit_invokes_subprocess_with_expected_argv,
        test_successful_run_maps_to_terminal_submit_and_idempotent_poll,
        test_non_zero_exit_maps_to_failed_submit,
        test_missing_credential_maps_to_deterministic_failure,
        test_missing_executable_raises_runtime_error,
        test_timeout_returns_timeout_submit,
        test_poll_unknown_raises,
        test_safe_text_bounded_decode,
        test_safe_tail_trims,
    ]
    failures = 0
    for t in tests:
        mocker = _StandaloneMocker()
        try:
            # The tests that don't need monkeypatch ignore the extra arg.
            try:
                t(mocker)  # type: ignore[arg-type]
            except TypeError:
                # Signature without monkeypatch — retry without it.
                t()
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {t.__name__}: {type(exc).__name__}: {exc}")
            failures += 1
        finally:
            mocker.undo()
    if failures:
        print(f"\n{failures} FAILURE(S)")
        return 1
    print()
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
