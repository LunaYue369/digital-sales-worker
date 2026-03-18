"""Unified OAuth2 for Gmail send + Drive — per-user credentials.

Auth flow: Slack-based (no local browser needed).
  1. User sends `/ auth` → bot replies with OAuth URL
  2. User opens URL, authorizes, copies the code
  3. User pastes code back in Slack → bot exchanges it for a token
"""

import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from core.user_config import user_config_dir

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

REDIRECT_URI = "http://localhost"

_creds: dict[str, Credentials] = {}
_gmail_services: dict[str, object] = {}
_drive_services: dict[str, object] = {}
_pending_flows: dict[str, Flow] = {}


def _get_creds(user_id: str):
    """Load or refresh OAuth2 credentials for a specific user.

    Raises RuntimeError if no token exists (user must run `/ auth` first).
    """
    if user_id in _creds and _creds[user_id].valid:
        return _creds[user_id]

    creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", "client_secret.json")
    token_path = os.path.join(user_config_dir(user_id), "gmail_token.json")

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError("Gmail not authorized. Please run `/ auth` first.")

    _creds[user_id] = creds
    return _creds[user_id]


def generate_auth_url(user_id: str) -> str:
    """Generate an OAuth URL for the user to authorize in their browser."""
    creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", "client_secret.json")
    flow = Flow.from_client_secrets_file(creds_path, scopes=SCOPES, redirect_uri=REDIRECT_URI)
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    _pending_flows[user_id] = flow
    return auth_url


def exchange_auth_code(user_id: str, code: str):
    """Exchange the authorization code for credentials and save the token."""
    flow = _pending_flows.pop(user_id, None)
    if not flow:
        raise RuntimeError("No pending auth flow. Please run `/ auth` first.")

    flow.fetch_token(code=code)
    creds = flow.credentials
    token_path = os.path.join(user_config_dir(user_id), "gmail_token.json")
    with open(token_path, "w") as f:
        f.write(creds.to_json())

    # Clear cached services so they rebuild with the new creds
    _creds.pop(user_id, None)
    _gmail_services.pop(user_id, None)
    _drive_services.pop(user_id, None)


def get_gmail_service(user_id: str):
    """Return a cached Gmail API service for the user."""
    if user_id not in _gmail_services:
        _gmail_services[user_id] = build("gmail", "v1", credentials=_get_creds(user_id))
    return _gmail_services[user_id]


def get_drive_service(user_id: str):
    """Return a cached Drive API service for the user."""
    if user_id not in _drive_services:
        _drive_services[user_id] = build("drive", "v3", credentials=_get_creds(user_id))
    return _drive_services[user_id]


def needs_auth(user_id: str) -> bool:
    """Check if user needs to go through OAuth setup."""
    token_path = os.path.join(user_config_dir(user_id), "gmail_token.json")
    return not os.path.exists(token_path)


def has_pending_flow(user_id: str) -> bool:
    """Check if user has a pending auth flow waiting for a code."""
    return user_id in _pending_flows
