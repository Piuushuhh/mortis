"""Start Streamlit and the MORTIS desktop companion as one managed session."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import ctypes
from ctypes import wintypes
from mortis_control import ACTIVE_STATUSES, read_status, request_stop, write_status


ROOT = Path(__file__).resolve().parent


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobIoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobBasicLimitInformation),
        ("IoInfo", _JobIoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class WindowsChildJob:
    """Own launcher children and descendants; closing the job reaps stragglers."""

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JobObjectExtendedLimitInformation = 9

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self.kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
        ]
        self.kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL

        self.handle = self.kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _JobExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel32.SetInformationJobObject(
            self.handle,
            self.JobObjectExtendedLimitInformation,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process):
        if self.handle and not self.kernel32.AssignProcessToJobObject(
            self.handle, wintypes.HANDLE(int(process._handle))
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            handle, self.handle = self.handle, None
            self.kernel32.CloseHandle(handle)


def raise_keyboard_interrupt(_signum, _frame):
    raise KeyboardInterrupt


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    if os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
        try:
            # Each Popen child has its own process group; this does not signal
            # unrelated terminal processes.
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=4)


def main():
    signal.signal(signal.SIGINT, raise_keyboard_interrupt)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, raise_keyboard_interrupt)
    control_dir = Path(tempfile.mkdtemp(prefix="mortis-control-"))
    stop_file = control_dir / "stop-crawl"
    status_file = control_dir / "status"
    shutdown_file = control_dir / "shutdown-request"
    app_url = os.environ.get("MORTIS_APP_URL", "http://localhost:8501")
    write_status(status_file, "starting")
    parent_pid = os.getpid()
    child_process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    child_env = os.environ.copy()
    child_env["MORTIS_STOP_FILE"] = str(stop_file)
    child_env["MORTIS_STATUS_FILE"] = str(status_file)
    child_env["MORTIS_SHUTDOWN_FILE"] = str(shutdown_file)

    pet = None
    streamlit = None
    child_job = WindowsChildJob()
    try:
        try:
            pet = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "desktop_spider.py"),
                    "--stop-file",
                    str(stop_file),
                    "--status-file",
                    str(status_file),
                    "--app-url",
                    app_url,
                    "--parent-pid",
                    str(parent_pid),
                ],
                cwd=ROOT,
                env=child_env,
                creationflags=child_process_group,
            )
            try:
                child_job.assign(pet)
            except OSError as exc:
                print(f"MORTIS could not add the desktop spider to its Windows child job: {exc}")
        except OSError as exc:
            print(f"MORTIS desktop spider could not start: {exc}")
            print("Continuing with the existing Streamlit app.")

        streamlit = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py")],
            cwd=ROOT,
            env=child_env,
            creationflags=child_process_group,
        )
        try:
            child_job.assign(streamlit)
        except OSError as exc:
            print(f"MORTIS could not add Streamlit to its Windows child job: {exc}")
        try:
            return streamlit.wait()
        except KeyboardInterrupt:
            print("\nShutdown requested. Stopping Streamlit and the desktop spider.")
            return 130
    finally:
        # Ignore another console interrupt while the owned process groups are
        # being asked to shut down and reaped.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        request_stop(stop_file, status_file)
        try:
            shutdown_file.touch()
        except OSError:
            pass
        stop_deadline = time.monotonic() + 3.0
        while (
            read_status(status_file) in ACTIVE_STATUSES
            and time.monotonic() < stop_deadline
        ):
            time.sleep(0.05)
        # The desktop spider also watches the shutdown file. Send Ctrl+Break
        # only to the process groups created above so Python/Streamlit can run
        # their own cleanup handlers.
        stop_process(pet)
        stop_process(streamlit)
        # KILL_ON_JOB_CLOSE reaps any descendant (for example Streamlit's
        # file-watcher child) still owned by this launcher.
        child_job.close()
        shutil.rmtree(control_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
