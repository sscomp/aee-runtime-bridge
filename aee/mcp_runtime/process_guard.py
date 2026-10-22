"""Native process-group guardian; no task/config input beyond argv/stdin.

Runs inside the runner-created session. Parent death kills the entire session's
process group, rather than leaving an orphan CLI after a gateway crash.
"""
import ctypes
import os
import signal
import subprocess
import sys


def main():
    parent = int(sys.argv[1])
    def terminate(*_):
        os.killpg(os.getpgrp(), signal.SIGKILL)
    signal.signal(signal.SIGTERM, terminate)
    if sys.platform == 'darwin':
        import select
        import threading
        watcher = select.kqueue()
        try:
            watcher.control([select.kevent(parent, filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT)], 0, 0)
        except ProcessLookupError:
            terminate()
        def watch_parent():
            watcher.control(None, 1)
            terminate()
        threading.Thread(target=watch_parent, daemon=True).start()
    elif sys.platform.startswith('linux'):
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            return 125
    else:
        return 125
    if os.getppid() != parent:
        terminate()
    try:
        args = sys.argv[2:]
        if args[:1] == ['--cwd-fd']:
            if len(args) < 4 or args[2] != '--':
                return 125
            fd = int(args[1])
            os.fchdir(fd)
            os.close(fd)
            args = args[3:]
        child = subprocess.Popen(args, close_fds=True)
        code = child.wait()
    except OSError:
        return 125
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    sys.exit(main())
