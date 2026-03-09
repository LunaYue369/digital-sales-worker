import base64
import json
import logging
import os
import re
import threading
import time

from services.auth import get_gmail_service
from services import email_sender

log = logging.getLogger(__name__)

REPLY_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "reply_log.json")
_lock = threading.Lock()


# 加载本地reply数据
def _load_reply_log() -> list[dict]:
    if os.path.exists(REPLY_LOG_PATH):
        with open(REPLY_LOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


# 整体覆盖保存回复记录到本地
def _save_reply_log(data: list[dict]):
    os.makedirs(os.path.dirname(REPLY_LOG_PATH), exist_ok=True)
    with open(REPLY_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# 对外暴露的线程安全读取接口
def get_reply_log() -> list[dict]:
    with _lock:
        return _load_reply_log()


# 调查reply_log里所有的数据list[dict{}]，把所有出现过的contact_email装进set里回复
def _get_replied_contacts(reply_log: list[dict]) -> set[str]:
    return {r.get("contact_email", "").lower() for r in reply_log if r.get("contact_email")}



# 输入gmail headers list[dict]和name，检查name是否在里面，返回其value
def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


# 从 gmail message payload 中提取纯文本正文dict，返回文本string
def _decode_body(payload: dict) -> str:
    # 如果正文就是简单消息，就直接返回
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    # multipart：遍历 parts 找 text/plain
    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        # 嵌套 multipart（如 multipart/alternative 套在 multipart/mixed 里）
        for sub in part.get("parts", []):
            if sub.get("mimeType") == "text/plain" and sub.get("body", {}).get("data"):
                return base64.urlsafe_b64decode(sub["body"]["data"]).decode("utf-8", errors="replace")
    return ""


# 对方如果直接在邮件里reply，那么Body内容里很可能保留我们发的原文，要摘除，只返回客户回复的内容
def _strip_quoted_text(body: str) -> str:
    lines = body.split("\n")
    clean = []
    for line in lines:
        # 遇到 "On <date> <name> wrote:" 截断（Gmail 引用格式）
        if re.match(r"^On .+ wrote:\s*$", line):
            break
        # 遇到 Gmail 转发分隔线截断
        if line.strip().startswith("---------- Forwarded message"):
            break
        # 遇到 Outlook 分隔线截断（From: / Sent: / To:）
        if re.match(r"^-{2,}\s*Original Message\s*-{2,}", line.strip(), re.IGNORECASE):
            break
        if re.match(r"^From:\s+.+", line.strip()) and any(
            l.strip().startswith("Sent:") for l in lines[lines.index(line):lines.index(line) + 3]
            if lines.index(line) + 3 <= len(lines)
        ):
            break
        # 跳过 > 开头的引用行
        if line.strip().startswith(">"):
            continue
        clean.append(line)
    return "\n".join(clean).strip()


# 检测是否是退信：mailer-daemon 发件人、退信主题关键词、SMTP 错误码
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
    # 检查正文前 500 字符中是否有 SMTP 错误码
    bounce_codes = ["550", "553", "mailbox not found", "user unknown", "address rejected", "no such user"]
    body_lower = body.lower()[:500]
    return any(c in body_lower for c in bounce_codes)


# 检测是否是 Out-of-Office 自动回复：Auto-Submitted header、X-Autoreply header、主题关键词
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


# 检测是否被识别为垃圾邮件的自动回复或其他无效自动回复
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


# 定义回复类型
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


# 将 sent_at 字符串转为 epoch 时间戳
def _parse_sent_at(sent_at_str: str) -> float:
    try:
        return time.mktime(time.strptime(sent_at_str, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError):
        return 0.0


# 每个 contact_email 只追踪第一封回复，
# 一旦该联系人在 reply_log 中出现过，后续所有该联系人的 thread 变动都跳过，
# 返回首次新发现的回复保存到 reply_log
#
# 优化策略（反向查询）：
#   旧方案：逐个检查每个未回复 thread → N 次 API 调用（N = 未回复邮件数）
#   新方案：先用 messages.list 一次性拉收件箱最近消息，
#          再用 threadId 跟我们的 thread_map 做交集，
#          只对命中的少量 thread 做 threads.get 拉详情。
#   效果：200 封未回复邮件 → 1 次 list + 仅对有回复的几个 thread 做 get
def check_replies() -> list[dict]:
    sent_log = email_sender.get_sent_log()

    # 加载已有回复记录
    with _lock:
        reply_log = _load_reply_log()
    # replied_contacts：已有首次回复的联系人邮箱集合
    replied_contacts = _get_replied_contacts(reply_log)

    # 构建 thread_map：{thread_id: 该 thread 对应的发送记录(sent_log dict)}
    # 只保留有 thread_id、发送成功、且该联系人尚未回复过的记录
    # 我们只关注还没有收到首次回复的 thread
    thread_map = {}
    for s in sent_log:
        if not s.get("thread_id") or s["status"] != "sent":
            continue
        tid = s["thread_id"]
        contact = s.get("contact_email", "").lower()
        # 该联系人已有首次回复，整个 thread 跳过
        if contact in replied_contacts:
            continue
        if tid not in thread_map:
            thread_map[tid] = s

    if not thread_map:
        return []

    gmail = get_gmail_service()

    # ── 第一步：反向查询收件箱，找出哪些 thread 有新消息 ────────────
    # 用 messages.list 一次性拉取收件箱最近消息（不下载内容，只拿 id 和 threadId）
    # 这比逐个 threads.get 高效得多：1 次 API 调用代替 N 次
    inbox_thread_ids = set()
    try:
        # newer_than:7d 限制范围，避免拉太多历史消息
        # maxResults=500 覆盖大多数场景，Gmail 单次最多返回 500 条
        response = gmail.users().messages().list(
            userId="me",
            q="in:inbox newer_than:7d",
            maxResults=500,
        ).execute()

        # 提取所有收件箱消息的 threadId
        for msg_meta in response.get("messages", []):
            inbox_thread_ids.add(msg_meta["threadId"])

        # 如果结果超过 500 条，继续翻页拉取
        while response.get("nextPageToken"):
            response = gmail.users().messages().list(
                userId="me",
                q="in:inbox newer_than:7d",
                maxResults=500,
                pageToken=response["nextPageToken"],
            ).execute()
            for msg_meta in response.get("messages", []):
                inbox_thread_ids.add(msg_meta["threadId"])

    except Exception as e:
        log.error("Gmail messages.list 失败: %s", e)
        return []

    # ── 第二步：取交集，找出哪些待检查的 thread 收到了回复 ────────────
    # thread_map 里是我们发出去的、尚未收到回复的 thread
    # inbox_thread_ids 里是收件箱最近 7 天有消息的 thread
    # 交集就是"我们发出去的邮件中，对方回复了的"
    matched_thread_ids = set(thread_map.keys()) & inbox_thread_ids

    if not matched_thread_ids:
        return []

    # ── 第三步：只对命中的 thread 拉完整详情 ────────────────────────
    # 这里的 API 调用数 = 实际有回复的 thread 数（通常很少），而非全部未回复数
    new_replies = []

    for thread_id in matched_thread_ids:
        sent_record = thread_map[thread_id]
        contact_email = sent_record.get("contact_email", "").lower()

        # 本轮内该联系人可能已经被前面的迭代处理过了
        if contact_email in replied_contacts:
            continue

        try:
            # 现在才用 format="full" 拉完整消息内容（只对有回复的 thread）
            thread = gmail.users().threads().get(
                userId="me", id=thread_id, format="full"
            ).execute()
        except Exception as e:
            log.error("拉取 thread %s 失败: %s", thread_id, e)
            continue

        messages = thread.get("messages", [])
        if len(messages) <= 1:
            # 只有我们发出去的消息（可能是收件箱里自己的已发送副本），没有回复
            continue

        # 我们发的那条消息的 message_id，用于排除
        our_msg_id = sent_record.get("message_id", "")

        for msg in messages:
            msg_id = msg["id"]
            # 跳过我们自己发的消息
            if msg_id == our_msg_id:
                continue

            # 处理首条回复消息
            headers = msg.get("payload", {}).get("headers", [])
            from_addr = _get_header(headers, "From")
            subject = _get_header(headers, "Subject")
            date_str = _get_header(headers, "Date")
            body = _decode_body(msg.get("payload", {}))
            clean_body = _strip_quoted_text(body)

            # 规则层分类
            reply_type = _classify_reply(from_addr, subject, body, headers)

            # 计算回复耗时（小时）
            internal_date = int(msg.get("internalDate", 0)) / 1000  # ms -> s
            sent_at_epoch = _parse_sent_at(sent_record.get("sent_at", ""))
            time_to_reply_hours = round((internal_date - sent_at_epoch) / 3600, 2) if sent_at_epoch else 0

            # 从 "Name <email>" 格式中提取姓名
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
                "analysis": None,  # 由 reply_analyzer GPT 填充
            }
            # 添加到 reply_log（全量）
            reply_log.append(record)
            # new_replies 负责给 Slack 报信本轮新增了什么回复
            new_replies.append(record)
            # 标记该联系人已有首次回复
            replied_contacts.add(contact_email)
            log.info("New %s reply: %s (%s)", reply_type, from_addr, sent_record.get("company_name", ""))
            # 只取首条回复，跳出内层循环
            break

    if new_replies:
        with _lock:
            _save_reply_log(reply_log)

    return new_replies


# 添加reply_analyzer人格写的分析
def update_reply_analysis(reply_message_id: str, analysis: dict):
    with _lock:
        reply_log = _load_reply_log()
        for r in reply_log:
            if r.get("reply_message_id") == reply_message_id:
                r["analysis"] = analysis
                break
        _save_reply_log(reply_log)
