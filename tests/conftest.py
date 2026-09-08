"""pytest conftest for ``tests/`` — live-bridge probe + skip policy
+ Telegram notification test isolation guard.

This conftest is loaded automatically by pytest when it discovers
``tests/``. Its job is to make the legacy ``tests/`` suite safer in
the presence of a running supervised bridge:

* When the live bridge is detected on port 8787, the conftest
  inspects the test module under collection and skips any test
  class that has a ``LIVE_DB_REQUIRED = True`` class attribute.
  This is opt-in: the AEE-7.5 G1/G2 write-side tests opt in
  because they intentionally exercise the bridge path.
* When the live bridge is NOT running, the conftest is a no-op
  (legacy tests are allowed to use a tempdir copy).

The conftest is intentionally tiny: it imports nothing from
``dispatcher`` at module load (to avoid triggering
``dispatcher/db.py:21`` evaluation of the production path) and
only touches the live bridge via the TCP-probe helper in
``tests/_live_db_guard.py`` (loaded by file path to avoid the
``tests`` namespace collision with ``hermes-agent/tests``).

Hard rules
----------

* No DB writes, no file unlinks, no module imports of the
  production dispatcher at conftest load time.
* The skip policy is opt-in via ``LIVE_DB_REQUIRED = True`` on
  the test class, so a future test author has to explicitly
  declare the dependency.

Telegram Notification Test Isolation Guard (TASK-20260805-0029 fix)
-------------------------------------------------------------------

An autouse fixture (``_guard_hermes_send_subprocess``) installs a
fail-on-call sentinel on ``subprocess.run`` for every pytest test
session. If ANY test triggers ``subprocess.run(["hermes", "send",
...])``, the sentinel raises ``AssertionError`` immediately —
preventing real Telegram messages from being sent during test
runs. The sentinel ONLY intercepts the ``hermes send`` argv shape;
all other subprocess calls (``git rev-parse``, ``claude -p ...``,
etc.) fall through to the real ``subprocess.run`` so tests that
legitimately use subprocess for non-notification purposes are not
affected.

Tests that intentionally need to mock the notification gate at a
higher level (e.g. ``test_aee_v3_telegram_gate.py`` which
monkey-patches ``subprocess.run`` itself) can opt out by setting
the ``DISABLE_HERMES_SEND_GUARD`` marker on the test function or
class::

    @pytest.mark.disable_hermes_send_guard
    def test_my_custom_notification_mock(): ...

The guard is a **safety net**, not a replacement for per-test
mocking. Tests should still mock ``notify_terminal_with_fallback``
or ``subprocess.run`` as needed; the guard exists to catch cases
where a test forgets to mock (incident root cause: 4 test files
in TASK-20260805-0029 did not mock, sending real Telegram messages
to the production chat).

Telegram Subprocess-Env Sanitizer (TASK-20260908-0007 fix)
----------------------------------------------------------

The sentinel above cannot see inside SPAWNED CHILD PROCESSES.
On 2026-09-07 a sandbox bridge child (``uvicorn app:app`` spawned by
``tests/test_aee76_sandbox_round_trip.py``) loaded the real production
Telegram credentials via ``app.py``'s import-time ``load_dotenv()``
and delivered 4 real Telegram messages (audit message_id 2930-2933).
This conftest now adds three layers:

1. **Session-wide env sanitization** — at conftest import time (before
   any test module imports ``app`` or spawns a child) the pytest
   process's ``os.environ`` gets dummy ``TELEGRAM_BOT_TOKEN`` /
   ``TELEGRAM_CHAT_ID`` / ``BRIDGE_API_KEY`` values; the originals are
   restored at session end. Because the sandbox child env is built
   from ``os.environ`` and ``load_dotenv()`` never overrides
   already-set variables, any child spawned from a sanitized env sees
   only dummy credentials.
2. **Fail-on-call HTTP guard** — ``urllib.request.urlopen`` (and
   ``http.client.HTTPConnection``/``HTTPSConnection``) raise
   ``AssertionError`` when the target host is ``api.telegram.org``,
   telling the test author to mock the transport instead. Opt out per
   test/class with ``@pytest.mark.allow_telegram_http`` (for tests
   that fake the response at the ``urlopen`` layer themselves).
3. **Default sanitized subprocess env** — ``subprocess.Popen`` (the single
   choke point also used by ``subprocess.run`` / ``check_call`` /
   ``check_output``) gets a sanitized ``env=`` injected when the caller
   omits ``env``, and missing Telegram keys filled with dummies when an
   explicit ``env=`` is passed. Tests that deliberately spawn with the
   RAW environment (only the guard self-test) use
   ``@pytest.mark.raw_subprocess_env``.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import urllib.error
import urllib.request
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from typing import Optional

import pytest

# Make the bridge root importable so the live_db_guard module
# can be loaded by the hook below.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ---------------------------------------------------------------------------
# Layer 1 — session-wide Telegram env sanitization (TASK-20260908-0007)
# ---------------------------------------------------------------------------
# Load the sanitizer by file path (same pattern as _live_db_guard.py) and
# sanitize os.environ IMMEDIATELY, before any test module is collected.
# This is what stops spawned children (sandbox bridge, uvicorn, hermes
# subprocesses) from ever seeing real Telegram credentials: their env is
# inherited from THIS process, and any load_dotenv() in the child cannot
# override the dummy values already present.
_env_guard_spec = importlib.util.spec_from_file_location(
    "_aee_env_guard", _ROOT / "tests" / "_env_guard.py"
)
if _env_guard_spec is None or _env_guard_spec.loader is None:  # pragma: no cover
    raise ImportError("could not load _env_guard spec")
env_guard = importlib.util.module_from_spec(_env_guard_spec)
_env_guard_spec.loader.exec_module(env_guard)
env_guard.sanitize_os_environ()


@pytest.fixture
def tmp_db_dir(tmp_path: Path) -> Path:
    """Per-test temp directory for a fresh ``dispatcher.db``.

    Used by ``tests/test_migration_aee1.py::test_run_migrations_public_api_idempotent``
    (added in commit fa98cbf) which rebinds ``dispatcher.db.DB_DIR`` /
    ``DB_PATH`` to this directory and restores the production paths in a
    ``finally`` block. The fixture only provides an empty directory; the
    test is responsible for creating the DB file via
    ``db.run_migrations()``.

    Why this lives in conftest.py: the test was added in fa98cbf with a
    parameter named ``tmp_db_dir`` but no corresponding fixture was
    defined in the repo (verified via ``git grep "def tmp_db_dir"
    $(git rev-list --all)`` -> empty). The error
    "fixture 'tmp_db_dir' not found" has been present since the test's
    introduction. This fixture closes the gap with the smallest possible
    repository change: one fixture in the existing conftest, delegating
    to pytest's built-in ``tmp_path`` for proper lifecycle/cleanup.
    """
    return tmp_path


def _load_guard():
    """Load ``tests/_live_db_guard.py`` by file path so the
    ``tests`` namespace collision with ``hermes-agent/tests``
    doesn't break the import."""
    spec = importlib.util.spec_from_file_location(
        "_aee76_live_db_guard", _ROOT / "tests" / "_live_db_guard.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"could not load guard spec")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def pytest_collection_modifyitems(config, items):
    """Skip ``LIVE_DB_REQUIRED`` test classes when the live bridge
    is running. The live bridge holds the production DB inode
    open; running a module that calls ``DB_PATH.unlink()`` against
    the live DB is unsafe in that state (see AEE_MASTER_PLAN
    §A.7.15).

    This hook is the second line of defense; the first is
    refactoring the unsafe modules to use the ``make_temp_dispatcher_db``
    helper from ``tests._live_db_guard``.
    """
    # Lazy import so the conftest does not pull in
    # ``dispatcher.db`` at pytest startup.
    guard = _load_guard()
    if not guard.is_live_bridge_running():
        # No bridge -> no skip needed.
        return

    import pytest
    skip_marker = pytest.mark.skip(
        reason="LIVE_BRIDGE_RUNNING on port 8787; opt-in LIVE_DB_REQUIRED "
        "tests are unsafe to run concurrently (see AEE_MASTER_PLAN §A.7.15). "
        "Stop the supervised bridge or unset LIVE_DB_REQUIRED."
    )
    for item in items:
        cls = getattr(item, "cls", None)
        if cls is not None and getattr(cls, "LIVE_DB_REQUIRED", False):
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Telegram Notification Test Isolation Guard (TASK-20260805-0029 fix)
# ---------------------------------------------------------------------------
#
# The following autouse fixture installs a fail-on-call sentinel on
# ``subprocess.run`` that raises ``AssertionError`` if any test triggers
# a ``hermes send`` subprocess call. This is the safety net that
# prevents real Telegram messages from being sent during pytest runs.
#
# Tests that intentionally mock ``subprocess.run`` at a higher level
# (e.g. ``test_aee_v3_telegram_gate.py``) can opt out with:
#
#     @pytest.mark.disable_hermes_send_guard
#
# The sentinel ONLY intercepts ``hermes send`` argv; all other
# subprocess calls fall through to the real ``subprocess.run``.

@pytest.fixture(autouse=True)
def _guard_hermes_send_subprocess(request):
    """Autouse fixture: block ``subprocess.run(["hermes", "send", ...])``
    during tests to prevent real Telegram notifications.

    The guard wraps ``subprocess.run`` with a sentinel that checks
    ``argv[0] == "hermes" and argv[1] == "send"``. If matched, it
    raises ``AssertionError`` immediately. All other subprocess calls
    pass through to the real implementation.

    Opt out with ``@pytest.mark.disable_hermes_send_guard`` for tests
    that provide their own ``subprocess.run`` mock.
    """
    # Check for opt-out marker on the test function or its class.
    item = getattr(request, "_pyfuncitem", None) or request
    has_optout = (
        request.node.get_closest_marker("disable_hermes_send_guard")
        if hasattr(request, "node")
        else False
    )
    if has_optout:
        yield
        return

    import subprocess as _sp

    _real_run = _sp.run
    _violations: list = []

    def _guarded_run(argv, *args, **kwargs):
        # Normalize argv: can be a list/tuple or a string.
        if isinstance(argv, str):
            parts = argv.split()
        else:
            parts = list(argv) if argv else []
        if len(parts) >= 2 and parts[0] == "hermes" and parts[1] == "send":
            _violations.append(list(parts))
            raise AssertionError(
                f"BLOCKED: subprocess.run invoked 'hermes send' during "
                f"test (argv={parts!r}); this would send a real Telegram "
                f"message. Mock dispatcher.notifier.notify_terminal_with_fallback "
                f"or subprocess.run in the test. To intentionally bypass "
                f"this guard, use @pytest.mark.disable_hermes_send_guard."
            )
        return _real_run(argv, *args, **kwargs)

    _sp.run = _guarded_run
    try:
        yield
    finally:
        _sp.run = _real_run


# ---------------------------------------------------------------------------
# Layer 2 — fail-on-call HTTP guard for api.telegram.org (TASK-20260908-0007)
# ---------------------------------------------------------------------------
# Blocks IN-PROCESS urllib/http.client calls to api.telegram.org with an
# AssertionError pointing the author at the right mock. Child processes are
# covered by Layer 1 (dummy credentials) + Layer 3 (sanitized env
# inheritance); this layer is the in-process tripwire.


def _register_guard_markers(config):
    config.addinivalue_line(
        "markers",
        "allow_telegram_http: opt-out for the api.telegram.org HTTP guard "
        "(tests that fake the Telegram transport at the urlopen layer)",
    )
    config.addinivalue_line(
        "markers",
        "raw_subprocess_env: opt-out for the sanitized-default subprocess "
        "env injection (tests that deliberately spawn with the raw env)",
    )
    config.addinivalue_line(
        "markers",
        "allow_notification_gate: opt-out for the shared fail-closed "
        "notification suppression gate (TASK-20260908-0008) — only for "
        "tests that deliberately exercise the gate's OPEN path with "
        "their own transport mocks",
    )


def pytest_configure(config):
    _register_guard_markers(config)


def _telegram_http_blocked(target) -> AssertionError:
    return AssertionError(
        f"BLOCKED: outbound HTTP call to api.telegram.org during tests "
        f"(target={target!r}); this would send a REAL Telegram message. "
        f"Mock the transport in the test (mock.patch "
        f"'dispatcher.notifier.urllib.request.urlopen' or the module-level "
        f"urlopen), or rely on the dummy Telegram credentials installed by "
        f"tests/_env_guard.py. To intentionally allow this call, use "
        f"@pytest.mark.allow_telegram_http."
    )


def _install_telegram_http_guard() -> None:
    """Wrap urllib.request.urlopen + http.client connection classes."""
    _real_urlopen = urllib.request.urlopen

    def _guarded_urlopen(url, *args, **kwargs):
        host = env_guard.url_host(url)
        if host in env_guard.TELEGRAM_API_HOSTS:
            raise _telegram_http_blocked(url)
        return _real_urlopen(url, *args, **kwargs)

    urllib.request.urlopen = _guarded_urlopen

    _real_http_init = HTTPConnection.__init__
    _real_https_init = HTTPSConnection.__init__

    def _guarded_http_init(self, host, *args, **kwargs):
        if str(host).lower() in env_guard.TELEGRAM_API_HOSTS:
            raise _telegram_http_blocked(host)
        return _real_http_init(self, host, *args, **kwargs)

    def _guarded_https_init(self, host, *args, **kwargs):
        if str(host).lower() in env_guard.TELEGRAM_API_HOSTS:
            raise _telegram_http_blocked(host)
        return _real_https_init(self, host, *args, **kwargs)

    HTTPConnection.__init__ = _guarded_http_init
    HTTPSConnection.__init__ = _guarded_https_init

    _install_telegram_http_guard._restore = (  # type: ignore[attr-defined]
        lambda: (
            setattr(urllib.request, "urlopen", _real_urlopen),
            setattr(HTTPConnection, "__init__", _real_http_init),
            setattr(HTTPSConnection, "__init__", _real_https_init),
        )
    )


def pytest_sessionstart(session):
    """Re-assert sanitization + install the HTTP guard for the session.

    (Sanitization already ran at conftest import time; this call is
    idempotent and also covers plugins that mutated the env during
    early config.)
    """
    env_guard.sanitize_os_environ()
    _install_telegram_http_guard()
    _install_notification_sink_redirect()
    # Layer 0 (TASK-20260908-0008): arm the SHARED fail-closed
    # suppression gate for the whole pytest session. This covers the
    # notifier's direct send entry points (``_send_telegram``,
    # ``notify_terminal_hermes_gateway``, ``notify_terminal_inprocess``)
    # which the argv sentinel / HTTP guard cannot see when a test stubs
    # the transport itself, and it arms the env sentinel so ANY spawned
    # child is suppressed. Opt out per test with
    # @pytest.mark.allow_notification_gate (only for tests that
    # deliberately exercise the gate's open path with their own mocks).
    from aee import _notification_guard as _task0008_guard

    _task0008_guard.enter_verification_mode()
    if _AUDIT_SINK_DIR is not None:
        _task0008_guard.set_audit_sink(
            _AUDIT_SINK_DIR / "suppressed_sends.jsonl"
        )


def pytest_sessionfinish(session, exitstatus):
    """Restore the real Telegram env AFTER the session ends (Layer 1)."""
    from aee import _notification_guard as _task0008_guard

    _task0008_guard.exit_verification_mode()
    env_guard.restore_os_environ()


# ---------------------------------------------------------------------------
# Layer 4 — notification audit / local-log sink redirect
# ---------------------------------------------------------------------------
# ``dispatcher.notifier._append_notification_audit`` and
# ``_append_local_log`` append to the LIVE ``logs/`` directory. Every
# notification gate fired inside the pytest process (even one whose
# ``hermes send`` was successfully mocked — the mock returns a fake
# message_id, the gate reports sent=True) then writes a
# ``sent:true``-shaped row into the production
# ``logs/notification_audit.jsonl``, which is exactly what the
# TASK-20260908-0007 acceptance audit counts. Redirect BOTH writers to
# a session-local sink under /tmp for the whole pytest session so the
# production audit file only ever receives rows from REAL bridge
# processes (the live bridge's own lifecycle notifications, and
# spawned sandbox children — which run with notifications disabled).
# Tests that assert on the audit trail read the session sink via
# :func:`session_notification_audit_path` /
# :func:`session_notifier_log_path`.


_AUDIT_SINK_DIR: Optional[Path] = None


def _install_notification_sink_redirect() -> None:
    """Point dispatcher.notifier's audit + local-log appends at a
    session-local /tmp sink (idempotent)."""
    global _AUDIT_SINK_DIR
    import json as _json
    import tempfile as _tempfile

    import dispatcher.notifier as _notifier

    if _AUDIT_SINK_DIR is not None:
        return
    _AUDIT_SINK_DIR = Path(_tempfile.mkdtemp(prefix="a2-notification-sink-"))
    env_guard.AUDIT_SINK_DIR = _AUDIT_SINK_DIR  # type: ignore[attr-defined]
    sink_audit = _AUDIT_SINK_DIR / "notification_audit.jsonl"
    sink_local = _AUDIT_SINK_DIR / "notifier.log"

    def _redirected_audit(record):
        try:
            with sink_audit.open("a", encoding="utf-8") as f:
                f.write(_json.dumps(record, default=str, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 — mirror the production swallow
            pass

    def _redirected_local(line):
        try:
            with sink_local.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass

    _install_notification_sink_redirect._originals = (  # type: ignore[attr-defined]
        _notifier._append_notification_audit,
        _notifier._append_local_log,
    )
    _notifier._append_notification_audit = _redirected_audit
    _notifier._append_local_log = _redirected_local


def session_notification_audit_path() -> Path:
    """The audit-log path tests should assert against: the session sink
    when the redirect is installed, the live path otherwise."""
    if _AUDIT_SINK_DIR is not None:
        return _AUDIT_SINK_DIR / "notification_audit.jsonl"
    from dispatcher.manager import _BRIDGE_ROOT  # noqa: PLC0415

    return _BRIDGE_ROOT / "logs" / "notification_audit.jsonl"


def session_notifier_log_path() -> Path:
    """The notifier.log path tests should assert against (session sink
    when installed, live path otherwise)."""
    if _AUDIT_SINK_DIR is not None:
        return _AUDIT_SINK_DIR / "notifier.log"
    from dispatcher.manager import _BRIDGE_ROOT  # noqa: PLC0415

    return _BRIDGE_ROOT / "logs" / "notifier.log"


# ---------------------------------------------------------------------------
# Layer 3 — sanitized default env for subprocess spawning (TASK-20260908-0007)
# ---------------------------------------------------------------------------
# ONE choke point: subprocess.Popen. ``subprocess.run`` / ``check_call`` /
# ``check_output`` all delegate to the module-global ``Popen``, so wrapping
# the class covers every stdlib path. Two behaviours:
#
# * env omitted (inheritance) -> inject ``env_guard.sanitized_env()`` so the
#   child cannot inherit real Telegram credentials from ``os.environ``;
# * explicit ``env=`` given -> fill in ONLY the missing Telegram keys with
#   dummies (whitelist envs like the sandbox builder's are then harmless
#   even if a future edit drops a key; explicit dummy values are kept).
#
# Tests that deliberately need the RAW environment (only the guard
# self-test) use ``@pytest.mark.raw_subprocess_env``.


@pytest.fixture(autouse=True)
def _guard_subprocess_env(request):
    """Autouse fixture: sanitize the env of every subprocess.Popen spawn
    (inherited or explicit) for the duration of each test."""
    if request.node.get_closest_marker("raw_subprocess_env"):
        yield
        return

    import subprocess as _sp

    _real_popen = _sp.Popen

    def _sanitized_kwargs(kwargs):
        env = kwargs.get("env")
        if env is None:
            kwargs["env"] = env_guard.sanitized_env()
        else:
            merged = dict(env)
            for key, dummy in env_guard.DUMMY_TELEGRAM_ENV.items():
                merged.setdefault(key, dummy)
            kwargs["env"] = merged
        return kwargs

    class _GuardedPopen(_real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **_sanitized_kwargs(kwargs))

    _sp.Popen = _GuardedPopen
    try:
        yield
    finally:
        _sp.Popen = _real_popen


@pytest.fixture(autouse=True)
def _resanitize_env_per_test():
    """Autouse fixture: re-assert the dummy Telegram env before every
    test. Legacy tests mutate ``os.environ`` directly (e.g.
    ``os.environ["TELEGRAM_CHAT_ID"] = "5132341473"`` in the blocking-
    gate suite); without this re-assertion the dirt leaks into every
    later test's environment and into spawned children. The sanitizer
    is idempotent and never saves dirty values over the true originals,
    so the session-finish restore still puts back exactly what the
    operator had."""
    env_guard.sanitize_os_environ()
    yield


# ---------------------------------------------------------------------------
# Layer 2b — per-test allow switch for the HTTP guard
# ---------------------------------------------------------------------------
# The HTTP guard is installed once per session (pytest_sessionstart).
# For tests carrying @pytest.mark.allow_telegram_http the autouse fixture
# below temporarily restores the REAL urlopen/connection constructors so
# the test's own transport fakes (or deliberate real-transport probes)
# behave as written.


@pytest.fixture(autouse=True)
def _allow_telegram_http_switch(request):
    if not request.node.get_closest_marker("allow_telegram_http"):
        yield
        return

    _guard = getattr(_install_telegram_http_guard, "_restore", None)
    if _guard is None:  # pragma: no cover — sessionstart always ran
        yield
        return
    _guard()
    try:
        yield
    finally:
        _install_telegram_http_guard()
        # Re-assert in case the test dirtied the env on purpose.
        env_guard.sanitize_os_environ()


@pytest.fixture(autouse=True)
def _allow_notification_gate_switch(request):
    """Per-test opt-out for the shared suppression gate
    (TASK-20260908-0008). Tests carrying
    @pytest.mark.allow_notification_gate (e.g. tests that verify the
    gate's OPEN path with their own subprocess/transport mocks)
    temporarily disarm verification mode. The production sentinel is
    NEVER installed by this fixture — only verification depth is
    borrowed, so the env sentinel and global state are restored
    verbatim."""
    if not request.node.get_closest_marker("allow_notification_gate"):
        yield
        return

    from aee import _notification_guard as _task0008_guard

    _task0008_guard.exit_verification_mode()
    try:
        yield
    finally:
        _task0008_guard.enter_verification_mode()
