"""Authenticated per-user named-pipe bridge for the Mortis native host."""

import base64
import json
import os
from multiprocessing.connection import Listener
from pathlib import Path
import queue
import secrets
import tempfile
import threading
import time
import uuid


MAX_IPC_MESSAGE = 1_000_000
PIPE_PREFIX = r"\\.\pipe\mortis-"


def _diagnostic(message):
    """Append bridge lifecycle diagnostics without using protocol stdout."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return
    try:
        log_path = Path(local_app_data) / "Mortis" / "native_host.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [pipe] {message}\n")
    except OSError:
        pass


def mortis_home():
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "Mortis"


def session_path():
    return mortis_home() / "active_session.json"


def allowlist_path():
    return mortis_home() / "allowed_extensions.json"


def encode_message(message):
    if not isinstance(message, dict):
        raise ValueError("IPC messages must be JSON objects.")
    try:
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("IPC message is not valid JSON.") from error
    if len(data) > MAX_IPC_MESSAGE:
        raise ValueError("IPC message exceeds the 1 MB limit.")
    return data


def decode_message(data):
    if not isinstance(data, bytes) or len(data) > MAX_IPC_MESSAGE:
        raise ValueError("IPC message is invalid or too large.")
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("IPC message is not valid UTF-8 JSON.") from error
    if not isinstance(message, dict):
        raise ValueError("IPC messages must be JSON objects.")
    return message


def load_session(path=None):
    path = Path(path or session_path())
    try:
        session = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Mortis desktop session is not running.") from error
    if (
        not isinstance(session, dict)
        or not isinstance(session.get("pipe"), str)
        or not session["pipe"].startswith(PIPE_PREFIX)
        or not isinstance(session.get("key"), str)
        or len(session["key"]) > 256
    ):
        raise RuntimeError("Mortis desktop session file is invalid.")
    try:
        key = base64.b64decode(session["key"], validate=True)
    except (ValueError, TypeError) as error:
        raise RuntimeError("Mortis desktop session key is invalid.") from error
    if len(key) != 32:
        raise RuntimeError("Mortis desktop session key has an invalid length.")
    return session, key


def load_allowed_origins(path=None):
    path = Path(path or allowlist_path())
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Mortis extension is not registered yet.") from error
    origins = config.get("allowed_origins") if isinstance(config, dict) else None
    if not isinstance(origins, list) or not origins:
        raise RuntimeError("Mortis extension allowlist is missing or invalid.")
    return {origin for origin in origins if isinstance(origin, str)}


class MortisBridge:
    """Owns one random authenticated pipe per running desktop pet session."""

    def __init__(self, on_message):
        self.on_message = on_message
        self.home = mortis_home()
        self.home.mkdir(parents=True, exist_ok=True)
        self.session_file = session_path()
        self.session_id = uuid.uuid4().hex
        self.key = secrets.token_bytes(32)
        self.pipe = PIPE_PREFIX + self.session_id
        self.listener = Listener(self.pipe, family="AF_PIPE", authkey=self.key)
        self.outgoing = queue.Queue()
        self.stopping = threading.Event()
        self.connected = threading.Event()
        self.connection = None
        self._write_session()
        _diagnostic(f"Pipe listener started for session {self.session_id}.")
        self.thread = threading.Thread(
            target=self._serve, name="mortis-native-pipe", daemon=True
        )
        self.thread.start()

    def _write_session(self):
        data = {
            "session_id": self.session_id,
            "pipe": self.pipe,
            "key": base64.b64encode(self.key).decode("ascii"),
        }
        descriptor, temporary = tempfile.mkstemp(
            prefix="active_session.", suffix=".tmp", dir=self.home
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
            os.replace(temporary, self.session_file)
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass

    def send_to_extension(self, message):
        if not self.connected.is_set() or self.stopping.is_set():
            return False
        try:
            encoded = encode_message({"target": "extension", "message": message})
        except ValueError:
            return False
        self.outgoing.put(encoded)
        _diagnostic(
            f"Queued desktop command for extension: {message.get('type', '<missing-type>')}."
        )
        return True

    def _serve(self):
        while not self.stopping.is_set():
            connection = None
            try:
                connection = self.listener.accept()
                self.connection = connection
                self.connected.set()
                _diagnostic(f"Authenticated native host pipe client connected for session {self.session_id}.")
                self.on_message({"type": "native_host_connected", "connected": True})
                while not self.stopping.is_set():
                    try:
                        outgoing = self.outgoing.get(timeout=0.03)
                    except queue.Empty:
                        outgoing = None
                    if outgoing is not None:
                        connection.send_bytes(outgoing)
                    if connection.poll(0.03):
                        incoming = decode_message(
                            connection.recv_bytes(MAX_IPC_MESSAGE)
                        )
                        if incoming.get("source") == "extension":
                            message = incoming.get("message")
                            if isinstance(message, dict):
                                self.on_message(message)
            except (EOFError, OSError, ValueError) as error:
                if not self.stopping.is_set():
                    _diagnostic(
                        f"Pipe client ended for session {self.session_id}: "
                        f"{type(error).__name__}: {error}"
                    )
            finally:
                self.connected.clear()
                self.connection = None
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass
                if not self.stopping.is_set():
                    _diagnostic(f"Native host pipe client disconnected for session {self.session_id}.")
                    self.on_message({"type": "native_host_connected", "connected": False})

    def close(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        if self.connection is not None:
            try:
                self.connection.close()
            except OSError:
                pass
        try:
            self.listener.close()
        except OSError:
            pass
        try:
            session, _ = load_session(self.session_file)
            if session.get("session_id") == self.session_id:
                self.session_file.unlink(missing_ok=True)
        except (OSError, RuntimeError):
            pass
        _diagnostic(f"Pipe listener stopped for session {self.session_id}.")
