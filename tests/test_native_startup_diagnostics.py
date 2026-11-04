import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import mortis_native_host


class NativeStartupDiagnosticTests(unittest.TestCase):
    def test_process_start_is_logged_before_startup_error_and_stdout_is_one_frame(self):
        extension_origin = "chrome-extension://ocekcapcpkjkgomniiekchldnonopcbi/"
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env["LOCALAPPDATA"] = temporary
            result = subprocess.run(
                [sys.executable, "-u", str(Path(__file__).parents[1] / "mortis_native_host.py"), extension_origin],
                cwd=Path(__file__).parents[1],
                env=env,
                input=b"",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, b"")
            self.assertGreaterEqual(len(result.stdout), 4)
            frame_size = struct.unpack("<I", result.stdout[:4])[0]
            self.assertEqual(len(result.stdout), frame_size + 4)
            response = json.loads(result.stdout[4:].decode("utf-8"))
            self.assertEqual(response["type"], "host_error")

            log_path = Path(temporary) / "Mortis" / "native_host.log"
            log = log_path.read_text(encoding="utf-8")
            self.assertIn("[native-startup] process start pid=", log)
            self.assertIn(extension_origin, log)
            self.assertIn("cwd=", log)
            self.assertIn("Startup failed for origin", log)
            self.assertNotIn("<redacted:", log)

    def test_session_id_must_match_pipe_suffix_before_connect(self):
        output = io.BytesIO()
        key = b"k" * 32
        session = {
            "session_id": "expected-session",
            "pipe": r"\\.\pipe\mortis-stale-session",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"LOCALAPPDATA": temporary}
        ):
            result = mortis_native_host.run_host(
                "chrome-extension://test/",
                stdin=io.BytesIO(),
                stdout=output,
                session_loader=lambda: (session, key),
                allowed_loader=lambda: {"chrome-extension://test/"},
                connect=lambda *args, **kwargs: self.fail("stale pipe must not be opened"),
            )
        self.assertEqual(result, 2)
        frame_size = struct.unpack("<I", output.getvalue()[:4])[0]
        response = json.loads(output.getvalue()[4:4 + frame_size].decode("utf-8"))
        self.assertEqual(response["type"], "host_error")
        self.assertIn("pipe/session ID mismatch", response["message"])

    def test_native_frames_are_length_prefixed_utf8_json(self):
        stream = io.BytesIO()
        message = {"type": "host_connected", "label": "Mortis ✓"}
        mortis_native_host.write_frame(stream, message)
        framed = stream.getvalue()
        size = struct.unpack("<I", framed[:4])[0]
        self.assertEqual(size, len(framed) - 4)
        self.assertEqual(mortis_native_host.read_frame(io.BytesIO(framed)), message)

    @unittest.skipUnless(os.name == "nt", "Windows launcher is platform-specific")
    def test_windows_launcher_forwards_stdio_and_emits_valid_native_frame(self):
        project = Path(__file__).parents[1]
        extension_origin = "chrome-extension://ocekcapcpkjkgomniiekchldnonopcbi/"
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env["LOCALAPPDATA"] = temporary
            result = subprocess.run(
                [str(project / "MortisNativeHost.exe"), extension_origin],
                cwd=project,
                env=env,
                input=b"",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, b"")
            frame_size = struct.unpack("<I", result.stdout[:4])[0]
            self.assertEqual(len(result.stdout), frame_size + 4)
            response = json.loads(result.stdout[4:].decode("utf-8"))
            self.assertEqual(response["type"], "host_error")
            log = (Path(temporary) / "Mortis" / "native_host.log").read_text(encoding="utf-8")
            self.assertIn("[native-launcher] Python child started", log)
            self.assertIn("[native-startup] process start pid=", log)
            self.assertIn(extension_origin, log)
            self.assertIn("Startup failed for origin", log)


if __name__ == "__main__":
    unittest.main()
