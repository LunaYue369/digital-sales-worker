"""User configuration registry — manages per-user settings (Drive folder, Gmail, etc.).

Directory layout:
  config/users.json          — user registry (Slack ID → dir name + settings)
  config/{dir}/              — per-user config (gmail_token, souls/)
  data/{dir}/                — per-user runtime data (sent_log, reply_log, etc.)
"""

import json
import logging
import os
import threading

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")
CONFIG_DIR = os.path.join(_PROJECT_ROOT, "config")
DATA_DIR = os.path.join(_PROJECT_ROOT, "data")
USERS_PATH = os.path.join(CONFIG_DIR, "users.json")
_lock = threading.Lock()


def _load_users() -> dict:
    if os.path.exists(USERS_PATH):
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_users(data: dict):
    os.makedirs(os.path.dirname(USERS_PATH), exist_ok=True)
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_user_dir(user_id: str) -> str | None:
    """Return the dir name for a user (e.g. 'nate'), or None if not registered."""
    config = get_user_config(user_id)
    if not config:
        return None
    return config.get("dir", user_id)


def get_user_config(user_id: str) -> dict | None:
    """Return config for a user, or None if not registered."""
    with _lock:
        users = _load_users()
    return users.get(user_id)


def get_drive_folder_id(user_id: str) -> str:
    config = get_user_config(user_id)
    if not config:
        return ""
    return config.get("drive_folder_id", "")


def get_user_name(user_id: str) -> str:
    config = get_user_config(user_id)
    if not config:
        return user_id
    return config.get("name", user_id)


def user_config_dir(user_id: str) -> str:
    """Return per-user config directory (config/{dir}/), creating it if needed."""
    d = _get_user_dir(user_id) or user_id
    path = os.path.join(CONFIG_DIR, d)
    os.makedirs(path, exist_ok=True)
    return path


def user_data_dir(user_id: str) -> str:
    """Return per-user data directory (data/{dir}/), creating it if needed."""
    d = _get_user_dir(user_id) or user_id
    path = os.path.join(DATA_DIR, d)
    os.makedirs(path, exist_ok=True)
    return path


def is_registered(user_id: str) -> bool:
    return get_user_config(user_id) is not None


def is_admin(user_id: str) -> bool:
    config = get_user_config(user_id)
    if not config:
        return False
    return config.get("role") == "admin"


def list_users() -> dict:
    with _lock:
        return _load_users()
