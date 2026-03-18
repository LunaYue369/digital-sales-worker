import base64
import json
import logging
import os
import re
import threading
import time

from services.auth import get_gmail_service
from services import email_sender
from services.junk_list import report_bounce
from core.user_config import user_data_dir, get_user_name

log = logging.getLogger(__name__)

_lock = threading.Lock()


def _reply_log_path(user_id: str) -> str:
    return os.path.join(user_data_dir(user_id), "reply_log.json")


def _load_reply_log(user_id: str) -> list[dict]:
    path = _reply_log_path(user_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_reply_log(user_id: str, data: list[dict]):
    path = _reply_log_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_reply_log(user_id: str) -> list[dict]:
    with _lock:
        return _load_reply_log(user_id)


def _get_replied_contacts(reply_log: list[dict]) -> set[str]:
    return {r.get("contact_email", "").lower() for r in reply_log if r.get("contact_email")}


def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode_body(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        for sub in part.get("parts", []):
            if sub.get("mimeType") == "text/plain" and sub.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(sub["body"]["data"]).decode("utf-8", errors="replace")
    return ""


def _strip_quoted_text(body: str) -> str:
    lines = body.split("\n")
    clean = []
    for line in lines:
        if re.match(r"^On .+ wrote:\s*$", line):
            break
        if line.strip().startswith("---------- Forwarded message"):
            break
        if re.match(r"^-{2,}\s*Original Message\s*-{2,}", line.strip(), re.IGNORECASE):
            break
        if re.match(r"^From:\s+.+", line.strip()) and any(
            l.strip().startswith("Sent:") for l in lines[lines.index(line):lines.index(line) + 3]
            if lines.index(line) + 3 <= len(lines)
        ):
            break
        if line.strip().startswith(">"):
            continue
        clean.append(line)
    return "\n".join(clean).strip()


def _is_bounce(from_addr: str, subject: str, body: str) -> bool:
    bounce_senders = {"mailer-daemon", "postmaster", "mail delivery subsystem"}
    from_lower = from_addr.lower()
    if any(s in from_lower for s in bounce_senders):
        return True
    bounce_subjects = [
        "delivery status notification", "undeliverable", "mail delivery failed",
        "returned mail", "delivery failure",
    ]
    subj_lower = subject.lower()
    if any(s in subj_lower for s in bounce_subjects):
        return True
    bounce_codes = ["550", "553", "mailbox not found", "user unknown", "address rejected", "no such user"]
    body_lower = body.lower()[:500]
    return any(c in body_lower for c in bounce_codes)


def _is_ooo(headers: list[dict], subject: str) -> bool:
    auto_sub = _get_header(headers, "Auto-Submitted")
    if auto_sub and auto_sub.lower() != "no":
        return True
    if _get_header(headers, "X-Autoreply").lower() in ("yes", "true"):
        return True
    ooo_patterns = [
        "out of office", "automatic reply", "auto-reply", "autoreply",
        "on vacation", "ooo", "away from office",
    ]
    subj_lower = subject.lower()
    if any(p in subj_lower for p in ooo_patterns):
        return True
    return False


def _is_spam_auto(subject: str, body: str) -> bool:
    spam_keywords = [
        "marked as spam", "identified as spam", "spam notification",
        "blocked as spam", "junk mail", "quarantined",
    ]
    body_lower = body.lower()[:500]
    subj_lower = subject.lower()
    if any(k in body_lower or k in subj_lower for k in spam_keywords):
        return True
    return False


def _classify_reply(from_addr: str, subject: str, body: str, headers: list[dict]) -> str:
    if _is_bounce(from_addr, subject, body):
        return "bounce"
    if _is_ooo(headers, subject):
        return "ooo"
    if _is_spam_auto(subject, body):
        return "spam_auto"
    auto_sub = _get_header(headers, "Auto-Submitted")
    if auto_sub and auto_sub.lower() != "no":
        return "auto_reply"
    return "human"


def _parse_sent_at(sent_at_str: str) -> float:
    try:
        return time.mktime(time.strptime(sent_at_str, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


def check_replies(user_id: str) -> list[dict]:
    sent_log = email_sender.get_sent_log(user_id)

    with _lock:
        reply_log = _load_reply_log(user_id)
    replied_contacts = _get_replied_contacts(reply_log)

    thread_map = {}
    for s in sent_log:
        if not s.get("thread_id") or s["status"] != "sent":
            continue
        tid = s["thread_id"]
        contact = s.get("contact_email", "").lower()
        if contact in replied_contacts:
            continue
        if tid not in thread_map:
            thread_map[tid] = s

    if not thread_map:
        return []

    gmail = get_gmail_service(user_id)

    inbox_thread_ids = set()
    try:
        response = gmail.users().messages().list(
            userId="me",
            q="in:inbox newer_than:14d",
            maxResults=500,
        ).execute()

        for msg_meta in response.get("messages", []):
            inbox_thread_ids.add(msg_meta["threadId"])

        while response.get("nextPageToken"):
            response = gmail.users().messages().list(
                userId="me",
                q="in:inbox newer_than:14d",
                maxResults=500,
                pageToken=response["nextPageToken"],
            ).execute()
            for msg_meta in response.get("messages", []):
                inbox_thread_ids.add(msg_meta["threadId"])

    except Exception as e:
        log.error("Gmail messages.list failed: %s", e)
        return []

    matched_thread_ids = set(thread_map.keys()) & inbox_thread_ids

    if not matched_thread_ids:
        return []

    new_replies = []

    for thread_id in matched_thread_ids:
        sent_record = thread_map[thread_id]
        contact_email = sent_record.get("contact_email", "").lower()

        if contact_email in replied_contacts:
            continue

        try:
            thread = gmail.users().threads().get(
                userId="me", id=thread_id, format="full"
            ).execute()
        except Exception as e:
            log.error("Failed to fetch thread %s: %s", thread_id, e)
            continue

        messages = thread.get("messages", [])
        if len(messages) <= 1:
            continue

        our_msg_id = sent_record.get("message_id", "")

        for msg in messages:
            msg_id = msg["id"]
            if msg_id == our_msg_id:
                continue

            headers = msg.get("payload", {}).get("headers", [])
            from_addr = _get_header(headers, "From")
            subject = _get_header(headers, "Subject")
            date_str = _get_header(headers, "Date")
            body = _decode_body(msg.get("payload", {}))
            clean_body = _strip_quoted_text(body)

            reply_type = _classify_reply(from_addr, subject, body, headers)

            internal_date = int(msg.get("internalDate", 0)) / 1000
            sent_at_epoch = _parse_sent_at(sent_record.get("sent_at", ""))
            time_to_reply_hours = round((internal_date - sent_at_epoch) / 3600, 2) if sent_at_epoch else 0

            name_match = re.match(r'^"?([^"<]+)"?\s*<', from_addr)
            from_name = name_match.group(1).strip() if name_match else from_addr.split("@")[0]

            record = {
                "thread_id": thread_id,
                "reply_message_id": msg_id,
                "campaign_id": sent_record.get("campaign_id", ""),
                "company_name": sent_record.get("company_name", ""),
                "contact_email": sent_record.get("contact_email", ""),
                "industry": sent_record.get("industry", ""),
                "original_subject": sent_record.get("subject", ""),
                "original_body_length": sent_record.get("body_length", 0),
                "reply_from": from_addr,
                "reply_from_name": from_name,
                "reply_body": clean_body,
                "reply_date": date_str,
                "reply_epoch": internal_date,
                "time_to_reply_hours": time_to_reply_hours,
                "reply_type": reply_type,
                "analysis": None,
            }
            reply_log.append(record)
            new_replies.append(record)
            replied_contacts.add(contact_email)
            log.info("New %s reply: %s (%s)", reply_type, from_addr, sent_record.get("company_name", ""))

            # Feed bounces to shared junk list for future filtering
            if reply_type == "bounce" and contact_email:
                report_bounce(contact_email, get_user_name(user_id))
            break

    if new_replies:
        with _lock:
            _save_reply_log(user_id, reply_log)

    return new_replies


def update_reply_analysis(user_id: str, reply_message_id: str, analysis: dict):
    with _lock:
        reply_log = _load_reply_log(user_id)
        for r in reply_log:
            if r.get("reply_message_id") == reply_message_id:
                r["analysis"] = analysis
                break
        _save_reply_log(user_id, reply_log)
