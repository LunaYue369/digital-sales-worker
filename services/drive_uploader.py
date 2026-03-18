"""Upload files to Google Drive folder — per-user."""

import logging

from googleapiclient.http import MediaFileUpload

from services.auth import get_drive_service
from core.user_config import get_drive_folder_id

log = logging.getLogger(__name__)


def upload_csv(user_id: str, local_path: str, filename: str) -> str:
    """Upload a local CSV file to the user's Drive folder.

    Returns the uploaded file's Drive ID.
    """
    drive = get_drive_service(user_id)
    folder_id = get_drive_folder_id(user_id)
    file_metadata = {
        "name": filename,
        "parents": [folder_id],
    }
    media = MediaFileUpload(local_path, mimetype="text/csv")
    uploaded = drive.files().create(
        body=file_metadata, media_body=media, fields="id"
    ).execute()
    file_id = uploaded["id"]
    log.info("Uploaded %s to Drive (id=%s, user=%s)", filename, file_id, user_id)
    return file_id
