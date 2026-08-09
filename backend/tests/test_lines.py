"""Core Utterance contract validation tests."""

import pytest
from pydantic import ValidationError

from pet.lines import IDLE_UTTERANCES, Utterance


@pytest.mark.parametrize(
    ("utterance_id", "text"),
    [
        ("", "有效文本"),
        ("valid-id", ""),
    ],
)
def test_utterance_rejects_empty_id_or_text(utterance_id: str, text: str) -> None:
    """Invalid dynamic utterances fail at construction instead of reaching clients."""
    with pytest.raises(ValidationError):
        Utterance(id=utterance_id, text=text, emotion="neutral")


def test_idle_lines_fit_non_game_context_and_keep_bubble_length_coverage() -> None:
    """The live table retains short, wrapping, and truncation regression cases."""
    lengths = [len(utterance.text) for utterance in IDLE_UTTERANCES]

    assert len(IDLE_UTTERANCES) >= 10
    assert sum(length <= 8 for length in lengths) >= 2
    assert sum(15 <= length <= 30 for length in lengths) >= 2
    assert sum(length > 30 for length in lengths) >= 1
    assert all("这一局" not in utterance.text for utterance in IDLE_UTTERANCES)
    assert all("刚才的操作" not in utterance.text for utterance in IDLE_UTTERANCES)
