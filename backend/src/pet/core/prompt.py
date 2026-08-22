"""Load product-owned system prompts and optional game vocabularies."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

PROMPTS_DIRECTORY = Path(__file__).resolve().parents[3] / "prompts"
PromptPersonality = Literal["brother", "caster", "inference"]


def load_system_prompt(
    vocabulary_id: str | None,
    *,
    max_chars: int,
    prompts_directory: Path = PROMPTS_DIRECTORY,
    include_reading_guide: bool = True,
) -> str:
    """Load the shared personality and inject one adapter-owned vocabulary."""
    del include_reading_guide
    shared_personality = prompts_directory / "personality.md"
    personality_path = shared_personality
    selected_vocabulary_path = (
        prompts_directory / vocabulary_id / "vocabulary.md"
        if vocabulary_id is not None
        else None
    )

    # Retain the old helper surface for offline tools and focused loader tests.
    legacy_personality = (
        prompts_directory / f"{vocabulary_id}.md"
        if vocabulary_id is not None
        else None
    )
    if legacy_personality is not None:
        if legacy_personality.exists():
            personality_path = legacy_personality
            selected_vocabulary_path = prompts_directory / "vocabulary.md"
        else:
            matches = tuple(prompts_directory.glob(f"*/{vocabulary_id}.md"))
            if len(matches) == 1:
                personality_path = matches[0]
                selected_vocabulary_path = matches[0].parent / "vocabulary.md"
            elif (
                vocabulary_id == "inference"
                and shared_personality.exists()
            ):
                vocabularies = tuple(prompts_directory.glob("*/vocabulary.md"))
                personality_path = shared_personality
                selected_vocabulary_path = (
                    vocabularies[0] if len(vocabularies) == 1 else None
                )
            elif not shared_personality.exists():
                personality_path = legacy_personality

    prompt = _read_prompt_file(personality_path)
    vocabulary = (
        _read_prompt_file(selected_vocabulary_path)
        if selected_vocabulary_path is not None and selected_vocabulary_path.exists()
        else ""
    )
    return (
        prompt.replace("{{VOCABULARY}}", vocabulary)
        .replace("{max_chars}", str(max_chars))
    )


def vocabulary_path(
    vocabulary_id: str | None,
    *,
    prompts_directory: Path = PROMPTS_DIRECTORY,
) -> Path | None:
    """Resolve a vocabulary identifier without interpreting game semantics."""
    if vocabulary_id is None:
        return None
    return prompts_directory / vocabulary_id / "vocabulary.md"


def _read_prompt_file(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise FileNotFoundError(f"系统提示词文件不存在：{path}") from error
    if not content.strip():
        raise ValueError(f"系统提示词文件为空：{path}")
    return content.strip()
