"""Small file-based status and stop channel shared by Mortis processes."""

import os
from pathlib import Path
import tempfile
import threading
import time


VALID_STATUSES = {
    "starting", "ready", "armed", "capturing", "indexing", "eating",
    "done_eating", "thinking", "answering", "crawling", "stopping",
    "stopped", "timed_out", "error",
}
STOPPABLE_STATUSES = {"capturing", "indexing", "eating", "crawling"}
ACTIVE_STATUSES = STOPPABLE_STATUSES | {"stopping"}


class TaskControl:
    """Bound one capture/crawl run and make its terminal status final."""

    def __init__(self, kind, timeout_seconds, stage):
        self.kind = kind
        self.stage = stage
        self.deadline = time.monotonic() + timeout_seconds
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self.terminal_status = None

    def set_timeout(self, timeout_seconds):
        self.deadline = time.monotonic() + timeout_seconds

    def expired(self, now=None):
        return (time.monotonic() if now is None else now) >= self.deadline

    def update_status(self, status_file, status):
        """Allow progress writes only before this task reaches a terminal state."""
        if status in {"done_eating", "ready", "stopped", "timed_out", "error"}:
            return False
        with self._lock:
            if self.terminal_status is not None:
                return False
            return write_status(status_file, status)

    def finish(self, status_file, status):
        """Set the first terminal state and prevent late workers overwriting it."""
        if status not in {"done_eating", "ready", "stopped", "timed_out", "error"}:
            return False
        with self._lock:
            if self.terminal_status is not None:
                return False
            self.terminal_status = status
            if status in {"stopped", "timed_out", "error"}:
                self.cancel_event.set()
            write_status(status_file, status)
            return True


def write_status(status_file, status):
    if not status_file or status not in VALID_STATUSES:
        return False

    destination = Path(status_file)
    temporary = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=destination.name + ".", suffix=".tmp", dir=destination.parent
        )
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(status)
        os.replace(temporary, destination)
        return True
    except OSError:
        return False
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def read_status(status_file):
    if not status_file:
        return None
    try:
        status = Path(status_file).read_text(encoding="ascii").strip().lower()
    except OSError:
        return None
    return status if status in VALID_STATUSES else None


def request_stop(stop_file, status_file):
    """Request graceful stop only while page capture, indexing, or crawl runs."""
    if read_status(status_file) not in STOPPABLE_STATUSES:
        return False
    try:
        Path(stop_file).touch()
    except OSError:
        return False
    write_status(status_file, "stopping")
    return True
