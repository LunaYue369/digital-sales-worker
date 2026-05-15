import io
import logging

import pandas as pd
from google.cloud import firestore
from googleapiclient.http import MediaIoBaseDownload

from services.auth import get_drive_service
from services.firestore_client import user_processed_files_col
from core.user_config import get_drive_folder_id

log = logging.getLogger(__name__)

MIME_CSV = "text/csv"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_XLS = "application/vnd.ms-excel"
MIME_GSHEET = "application/vnd.google-apps.spreadsheet"
SUPPORTED_MIMES = {MIME_CSV, MIME_XLSX, MIME_XLS, MIME_GSHEET}


def _load_processed(user_id: str) -> set[str]:
    """Stream every processed-file doc id (= Drive file_id) for this user."""
    return {doc.id for doc in user_processed_files_col(user_id).stream()}


def mark_processed(user_id: str, file_id: str, file_name: str):
    user_processed_files_col(user_id).document(file_id).set({
        "file_name": file_name,
        "processed_at": firestore.SERVER_TIMESTAMP,
    })


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
