import threading

_users: dict[str, dict] = {}
_lock = threading.Lock()


def _get_user(user_id: str) -> dict:
    """Lazily initialize per-user state."""
    if user_id not in _users:
        _users[user_id] = {
            "auto_mode": False,
            "auto_thread": None,
            "tracking_mode": False,
            "tracking_thread": None,
        }
    return _users[user_id]


def start_auto(user_id: str):
    with _lock:
        _get_user(user_id)["auto_mode"] = True


def stop_auto(user_id: str):
    with _lock:
        u = _get_user(user_id)
        u["auto_mode"] = False
        u["auto_thread"] = None


def is_auto_running(user_id: str) -> bool:
    with _lock:
        return _get_user(user_id)["auto_mode"]


def get_auto_thread(user_id: str):
    with _lock:
        return _get_user(user_id)["auto_thread"]


def set_auto_thread(user_id: str, thread):
    with _lock:
        _get_user(user_id)["auto_thread"] = thread


def start_tracking(user_id: str):
    with _lock:
        _get_user(user_id)["tracking_mode"] = True


def stop_tracking(user_id: str):
    with _lock:
        u = _get_user(user_id)
        u["tracking_mode"] = False
        u["tracking_thread"] = None


def is_tracking(user_id: str) -> bool:
    with _lock:
        return _get_user(user_id)["tracking_mode"]


def get_tracking_thread(user_id: str):
    with _lock:
        return _get_user(user_id)["tracking_thread"]


def set_tracking_thread(user_id: str, thread):
    with _lock:
        _get_user(user_id)["tracking_thread"] = thread
