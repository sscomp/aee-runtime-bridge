"""Exercise native ownership and guardian behavior on macOS."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from aee.mcp_runtime.store import owner_identity

GUARD = Path(__file__).resolve().parents[2] / 'aee/mcp_runtime/process_guard.py'


@unittest.skipUnless(sys.platform == 'darwin', 'macOS native runtime')
class MacOSExecution(unittest.TestCase):
    def test_owner_identity_is_stable_and_observed(self):
        identity = owner_identity()
        self.assertEqual(identity, owner_identity())
        self.assertEqual(identity['pid'], os.getpid())
        self.assertTrue(identity['start_ticks'])
        self.assertTrue(identity['boot_id'])

    def test_guard_enters_pinned_directory_after_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'original'
            directory.mkdir()
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                directory.rename(Path(tmp) / 'moved')
                directory.mkdir()
                result = subprocess.run([sys.executable, str(GUARD), str(os.getpid()),
                    '--cwd-fd', str(fd), '--', sys.executable, '-c',
                    "from pathlib import Path;Path('proof').write_text('pinned')"],
                    pass_fds=(fd,), start_new_session=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual((Path(tmp) / 'moved/proof').read_text(), 'pinned')
                self.assertFalse((directory / 'proof').exists())
            finally:
                os.close(fd)

    def test_parent_crash_stops_child_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat = Path(tmp) / 'heartbeat'
            child = (f"import time;f=open({str(heartbeat)!r},'ab',buffering=0);"
                     "\nwhile True:f.write(b'x');time.sleep(.02)")
            script = ("import os,subprocess,sys,time;"
                      f"p=subprocess.Popen([sys.executable,{str(GUARD)!r},str(os.getpid()),"
                      f"sys.executable,'-c',{child!r}],start_new_session=True);"
                      "print(p.pid,flush=True);time.sleep(60)")
            parent = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
            group = int(parent.stdout.readline())
            try:
                deadline = time.monotonic() + 5
                while not heartbeat.exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue(heartbeat.exists())
                parent.kill()
                parent.wait(timeout=5)
                time.sleep(.2)
                size = heartbeat.stat().st_size
                time.sleep(.2)
                self.assertEqual(heartbeat.stat().st_size, size)
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                parent.stdout.close()
                parent.stderr.close()


if __name__ == '__main__':
    unittest.main()
