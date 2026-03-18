import io
import json
import logging
import os
import threading

import pandas as pd
from googleapiclient.http import MediaIoBaseDownload

from services.auth import get_drive_service
from core.user_config import user_data_dir, get_drive_folder_id

log = logging.getLogger(__name__)

MIME_CSV = "text/csv"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_XLS = "application/vnd.ms-excel"
MIME_GSHEET = "application/vnd.google-apps.spreadsheet"
SUPPORTED_MIMES = {MIME_CSV, MIME_XLSX, MIME_XLS, MIME_GSHEET}
_lock = threading.Lock()


def _processed_path(user_id: str) -> str:
    return os.path.join(user_data_dir(user_id), "processed_files.json")


def _load_processed(user_id: str) -> set[str]:
    path = _processed_path(user_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def _save_processed(user_id: str, ids: set[str]):
    path = _processed_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(list(ids), f)


def mark_processed(user_id: str, file_id: str):
    with _lock:
        ids = _load_processed(user_id)
        ids.add(file_id)
        _save_processed(user_id, ids)


def poll_new_files(user_id: str) -> list[dict]:
    drive = get_drive_service(user_id)
    processed = _load_processed(user_id)
    folder_id = get_drive_folder_id(user_id)

    if not folder_id:
        log.warning("No Drive folder configured for user %s", user_id)
        return []

    query = f"'{folder_id}' in parents and trashed = false"
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

    log.info("Drive poll (user %s): %d total files, %d new", user_id, len(files), len(new_files))
    return new_files


def download_file(user_id: str, file_info: dict) -> pd.DataFrame:
    drive = get_drive_service(user_id)
    file_id = file_info["id"]
    mime = file_info["mimeType"]
    name = file_info["name"]

    buf = io.BytesIO()

    if mime == MIME_GSHEET:
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
