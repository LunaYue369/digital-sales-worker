import base64
import json
import logging
import os
import threading
import time
from email.mime.text import MIMEText

from services.auth import get_gmail_service

log = logging.getLogger(__name__)

SENT_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "sent_log.json")
_lock = threading.Lock()


def _load_sent_log() -> list[dict]:
    if os.path.exists(SENT_LOG_PATH):
        with open(SENT_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


# 整体覆盖保存
def _save_sent_log(data: list[dict]):
    os.makedirs(os.path.dirname(SENT_LOG_PATH), exist_ok=True)
    with open(SENT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# 单独发送一封邮件，若成功会返回
"""
  {
    "id": "18e1a2b3c4d5e6f7",
    "threadId": "18e1a2b3c4d5e6f7",
    "labelIds": ["SENT"]
  }
"""
def send_email(to: str, subject: str, body: str) -> dict | None:
    """Send a single email via Gmail API. Returns Gmail message dict on success, None on failure."""
    try:
        gmail = get_gmail_service()
        msg = MIMEText(body, "plain", "utf-8")
        msg["to"] = to
        msg["subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        result = gmail.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        log.info("Email sent to %s (threadId=%s)", to, result.get("threadId"))
        return result
    except Exception as e:
        log.error("Failed to send email to %s: %s", to, e)
        return None


# 收到一组需要的审核成功的邮件字典列表，是该file里所有最后要发送的邮件，算作同一个campaign下面
# 返回发送邮件成功和不成功的数量
def send_campaign(emails: list[dict], campaign_id: str) -> dict:
    sent_count = 0
    failed_count = 0

    with _lock:
        # 加载所有本地sent log数据，list[dict]
        sent_log = _load_sent_log()

        for email in emails:
            # 对于每一家公司都调用一次send_email，传入信息字典里的邮箱，subject，body
            # 若result有字典，就成功，没有就说明发送失败
            result = send_email(email["contact_email"], email["subject"], email["body"])
            success = result is not None
            # 储存在本地数据库sent_log里
            record = {
                "campaign_id": campaign_id,
                "company_name": email.get("company_name", ""),
                "contact_email": email["contact_email"],
                "subject": email["subject"],
                "body": email.get("body", ""),
                "industry": email.get("industry", ""),
                "thread_id": result.get("threadId", "") if result else "",
                "message_id": result.get("id", "") if result else "",
                "status": "sent" if success else "failed",
                "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            # 添加新数据
            sent_log.append(record)
            # 整体覆盖保存
            _save_sent_log(sent_log)
            # 记录数量
            if success:
                sent_count += 1
            else:
                failed_count += 1

    return {"sent": sent_count, "failed": failed_count}


# 获取Sent了多少份
def get_sent_count() -> int:
    with _lock:
        return len(_load_sent_log())


# 获取整个Sent_log数据库
def get_sent_log() -> list[dict]:
    with _lock:
        return _load_sent_log()
