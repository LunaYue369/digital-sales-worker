import io
import json
import logging
import os
import threading

import pandas as pd
from googleapiclient.http import MediaIoBaseDownload

from services.auth import get_drive_service

log = logging.getLogger(__name__)

# 储存被processed过的files
PROCESSED_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "processed_files.json")
# 检测某个drive folder
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID", "")
# 能接受的files格式
MIME_CSV = "text/csv"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_XLS = "application/vnd.ms-excel"
MIME_GSHEET = "application/vnd.google-apps.spreadsheet"
SUPPORTED_MIMES = {MIME_CSV, MIME_XLSX, MIME_XLS, MIME_GSHEET}
_lock = threading.Lock()


# 打开本地储存的被处理过的files的文件，返回set of [processed ids]
def _load_processed() -> set[str]:
    if os.path.exists(PROCESSED_PATH):
        with open(PROCESSED_PATH, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

# 在本地储存set of list of processed files ids
def _save_processed(ids: set[str]):
    os.makedirs(os.path.dirname(PROCESSED_PATH), exist_ok=True)
    with open(PROCESSED_PATH, "w", encoding="utf-8") as f:
        json.dump(list(ids), f)

# 标记某个为processed
def mark_processed(file_id: str):
    with _lock:
        ids = _load_processed()
        ids.add(file_id)
        _save_processed(ids)

# 返回drive folder里抓没有被processed的files，list[file dicts{id, name, mimType}]
def poll_new_files() -> list[dict]:
    drive = get_drive_service()
    # 已经被处理过的files
    processed = _load_processed()
    
    # 在drive folder里抓出所有files
    # 在文件夹里找出没有被trashed的文件
    query = f"'{DRIVE_FOLDER_ID}' in parents and trashed = false"
    # 并且只返回我们想要的字段，就是files里的id, name, mimeType
    resp = drive.files().list(
        q=query, fields="files(id, name, mimeType)", pageSize=100
    ).execute()
    # list[dict]
    files = resp.get("files", [])

    # 筛选出未被处理的files而且符合我们想要的文件形式
    new_files = []
    for f in files:
        # 如果drive里标记的文件类型虽然特殊但也是我们想要的
        if f["id"] not in processed and f["mimeType"] in SUPPORTED_MIMES:
            new_files.append(f)
        # 如果drive里标记的类型就是普通的正确版本
        elif f["id"] not in processed and f["name"].lower().endswith((".csv", ".xlsx", ".xls")):
            new_files.append(f)

    log.info("Drive poll: %d total files, %d new", len(files), len(new_files))
    return new_files

# 下载某个file dict{id, name, mimeType}，到pd.DataFrame
def download_file(file_info: dict) -> pd.DataFrame:
    drive = get_drive_service()
    file_id = file_info["id"]
    mime = file_info["mimeType"]
    name = file_info["name"]

    buf = io.BytesIO()
    
    # 把google spreedsheet改成xlsx给本地处理
    if mime == MIME_GSHEET:
        request = drive.files().export_media(fileId=file_id, mimeType=MIME_XLSX)
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buf.seek(0)
        return pd.read_excel(buf)
    # 其他正确的处理，反正不管一开始怎样，最后导出到本地处理都是csv，xlsx，xls的三种形式之一
    # 下载成panda dataframes到io内存缓存区，而不是写进磁盘，之后自己会被释放
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
