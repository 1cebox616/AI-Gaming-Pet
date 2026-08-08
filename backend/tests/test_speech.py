"""Real Windows system-speech tests without downloaded models or audio playback."""

import sys

import pytest

from pet.speech import SpeechService


def create_loaded_speech_service() -> SpeechService:
    """Load the actual installed OneCore Chinese voice for a synthesis test."""
    if sys.platform != "win32":
        pytest.skip("requires Windows and an installed OneCore Chinese text-to-speech voice")

    service = SpeechService()
    if not service.load():
        service.shutdown()
        pytest.fail(
            "could not load a Windows OneCore Chinese text-to-speech voice; "
            "install a Chinese voice in Windows Settings before running this test"
        )
    return service


def test_chinese_text_produces_nonempty_audio_with_duration() -> None:
    """The actual installed system voice produces an audible-length WAV clip."""
    speech_service = create_loaded_speech_service()
    audio = speech_service.synthesize("恭喜你今天完成了一个很棒的练习。")

    assert audio.wav_bytes
    assert audio.duration_seconds > 0
    speech_service.shutdown()


def test_mixed_text_synthesizes_without_an_audio_device() -> None:
    """Numbers, English, punctuation, and emoji are accepted by the synthesizer."""
    speech_service = create_loaded_speech_service()
    audio = speech_service.synthesize("这局我 3 杀，CS2 真好玩！🙂")

    assert audio.wav_bytes
    assert audio.duration_seconds > 0
    speech_service.shutdown()


def test_speak_without_an_available_voice_returns_without_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An uninitialized service logs and remains silent instead of crashing."""
    speech_service = SpeechService()

    assert speech_service.speak("没有中文语音时也不能让服务崩溃。") is None
    assert "no OneCore Chinese voice is available" in caplog.text
    speech_service.shutdown()
