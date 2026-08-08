"""Real local speech synthesis tests that do not need an audio output device."""

from collections.abc import Iterator

import pytest

from pet.speech import SpeechService


@pytest.fixture(scope="module")
def speech_service() -> Iterator[SpeechService]:
    """Load the actual local Chinese model once for the synthesis test module."""
    service = SpeechService()
    assert service.load(), "install the model with `python -m pet.speech --install-model`"
    yield service
    service.shutdown()


def test_chinese_text_produces_nonempty_audio_with_duration(
    speech_service: SpeechService,
) -> None:
    """The real Chinese model produces an audible-length WAV clip."""
    audio = speech_service.synthesize("恭喜你今天完成了一个很棒的练习。")

    assert audio.wav_bytes
    assert audio.duration_seconds > 0


def test_mixed_text_synthesizes_without_an_audio_device(
    speech_service: SpeechService,
) -> None:
    """Numbers, English, punctuation, and emoji are accepted by the synthesizer."""
    audio = speech_service.synthesize("这局我 3 杀，CS2 真好玩！🙂")

    assert audio.wav_bytes
    assert audio.duration_seconds > 0
