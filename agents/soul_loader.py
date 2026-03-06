"""Load and cache soul definitions from souls/*.md at startup."""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SOULS_DIR = Path(os.path.dirname(__file__)) / "souls"

_souls: dict[str, str] = {}
_shared: str = ""


def load_all():
    """Load all soul files. Call once at startup."""
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


def build_system_prompt(agent_id: str) -> str:
    """Build the full system prompt: shared context + agent soul."""
    parts = []
    if _shared:
        parts.append(_shared)
    soul = get_soul(agent_id)
    if soul:
        parts.append(soul)
    return "\n\n---\n\n".join(parts)
