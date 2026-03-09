import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)
# 储存souls的文件夹
SOULS_DIR = Path(os.path.dirname(__file__)) / "souls"
# 储存
_souls: dict[str, str] = {}
# 储存通用给所有agents的_shared.md的string
_shared: str = ""

# load所有agents都需要阅读的md，该function在main.py初次启动的时候就load了，然后
def load_all():
    global _shared

    if not SOULS_DIR.is_dir():
        log.error("Souls directory not found: %s", SOULS_DIR)
        return

    # 在_share里储存_shared.md的string
    shared_path = SOULS_DIR / "_shared.md"
    if shared_path.exists():
        _shared = shared_path.read_text(encoding="utf-8")
        log.info("Loaded shared soul (%d chars)", len(_shared))
    else:
        log.warning("Shared soul not found: %s", shared_path)

    # 对于soul文件夹里的所有其他独立的md
    for md_file in SOULS_DIR.glob("*.md"):
        if md_file.name.startswith("_"):
            continue
        # 存进dictionary，{人格名字：对应md的string}
        agent_id = md_file.stem
        _souls[agent_id] = md_file.read_text(encoding="utf-8")
        log.info("Loaded soul: %s (%d chars)", agent_id, len(_souls[agent_id]))

    if not _souls:
        log.warning("No agent souls loaded — check %s", SOULS_DIR)
    else:
        log.info("Soul loader ready — %d agent souls loaded", len(_souls))

# 获取_shared string
def get_shared() -> str:
    return _shared

# 获取某个人格的string
def get_soul(agent_id: str) -> str:
    return _souls.get(agent_id, "")

# 拼接_shared + 独立人格 string
def build_system_prompt(agent_id: str) -> str:
    parts = []
    if _shared:
        parts.append(_shared)
    soul = get_soul(agent_id)
    if soul:
        parts.append(soul)
    return "\n\n---\n\n".join(parts)
