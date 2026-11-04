"""Animated, always-on-top Mortis desktop spider for web crawling and local knowledge retrieval."""

import argparse
import ctypes
import logging
import math
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
import uuid
import webbrowser
from ctypes import wintypes

from mortis_control import TaskControl, read_status, request_stop, write_status
from mortis_core import (
    CHAT_DEFAULT, EMBED_DEFAULT, CrawlStopped, ask, capture_page, classify_intent,
    conversational_response, crawl, stop_requested,
)
from mortis_ipc import MortisBridge


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL


logger = logging.getLogger(__name__)


def _enable_per_monitor_dpi_awareness():
    if os.name != "nt":
        return
    try:
        setter = ctypes.windll.user32.SetProcessDpiAwarenessContext
        setter.argtypes = (ctypes.c_void_p,)
        setter.restype = wintypes.BOOL
        setter(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


class TypingAnimation:
    """Small, cancellable dot animation, independent of Tk widgets for testing."""

    FRAMES = ("•  ·  ·", "·  •  ·", "·  ·  •")

    def __init__(self, schedule, cancel, update, interval_ms=360):
        self.schedule = schedule
        self.cancel = cancel
        self.update = update
        self.interval_ms = interval_ms
        self.active = False
        self.phase = -1
        self.after_id = None

    def start(self):
        self.stop()
        self.active = True
        self.phase = -1
        self._tick()

    def _tick(self):
        if not self.active:
            return
        self.phase = (self.phase + 1) % len(self.FRAMES)
        self.update(self.FRAMES[self.phase])
        self.after_id = self.schedule(self.interval_ms, self._tick)

    def stop(self):
        self.active = False
        if self.after_id is not None:
            try:
                self.cancel(self.after_id)
            except Exception:
                pass
            self.after_id = None


def _parent_process_alive(pid):
    """Return True/False when known; return None when Windows denies the check."""
    if not isinstance(pid, int) or pid <= 0:
        return False

    if os.name == "nt":
        synchronize = 0x00100000
        query_limited_information = 0x1000
        error_access_denied = 5
        error_invalid_parameter = 87
        handle = _kernel32.OpenProcess(
            synchronize | query_limited_information, False, pid
        )
        if not handle:
            error = ctypes.get_last_error()
            if error == error_invalid_parameter:
                return False
            if error == error_access_denied:
                handle = _kernel32.OpenProcess(synchronize, False, pid)
                if not handle:
                    return False if ctypes.get_last_error() == error_invalid_parameter else None
            else:
                return None

        try:
            result = _kernel32.WaitForSingleObject(handle, 0)
            if result == 0:
                return False
            if result == 258:
                return True
            return None
        finally:
            _kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    except OSError:
        return None


class SpiderWindow:
    WIDTH = 180
    HEIGHT = 160
    TRANSPARENT_KEY = "#ff00ff"
    BROWSER_CAPTURE_TIMEOUT = 35.0
    CAPTURE_INDEX_TIMEOUT = 900.0
    CRAWL_TASK_TIMEOUT = 1800.0

    STATUS_STYLE = {
        "starting": ("ONLINE", "#8bdcff"),
        "ready": ("IDLE", "#8ee3a6"),
        "armed": ("CURIOUS", "#c5b7ff"),
        "capturing": ("CAPTURING", "#60dbff"),
        "indexing": ("READING", "#63e3ce"),
        "eating": ("READING", "#63e3ce"),
        "done_eating": ("ALL DONE", "#9cf2b6"),
        "thinking": ("THINKING", "#b5a6ff"),
        "answering": ("ANSWERING", "#8adfff"),
        "crawling": ("EXPLORING", "#55d8ff"),
        "stopping": ("STOPPING", "#ffd27a"),
        "stopped": ("STOPPED", "#c3cbd1"),
        "timed_out": ("TIMED OUT", "#ffad78"),
        "error": ("NEEDS HELP", "#ff8e88"),
        "asking": ("THINKING", "#9fe8ff"),
    }

    def __init__(self, stop_file, status_file, app_url, parent_pid=None):
        self.stop_file = Path(stop_file)
        self.status_file = Path(status_file) if status_file else None
        shutdown_path = os.environ.get("MORTIS_SHUTDOWN_FILE")
        self.shutdown_file = Path(shutdown_path) if shutdown_path else None
        self.app_url = app_url
        self.parent_pid = parent_pid
        _enable_per_monitor_dpi_awareness()
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        self.WIDTH = 180
        self.HEIGHT = 160
        initial_x = max(8, self.root.winfo_screenwidth() - self.WIDTH - 24)
        initial_y = max(8, self.root.winfo_screenheight() - self.HEIGHT - 80)
        self.initial_x, self.initial_y = initial_x, initial_y
        self.root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{initial_x}+{initial_y}")
        self.transparent = self._enable_transparency()

        background = self.TRANSPARENT_KEY if self.transparent else "#071321"
        self.root.configure(bg=background)
        if not self.transparent:
            try:
                self.root.attributes("-alpha", 0.97)
            except tk.TclError:
                pass

        self.canvas = tk.Canvas(
            self.root,
            width=self.WIDTH,
            height=self.HEIGHT,
            bg=background,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self._draw_creature()
        self._draw_status()
        self._build_context_menu()

        self.drag_offset = (0, 0)
        self.facing = 1
        self.last_pointer_x = None
        self.dragging = False
        self.activity = None
        self.last_status = None
        self.ask_busy = False
        self.ask_queue = queue.Queue()
        self.bridge_queue = queue.Queue()
        self.bridge = None
        self.bridge_ready = False
        self.armed_page = None
        self.capture_request_id = None
        self.last_capture = None
        self.task_kind = None
        self.active_task = None
        self.closing = False
        self.answer_window = None
        self.message_history = []
        self.current_expression = "neutral"
        self.animation_started = time.monotonic()
        self.typing_animation = None
        self.typing_row = None

        self.canvas.bind("<ButtonPress-1>", self._start_drag)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._end_drag)
        self.canvas.bind("<Double-Button-1>", self._request_stop)
        self.canvas.bind("<Button-3>", self._show_context_menu)
        self.canvas.bind("<Enter>", self._become_curious)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Escape>", lambda _event: self._close())

        try:
            self.bridge = MortisBridge(self.bridge_queue.put)
            self.bridge_error = ""
        except (OSError, RuntimeError) as error:
            self.bridge_error = str(error)

        self._update_status()
        self.root.after(65, self._animate)
        self.root.after(350, self._poll_status)
        self.root.after(100, self._poll_ask_result)
        self.root.after(100, self._poll_bridge_events)
        if self.shutdown_file:
            self.root.after(500, self._poll_shutdown_request)
        if self.parent_pid:
            self.root.after(1500, self._check_parent)

    def _enable_transparency(self):
        self.root.configure(bg=self.TRANSPARENT_KEY)
        try:
            self.root.wm_attributes("-transparentcolor", self.TRANSPARENT_KEY)
            return True
        except tk.TclError:
            return False

    def _get_eye_color(self, expression):
        colors = {
            "neutral": "#5ef1d5",
            "happy": "#a3be8c",
            "curious": "#81a1c1",
            "thinking": "#b48ead",
            "surprised": "#ebcb8b",
            "serious": "#d08770",
            "error": "#bf616a",
            "stopped": "#c3cbd1",
        }
        return colors.get(expression, "#5ef1d5")

    def _draw_avatar_icon(self, canvas):
        canvas.delete("all")
        cx, cy = 16, 16
        eye_color = self._get_eye_color(getattr(self, "current_expression", "neutral"))
        canvas.create_oval(2, 2, 30, 30, fill="#242933", outline="#3b4252", width=1)
        offsets = [(-6, -4, -12, -2), (-8, 0, -13, 4), (6, -4, 12, -2), (8, 0, 13, 4)]
        for x1, y1, x2, y2 in offsets:
            canvas.create_line(cx + x1, cy + y1, cx + x2, cy + y2, fill="#4c566a", width=2)
        canvas.create_oval(cx - 8, cy - 8, cx + 8, cy + 8, fill="#1c202a", outline="#4c566a", width=1)
        canvas.create_oval(cx - 4, cy - 2, cx - 1, cy + 1, fill=eye_color, outline=eye_color, tags="avatar_eyes")
        canvas.create_oval(cx + 1, cy - 2, cx + 4, cy + 1, fill=eye_color, outline=eye_color, tags="avatar_eyes")

    def _draw_creature(self):
        c = self.canvas
        c.delete("character")
        cx = self.WIDTH // 2
        cy = self.HEIGHT // 2 - 12
        eye_color = self._get_eye_color(getattr(self, "current_expression", "neutral"))

        leg_offsets = [
            (-26, -20, -46, -12),
            (-32, -8, -54, 6),
            (-30, 8, -50, 24),
            (-24, 20, -40, 38),
            (26, -20, 46, -12),
            (32, -8, 54, 6),
            (30, 8, 50, 24),
            (24, 20, 40, 38),
        ]
        for i, (kdx, kdy, fdx, fdy) in enumerate(leg_offsets):
            side = -1 if i < 4 else 1
            root_x = cx + (side * 12)
            root_y = cy - 6 + ((i % 4) * 6)
            c.create_line(
                root_x, root_y,
                cx + kdx, cy + kdy,
                cx + fdx, cy + fdy,
                fill="#434c5e", width=3, capstyle="round", joinstyle="round",
                tags="character",
            )

        c.create_oval(
            cx - 20, cy - 4, cx + 20, cy + 32,
            fill="#1c202a", outline="#3b4252", width=2, tags="character",
        )
        c.create_polygon(
            cx, cy + 6, cx - 6, cy + 16, cx, cy + 24, cx + 6, cy + 16,
            fill="#2e3440", outline="#5e81ac", width=1, tags="character",
        )
        c.create_oval(
            cx - 15, cy - 24, cx + 15, cy + 2,
            fill="#242933", outline="#4c566a", width=2, tags="character",
        )

        eye_coords = [
            (cx - 6, cy - 14, cx - 2, cy - 10),
            (cx + 2, cy - 14, cx + 6, cy - 10),
            (cx - 10, cy - 18, cx - 7, cy - 15),
            (cx + 7, cy - 18, cx + 10, cy - 15),
            (cx - 4, cy - 20, cx - 1, cy - 17),
            (cx + 1, cy - 20, cx + 4, cy - 17),
        ]
        for x1, y1, x2, y2 in eye_coords:
            c.create_oval(
                x1, y1, x2, y2,
                fill=eye_color, outline=eye_color,
                tags=("character", "spider_eyes"),
            )

    def _draw_status(self):
        self.status_dot = None
        self.status_text = self.canvas.create_text(
            self.WIDTH // 2, self.HEIGHT - 10, text="IDLE", fill="#8ee3a6",
            font=("Segoe UI", 8, "bold"),
        )

    def _build_context_menu(self):
        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Eat armed page", command=self._eat_armed_page)
        self.context_menu.add_command(label="Crawl links (8 pages / depth 2)", command=self._crawl_links)
        self.context_menu.add_command(label="Ask Mortis...", command=self._focus_question)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Scan URL in dashboard...", command=self._open_dashboard)
        self.context_menu.add_command(label="Pause / Resume (planned)", state=tk.DISABLED)
        self.context_menu.add_command(label="Settings in dashboard...", command=self._open_dashboard)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Exit Mortis Spider", command=self._close)

    def _focus_question(self):
        self._answer_window()
        self._clear_chat_placeholder()
        self.chat_entry.focus_set()

    def _submit_question(self, _event=None, source=None):
        source_entry = source or (getattr(_event, "widget", None) if _event is not None else self.chat_entry)
        if not isinstance(source_entry, tk.Text):
            source_entry = self.chat_entry
        question = (source_entry.get("1.0", "end-1c") if isinstance(source_entry, tk.Text)
                    else source_entry.get()).strip()
        if not question or question in {"Ask Mortis...", "Write a message..."}:
            self.chat_entry.focus_set()
            return "break"
        if self.ask_busy:
            return "break"

        history = [
            {"role": item["role"], "content": item["content"]}
            for item in self.message_history[-8:]
            if item.get("role") in {"user", "assistant"}
        ]
        self.ask_busy = True
        source_entry.delete("1.0", tk.END)
        self._append_message("user", question, "user")
        if hasattr(self, "chat_send_button"):
            self.chat_send_button.config(state=tk.DISABLED)
        self.activity = "thinking"
        self._update_status()
        self._show_typing_indicator()
        worker = threading.Thread(
            target=self._ask_worker,
            args=(question, history),
            name="mortis-chat",
            daemon=True,
        )
        worker.start()
        return "break"

    def _chat_return(self, event):
        if event.state & 0x0001:  # Shift+Enter inserts a newline in the Text widget.
            return None
        self._submit_question(event)
        return "break"

    def _ask_worker(self, question, history=None):
        intent = classify_intent(question)
        try:
            if intent == "conversation":
                answer = conversational_response(question, CHAT_DEFAULT, history)
                sources = []
            else:
                answer, sources = ask(
                    question, CHAT_DEFAULT, EMBED_DEFAULT, history=history
                )
            self.ask_queue.put((True, answer, sources, intent))
        except Exception:
            logger.exception("Mortis local response failed")
            self.ask_queue.put((False, "", [], intent))

    def _poll_ask_result(self):
        try:
            while True:
                succeeded, answer, sources, _intent = self.ask_queue.get_nowait()
                self.ask_busy = False
                self._remove_typing_indicator()
                if hasattr(self, "chat_send_button"):
                    self.chat_send_button.config(state=tk.NORMAL)
                if succeeded:
                    self._set_temporary_activity("answering", 1800)
                    self._show_answer(answer, sources)
                else:
                    self._show_answer_message(
                        "I couldn’t get a reply just now. Please try again in a moment."
                    )
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ask_result)

    def _show_typing_indicator(self):
        self._answer_window()
        if self.typing_row is not None:
            self._remove_typing_indicator()
        row = tk.Frame(self.chat_messages_frame, bg="#11131b")
        row.pack(fill="x", pady=(0, 10))
        column = tk.Frame(row, bg="#11131b")
        column.pack(side="left", anchor="w", padx=(3, 42))
        tk.Label(column, text="Mortis", bg="#11131b", fg="#7998ee",
                 font=("Segoe UI", 8)).pack(anchor="w", padx=4, pady=(0, 3))
        bubble = tk.Frame(column, bg="#29232f", padx=14, pady=9)
        bubble.pack(anchor="w")
        dots = tk.Label(bubble, text="•  ·  ·", bg="#29232f", fg="#eab4cc",
                        font=("Segoe UI", 11, "bold"), width=8, anchor="w")
        dots.pack()
        self.typing_row = row
        self.typing_animation = TypingAnimation(
            self.root.after, self.root.after_cancel, lambda value: dots.configure(text=value)
        )
        self.typing_animation.start()
        self._scroll_chat_to_bottom()

    def _remove_typing_indicator(self):
        if self.typing_animation is not None:
            self.typing_animation.stop()
            self.typing_animation = None
        if self.typing_row is not None:
            try:
                self.typing_row.destroy()
            except tk.TclError:
                pass
            self.typing_row = None

    def _set_answer_window_visibility(self, visible):
        if self.answer_window is None:
            return
        try:
            if visible:
                self.answer_window.deiconify()
                self.answer_window.lift()
            else:
                self.answer_window.withdraw()
        except tk.TclError:
            pass

    def _open_chat_options(self, event=None):
        menu = tk.Menu(self.answer_window, tearoff=0)
        menu.add_command(label="Clear conversation", command=self._clear_conversation)
        menu.add_command(label="Open dashboard", command=self._open_dashboard)
        try:
            x = event.x_root if event else self.answer_window.winfo_rootx() + self.answer_window.winfo_width() - 24
            y = event.y_root if event else self.answer_window.winfo_rooty() + 42
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _clear_conversation(self):
        self.message_history.clear()
        for child in self.chat_messages_frame.winfo_children():
            child.destroy()
        self._remove_typing_indicator()
        if self.ask_busy:
            self._show_typing_indicator()
        self._scroll_chat_to_bottom()

    def _answer_window(self):
        if self.answer_window is not None:
            try:
                if self.answer_window.winfo_exists():
                    self.answer_window.deiconify()
                    self.answer_window.lift()
                    return self.chat_canvas
            except tk.TclError:
                pass

        window = tk.Toplevel(self.root)
        self.answer_window = window
        window.title("Mortis — Conversation")
        chat_w, chat_h = 430, 560
        chat_x = self.root.winfo_x() + self.WIDTH + 12
        if chat_x + chat_w > self.root.winfo_screenwidth() - 8:
            chat_x = self.root.winfo_x() - chat_w - 12
        chat_x = max(8, min(chat_x, self.root.winfo_screenwidth() - chat_w - 8))
        chat_y = max(8, min(self.root.winfo_y() - 120,
                            self.root.winfo_screenheight() - chat_h - 8))
        window.geometry(f"{chat_w}x{chat_h}+{chat_x}+{chat_y}")
        window.minsize(360, 430)
        window.update_idletasks()
        window.configure(bg="#111116")
        window.attributes("-topmost", True)
        window.protocol("WM_DELETE_WINDOW", lambda: self._set_answer_window_visibility(False))

        header = tk.Frame(window, bg="#19171e", padx=12, pady=9)
        header.pack(fill="x")
        self.chat_avatar_canvas = tk.Canvas(header, width=32, height=32, bg="#19171e", highlightthickness=0, bd=0)
        self.chat_avatar_canvas.pack(side="left", padx=(0, 9))
        self._draw_avatar_icon(self.chat_avatar_canvas)
        identity = tk.Frame(header, bg="#19171e")
        identity.pack(side="left", fill="y")
        tk.Label(identity, text="Mortis Spider", bg="#19171e", fg="#f2e8ee",
                 font=("Segoe UI", 11, "bold")).pack(anchor="w", pady=(3, 1))
        tk.Label(identity, text="● Local AI & Web Knowledge", bg="#19171e", fg="#5ef1d5",
                 font=("Segoe UI", 8)).pack(anchor="w")
        tk.Button(header, text="⋯", command=self._open_chat_options, bg="#19171e",
                  fg="#c5aabd", activebackground="#302833", activeforeground="#ffffff",
                  relief="flat", bd=0, font=("Segoe UI", 14), padx=7).pack(side="right", anchor="n")
        tk.Button(header, text="×", command=lambda: self._set_answer_window_visibility(False),
                  bg="#19171e", fg="#aaa2ad", activebackground="#302833",
                  activeforeground="#ffffff", relief="flat", bd=0,
                  font=("Segoe UI", 14), padx=7).pack(side="right", anchor="n")
        tk.Frame(window, bg="#322832", height=1).pack(fill="x")

        chat_area = tk.Frame(window, bg="#111116")
        chat_area.pack(fill="both", expand=True, padx=(10, 4), pady=(8, 4))
        self.chat_canvas = tk.Canvas(chat_area, bg="#111116", highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(chat_area, orient="vertical", command=self.chat_canvas.yview,
                                 troughcolor="#17151c", bg="#403640", activebackground="#695565")
        self.chat_canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.chat_canvas.pack(side="left", fill="both", expand=True)
        self.chat_messages_frame = tk.Frame(self.chat_canvas, bg="#111116")
        self.chat_window_item = self.chat_canvas.create_window(
            (0, 0), window=self.chat_messages_frame, anchor="nw"
        )
        self.chat_messages_frame.bind("<Configure>", self._resize_chat_scrollregion)
        self.chat_canvas.bind("<Configure>", self._resize_chat_content)
        self.chat_canvas.bind("<MouseWheel>", self._chat_mousewheel)

        composer = tk.Frame(window, bg="#19171e", padx=9, pady=8)
        composer.pack(fill="x", padx=10, pady=(0, 10))
        self.chat_entry = tk.Text(composer, height=2, wrap="word", bg="#27232b", fg="#eee8ef",
                                  insertbackground="#f1c0d4", selectbackground="#61465e",
                                  relief="flat", bd=0, padx=9, pady=7,
                                  font=("Segoe UI", 9))
        self.chat_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.chat_entry.insert("1.0", "Write a message...")
        self.chat_entry.config(fg="#9d929f")
        self.chat_entry.bind("<FocusIn>", self._clear_chat_placeholder)
        self.chat_entry.bind("<FocusOut>", self._restore_chat_placeholder)
        self.chat_entry.bind("<Return>", self._chat_return)
        self.chat_send_button = tk.Button(
            composer, text="Send", command=lambda: self._submit_question(source=self.chat_entry),
            bg="#a94979", fg="#ffffff", activebackground="#bc5c8c", relief="flat",
            bd=0, font=("Segoe UI", 9, "bold"), padx=13, pady=9,
        )
        self.chat_send_button.pack(side="right")

        for item in self.message_history:
            self._render_message(item)
        return self.chat_canvas

    def _resize_chat_scrollregion(self, _event=None):
        try:
            self.chat_canvas.configure(scrollregion=self.chat_canvas.bbox("all"))
        except tk.TclError:
            pass

    def _resize_chat_content(self, event):
        self.chat_canvas.itemconfigure(self.chat_window_item, width=event.width)

    def _chat_mousewheel(self, event):
        self.chat_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _scroll_chat_to_bottom(self):
        try:
            self.root.after_idle(lambda: self.chat_canvas.yview_moveto(1.0))
        except tk.TclError:
            pass

    def _clear_chat_placeholder(self, _event=None):
        if self.chat_entry.get("1.0", "end-1c") == "Write a message...":
            self.chat_entry.delete("1.0", tk.END)
            self.chat_entry.config(fg="#eee8ef")

    def _restore_chat_placeholder(self, _event=None):
        if not self.chat_entry.get("1.0", "end-1c").strip():
            self.chat_entry.insert("1.0", "Write a message...")
            self.chat_entry.config(fg="#9d929f")

    @staticmethod
    def _rounded_rectangle(width, height, radius=14):
        radius = min(radius, width // 2, height // 2)
        return [
            radius, 0, width - radius, 0, width, 0, width, radius,
            width, height - radius, width, height, width - radius, height,
            radius, height, 0, height, 0, height - radius, 0, radius, 0, 0,
        ]

    def _render_message(self, item):
        user = item["role"] == "user"
        background = "#1e293b" if user else "#252b3b"
        ink = "#f8fafc" if user else "#e2e8f0"
        row = tk.Frame(self.chat_messages_frame, bg="#111116")
        row.pack(fill="x", pady=(2, 8))
        side = "right" if user else "left"
        column = tk.Frame(row, bg="#111116")
        column.pack(side=side, anchor="e" if user else "w", padx=(42, 3) if user else (3, 42))
        role_label = "You" if user else "Mortis"
        tk.Label(column, text=role_label, bg="#111116",
                 fg="#7998ee" if not user else "#a6a0ad",
                 font=("Segoe UI", 8)).pack(anchor="e" if user else "w", padx=4, pady=(0, 3))

        body = item["content"]
        font = tkfont.Font(root=self.root, font=("Segoe UI", 9))
        max_width = min(340, max(120, self.answer_window.winfo_width() - 92))
        measured = max((font.measure(line) for line in body.splitlines()), default=0)
        bubble_width = min(max_width, max(112, measured + 28))
        bubble = tk.Canvas(column, width=bubble_width, height=36, bg="#111116",
                           highlightthickness=0, bd=0)
        bubble.pack(anchor="e" if user else "w")
        text_id = bubble.create_text(
            14, 11, text=body, anchor="nw", width=max(80, bubble_width - 28),
            fill=ink, font=("Segoe UI", 9), justify="left",
        )
        bbox = bubble.bbox(text_id) or (14, 11, bubble_width - 14, 26)
        bubble_height = bbox[3] + 11
        bubble.configure(height=bubble_height)
        shape = bubble.create_polygon(
            *self._rounded_rectangle(bubble_width, bubble_height, 13),
            smooth=True, splinesteps=12, fill=background, outline=background,
        )
        bubble.tag_lower(shape, text_id)

        sources = item.get("sources") or []
        if sources:
            source_panel = tk.Frame(column, bg="#1d1921", padx=9, pady=7,
                                    highlightthickness=1, highlightbackground="#3b303e")
            source_button = tk.Button(
                column, text=f"Sources  ·  {len(sources)}", anchor="w",
                command=lambda panel=source_panel: panel.pack_forget() if panel.winfo_manager() else panel.pack(fill="x", pady=(5, 0)),
                bg="#111116", fg="#c5a3bc", activebackground="#111116",
                activeforeground="#eac1d8", relief="flat", bd=0,
                font=("Segoe UI", 8, "bold"), padx=5, pady=3,
            )
            source_button.pack(anchor="w", pady=(3, 0))
            source_panel.pack_forget()
            for index, source in enumerate(sources, 1):
                _score, url, title, passage = source
                label = tk.Label(source_panel, text=f"{index}. {title or url}", anchor="w",
                                 justify="left", wraplength=max_width - 28,
                                 bg="#1d1921", fg="#9fc5ff", cursor="hand2",
                                 font=("Segoe UI", 8, "underline"))
                label.pack(fill="x", anchor="w", pady=(2, 0))
                label.bind("<Button-1>", lambda _event, target=url: webbrowser.open(target, new=2))
                details = tk.Label(source_panel, text=f"{url}\n{passage[:260].strip()}",
                                   anchor="w", justify="left", wraplength=max_width - 28,
                                   bg="#1d1921", fg="#aaa1ae", font=("Segoe UI", 7))
                details.pack(fill="x", anchor="w", pady=(1, 5))
        self._scroll_chat_to_bottom()

    def _append_message(self, role, body, kind="assistant", sources=None):
        if role not in {"user", "assistant"}:
            role = "assistant"
        item = {"role": role, "content": str(body).strip(), "kind": kind,
                "sources": list(sources or [])}
        self.message_history.append(item)
        self.message_history = self.message_history[-60:]
        window_existed = False
        if self.answer_window is not None:
            try:
                window_existed = bool(self.answer_window.winfo_exists())
            except tk.TclError:
                pass
        self._answer_window()
        if window_existed:
            self._render_message(item)

    def _show_answer_message(self, message):
        self._append_message("assistant", message, "assistant")

    def _show_answer(self, answer, sources):
        self._remove_typing_indicator()
        self._append_message("assistant", answer.strip() or "I didn’t get a response that time.",
                             "assistant", sources=sources)
    def _become_curious(self, _event=None):
        if self.activity is None and self.armed_page:
            self.activity = "armed"
            self._update_status()
            self.root.after(1400, self._clear_temporary_activity)

    def _clear_temporary_activity(self):
        if self.activity == "armed":
            self.activity = None
            self._update_status()

    def _set_temporary_activity(self, status, milliseconds=4000):
        self.activity = status
        self._update_status()
        self.root.after(milliseconds, lambda: self._expire_activity(status))

    def _expire_activity(self, expected_status):
        if self.activity == expected_status:
            self.activity = None
            if read_status(self.status_file) == expected_status:
                write_status(self.status_file, "ready")
            self._update_status()

    def _remove_old_stop_request(self):
        try:
            self.stop_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _eat_armed_page(self):
        if self.task_kind:
            self._show_error("Mortis is already working. Wait for the current task to finish.")
            return
        if not self.bridge_ready or self.bridge is None:
            self._show_error("The browser bridge is not connected. Check the Mortis extension popup.")
            return
        if not self.armed_page:
            self._show_error("No tab is armed. Open the extension and choose Arm this tab first.")
            self.bridge.send_to_extension({"type": "get_status"})
            return

        self._remove_old_stop_request()
        request_id = uuid.uuid4().hex
        task = TaskControl("capture", self.BROWSER_CAPTURE_TIMEOUT, "browser")
        task.request_id = request_id
        self.capture_request_id = request_id
        self.active_task = task
        self.task_kind = "capture"
        self.activity = "capturing"
        task.update_status(self.status_file, "capturing")
        self._update_status()
        if not self.bridge.send_to_extension(
            {"type": "capture_active_tab", "request_id": request_id}
        ):
            task.finish(self.status_file, "error")
            self._release_task(task)
            self._show_error("The extension disconnected before page capture started.")

    def _crawl_links(self):
        if self.task_kind:
            self._show_error("Mortis is already working. Wait for the current task to finish.")
            return
        if not self.last_capture:
            self._show_error("Eat and index the current page before crawling its links.")
            return
        links = self.last_capture.get("links", [])
        if not links:
            self._show_error("The captured page did not contain eligible links.")
            return
        self._remove_old_stop_request()
        task = TaskControl("crawl", self.CRAWL_TASK_TIMEOUT, "worker")
        self.active_task = task
        self.task_kind = "crawl"
        self.activity = "crawling"
        task.update_status(self.status_file, "crawling")
        self._update_status()
        start = self.last_capture["url"]
        self._start_task_thread(
            self._crawl_worker, start, links, task,
            name="mortis-link-crawl",
        )

    def _release_task(self, task):
        if self.active_task is not task:
            return
        if task.kind == "capture":
            self.capture_request_id = None
        self.active_task = None
        self.task_kind = None

    def _check_task_deadline(self, now=None):
        task = self.active_task
        if task is None or not task.expired(now):
            return
        if task.stage == "browser":
            if task.terminal_status is None:
                task.finish(self.status_file, "timed_out")
                self.activity = "timed_out"
                self._update_status()
                self._show_answer_message("Browser page capture timed out. The page was not indexed.")
            # A late browser response will fail request ID matching and cannot index.
            self._release_task(task)
            return
        if task.terminal_status is None:
            task.finish(self.status_file, "timed_out")
            self.activity = "timed_out"
            self._update_status()
            self._show_answer_message(
                f"Mortis {task.kind} timed out. Cancellation was requested; the active worker must exit before another task starts."
            )

    def _start_task_thread(self, target, *args, name):
        threading.Thread(
            target=target, args=args, name=name, daemon=True
        ).start()

    def _crawl_worker(self, start, links, task):
        logs = []
        should_stop = lambda: task.cancel_event.is_set() or stop_requested()
        try:
            done, indexed = crawl(
                start, 8, 1.5, EMBED_DEFAULT,
                log=logs.append,
                progress=lambda _done, _total, _chunks: None,
                should_stop=should_stop,
                seed_links=links,
                max_depth=2,
                clear_stop_file=False,
                status_callback=lambda status: task.update_status(self.status_file, status),
            )
            self.bridge_queue.put({
                "type": "crawl_complete", "task": task, "pages": done,
                "chunks": indexed, "logs": logs[-12:],
                "stopped": should_stop(),
            })
        except Exception as error:
            self.bridge_queue.put({
                "type": "task_error", "task": task, "message": str(error)[:360]
            })

    def _poll_bridge_events(self):
        self._check_task_deadline()
        try:
            while True:
                event = self.bridge_queue.get_nowait()
                kind = event.get("type")
                if kind == "native_host_connected":
                    if not event.get("connected"):
                        self.bridge_ready = False
                        self.armed_page = None
                        if self.activity == "armed":
                            self.activity = None
                            self._update_status()
                    continue
                if kind == "bridge_ready":
                    self.bridge_ready = True
                    armed = event.get("armed")
                    self.armed_page = armed if isinstance(armed, dict) else None
                    if not self.task_kind:
                        if self.armed_page:
                            self.activity = "armed"
                        elif self.activity == "armed":
                            self.activity = None
                    self._update_status()
                elif kind == "armed":
                    page = event.get("page")
                    self.armed_page = page if isinstance(page, dict) else None
                    if not self.task_kind:
                        self.activity = "armed" if self.armed_page else None
                    self._update_status()
                elif kind == "disarmed":
                    self.armed_page = None
                    if self.activity == "armed":
                        self.activity = None
                    self._update_status()
                elif kind == "capture_result":
                    self._handle_capture_result(event)
                elif kind == "extension_error":
                    self._show_error(event.get("message", "Browser extension error."))
                elif kind == "crawl_complete":
                    task = event.get("task")
                    if task is not self.active_task:
                        continue
                    if task.terminal_status is not None:
                        self._release_task(task)
                        continue
                    terminal = "stopped" if event.get("stopped") else "ready"
                    task.finish(self.status_file, terminal)
                    self._release_task(task)
                    self.activity = terminal
                    self._update_status()
                    summary = (
                        f"Link crawl stopped safely. {event['pages']} pages and "
                        f"{event['chunks']} chunks were indexed."
                        if event.get("stopped") else
                        f"Link crawl complete: {event['pages']} pages and "
                        f"{event['chunks']} chunks indexed."
                    )
                    self._show_answer_message(summary)
                    self._set_temporary_activity("stopped" if event.get("stopped") else "ready", 4500)
                elif kind == "capture_indexed":
                    task = event.get("task")
                    if task is not self.active_task:
                        continue
                    if task.terminal_status is not None:
                        self._release_task(task)
                        continue
                    task.finish(self.status_file, "done_eating")
                    self._release_task(task)
                    self.last_capture = event["page"]
                    self._set_temporary_activity("done_eating", 5000)
                    self._show_answer_message(
                        f"Done eating: {self.last_capture['title']}\n"
                        f"{self.last_capture['url']}\n"
                        f"Indexed {self.last_capture['chunks']} chunks."
                    )
                elif kind == "task_stopped":
                    task = event.get("task")
                    if task is not self.active_task:
                        continue
                    if task.terminal_status is None:
                        task.finish(self.status_file, "stopped")
                    self._release_task(task)
                    self.activity = task.terminal_status or "stopped"
                    self._update_status()
                elif kind == "task_error":
                    task = event.get("task")
                    if task is not self.active_task:
                        continue
                    if task.terminal_status is not None:
                        self._release_task(task)
                        continue
                    task.finish(self.status_file, "error")
                    self._release_task(task)
                    self._show_error(event.get("message", "Mortis task failed."))
        except queue.Empty:
            pass
        try:
            self.root.after(100, self._poll_bridge_events)
        except tk.TclError:
            pass

    def _handle_capture_result(self, event):
        task = self.active_task
        if task is None or event.get("request_id") != self.capture_request_id:
            return
        if task.terminal_status is not None:
            self._release_task(task)
            return
        if not event.get("ok"):
            task.finish(self.status_file, "error")
            self._release_task(task)
            self._show_error(event.get("error", "The browser could not capture this page."))
            return
        page = event.get("page")
        if not isinstance(page, dict):
            task.finish(self.status_file, "error")
            self._release_task(task)
            self._show_error("The browser returned an invalid page snapshot.")
            return
        task.stage = "worker"
        task.set_timeout(self.CAPTURE_INDEX_TIMEOUT)
        self.activity = "eating"
        self._update_status()
        self._start_task_thread(
            self._index_worker, page, task, name="mortis-page-index"
        )

    def _index_worker(self, page, task):
        try:
            body = page.get("text", "")
            selected = page.get("selected_text", "")
            if isinstance(selected, str) and selected.strip() and selected.strip() not in body:
                body = "Selected text: " + selected.strip() + "\n\n" + body
            result = capture_page(
                page.get("url"), page.get("title"), body,
                page.get("links"), EMBED_DEFAULT,
                should_stop=lambda: task.cancel_event.is_set() or stop_requested(),
                status_callback=lambda status: task.update_status(self.status_file, status),
            )
            self.bridge_queue.put({"type": "capture_indexed", "task": task, "page": result})
        except CrawlStopped:
            self.bridge_queue.put({"type": "task_stopped", "task": task})
        except Exception as error:
            self.bridge_queue.put({
                "type": "task_error", "task": task, "message": str(error)[:360]
            })

    def _show_error(self, message):
        if self.task_kind:
            self._show_answer_message("Mortis error: " + message)
            return
        write_status(self.status_file, "error")
        self._set_temporary_activity("error", 5000)
        self._show_answer_message("Mortis error: " + message)

    def _open_dashboard(self):
        webbrowser.open(self.app_url, new=2)

    def _show_context_menu(self, event):
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _start_drag(self, event):
        self.drag_offset = (
            event.x_root - self.root.winfo_x(),
            event.y_root - self.root.winfo_y(),
        )
        self.dragging = True

    def _drag(self, event):
        x = event.x_root - self.drag_offset[0]
        y = event.y_root - self.drag_offset[1]
        if self.last_pointer_x is not None and event.x_root != self.last_pointer_x:
            self.facing = 1 if event.x_root > self.last_pointer_x else -1
        self.last_pointer_x = event.x_root
        self.root.geometry(f"+{x}+{y}")

    def _end_drag(self, _event):
        self.dragging = False
        self.last_pointer_x = None

    def _request_stop(self, _event):
        if request_stop(self.stop_file, self.status_file):
            task = self.active_task
            if task is not None:
                task.finish(self.status_file, "stopped")
            self.activity = "stopped"
            self._update_status()
            self._show_answer_message(
                "Cancellation requested. Any in-flight request will be discarded before it can report success."
            )

    def _update_status(self):
        status = self.activity or read_status(self.status_file) or "starting"
        if status == self.last_status:
            return
        self.last_status = status
        label, color = self.STATUS_STYLE.get(status, ("READY", "#8ee3a6"))
        if self.status_dot is not None:
            self.canvas.itemconfigure(self.status_dot, fill=color)
        self.canvas.itemconfigure(self.status_text, text=label, fill=color)
        expression = {
            "armed": "curious", "capturing": "curious", "indexing": "thinking",
            "eating": "thinking", "thinking": "thinking", "asking": "thinking",
            "answering": "happy", "done_eating": "happy", "error": "surprised",
            "stopping": "serious", "stopped": "neutral", "ready": "neutral",
        }.get(status, "neutral")
        self._set_expression(expression)

    def _set_expression(self, expression):
        if expression == getattr(self, "current_expression", None):
            return
        self.current_expression = expression
        eye_color = self._get_eye_color(expression)
        try:
            self.canvas.itemconfigure("spider_eyes", fill=eye_color, outline=eye_color)
        except tk.TclError:
            pass
        avatar = getattr(self, "chat_avatar_canvas", None)
        if avatar is not None:
            try:
                avatar.itemconfigure("avatar_eyes", fill=eye_color, outline=eye_color)
            except tk.TclError:
                pass

    def _poll_status(self):
        self._update_status()
        self.root.after(350, self._poll_status)

    def _animate(self):
        now = time.monotonic()
        bob = math.sin(now * 1.5) * 0.35
        delta = bob - getattr(self, "last_bob", 0.0)
        self.canvas.move("character", 0, delta)
        self.last_bob = bob
        self.root.after(80, self._animate)

    def _check_parent(self):
        if _parent_process_alive(self.parent_pid) is False:
            self._close()
            return
        self.root.after(1500, self._check_parent)

    def _poll_shutdown_request(self):
        if self.shutdown_file and self.shutdown_file.exists():
            self._close()
            return
        try:
            self.root.after(500, self._poll_shutdown_request)
        except tk.TclError:
            pass

    def _close(self):
        if self.closing:
            return
        if self.task_kind:
            self.closing = True
            if request_stop(self.stop_file, self.status_file):
                self.activity = "stopping"
                self._update_status()
            self.root.after(100, lambda: self._wait_for_task_stop(time.monotonic() + 4.0))
            return
        self._finish_close()

    def _wait_for_task_stop(self, deadline):
        if self.task_kind and time.monotonic() < deadline:
            self.root.after(100, lambda: self._wait_for_task_stop(deadline))
            return
        self._finish_close()

    def _finish_close(self):
        self._remove_typing_indicator()
        if self.bridge is not None:
            self.bridge.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self):
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self._close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop-file", required=True)
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--app-url", default="http://localhost:8501")
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    SpiderWindow(args.stop_file, args.status_file, args.app_url, args.parent_pid).run()


if __name__ == "__main__":
    main()
