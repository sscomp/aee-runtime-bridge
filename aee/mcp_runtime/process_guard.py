"""Standalone Linux process-group guardian; no task/config input beyond argv/stdin.

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
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        return 125
    if os.getppid() != parent:
        terminate()
    try:
        child = subprocess.Popen(sys.argv[2:], close_fds=True)
        code = child.wait()
    except OSError:
        return 125
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    sys.exit(main())
