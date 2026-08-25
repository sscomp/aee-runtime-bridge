"""Executor-run identity predicates.

A single source of truth for the placeholders + identities that the
dispatcher watcher, reaper, and lifecycle sync must recognize. The
previous code special-cased ``claude-cli-pending-*`` in the watcher
inline (see ``dispatcher/watcher._tick``) and hardcoded
``trow["adapter_name"] or "hermes"`` in
``TaskManager._sync_executor_runs_status`` (line ~1339). That made it
easy to add a new executor (e.g. ``dsh-headless``) and forget to teach
every consumer about it — which is exactly what
TASK-20260825-0027 hit: the watcher polled a DSH placeholder against
the Hermes gateway, and the reaper reaped a queued DSH-managed task
because the metadata-lag race let it sit in ``queued`` past
``stale_queued_sec=300``.

This module owns:

* ``is_executor_placeholder(external_id)`` — True for any of the
  known non-Hermes executor pending placeholders. Centralized so
  ``watcher._tick``, ``reaper.reap_once``, and any future consumer
  share one predicate and adding a new executor is a one-line edit.

* ``is_dsh_executor(selected_executor)`` — True when the run/identity
  is owned by the DSH headless bridge. ``None`` / ``"hermes"`` /
  ``"claude-code-cli"`` are NOT DSH — they are the legacy
  synchronous and asynchronous paths. ``"dsh-headless"`` and any
  canonical alias (``"dsh"``, ``"dsh_headless"``, ``"deepseek-harness"``,
  ``"deepseek_harness"``) ARE DSH per
  ``config/executor.json::executor_aliases``.

* ``non_hermes_executors()`` — the bounded set of selected_executor
  values that are NOT polled via the Hermes gateway. Used by the
  reaper to skip queued tasks that an executor path owns (i.e. the
  reaper must not independently reap a queued task that the DSH /
  claude-code-cli bridge is responsible for driving to terminal).

The predicates are pure (no I/O, no env reads, no logging) so they
are safe to call from the watcher's hot path and from the reaper's
scan loop. Tests construct synthetic strings; no fixture is needed.
"""
from __future__ import annotations

from typing import FrozenSet, Optional


# Canonical placeholder prefixes stamped by the executor dispatch path
# BEFORE the real run_id is known. ``start()`` writes one of these
# into ``tasks.hermes_run_id`` / ``tasks.runtime_run_id`` and the
# executor's real terminal call (or ``reconcile_executor_completion``)
# overwrites it with the real id. While the placeholder is in place
# the watcher must NOT poll the adapter (the placeholder is not a
# valid Hermes run id) and the reaper must not interpret the task as
# "abandoned by the dispatcher" — the executor is still responsible
# for the lifecycle.
#
# Format: ``<executor>-pending-<task_id>``. Adding a new executor?
# Append its prefix here. Do NOT special-case prefixes anywhere
# else; route every recognition through ``is_executor_placeholder``.
_EXECUTOR_PLACEHOLDER_PREFIXES: FrozenSet[str] = frozenset({
    "claude-cli-pending-",
    "dsh-headless-pending-",
})


def is_executor_placeholder(external_id: Optional[str]) -> bool:
    """Return True iff ``external_id`` is a known non-Hermes executor
    pending placeholder (e.g. ``claude-cli-pending-TASK-...`` or
    ``dsh-headless-pending-TASK-...``).

    Empty / None / unrecognised strings return False. This is the
    single predicate the watcher and the reaper use to decide
    whether the task is owned by an executor and therefore must NOT
    be polled or independently reaped.
    """
    if not external_id:
        return False
    for prefix in _EXECUTOR_PLACEHOLDER_PREFIXES:
        if external_id.startswith(prefix):
            return True
    return False


# The bounded set of ``selected_executor`` values that are NOT
# polled via the Hermes gateway. Used by the reaper to skip queued
# tasks owned by these executors — they have their own dispatch
# path that drives the lifecycle through ``start()`` →
# ``complete()`` / ``fail()`` / ``reconcile_executor_completion``
# and are independent of the watcher's Hermes poll loop.
#
# Aliases from ``config/executor.json::executor_aliases`` are
# flattened into the set so a task dispatched via
# ``executor="dsh"`` (alias of ``"dsh-headless"``) is also
# recognised. The aliases here mirror the canonical alias list
# declared in ``aee/runtimes/executor_config.py`` and
# ``config/executor.json``; if a new alias is added there, add it
# here too (the test suite asserts the sets stay in sync).
_DSH_EXECUTOR_IDS: FrozenSet[str] = frozenset({
    "dsh-headless",
    "dsh_headless",
    "dsh",
    "deepseek-harness",
    "deepseek_harness",
})

_CLAUDE_CLI_EXECUTOR_IDS: FrozenSet[str] = frozenset({
    "claude-code-cli",
    "claude_cli",
    "claude_code",
    "claude",
})


def is_dsh_executor(selected_executor: Optional[str]) -> bool:
    """Return True iff ``selected_executor`` is a DSH headless
    identity (or one of its canonical aliases)."""
    if not selected_executor:
        return False
    return selected_executor in _DSH_EXECUTOR_IDS


def is_non_hermes_executor(selected_executor: Optional[str]) -> bool:
    """Return True iff ``selected_executor`` is owned by an executor
    that drives its own lifecycle (DSH, claude-code-cli). The
    dispatcher reaper uses this to skip queued tasks whose
    ``executor_runs`` row is owned by these executors — the executor
    is the sole authority for the task's terminal status, so the
    reaper must not independently mark it ``timeout`` solely
    because metadata-lag left it in ``queued`` past
    ``stale_queued_sec``.

    ``None`` and ``"hermes"`` return False — Hermes is the legacy
    async path that the dispatcher watcher already polls, and a
    queued Hermes task genuinely is the reaper's responsibility.
    """
    if not selected_executor:
        return False
    if is_dsh_executor(selected_executor):
        return True
    return selected_executor in _CLAUDE_CLI_EXECUTOR_IDS


__all__ = [
    "is_executor_placeholder",
    "is_dsh_executor",
    "is_non_hermes_executor",
    "_EXECUTOR_PLACEHOLDER_PREFIXES",
    "_DSH_EXECUTOR_IDS",
    "_CLAUDE_CLI_EXECUTOR_IDS",
]
