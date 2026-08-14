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
    """Load one prompt and inject the product-owned vocabulary verbatim."""
    del include_reading_guide
    prompt = _read_prompt_file(prompts_directory / f"{personality_style}.md")
    vocabulary_path = prompts_directory / "vocabulary.md"
    vocabulary = (
        _read_prompt_file(vocabulary_path)
        if vocabulary_path.exists()
        else ""
    )
    return (
        prompt.replace("{{VOCABULARY}}", vocabulary)
        .replace("{max_chars}", str(max_chars))
    )


def _read_prompt_file(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise FileNotFoundError(f"系统提示词文件不存在：{path}") from error
    if not content.strip():
        raise ValueError(f"系统提示词文件为空：{path}")
    return content.strip()
