import tempfile
from pathlib import Path
import unittest

from desktop_spider import SpiderWindow
from mortis_control import TaskControl, read_status, write_status


class _Canvas:
    def itemconfigure(self, *_args, **_kwargs):
        pass


def _bare_spider(directory, task):
    spider = SpiderWindow.__new__(SpiderWindow)
    spider.stop_file = Path(directory) / "stop-crawl"
    spider.status_file = Path(directory) / "status"
    spider.active_task = task
    spider.task_kind = task.kind
    spider.capture_request_id = getattr(task, "request_id", None)
    spider.activity = "capturing"
    spider.last_status = None
    spider.status_dot = "dot"
    spider.status_text = "label"
    spider.canvas = _Canvas()
    spider.root = None
    spider.messages = []
    spider._show_answer_message = spider.messages.append
    spider.started_workers = []
    spider._start_task_thread = lambda *args, **kwargs: spider.started_workers.append((args, kwargs))
    return spider


class CaptureCancellationTests(unittest.TestCase):
    def test_stalled_browser_response_times_out_and_late_success_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            task = TaskControl("capture", 30, "browser")
            task.request_id = "stalled-request"
            spider = _bare_spider(directory, task)
            write_status(spider.status_file, "capturing")

            spider._check_task_deadline(now=task.deadline + 0.01)
            self.assertEqual(task.terminal_status, "timed_out")
            self.assertEqual(read_status(spider.status_file), "timed_out")
            self.assertIsNone(spider.active_task)
            spider._handle_capture_result({
                "request_id": "stalled-request",
                "ok": True,
                "page": {"url": "https://example.test/", "text": "late page"},
            })
            self.assertEqual(spider.started_workers, [])
            self.assertEqual(read_status(spider.status_file), "timed_out")

    def test_double_click_cancels_stalled_capture_promptly_and_blocks_late_success(self):
        with tempfile.TemporaryDirectory() as directory:
            task = TaskControl("capture", 30, "browser")
            task.request_id = "cancelled-request"
            spider = _bare_spider(directory, task)
            write_status(spider.status_file, "capturing")

            spider._request_stop(None)
            self.assertTrue(task.cancel_event.is_set())
            self.assertEqual(task.terminal_status, "stopped")
            self.assertEqual(read_status(spider.status_file), "stopped")
            # Keep the task slot until the outstanding browser response or its
            # deadline, so a second capture cannot overlap this one.
            self.assertIs(spider.active_task, task)
            self.assertEqual(spider.task_kind, "capture")

            spider._handle_capture_result({
                "request_id": "cancelled-request",
                "ok": True,
                "page": {"url": "https://example.test/", "text": "late page"},
            })
            self.assertIsNone(spider.active_task)
            self.assertIsNone(spider.task_kind)
            self.assertEqual(spider.started_workers, [])
            self.assertEqual(read_status(spider.status_file), "stopped")
            self.assertFalse(task.update_status(spider.status_file, "done_eating"))
            self.assertEqual(read_status(spider.status_file), "stopped")


if __name__ == "__main__":
    unittest.main()
