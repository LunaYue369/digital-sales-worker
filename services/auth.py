"""Unified OAuth2 for Gmail send + Drive readonly."""

import logging
import os

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

_gmail_service = None
_drive_service = None
_creds = None


def _get_creds():
    """Load or refresh OAuth2 credentials (Gmail + Drive combined scope)."""
    global _creds
    if _creds and _creds.valid:
        return _creds

    creds_path = os.getenv("GMAIL_CREDENTIALS_PATH", "client_secret.json")
    token_path = os.getenv("GMAIL_TOKEN_PATH", "gmail_token.json")

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    _creds = creds
    return _creds

def get_gmail_service():
    """Return a cached Gmail API service."""
    global _gmail_service
    if _gmail_service is None:
        _gmail_service = build("gmail", "v1", credentials=_get_creds())
    return _gmail_service

def get_drive_service():
    """Return a cached Drive API service."""
    global _drive_service
    if _drive_service is None:
        _drive_service = build("drive", "v3", credentials=_get_creds())
    return _drive_service
