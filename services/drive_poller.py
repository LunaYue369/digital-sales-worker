"""Poll Google Drive folder for new CSV/Excel files."""

import io
import json
import logging
import os
import threading

import pandas as pd
from googleapiclient.http import MediaIoBaseDownload

from services.auth import get_drive_service

log = logging.getLogger(__name__)

PROCESSED_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "processed_files.json")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "")

MIME_CSV = "text/csv"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_XLS = "application/vnd.ms-excel"
MIME_GSHEET = "application/vnd.google-apps.spreadsheet"
SUPPORTED_MIMES = {MIME_CSV, MIME_XLSX, MIME_XLS, MIME_GSHEET}
_lock = threading.Lock()


def _load_processed() -> set[str]:
    if os.path.exists(PROCESSED_PATH):
        with open(PROCESSED_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def _save_processed(ids: set[str]):
    os.makedirs(os.path.dirname(PROCESSED_PATH), exist_ok=True)
    with open(PROCESSED_PATH, "w", encoding="utf-8") as f:
        json.dump(list(ids), f)


def mark_processed(file_id: str):
    with _lock:
        ids = _load_processed()
        ids.add(file_id)
        _save_processed(ids)


def poll_new_files() -> list[dict]:
    """Return list of new (unprocessed) files: [{id, name, mimeType}]."""
    drive = get_drive_service()
    processed = _load_processed()

    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed = false"
    resp = drive.files().list(
        q=query, fields="files(id, name, mimeType)", pageSize=100
    ).execute()
    files = resp.get("files", [])

    new_files = []
    for f in files:
        if f["id"] not in processed and f["mimeType"] in SUPPORTED_MIMES:
            new_files.append(f)
        elif f["id"] not in processed and f["name"].lower().endswith((".csv", ".xlsx", ".xls")):
            new_files.append(f)

    log.info("Drive poll: %d total files, %d new", len(files), len(new_files))
    return new_files


def download_file(file_info: dict) -> pd.DataFrame:
    """Download a Drive file and return as DataFrame."""
    drive = get_drive_service()
    file_id = file_info["id"]
    mime = file_info["mimeType"]
    name = file_info["name"]

    buf = io.BytesIO()

    if mime == MIME_GSHEET:
        # Export Google Sheet as xlsx
        request = drive.files().export_media(fileId=file_id, mimeType=MIME_XLSX)
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buf.seek(0)
        return pd.read_excel(buf)
    else:
        request = drive.files().get_media(fileId=file_id)
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buf.seek(0)

        if name.lower().endswith(".csv") or mime == MIME_CSV:
            return pd.read_csv(buf)
        else:
            return pd.read_excel(buf)
