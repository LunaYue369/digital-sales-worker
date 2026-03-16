import json
import logging
import os

import pandas as pd

log = logging.getLogger(__name__)

# 记录发送的数据的信息在本地
SENT_LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "sent_log.json")

# 所有可能出现在表格里的column names，以及对应的下游文件所使用的简化版名字
_COLUMN_MAP = {
    "company name": "company_name",
    "company": "company_name",
    "contact email": "contact_email",
    "email": "contact_email",
    "website": "website",
    "primary industry": "industry",
    "industry": "industry",
    "core business": "core_business",
    "business": "core_business",
    "company country": "country",
    "country": "country",
    "company city": "city",
    "city": "city",
    "revenue range (in usd)": "revenue",
    "revenue": "revenue",
    "employee range": "employees",
    "employees": "employees",
    "contact name": "contact_name",
    "contact": "contact_name",
    "phone": "phone",
    "review_rating": "review_rating",
    "review rating": "review_rating",
    "rating": "review_rating",
    "review_count": "review_count",
    "review count": "review_count",
    "reviews": "review_count",
    "state": "state",
}

# 表格里必须要包含的两个要素
REQUIRED_FIELDS = {"company_name", "contact_email"}

# sent_log的本地数据库里记载的是所有已经发过邮件的公司
"""
{"campaign_id": "campaign_a1b2c3d4",
"company_name": "Joe's Pizza",
"contact_email": "joe@joespizza.com",
"subject": "Quick question about Joe's Pizza",
"body_length": 450,
"industry": "Restaurant",
"thread_id": "18e3f...",
"message_id": "18e3f...",
"status": "sent",
"sent_at": "2026-03-06 10:30:00"}"""
# 只挑出来contact_emails打包成set 
def _load_sent_emails() -> set[str]:
    if os.path.exists(SENT_LOG_PATH):
        with open(SENT_LOG_PATH, "r", encoding="utf-8") as f:
            records = json.load(f)
        return {r["contact_email"].lower() for r in records}
    return set()

# parse 下载到缓冲区的已经转化为panda dataframe的new file
def parse_dataframe(df: pd.DataFrame) -> list[dict]:
    # 给表格里每一个column都normalize下游文件想使用的名字
    rename = {}
    for col in df.columns:
        key = col.strip().lower()
        if key in _COLUMN_MAP:
            rename[col] = _COLUMN_MAP[key]
    df = df.rename(columns=rename)

    # 检查是否包含必要的fields，就是公司名字和邮箱
    missing = REQUIRED_FIELDS - set(df.columns)
    if missing:
        log.error("Spreadsheet missing required columns: %s", missing)
        return []

    # 使用load_sent_emails检查sent_log里已经发过邮件的公司
    sent_emails = _load_sent_emails()
    companies = []

    # parse new file 的 panda df
    # 对于每一行数据
    for _, row in df.iterrows():
        record = {}
        # 把df上每一行都存进record里，col: val
        for col in df.columns:
            val = row[col]
            record[col] = "" if pd.isna(val) else str(val).strip()

        if not record.get("contact_email"):
            continue

        # 如果发现某个email之前联系过，就不再管了
        if record["contact_email"].lower() in sent_emails:
            log.info("Skipping already-sent: %s", record["contact_email"])
            continue

        # 如果没有提供contact name，写成company name's friends
        if not record.get("contact_name"):
            record["contact_name"] = f"{record.get('company_name', 'Unknown')}'s friends"

        # list[dict{某个company的所有信息}]
        companies.append(record)

    log.info("Parsed %d companies (%d after dedup)", len(df), len(companies))
    return companies
