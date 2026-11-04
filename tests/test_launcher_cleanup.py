import ctypes
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from launch_mortis import WindowsChildJob, stop_process


@unittest.skipUnless(os.name == "nt", "Windows process jobs are Windows-only")
class LauncherCleanupTests(unittest.TestCase):
    def _process_is_alive(self, process_id):
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel32.WaitForSingleObject.restype = ctypes.c_ulong
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x00100000, False, process_id)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)

    def test_ctrl_break_and_job_close_reap_owned_tree_only(self):
        with tempfile.TemporaryDirectory(prefix="mortis-job-test-") as temp:
            folder = Path(temp)
            gate = folder / "spawn-grandchild"
            child_pid_file = folder / "grandchild.pid"
            child_code = "import time; time.sleep(60)"
            parent_code = (
                "import os,subprocess,sys,time; "
                f"gate={str(gate)!r}; pidfile={str(child_pid_file)!r}; "
                "while_not_ready=True; "
                "exec('while not os.path.exists(gate): time.sleep(0.01)'); "
                f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
                "open(pidfile,'w').write(str(child.pid)); time.sleep(60)"
            )
            unrelated = subprocess.Popen(
                [sys.executable, "-c", child_code],
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            parent = subprocess.Popen(
                [sys.executable, "-c", parent_code],
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            job = WindowsChildJob()
            grandchild_pid = None
            try:
                job.assign(parent)
                gate.write_text("go", encoding="ascii")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not child_pid_file.exists():
                    time.sleep(0.02)
                self.assertTrue(child_pid_file.exists(), "owned child did not start")
                grandchild_pid = int(child_pid_file.read_text(encoding="ascii"))

                stop_process(parent)
                job.close()  # KILL_ON_JOB_CLOSE also reaps descendants.

                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and self._process_is_alive(grandchild_pid):
                    time.sleep(0.05)
                self.assertFalse(self._process_is_alive(parent.pid), "launcher child remained alive")
                self.assertFalse(self._process_is_alive(grandchild_pid), "owned grandchild remained alive")
                self.assertIsNone(unrelated.poll(), "unrelated process was affected")
            finally:
                job.close()
                stop_process(parent)
                unrelated.terminate()
                try:
                    unrelated.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    unrelated.kill()
                    unrelated.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
