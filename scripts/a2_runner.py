#!/usr/bin/env python3
"""Verification-mode CLI wrapper (TASK-20260908-0008).

The B1b deploy-verify semantic smoke (2026-09-08) ran dispatcher
terminal methods OUTSIDE pytest with the real ``.env`` and delivered 5
real Telegram messages (audit mids 3073-3077). The lesson: every
verification / smoke / ad-hoc dispatcher invocation must run under the
shared fail-closed notification suppression contract WITHOUT relying
on the caller remembering to export env vars.

This wrapper IS that contract for shell invocations::

    .venv/bin/python scripts/a2_runner.py <script.py> [args...]

    echo '...' | .venv/bin/python scripts/a2_runner.py -   # stdin mode

or, programmatically inside a script::

    from aee._notification_guard import verification_mode
    with verification_mode():
        ...

Behavior:

* Arms ``aee._notification_guard.enter_verification_mode()`` BEFORE the
  target runs — every notification send entry point (manager gate +
  notifier direct sends) is fail-closed suppressed.
* Credentials in this process's env are sanitized (dummies) so spawned
  children cannot live-send either.
* Suppressed-send attempts are recorded to the process-local sink
  (``/tmp/a2-notification-sink-<pid>/``), never the live audit.
* The suppression boundary never writes ``.env`` and never sends.

Exit code: the target script's exit code (0 for stdin mode unless the
code raises).
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

# Make the repo root importable regardless of cwd.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from aee import _notification_guard as guard  # noqa: E402


def main(argv: list) -> int:
    if len(argv) < 2:
        print(
            "usage: a2_runner.py <script.py> [args...] | - (read stdin)",
            file=sys.stderr,
        )
        return 2

    # The verification-mode contract: arm suppression FIRST, before any
    # dispatcher/notifier import in the target can read credentials.
    snap = guard.enter_verification_mode()
    guard.sanitize_os_environ()
    print(
        "[a2_runner] verification mode armed "
        f"(env={guard.NOTIFICATIONS_DISABLED_ENV}=true, "
        f"production_sentinel={guard.production_authorized()})",
        file=sys.stderr,
    )

    try:
        if argv[1] == "-":
            code = sys.stdin.read()
            exec(compile(code, "<stdin>", "exec"), {"__name__": "__main__"})
            return 0
        target = argv[1]
        if not Path(target).exists():
            print(f"a2_runner: target not found: {target}", file=sys.stderr)
            return 2
        sys.argv = argv[1:]
        # runpy executes the target with __name__ == "__main__" so
        # scripts with an if-main guard behave identically to a direct
        # run — but under the armed suppression contract.
        runpy.run_path(target, run_name="__main__")
        return 0
    except SystemExit as exc:  # target called sys.exit()
        return int(exc.code or 0)
    except BaseException as exc:  # noqa: BLE001 — report, never live-send
        print(f"a2_runner: target raised {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    finally:
        guard.exit_verification_mode()
        guard.restore_os_environ()
        _ = snap  # snapshot kept for debuggability only


if __name__ == "__main__":
    sys.exit(main(sys.argv))