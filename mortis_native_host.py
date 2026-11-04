"""Chromium Native Messaging host for the Mortis desktop bridge."""

# Keep this bootstrap ahead of project imports and argument handling. If the
# interpreter starts this file but a later import or initialization fails,
# the failure is still visible without contaminating Native Messaging stdout.
import os
import sys
import time


def _bootstrap_diagnostic(event, error=None):
    local_app_data = os.environ.get("LOCALAPPDATA")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        cwd = os.getcwd()
    except OSError as cwd_error:
        cwd = f"<unavailable:{type(cwd_error).__name__}>"

    sanitized_args = []
    for argument in sys.argv[1:]:
        if argument.startswith("chrome-extension://") and argument.endswith("/"):
            sanitized_args.append(argument[:240])
        elif argument.startswith("--parent-window=") and argument[16:].isdigit():
            sanitized_args.append(argument[:64])
        else:
            sanitized_args.append(f"<redacted:{len(argument)} chars>")

    line = (
        f"{stamp} [native-startup] {event} pid={os.getpid()} "
        f"argv={sanitized_args!r} cwd={cwd!r}"
    )
    if error is not None:
        line += f" exception={type(error).__name__}: {str(error)[:500]}"
    line += "\n"
    try:
        if local_app_data:
            log_path = os.path.join(local_app_data, "Mortis", "native_host.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as log:
                log.write(line)
            return
    except OSError:
        pass
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except (OSError, AttributeError):
        pass


if __name__ == "__main__":
    _bootstrap_diagnostic("process start")

try:
    import json
    import queue
    import struct
    import threading
    from pathlib import Path

    from multiprocessing.connection import Client

    from mortis_ipc import (
        MAX_IPC_MESSAGE,
        PIPE_PREFIX,
        decode_message,
        encode_message,
        load_allowed_origins,
        load_session,
    )
except BaseException as error:
    _bootstrap_diagnostic("module import failed", error)
    raise


MAX_NATIVE_MESSAGE = 1_000_000
EXTENSION_MESSAGE_TYPES = {
    "bridge_ready", "armed", "disarmed", "capture_result", "extension_error",
    "heartbeat",
}
DESKTOP_COMMAND_TYPES = {"capture_active_tab", "get_status", "disarm"}


def _diagnostic(message):
    """Write startup/transport diagnostics away from Native Messaging stdout."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n"
    try:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            log_path = Path(local_app_data) / "Mortis" / "native_host.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(line)
            return
    except OSError:
        pass
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except (OSError, AttributeError):
        pass


def _read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        block = stream.read(size - len(data))
        if not block:
            return None
        data.extend(block)
    return bytes(data)


def read_frame(stream):
    header = _read_exact(stream, 4)
    if header is None:
        return None
    size = struct.unpack("<I", header)[0]
    if size < 2 or size > MAX_NATIVE_MESSAGE:
        raise ValueError("Native Messaging frame exceeds Mortis' 1 MB limit.")
    payload = _read_exact(stream, size)
    if payload is None:
        raise EOFError("Native Messaging frame ended unexpectedly.")
    return decode_message(payload)


def write_frame(stream, message):
    payload = encode_message(message)
    if len(payload) > MAX_NATIVE_MESSAGE:
        raise ValueError("Native Messaging output exceeds the 1 MB limit.")
    stream.write(struct.pack("<I", len(payload)))
    stream.write(payload)
    stream.flush()


def _read_browser(stream, messages, done):
    try:
        while not done.is_set():
            message = read_frame(stream)
            if message is None:
                _diagnostic("Browser closed Native Messaging stdin.")
                break
            _diagnostic(f"Browser -> host message: {message.get('type', '<missing-type>')}.")
            if message.get("type") not in EXTENSION_MESSAGE_TYPES:
                _diagnostic("Ignored browser message with an unsupported type.")
                continue
            messages.put(message)
    except (OSError, EOFError, ValueError, json.JSONDecodeError) as error:
        _diagnostic(f"Browser input framing failed: {type(error).__name__}: {error}")
        messages.put({"type": "extension_error", "message": str(error)[:240]})
    finally:
        done.set()


def run_host(origin, stdin=None, stdout=None, session_loader=load_session,
             allowed_loader=load_allowed_origins, connect=Client):
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
            msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
        allowed = allowed_loader()
        if origin not in allowed:
            raise RuntimeError("Calling extension ID is not allowlisted.")
        session, key = session_loader()
        session_id = session.get("session_id")
        expected_pipe = f"{PIPE_PREFIX}{session_id}"
        if not isinstance(session_id, str) or session.get("pipe") != expected_pipe:
            raise RuntimeError("Mortis desktop pipe/session ID mismatch.")
        pipe = connect(session["pipe"], family="AF_PIPE", authkey=key)
    except Exception as error:
        _diagnostic(
            f"Startup failed for origin {origin!r}: {type(error).__name__}: {error}"
        )
        try:
            _diagnostic("Host -> browser frame: host_error.")
            write_frame(stdout, {"type": "host_error", "message": str(error)[:240]})
        except Exception:
            pass
        return 2

    _diagnostic(
        f"Authenticated desktop pipe connected for origin {origin!r}; "
        f"session_id={session_id}; pipe={session['pipe']!r}."
    )

    incoming = queue.Queue()
    reader_done = threading.Event()
    reader = threading.Thread(
        target=_read_browser,
        args=(stdin, incoming, reader_done),
        name="mortis-native-stdin",
        daemon=True,
    )
    reader.start()
    try:
        _diagnostic("Host -> browser frame: host_connected.")
        write_frame(stdout, {"type": "host_connected"})
        while not reader_done.is_set() or not incoming.empty():
            try:
                extension_message = incoming.get(timeout=0.02)
                if extension_message.get("type") == "heartbeat":
                    write_frame(stdout, {
                        "type": "heartbeat_ack",
                        "nonce": extension_message.get("nonce"),
                    })
                    _diagnostic("Host -> browser heartbeat_ack.")
                    continue
                pipe.send_bytes(
                    encode_message({"source": "extension", "message": extension_message})
                )
            except queue.Empty:
                pass

            if pipe.poll(0.02):
                desktop_message = decode_message(pipe.recv_bytes(MAX_IPC_MESSAGE))
                if desktop_message.get("target") != "extension":
                    continue
                message = desktop_message.get("message")
                if not isinstance(message, dict) or message.get("type") not in DESKTOP_COMMAND_TYPES:
                    continue
                _diagnostic(f"Desktop -> browser message: {message.get('type')}.")
                write_frame(stdout, message)
        return 0
    except (EOFError, OSError, ValueError) as error:
        _diagnostic(f"Bridge transport ended: {type(error).__name__}: {error}")
        try:
            write_frame(stdout, {"type": "host_error", "message": str(error)[:240]})
        except Exception:
            pass
        return 1
    finally:
        reader_done.set()
        try:
            _diagnostic("Closing authenticated desktop pipe connection.")
            pipe.close()
        except OSError:
            pass
        _diagnostic("Native Messaging host stopped.")


def main():
    if len(sys.argv) < 2:
        _diagnostic("Native Messaging host was started without an extension origin.")
        try:
            _diagnostic("Host -> browser frame: host_error.")
            write_frame(sys.stdout.buffer, {
                "type": "host_error",
                "message": "Native Messaging host did not receive its extension origin.",
            })
        except (OSError, ValueError, AttributeError):
            pass
        return 2
    return run_host(sys.argv[1])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException as error:
        _bootstrap_diagnostic("unhandled host exception", error)
        raise
