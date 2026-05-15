"""Slack Block Kit UI — progress indicators and result cards for sales pipeline."""


# ── Stage Definitions ─────────────────────────────────────────────

AI_STAGES = [
    {"key": "parse",    "emoji": ":clipboard:",      "label": "Parsing spreadsheet"},
    {"key": "research", "emoji": ":mag:",             "label": "Researching companies"},
    {"key": "write",    "emoji": ":pencil:",          "label": "Writing emails"},
    {"key": "review",   "emoji": ":eyes:",            "label": "Reviewing emails"},
    {"key": "send",     "emoji": ":outbox_tray:",     "label": "Sending emails"},
    {"key": "done",     "emoji": ":white_check_mark:", "label": "Campaign complete"},
]

STATIC_STAGES = [
    {"key": "parse",    "emoji": ":clipboard:",      "label": "Parsing spreadsheet"},
    {"key": "generate", "emoji": ":pencil:",          "label": "Generating emails"},
    {"key": "send",     "emoji": ":outbox_tray:",     "label": "Sending emails"},
    {"key": "done",     "emoji": ":white_check_mark:", "label": "Campaign complete"},
]


def _get_stages(is_static: bool) -> list[dict]:
    return STATIC_STAGES if is_static else AI_STAGES


# ── Progress Blocks ───────────────────────────────────────────────

def build_progress_blocks(
    current_stage: str,
    is_static: bool = False,
    template: str = "default",
    extra: str = "",
    company_count: int = 0,
) -> list[dict]:
    """Build Slack Block Kit blocks showing pipeline progress."""
    stages = _get_stages(is_static)
    stage_keys = [s["key"] for s in stages]
    current_idx = stage_keys.index(current_stage) if current_stage in stage_keys else -1

    tpl_label = f"  |  Template: `{template}`" if template != "default" else ""

    lines = []
    for i, stage in enumerate(stages):
        if i < current_idx:
            lines.append(f":white_check_mark:  {stage['label']}")
        elif i == current_idx:
            if stage["key"] == "done":
                lines.append(f":white_check_mark:  *{stage['label']}*")
            else:
                lines.append(f"{stage['emoji']}  *{stage['label']}...*")
        else:
            lines.append(f":white_circle:  {stage['label']}")

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":rocket: *Email Campaign Running*{tpl_label}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\n".join(lines),
            },
        },
    ]

    # Extra info line (e.g. "Found 321 companies", "Sent 50/321")
    if extra or company_count:
        info_parts = []
        if company_count:
            info_parts.append(f":busts_in_silhouette: {company_count} companies")
        if extra:
            info_parts.append(extra)
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "  |  ".join(info_parts)}],
        })

    return blocks


# ── Result Card ───────────────────────────────────────────────────

def build_result_blocks(
    sent: int,
    failed: int,
    campaign_id: str,
    template: str = "default",
    is_static: bool = False,
) -> list[dict]:
    """Build Slack Block Kit blocks for campaign completion."""
    total = sent + failed
    tpl_label = f"  |  Template: `{template}`" if template != "default" else ""
    mode = "Static" if is_static else "AI"

    if failed == 0:
        header = f":tada: *Campaign Complete — All {sent} Emails Sent!*"
    else:
        header = f":warning: *Campaign Complete — {failed} Failed*"

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": header},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f":white_check_mark: *Sent:* {sent}"},
                {"type": "mrkdwn", "text": f":x: *Failed:* {failed}"},
                {"type": "mrkdwn", "text": f":label: *Campaign:* `{campaign_id}`"},
                {"type": "mrkdwn", "text": f":gear: *Mode:* {mode}{tpl_label}"},
            ],
        },
    ]

    return blocks
