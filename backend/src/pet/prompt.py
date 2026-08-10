"""Load product-owned system prompts for model commentary."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

PROMPTS_DIRECTORY = Path(__file__).resolve().parents[2] / "prompts"
PromptPersonality = Literal["brother", "caster", "inference"]


def load_system_prompt(
    personality_style: PromptPersonality,
    *,
    max_chars: int,
    prompts_directory: Path = PROMPTS_DIRECTORY,
    include_reading_guide: bool = True,
) -> str:
    """Join the shared reading guide and one product-owned personality prompt."""
    parts = []
    if include_reading_guide:
        parts.append(_read_prompt_file(prompts_directory / "reading.md"))
    personality = _read_prompt_file(
        prompts_directory / f"{personality_style}.md"
    )
    parts.append(personality)
    return "\n\n".join(parts).replace("{max_chars}", str(max_chars))


def _read_prompt_file(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise FileNotFoundError(f"系统提示词文件不存在：{path}") from error
    if not content.strip():
        raise ValueError(f"系统提示词文件为空：{path}")
    return content.strip()
