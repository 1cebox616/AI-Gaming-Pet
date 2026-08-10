"""Load product-owned system prompts for model commentary."""

from __future__ import annotations

from pathlib import Path

from pet.config import PersonalityStyle

PROMPTS_DIRECTORY = Path(__file__).resolve().parents[2] / "prompts"


def load_system_prompt(
    personality_style: PersonalityStyle,
    *,
    max_chars: int,
    prompts_directory: Path = PROMPTS_DIRECTORY,
) -> str:
    """Load one external prompt and replace its measured length limit."""
    path = prompts_directory / f"{personality_style}.md"
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise FileNotFoundError(f"系统提示词文件不存在：{path}") from error
    if not content.strip():
        raise ValueError(f"系统提示词文件为空：{path}")
    if "{max_chars}" not in content:
        raise ValueError(f"系统提示词缺少 {{max_chars}} 占位符：{path}")
    return content.strip().replace("{max_chars}", str(max_chars))
