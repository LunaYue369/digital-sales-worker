"""Global state for the auto-mode pipeline."""

import threading

auto_mode: bool = False
auto_thread: threading.Thread | None = None
_lock = threading.Lock()


def start():
    global auto_mode
    with _lock:
        auto_mode = True


def stop():
    global auto_mode, auto_thread
    with _lock:
        auto_mode = False
        auto_thread = None


def is_running() -> bool:
    with _lock:
        return auto_mode
