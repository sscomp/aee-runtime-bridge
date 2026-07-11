"""pytest conftest for ``tests/`` — live-bridge probe + skip policy.

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
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Make the bridge root importable so the live_db_guard module
# can be loaded by the hook below.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


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
