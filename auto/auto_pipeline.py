"""Auto Mode pipeline — orchestrates the full flow in a background thread."""

import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import state
from services import drive_poller, spreadsheet, email_sender, usage_tracker, failed_log
from agents import researcher, copywriter, reviewer

log = logging.getLogger(__name__)

# 从.env里找AUTO_POLL_INTERVAL，若没有，就设定为300s
POLL_INTERVAL = int(os.getenv("AUTO_POLL_INTERVAL", "300"))
# copywriter->reviewer审核的次数
MAX_REVIEW_ROUNDS = int(os.getenv("REVIEWER_MAX_ROUNDS", "3"))
# copywriter->reviewer并发多少个
MAX_WORKERS = int(os.getenv("PIPELINE_MAX_WORKERS", "5"))
# 单家公司的重试次数。一家公司走 Copywriter → Reviewer 这条路，如果 GPT 调用失败（比如 token 超限、API报错），最多重试 3 次。3 次都失败就放弃这家，不发邮件
MAX_RETRIES_PER_COMPANY = int(os.getenv("PIPELINE_RETRY_LIMIT", "3"))


# 开启auto
def run_auto_pipeline(say):
    say(f"Auto mode started. Polling Google Drive every {POLL_INTERVAL}s for new files...\n"
        f"Drive folder: `{os.getenv('DRIVE_FOLDER_ID', 'NOT SET')}`")

    first_run = True
    # 当auto模式一直开启的时候，就不断poll drive
    while state.is_auto_running():
        try:
            # 运行poll drive，查看是否有新文件
            found = _poll_once(say)
            # 第一次开启auto的时候给出提示
            if first_run and not found:
                say("First poll complete — no new files found. Waiting for uploads...")
            first_run = False
        except Exception as e:
            log.error("Pipeline error: %s", e, exc_info=True)
            say(f"Pipeline error: {e}")

        # POLL_INTERVAL里的每一秒都暂停1秒，达到了每POLL_INTERVAL秒就运行一次while loop的效果
        for _ in range(POLL_INTERVAL):
            # 如果这中途用户停止auto，就break
            if not state.is_auto_running():
                break
            time.sleep(1)

    say("Auto mode stopped.")


# 单轮poll drive，返回是否有新文件
def _poll_once(say) -> bool:
    # 使用drive_poller的poll_new_files的功能
    new_files = drive_poller.poll_new_files()
    
    # 如果没有新文件，直接返回False
    if not new_files:
        return False

    # 如果有新文件，有list[dict{id, name, mimeType}]
    for file_info in new_files:
        if not state.is_auto_running():
            break
        # 对于每个新file dict 提取信息
        file_name = file_info["name"]
        file_id = file_info["id"]
        say(f"New file detected: `{file_name}` — processing...")

        # 并且对于每一个新file dict
        try:
            # 运行_process_file，不并发，因为每一个_process_file里就已经有并发运行GPT Agents
            _process_file(file_info, say)
            # 并且标记该文件已经被processed
            drive_poller.mark_processed(file_id)
        except Exception as e:
            log.error("Error processing %s: %s", file_name, e, exc_info=True)
            say(f"Error processing `{file_name}`: {e}")
    
    # 所有新文件都处理完之后就返回True 
    return True


# 对于每一个new file dict使用三重AI Agents处理
def _process_file(file_info: dict, say):
    # 我们按照每一个列表的公司来处理，每一组公司的宣传就算做一组campaign
    campaign_id = f"campaign_{uuid.uuid4().hex[:8]}"

    # 1.把file下载到缓存区 + parse表格
    df = drive_poller.download_file(file_info)
    # list[dict{每个公司的表格信息}]
    companies = spreadsheet.parse_dataframe(df)
    # 如果没有companies就说明这个表格有问题
    if not companies:
        say(f"No new companies to process in `{file_info['name']}` (all already sent or missing required fields).")
        return

    say(f"Found {len(companies)} new companies in `{file_info['name']}`. Starting research...")

    # 2.Research Agent，交给他一组companies的信息，list[dict{公司的df信息}]和对应的campaign_id
    researched = researcher.research_batch(companies, campaign_id)
    # 计算这组companies里有多少个公司在本地的Cache里没过期
    cached = sum(1 for r in researched if r.get("from_cache"))
    say(f"*Research complete:* {len(researched)} companies ({cached} from cache)")

    if not state.is_auto_running():
        return

    # 3.并发处理邮件编辑，审阅，发送
    say(f"Writing & reviewing {len(researched)} emails (up to {MAX_REVIEW_ROUNDS} rounds each, {MAX_WORKERS} concurrent)...")
    # 准备好发送邮件的公司
    ready_to_send = []
    # 等待发送邮件的公司
    pending = list(researched)

    # 一个公司最多重试3次
    for attempt in range(1, MAX_RETRIES_PER_COMPANY + 1):
        if not pending:
            break

        if attempt > 1:
            say(f"Retry attempt {attempt}/{MAX_RETRIES_PER_COMPANY} for {len(pending)} failed companies (waiting 10s)...")
            time.sleep(10)

        still_failed = []
        
        # 一次性对5个公司，并发运行_write_and_review
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_write_and_review, company, campaign_id): company
                for company in pending
            }

            # 对于完成_write_and_review的公司
            for future in as_completed(futures):
                if not state.is_auto_running():
                    pool.shutdown(wait=False, cancel_futures=True)
                    return

                company = futures[future]
                name = company.get("company_name", "?")
                try:
                    # 抓取_write_and_review传回来的字典
                    result = future.result()
                    ready_to_send.append(result)
                except Exception as e:
                    log.error("Failed for %s (attempt %d): %s", name, attempt, e)
                    # 把失败写邮件的公司加入到still_failed里
                    still_failed.append(company)

                if len(ready_to_send) % 5 == 0 and ready_to_send:
                    say(f"  Progress: {len(ready_to_send)}/{len(researched)} emails done")
        
        # pending（重试） — _write_and_review 抛了异常（API 报错、网络超时等），进 still_failed，会重试
        pending = still_failed

    # pending代表重试失败的
    if pending:
        for c in pending:
            # 储存
            failed_log.record_error(c, campaign_id, f"Failed all {MAX_RETRIES_PER_COMPANY} retry attempts")
        names = [c.get("company_name", "?") for c in pending]
        say(f"Gave up on {len(pending)} companies after {MAX_RETRIES_PER_COMPANY} attempts: {', '.join(names)}")

    # discarded代表，虽然三轮review走下来，但是review失败的，那么我们就不会发出去
    discarded = [e for e in ready_to_send if e.get("_review_failed")]
    # discarded代表，三轮review走下来，review成功的，那么我们就发出去
    approved = [e for e in ready_to_send if not e.get("_review_failed")]

    if discarded:
        for e in discarded:
            # 储存discarded
            failed_log.record_discarded(e, campaign_id)
        names = [e.get("company_name", "?") for e in discarded]
        say(f"Discarded {len(discarded)} emails (failed all {MAX_REVIEW_ROUNDS} review rounds): {', '.join(names)}")
    if not approved:
        say("No emails passed review. Nothing to send.")
        return
    say(f"{len(approved)} emails passed review.")

    if not state.is_auto_running():
        return

    # 4.群发邮件
    say(f"Sending {len(approved)} emails...")
    # 调用email_sender的send_campaign，把所有approved的邮件字典传输进去
    # 获取成功发送了多少份，失败发送了多少份
    result = email_sender.send_campaign(approved, campaign_id)
    say(f"*Campaign `{campaign_id}` complete:*\n"
        f"  Sent: {result['sent']}\n"
        f"  Failed: {result['failed']}")

    # 5.Tokens使用报告
    report = usage_tracker.format_slack_report(campaign_id)
    say(report)


# 对某个公司的dict做copywriter->reviewer，返回一个字典，包含公司信息，approved的邮件的sub，body，和是否审核失败
# copywriter write_email -> reviewer review_email -> 不合格的email reviewer biuld_feedback -> copywriter write_email with feedback
def _write_and_review(company: dict, campaign_id: str) -> dict:
    name = company.get("company_name", "?")
    # 把该公司dict传入copywriter的write_email字典
    # 获取subject str, body str, tokens字典
    subject, body, _ = copywriter.write_email(company, campaign_id)

    # copywriter->reviewer的过程最多审核3次
    for round_num in range(1, MAX_REVIEW_ROUNDS + 1):
        # review
        review = reviewer.review_email(company, subject, body, campaign_id)
        # 如果被判定合格，就提前跳出循环
        if review.get("approved", False):
            log.info("%s APPROVED at round %d", name, round_num)
            break
        # 否则reviewer写feedback
        feedback = reviewer.build_feedback(review)
        log.info("%s REJECTED round %d", name, round_num)
        # 把被rejected的邮件Subject和body
        previous_email = f"Subject: {subject}\n\n{body}"
        # 加上Feedback重新交给copywriter来写
        subject, body, _ = copywriter.write_email(
            company, campaign_id, feedback=feedback, previous_email=previous_email
        )

    else:
        log.warning("%s FAILED all %d review rounds — discarding", name, MAX_REVIEW_ROUNDS)
        # 三轮过后还是审核失败
        return {**company, "subject": subject, "body": body, "_review_failed": True}

    # 三轮过后审核成功
    return {**company, "subject": subject, "body": body, "_review_failed": False}


