"""Track Pipeline — 后台轮询检测首次回复，调用 GPT 分析，Slack 通知。

流程：
1. 每 POLL_INTERVAL 秒调用 reply_tracker.check_replies() 检测新回复
2. 非真人回复（bounce / ooo / spam / auto_reply）→ 直接 Slack 通知
3. 真人回复 → 并发调用 reply_analyzer GPT 分析 → 写回 reply_log → Slack 通知
"""

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import state
from services import reply_tracker
from agents import reply_analyzer

log = logging.getLogger(__name__)

POLL_INTERVAL = int(os.getenv("REPLY_POLL_INTERVAL", "60"))
# reply_analyzer 分析并发数，真人回复较少所以不需要太多
MAX_ANALYZE_WORKERS = int(os.getenv("TRACK_MAX_WORKERS", "5"))


# 后台轮询主循环，由 bot.py 的 / track 命令在独立线程中启动
def run_track_pipeline(say):

    say(f"Reply tracking started. Polling every {POLL_INTERVAL}s...")

    while state.is_tracking():
        try:
            # 检查有没有新 replies
            new_replies = reply_tracker.check_replies()

            if new_replies:
                _process_replies(new_replies, say)

        except Exception as e:
            log.error("Tracking replies error: %s", e, exc_info=True)

        # 可中断等待：每秒检查一次 tracking 状态，确保 / stop track 能秒停
        for _ in range(POLL_INTERVAL):
            if not state.is_tracking():
                break
            time.sleep(1)

    say("Reply tracking stopped.")


# 处理本轮检测到的所有新回复。非真人回复直接通知，真人回复并发调用 GPT 分析
def _process_replies(new_replies: list[dict], say):
    # 先处理非真人回复（不需要 GPT，直接通知）
    human_replies = []
    for reply in new_replies:
        rtype = reply["reply_type"]
        company = reply.get("company_name", "?")
        email = reply.get("contact_email", "?")

        if rtype == "human":
            human_replies.append(reply)
        elif rtype == "bounce":
            say(f"Bounce: `{email}` ({company})")
        elif rtype == "ooo":
            say(f"Out-of-office: `{email}` ({company})")
        elif rtype == "spam_auto":
            say(f"Spam notification: `{email}` ({company})")
        elif rtype == "auto_reply":
            say(f"Auto-reply: `{email}` ({company})")

    if not human_replies:
        return

    # 真人回复：并发调用 reply_analyzer 分析
    # 刚开启 tracking 时可能积攒了很多未处理的回复，并发能大幅加速
    # 稳定运行后通常每轮 0-2 封，并发开销可忽略
    with ThreadPoolExecutor(max_workers=MAX_ANALYZE_WORKERS) as pool:
        futures = {
            pool.submit(_analyze_reply, reply): reply
            for reply in human_replies
        }
        for future in as_completed(futures):
            reply = futures[future]
            try:
                analysis = future.result()
            except Exception as e:
                log.error("Reply analysis failed for %s: %s",
                          reply.get("company_name", "?"), e)
                analysis = {}
            # 不管分析成功还是失败，都发 Slack 通知
            _notify_human_reply(reply, analysis, say)


# 调用 reply_analyzer 人格分析，并将结果写回 reply_log
def _analyze_reply(reply_record: dict) -> dict:
    analysis = reply_analyzer.analyze_reply(reply_record)
    reply_tracker.update_reply_analysis(reply_record["reply_message_id"], analysis)
    return analysis


# 格式化并发送真人首次回复的 Slack 通知
def _notify_human_reply(reply_record: dict, analysis: dict, say):
    company = reply_record.get("company_name", "?")
    from_addr = reply_record.get("reply_from", "?")
    industry = reply_record.get("industry", "?")

    sentiment = analysis.get("sentiment", "?")
    intent = analysis.get("intent", "?")
    reason = analysis.get("reason_summary", "")
    why = analysis.get("why_accepted_or_rejected", "")
    follow_up = analysis.get("follow_up_advice", "")
    tip = analysis.get("improvement_tip", "")
    factors = analysis.get("company_factors", {})

    # 构建 Slack 消息
    msg = (
        f"*New reply: {company}* ({industry})\n"
        f"From: {from_addr}\n"
        f"Sentiment: *{sentiment}* | Intent: {intent}\n"
        f"Time to reply: {reply_record.get('time_to_reply_hours', '?')}h\n"
    )
    if reason:
        msg += f"Summary: {reason}\n"
    if why:
        msg += f"Analysis: {why}\n"

    # 公司画像
    if factors:
        parts = []
        if factors.get("size_signal") and factors["size_signal"] != "unknown":
            parts.append(f"Size: {factors['size_signal']}")
        if factors.get("pain_point"):
            parts.append(f"Pain point: {factors['pain_point']}")
        if factors.get("current_solution"):
            parts.append(f"Current solution: {factors['current_solution']}")
        if parts:
            msg += f"Company profile: {' | '.join(parts)}\n"

    # 跟进建议（告诉销售怎么回复这封邮件）
    if follow_up:
        msg += f"\n*Follow-up advice:* {follow_up}\n"

    # 邮件改进建议（改进未来的冷邮件）
    if tip:
        msg += f"\n*Improvement tip:* {tip}\n"

    # 附上回复原文摘要
    msg += f"---\n_{reply_record.get('reply_body', '')[:300]}_"

    say(msg)
