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
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Make the bridge root importable so the live_db_guard module
# can be loaded by the hook below.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


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
