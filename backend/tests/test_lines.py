"""Core Utterance contract validation tests."""

import pytest
from pydantic import ValidationError

from pet.core.lines import Utterance


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
