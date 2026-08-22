"""Turn selected CS2 events into Chinese template commentary."""

from __future__ import annotations

from collections.abc import Mapping
import random
from typing import Any

from pydantic import BaseModel, Field

from pet.games.cs2.template_lines import (
    COMMENTARY_TEMPLATES,
    EQUIP_DETAIL_FORMAT,
    KILL_DETAIL_FORMAT,
    METHOD_CATEGORY_BY_LABEL,
    SCORE_DETAIL_FORMAT,
    SURVIVAL_DETAIL_FORMAT,
    CommentaryCategory,
    CommentaryTemplate,
    Emotion,
)
from pet.games.cs2.template_rules import WIN_METHOD_LABELS
from pet.core.config import PersonalityStyle
from pet.games.cs2.events import EventType, GameEvent


class TemplateUtterance(BaseModel):
    """Adapter-local template output used to build a core SpeechRequest."""

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    emotion: Emotion

_MULTI_CATEGORIES: dict[int | None, CommentaryCategory] = {
    2: "multi_2",
    3: "multi_3",
    4: "multi_4",
    5: "multi_5",
    None: "multi_general",
}
_ROUND_CATEGORIES: dict[tuple[EventType, str | None], CommentaryCategory] = {
    ("round_win", "elimination"): "round_win_elimination",
    ("round_win", "bomb"): "round_win_bomb",
    ("round_win", "defuse"): "round_win_defuse",
    ("round_win", "time"): "round_win_time",
    ("round_win", None): "round_win_general",
    ("round_loss", "elimination"): "round_loss_elimination",
    ("round_loss", "bomb"): "round_loss_bomb",
    ("round_loss", "defuse"): "round_loss_defuse",
    ("round_loss", "time"): "round_loss_time",
    ("round_loss", None): "round_loss_general",
}
_DIRECT_CATEGORIES: dict[EventType, CommentaryCategory] = {
    "kill": "kill",
    "kill_headshot": "kill_headshot",
    "death": "death",
    "death_after_kill": "death_after_kill",
    "death_thrown_away": "death_thrown_away",
}


def templates_for_map(
    templates: tuple[CommentaryTemplate, ...], map_name: str | None
) -> tuple[CommentaryTemplate, ...]:
    """Return generic lines plus lines explicitly scoped to the active map."""
    normalized_current_map = _normalize_map_name(map_name)
    return tuple(
        template
        for template in templates
        if template.applicable_maps is None
        or (
            normalized_current_map is not None
            and any(
                _normalize_map_name(scoped_map) == normalized_current_map
                for scoped_map in template.applicable_maps
            )
        )
    )


def _normalize_map_name(map_name: str | None) -> str | None:
    if not isinstance(map_name, str):
        return None
    normalized = map_name.strip().casefold()
    if normalized.startswith("de_"):
        normalized = normalized.removeprefix("de_")
    return normalized or None


class CommentaryGenerator:
    """Choose and safely fill templates without repeating one category consecutively."""

    def __init__(
        self,
        rng: random.Random | None = None,
        *,
        personality_style: PersonalityStyle = "brother",
    ) -> None:
        self._rng = rng or random.Random()
        self._templates = COMMENTARY_TEMPLATES[personality_style]
        self._last_templates: dict[CommentaryCategory, CommentaryTemplate] = {}

    def generate(
        self, event: GameEvent, *, map_name: str | None = None
    ) -> TemplateUtterance:
        """Generate one valid utterance without exposing missing or raw method values."""
        category = commentary_category(event)
        templates = templates_for_map(self._templates[category], map_name)
        if not templates:
            raise ValueError(
                f"no commentary template for category {category!r} on map {map_name!r}"
            )
        template_index = self._choose_template_index(category, templates)
        template = templates[template_index]
        text = template.text.format_map(_template_context(event))
        return TemplateUtterance(
            id=f"game-{event.id}",
            text=text,
            emotion=template.emotion,
        )

    def _choose_template_index(
        self,
        category: CommentaryCategory,
        templates: tuple[CommentaryTemplate, ...],
    ) -> int:
        previous = self._last_templates.get(category)
        if previous is None or len(templates) == 1:
            selected = self._rng.randrange(len(templates))
        else:
            alternatives = tuple(
                index for index, template in enumerate(templates) if template != previous
            )
            selected = self._rng.choice(alternatives)
        self._last_templates[category] = templates[selected]
        return selected


def commentary_category(event: GameEvent) -> CommentaryCategory:
    """Map structured event facts to one independently editable template group."""
    if event.type == "multi_kill":
        count = _integer_fact(event.facts, "count")
        return _MULTI_CATEGORIES.get(count, "multi_general")
    if event.type in {"round_win", "round_loss"}:
        method = _method_suffix(event.facts.get("method"))
        return _ROUND_CATEGORIES[(event.type, method)]
    return _DIRECT_CATEGORIES[event.type]


def _template_context(event: GameEvent) -> dict[str, str]:
    kill_index = _integer_fact(event.facts, "round_kill_index")
    survival_seconds = _numeric_fact(event.facts, "survival_seconds")
    equip_value = _integer_fact(event.facts, "equip_value")
    score_ct = _integer_fact(event.facts, "score_ct")
    score_t = _integer_fact(event.facts, "score_t")
    return {
        "kill_detail": KILL_DETAIL_FORMAT.format(kill_index=kill_index)
        if kill_index is not None
        else "",
        "survival_detail": (
            SURVIVAL_DETAIL_FORMAT.format(
                seconds=_display_seconds(survival_seconds)
            )
            if survival_seconds is not None
            else ""
        ),
        "equip_detail": (
            EQUIP_DETAIL_FORMAT.format(equip_value=equip_value)
            if equip_value is not None
            else ""
        ),
        "score_detail": (
            SCORE_DETAIL_FORMAT.format(score_ct=score_ct, score_t=score_t)
            if score_ct is not None and score_t is not None
            else ""
        ),
    }


def _integer_fact(facts: Mapping[str, Any], key: str) -> int | None:
    value = facts.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _numeric_fact(facts: Mapping[str, Any], key: str) -> float | None:
    value = facts.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _display_seconds(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _method_suffix(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    label = WIN_METHOD_LABELS.get(value)
    if label is None:
        suffix = value.partition("_win_")[2] or value
        label = WIN_METHOD_LABELS.get(suffix)
    return METHOD_CATEGORY_BY_LABEL.get(label) if label is not None else None
