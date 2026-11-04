import io
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import time
import unittest
from multiprocessing.connection import Client
from pathlib import Path
from unittest.mock import patch

import requests

import mortis_core as core
from desktop_spider import _parent_process_alive
from mortis_control import read_status
from mortis_ipc import MortisBridge, decode_message, encode_message, load_session
from mortis_native_host import read_frame, run_host, write_frame


class FixtureHandler(BaseHTTPRequestHandler):
    hits = []
    pages = {
        "/root": (
            '<main><h1>Root page</h1><p>Root fixture has enough readable material to '
            'index safely into the local temporary database.</p>'
            '<a href="/allowed">allowed</a><a href="/blocked">blocked</a>'
            '<a href="/deep">depth one</a></main>'
        ),
        "/allowed": (
            '<main><h1>Allowed page</h1><p>Allowed fixture has enough readable '
            'material to index safely and links to a deeper page.</p>'
            '<a href="/nested">nested page</a></main>'
        ),
        "/deep": (
            '<main><h1>Depth page</h1><p>Depth fixture has enough readable material '
            'to index safely into the local temporary database.</p></main>'
        ),
        "/nested": (
            '<main><h1>Nested page</h1><p>This deeper page must not be fetched when '
            'the crawl depth is limited to one.</p></main>'
        ),
        "/blocked": (
            '<main><h1>Blocked page</h1><p>Robots should prevent indexing this page.</p></main>'
        ),
    }

    def do_GET(self):
        type(self).hits.append(self.path)
        if self.path == "/robots.txt":
            body = b"User-agent: *\nDisallow: /blocked\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        page = self.pages.get(self.path)
        if page is None:
            self.send_error(404)
            return
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class MortisMilestoneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mortis-test-")
        self.addCleanup(self.temp.cleanup)
        self.temp_path = Path(self.temp.name)
        self.db = str(self.temp_path / "test.db")
        self.stop_file = self.temp_path / "stop-crawl"
        self.status_file = self.temp_path / "status"
        self.env_patch = patch.dict(os.environ, {
            "MORTIS_STOP_FILE": str(self.stop_file),
            "MORTIS_STATUS_FILE": str(self.status_file),
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.embed_patch = patch.object(core, "embed", lambda _text, _model: [1.0, 0.0, 0.0])
        self.embed_patch.start()
        self.addCleanup(self.embed_patch.stop)

    def test_capture_duplicate_upsert_grounded_qa_and_cancel_safety(self):
        url = "https://example.test/mortis"
        original = "Mortis stores page text in searchable chunks with local embeddings. " * 3
        first = core.capture_page(url, "First title", original, [], "nomic-embed-text", db_path=self.db)
        second_body = "Updated Mortis page text is stored as a replacement, not duplicate chunks. " * 3
        second = core.capture_page(url, "Updated title", second_body, [], "nomic-embed-text", db_path=self.db)
        self.assertEqual(first["url"], second["url"])
        connection = core.conn(self.db)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM chunks WHERE url=?", (url,)).fetchone()[0], 1)
        saved = connection.execute("SELECT title,body,status FROM pages WHERE url=?", (url,)).fetchone()
        connection.close()
        self.assertEqual(saved, ("Updated title", second_body.strip(), 0))
        self.assertEqual(read_status(self.status_file), "done_eating")

        captured = {}
        class FakeResponse:
            def raise_for_status(self):
                return None
            def json(self):
                return {"message": {"content": "It stores searchable chunks [Source 1]."}}
        def fake_post(endpoint, **kwargs):
            captured["endpoint"] = endpoint
            captured["prompt"] = kwargs["json"]["messages"][0]["content"]
            return FakeResponse()
        with patch.object(core.requests, "post", fake_post):
            answer, sources = core.ask("How are pages stored?", "llama3.2", "nomic-embed-text", db_path=self.db)
        self.assertIn("[Source 1]", answer)
        self.assertEqual(sources[0][1:3], (url, "Updated title"))
        self.assertIn(url, captured["prompt"])
        self.assertIn("Updated Mortis page text", captured["prompt"])

        calls = {"n": 0}
        def cancel_after_embedding():
            calls["n"] += 1
            return calls["n"] >= 2
        with self.assertRaises(core.CrawlStopped):
            core.savepage(url, "Unsafe replacement", "Another replacement body for cancellation.",
                          0, "nomic-embed-text", should_stop=cancel_after_embedding, db_path=self.db)
        connection = core.conn(self.db)
        self.assertEqual(connection.execute("SELECT title,body FROM pages WHERE url=?", (url,)).fetchone(),
                         ("Updated title", second_body.strip()))
        connection.close()

        with patch.object(core, "embed", side_effect=RuntimeError("embedding failed")):
            with self.assertRaisesRegex(RuntimeError, "embedding failed"):
                core.capture_page(
                    url, "Failed replacement", "A different readable page body " * 8,
                    [], "nomic-embed-text", db_path=self.db,
                )
        connection = core.conn(self.db)
        self.assertEqual(connection.execute("SELECT title,body FROM pages WHERE url=?", (url,)).fetchone(),
                         ("Updated title", second_body.strip()))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM chunks WHERE url=?", (url,)).fetchone()[0], 1)
        connection.close()

    def test_capture_validation_and_ollama_failure(self):
        with self.assertRaises(ValueError):
            core.capture_page("chrome://settings", "Settings", "A" * 80, [], "nomic-embed-text", db_path=self.db)
        with self.assertRaises(ValueError):
            core.capture_page("https://example.test/empty", "Empty", "short", [], "nomic-embed-text", db_path=self.db)
        url = "https://example.test/indexed"
        core.capture_page(url, "Stored", "This page has enough readable body text for retrieval and testing. " * 2,
                          [], "nomic-embed-text", db_path=self.db)
        with patch.object(core.requests, "post", side_effect=requests.ConnectionError("Ollama unavailable")):
            with self.assertRaises(requests.ConnectionError):
                core.ask("Question", "llama3.2", "nomic-embed-text", db_path=self.db)

    def test_crawl_robots_depth_page_limit_and_graceful_stop(self):
        FixtureHandler.hits = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        root = f"http://127.0.0.1:{server.server_port}/root"

        logs = []
        done, chunks = core.crawl(
            root, 8, 0, "nomic-embed-text", logs.append,
            lambda *_args: None,
            seed_links=[
                f"http://127.0.0.1:{server.server_port}/allowed",
                f"http://127.0.0.1:{server.server_port}/blocked",
                f"http://127.0.0.1:{server.server_port}/deep",
                "https://external.invalid/should-not-fetch",
            ],
            max_depth=1, db_path=self.db,
        )
        self.assertEqual(done, 2)
        self.assertGreater(chunks, 0)
        self.assertNotIn("/blocked", FixtureHandler.hits)
        self.assertNotIn("/nested", FixtureHandler.hits)
        self.assertTrue(any("ROBOTS SKIP" in line for line in logs))
        connection = core.conn(self.db)
        urls = {row[0] for row in connection.execute("SELECT url FROM pages")}
        connection.close()
        self.assertTrue(any(url.endswith("/allowed") for url in urls))
        self.assertFalse(any(url.endswith("/blocked") for url in urls))

        page_limit_db = str(self.temp_path / "page-limit.db")
        done, _ = core.crawl(root, 1, 0, "nomic-embed-text", lambda *_: None,
                             lambda *_: None, db_path=page_limit_db)
        self.assertEqual(done, 1)
        connection = core.conn(page_limit_db)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 1)
        connection.close()

        stop_db = str(self.temp_path / "stopped.db")
        progress_calls = {"n": 0}
        def cancel_after_first_commit(_done, _total, _chunks):
            progress_calls["n"] += 1
            if progress_calls["n"] == 1:
                self.stop_file.touch()
        done, _ = core.crawl(root, 8, 0, "nomic-embed-text", lambda *_: None,
                             cancel_after_first_commit, db_path=stop_db)
        self.assertEqual(done, 1)
        self.assertEqual(read_status(self.status_file), "stopped")
        connection = core.conn(stop_db)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 1)
        connection.close()

    def test_native_frame_and_authenticated_named_pipe_bridge(self):
        stream = io.BytesIO()
        write_frame(stream, {"type": "capture_result", "ok": True})
        stream.seek(0)
        self.assertEqual(read_frame(stream), {"type": "capture_result", "ok": True})
        with self.assertRaises(ValueError):
            decode_message(b"[]")

        events = __import__("queue").Queue()
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp_path)}):
            bridge = MortisBridge(events.put)
            try:
                session, key = load_session()
                connection = Client(session["pipe"], family="AF_PIPE", authkey=key)
                deadline = time.monotonic() + 5
                while not bridge.connected.is_set() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(bridge.connected.is_set())
                event = {"source": "extension", "message": {"type": "armed", "page": {"url": "https://example.test/"}}}
                connection.send_bytes(encode_message(event))
                observed = events.get(timeout=3)
                self.assertEqual(observed["type"], "native_host_connected")
                observed = events.get(timeout=3)
                self.assertEqual(observed["type"], "armed")
                self.assertTrue(bridge.send_to_extension({"type": "capture_active_tab", "request_id": "test"}))
                deadline = time.monotonic() + 3
                outgoing = None
                while time.monotonic() < deadline:
                    if connection.poll(0.1):
                        candidate = decode_message(connection.recv_bytes())
                        if (
                            candidate.get("target") == "extension"
                            and candidate.get("message", {}).get("type") == "capture_active_tab"
                        ):
                            outgoing = candidate
                            break
                self.assertIsNotNone(outgoing)
                self.assertEqual(outgoing["message"], {
                    "type": "capture_active_tab", "request_id": "test"
                })
                connection.close()

                origin = "chrome-extension://" + "a" * 32 + "/"
                rejected_output = io.BytesIO()
                rejected = run_host(
                    "chrome-extension://" + "b" * 32 + "/",
                    stdin=io.BytesIO(), stdout=rejected_output,
                    session_loader=lambda: load_session(),
                    allowed_loader=lambda: {origin},
                )
                rejected_output.seek(0)
                self.assertEqual(rejected, 2)
                self.assertEqual(read_frame(rejected_output)["type"], "host_error")

                native_input = io.BytesIO()
                write_frame(native_input, {
                    "type": "armed",
                    "page": {"url": "https://example.test/native-host"},
                })
                native_input.seek(0)
                native_output = io.BytesIO()
                self.assertEqual(run_host(
                    origin, stdin=native_input, stdout=native_output,
                    session_loader=lambda: load_session(),
                    allowed_loader=lambda: {origin},
                ), 0)
                native_output.seek(0)
                self.assertEqual(read_frame(native_output)["type"], "host_connected")
                deadline = time.monotonic() + 3
                forwarded = None
                while time.monotonic() < deadline:
                    try:
                        event = events.get(timeout=0.1)
                    except __import__("queue").Empty:
                        continue
                    if event.get("type") == "armed":
                        forwarded = event
                        break
                self.assertEqual(forwarded["page"]["url"], "https://example.test/native-host")

                heartbeat_input = io.BytesIO()
                write_frame(heartbeat_input, {"type": "heartbeat", "nonce": "test-heartbeat"})
                heartbeat_input.seek(0)
                heartbeat_output = io.BytesIO()
                self.assertEqual(run_host(
                    origin, stdin=heartbeat_input, stdout=heartbeat_output,
                    session_loader=lambda: load_session(),
                    allowed_loader=lambda: {origin},
                ), 0)
                heartbeat_output.seek(0)
                self.assertEqual(read_frame(heartbeat_output), {"type": "host_connected"})
                self.assertEqual(read_frame(heartbeat_output), {
                    "type": "heartbeat_ack", "nonce": "test-heartbeat"
                })
                self.assertIsNone(read_frame(heartbeat_output))
            finally:
                bridge.close()

    def test_parent_liveness_probe_is_non_destructive(self):
        process_id = os.getpid()
        self.assertTrue(_parent_process_alive(process_id))
        self.assertEqual(os.getpid(), process_id)

    def test_local_browser_fixture_is_served(self):
        from functools import partial
        from http.server import SimpleHTTPRequestHandler

        handler = partial(SimpleHTTPRequestHandler, directory="tests/fixtures")
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        response = requests.get(
            f"http://127.0.0.1:{server.server_port}/mortis_fixture.html", timeout=3
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Mortis Local Capture Fixture", response.text)
        self.assertIn("/linked.html", response.text)


if __name__ == "__main__":
    unittest.main()
