"""Upload files to Google Drive folder."""

import logging
import os

from googleapiclient.http import MediaFileUpload

from services.auth import get_drive_service

log = logging.getLogger(__name__)

DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "")


def upload_csv(local_path: str, filename: str) -> str:
    """Upload a local CSV file to the Drive folder.

    Returns the uploaded file's Drive ID.
    """
    drive = get_drive_service()
    file_metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID],
    }
    media = MediaFileUpload(local_path, mimetype="text/csv")
    uploaded = drive.files().create(
        body=file_metadata, media_body=media, fields="id"
    ).execute()
    file_id = uploaded["id"]
    log.info("Uploaded %s to Drive (id=%s)", filename, file_id)
    return file_id
