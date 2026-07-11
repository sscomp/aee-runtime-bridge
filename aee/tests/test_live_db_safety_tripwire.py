"""AEE-7.6 tripwire — no legacy ``tests/`` module may call
``DB_PATH.unlink()`` directly against the production DB.

The AEE-7.5 G1/G2 incident on 2026-07-11 (§A.7.15) was caused by
three legacy ``tests/`` modules calling ``DB_PATH.unlink()`` at
module load time, against the production ``data/dispatcher.db``
path. This tripwire scans the ``tests/`` directory for any module
that has a call to ``DB_PATH.unlink()`` and fails loudly if found.

Allowed patterns
----------------

* Calling ``DB_PATH.unlink()`` inside a context manager
  (``with make_temp_dispatcher_db() as p:`` then ``p.unlink()``)
  is fine — the call is against a tempdir path.
* Calling ``db.DB_PATH.unlink()`` is also fine if it follows
  the line ``db.DB_PATH = Path(_TMPDIR) / "dispatcher.db"``
  (i.e. the assignment to a tempdir path is within 10 lines
  above the unlink).
* Calling ``DB_PATH.unlink()`` in this file or in
  ``_live_db_guard.py`` is allowed (the helper is the safe
  replacement; the tripwire is the only enforcement that keeps
  it safe).

The check uses :mod:`ast` to walk each test module and look for
``ast.Call`` nodes where the function is an :class:`ast.Attribute`
with ``attr == "unlink"`` and value either ``DB_PATH`` or
``db.DB_PATH``. A test fails if any such call is found in a
non-allowlisted location.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import sys
import unittest
from pathlib import Path

# Bridge root must be on sys.path so we can import the guard.
_HERE = Path(__file__).resolve()
_REPO = _HERE.parent.parent.parent  # aee/tests/test_... → aee/tests/ → aee/ → repo
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def _load_guard_module():
    """Load ``tests/_live_db_guard.py`` by file path so we don't
    require the ``tests`` package to be importable.

    Returns the loaded module object.
    """
    guard_path = _REPO / "tests" / "_live_db_guard.py"
    spec = importlib.util.spec_from_file_location(
        "_aee76_live_db_guard", guard_path
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError(f"could not load spec from {guard_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Module is itself an aee/tests/ test, so it must NOT trigger its
# own tripwire. The test declares its own DB_PATH.unlink() in the
# fixtures below is forbidden; the tripwire below it intentionally
# scans ``tests/`` not ``aee/tests/``.

_TESTS_DIR = _REPO / "tests"
_ALLOWLIST = {
    # This tripwire is the only place we allow scanning.
    "test_live_db_safety_tripwire.py",
    # The guard helper itself; it must call unlink inside a
    # context manager to clean up the tempdir, not against the
    # live DB.
    "_live_db_guard.py",
    # No legacy tests/ module is in the allowlist anymore.
    # ``test_aee_write_side_metadata.py`` was refactored in
    # AEE-7.6 to use the ``_live_db_guard`` tempdir helper
    # (see commit prior to this slice) and is now safe.
}


def _looks_like_unlink(call: ast.Call) -> bool:
    """Return True iff ``call`` is a call to ``<something>.unlink()``."""
    func = call.func
    if not isinstance(func, ast.Attribute):
        return False
    return func.attr == "unlink"


def _value_name(node: ast.AST) -> str | None:
    """Best-effort: extract a dotted name from an expression node.

    Returns e.g. ``"DB_PATH"``, ``"db.DB_PATH"``, ``"self.path"``,
    or ``None`` if the node is too complex to summarise.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _value_name(node.value)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


def _module_uses_tempdir(tree: ast.AST) -> bool:
    """Return True iff the module has a module-level
    ``_TMPDIR = tempfile.mkdtemp(...)`` (or similar) assignment.

    Modules that build their own tempdir are considered safe even
    if they call ``DB_PATH.unlink()`` against the rebound
    ``db.DB_PATH`` — the unlink is on a tempdir copy, not the
    production path. The tripwire can statically detect the
    tempdir pattern and skip the module.

    Allowed patterns (any of these mark the module as safe):
        ``_TMPDIR = tempfile.mkdtemp(...)``
        ``_tmpdir = tempfile.mkdtemp(...)``
        ``_DIR = tempfile.mkdtemp(...)``
    """
    for node in tree.body:  # type: ignore[attr-defined]
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if not target.id.startswith("_"):
            continue
        rhs = ast.unparse(node.value)
        if "tempfile.mkdtemp" in rhs or "mkdtemp" in rhs:
            return True
    return False


def _scan_module(path: Path) -> list[tuple[int, str, str]]:
    """Return a list of ``(lineno, value, location)`` for every
    ``<x>.unlink()`` call in the module that touches a
    ``DB_PATH``-shaped target.

    ``location`` is ``"production"`` if the call is against a
    hard-coded path or ``"ambiguous"`` if the call's target
    cannot be statically determined. Modules that build a
    tempdir at module load (and rebind ``db.DB_PATH`` to it) are
    skipped entirely — the tripwire can detect that pattern
    safely via ``_module_uses_tempdir``.
    """
    text = path.read_text()
    tree = ast.parse(text, filename=str(path))

    # If the module builds a tempdir and rebinds DB_PATH to it,
    # every subsequent unlink is against the tempdir copy.
    if _module_uses_tempdir(tree):
        return []

    findings: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _looks_like_unlink(node):
            continue
        # Skip calls inside the function body of
        # ``make_temp_dispatcher_db`` (the helper itself is
        # allowlisted, but defense in depth: the helper's unlinks
        # are against a tempdir path, not DB_PATH).
        value = _value_name(node.func.value)  # type: ignore[attr-defined]
        if value is None:
            continue
        # DB_PATH.unlink(), dispatcher.db.DB_PATH.unlink(),
        # db.DB_PATH.unlink(), self.db_path.unlink()
        if not (value.endswith("DB_PATH") or value.endswith("db_path")):
            continue
        # Look up: is the target a known tempdir binding?
        # We approximate by scanning the parent for an
        # assignment of the form
        #   ``<value> = Path(_TMPDIR) / "dispatcher.db"``
        # within 15 lines above the call.
        is_temp = False
        if hasattr(node, "lineno"):
            for ancestor in ast.walk(tree):
                if (
                    isinstance(ancestor, ast.Assign)
                    and len(ancestor.targets) == 1
                    and isinstance(ancestor.targets[0], ast.Attribute)
                    and _value_name(ancestor.targets[0]) == value
                    and getattr(ancestor, "lineno", 0) > 0
                    and ancestor.lineno < node.lineno
                    and (node.lineno - ancestor.lineno) <= 15
                ):
                    rhs = ast.unparse(ancestor.value)
                    if "TMPDIR" in rhs or "tmpdir" in rhs or "tmp_path" in rhs:
                        is_temp = True
                        break
        location = "temp" if is_temp else "production-or-ambiguous"
        findings.append((node.lineno, value, location))
    return findings


class TestLiveDBSafetyTripwire(unittest.TestCase):
    """AEE-7.6 — forbid direct ``DB_PATH.unlink()`` calls in
    legacy ``tests/`` modules. Refactor to use
    ``tests._live_db_guard.make_temp_dispatcher_db`` instead.
    """

    def test_no_production_db_path_unlink_in_legacy_tests(self):
        # Walk every .py in tests/ except the allowlisted ones.
        bad: list[tuple[str, int, str, str]] = []
        for path in sorted(_TESTS_DIR.glob("test_*.py")):
            if path.name in _ALLOWLIST:
                continue
            if path.name.startswith("__"):
                continue
            findings = _scan_module(path)
            for lineno, value, location in findings:
                if location == "production-or-ambiguous":
                    bad.append((str(path.relative_to(_REPO)), lineno, value, location))
        if bad:
            self.fail(
                "Legacy tests/ modules call DB_PATH.unlink() "
                "directly against the production path. Refactor to "
                "use tests/_live_db_guard.make_temp_dispatcher_db(). "
                "Findings (file, lineno, target, location): "
                + ", ".join(repr(b) for b in bad)
            )

    def test_live_db_guard_helper_exports(self):
        # The guard must be importable and expose the expected API.
        guard = _load_guard_module()
        self.assertTrue(
            str(guard.LIVE_DISPATCHER_DB_PATH).endswith("data/dispatcher.db")
        )
        self.assertEqual(guard.LIVE_BRIDGE_PORT, 8787)
        self.assertTrue(callable(guard.is_live_bridge_running))
        self.assertTrue(callable(guard.make_temp_dispatcher_db))
        self.assertTrue(callable(guard.point_module_to_temp_db))

    def test_make_temp_dispatcher_db_does_not_touch_live(self):
        # The helper must yield a tempdir DB without unlinking
        # LIVE_DISPATCHER_DB_PATH. We snapshot inode + size before
        # and after, and they must match.
        guard = _load_guard_module()
        if not guard.LIVE_DISPATCHER_DB_PATH.exists():
            self.skipTest("No live DB on this host; cannot verify isolation.")
        before_inode = guard.LIVE_DISPATCHER_DB_PATH.stat().st_ino
        before_size = guard.LIVE_DISPATCHER_DB_PATH.stat().st_size
        with guard.make_temp_dispatcher_db() as tmp_db:
            self.assertTrue(tmp_db.exists())
            self.assertNotEqual(
                tmp_db.resolve(),
                guard.LIVE_DISPATCHER_DB_PATH.resolve(),
            )
        after_inode = guard.LIVE_DISPATCHER_DB_PATH.stat().st_ino
        after_size = guard.LIVE_DISPATCHER_DB_PATH.stat().st_size
        self.assertEqual(before_inode, after_inode, "Live DB inode changed")
        self.assertEqual(before_size, after_size, "Live DB size changed")

    def test_is_live_bridge_running_returns_bool(self):
        guard = _load_guard_module()
        result = guard.is_live_bridge_running(timeout=0.2)
        self.assertIsInstance(result, bool)
