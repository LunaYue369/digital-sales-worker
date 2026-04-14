import logging
import os
from pathlib import Path

from core.user_config import user_config_dir

log = logging.getLogger(__name__)

# 共享的默认 souls 目录
SOULS_DIR = Path(os.path.dirname(__file__)) / "souls"
_souls: dict[str, str] = {}
_shared: str = ""


def load_all():
    """Load shared and default agent souls at startup."""
    global _shared

    if not SOULS_DIR.is_dir():
        log.error("Souls directory not found: %s", SOULS_DIR)
        return

    shared_path = SOULS_DIR / "_shared.md"
    if shared_path.exists():
        _shared = shared_path.read_text(encoding="utf-8")
        log.info("Loaded shared soul (%d chars)", len(_shared))
    else:
        log.warning("Shared soul not found: %s", shared_path)

    for md_file in SOULS_DIR.glob("*.md"):
        if md_file.name.startswith("_"):
            continue
        agent_id = md_file.stem
        _souls[agent_id] = md_file.read_text(encoding="utf-8")
        log.info("Loaded soul: %s (%d chars)", agent_id, len(_souls[agent_id]))

    if not _souls:
        log.warning("No agent souls loaded — check %s", SOULS_DIR)
    else:
        log.info("Soul loader ready — %d agent souls loaded", len(_souls))


def get_shared() -> str:
    return _shared


def get_soul(agent_id: str) -> str:
    return _souls.get(agent_id, "")


def _get_user_soul(agent_id: str, user_id: str, template: str = "default") -> str | None:
    """Try to load a per-user soul from config/{dir}/souls/.

    For non-default templates, tries {agent_id}.{template}.md first,
    then falls back to {agent_id}.md.
    """
    if not user_id:
        return None
    souls_dir = Path(user_config_dir(user_id)) / "souls"

    # Try template-specific soul first (e.g. copywriter.discovery.md)
    if template != "default":
        template_path = souls_dir / f"{agent_id}.{template}.md"
        if template_path.exists():
            return template_path.read_text(encoding="utf-8")

    # Fall back to default soul (e.g. copywriter.md)
    default_path = souls_dir / f"{agent_id}.md"
    if default_path.exists():
        return default_path.read_text(encoding="utf-8")
    return None


def build_system_prompt(agent_id: str, user_id: str = "", template: str = "default") -> str:
    """Build system prompt: _shared + default soul + per-user soul (if exists).

    Layered composition:
    1. agents/souls/_shared.md           (always — company, product, rules)
    2. agents/souls/{agent_id}.md        (always — agent role, format, rules)
    3. config/{dir}/souls/{agent_id}.md  (if exists — per-user style/preferences)

    Per-user soul is ADDITIVE, not a replacement. This way per-user files
    only need to contain what's different (e.g. Nate's subject line style),
    while the generic agent soul provides the base (input format, output format, etc.).

    Template support: for non-default templates (e.g. "discovery"), tries
    {agent_id}.{template}.md first, falls back to {agent_id}.md.
    """
    parts = []
    if _shared:
        parts.append(_shared)

    # Always include the default agent soul
    soul = get_soul(agent_id)
    if soul:
        parts.append(soul)

    # Layer per-user soul on top (additive)
    user_soul = _get_user_soul(agent_id, user_id, template)
    if user_soul is not None:
        parts.append(user_soul)
        log.debug("Layered per-user soul for %s (user=%s, template=%s)", agent_id, user_id, template)

    return "\n\n---\n\n".join(parts)
