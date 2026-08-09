"""Real Windows system-speech synthesis and interruptible playback tests."""

import sys
import time
from concurrent.futures import Future

import pytest

from pet.lines import IDLE_UTTERANCES
from pet.speech import SpeechMetrics, SpeechService, _has_wave_output_device

INTERRUPTION_LIMIT_SECONDS = 1.3
STOP_AFTER_PLAYBACK_SECONDS = 1.0
PLAYBACK_START_TIMEOUT_SECONDS = 5.0


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


def create_playable_speech_service() -> SpeechService:
    """Load real speech and skip only when Windows has no audio output device."""
    if sys.platform != "win32":
        pytest.skip("requires Windows waveform audio playback")
    if not _has_wave_output_device():
        pytest.skip("Windows has no audio output device for the real interruption test")
    return create_loaded_speech_service()


def wait_for_playback_start(
    speech_service: SpeechService,
    *,
    after: float = 0.0,
) -> SpeechMetrics:
    """Observe the real WinMM buffer submission without mocking playback."""
    deadline = time.perf_counter() + PLAYBACK_START_TIMEOUT_SECONDS
    while time.perf_counter() < deadline:
        metrics = speech_service.last_metrics()
        if metrics is not None and metrics.playback_started_at > after:
            return metrics
        time.sleep(0.005)
    pytest.fail("real Windows speech playback did not start within five seconds")


def stop_one_second_after_playback_start(
    speech_service: SpeechService,
    future: Future[None],
    metrics: SpeechMetrics,
) -> float:
    """Stop at the measured playback start plus one second and return completion time."""
    stop_at = metrics.playback_started_at + STOP_AFTER_PLAYBACK_SECONDS
    time.sleep(max(0.0, stop_at - time.perf_counter()))
    speech_service.stop()
    future.result(timeout=INTERRUPTION_LIMIT_SECONDS)
    return time.perf_counter() - metrics.playback_started_at


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


def test_stop_returns_real_long_playback_within_interruption_limit() -> None:
    """Stopping after one second returns a real 3+ second WinMM playback promptly."""
    speech_service = create_playable_speech_service()
    long_text = IDLE_UTTERANCES[7].text

    try:
        assert speech_service.synthesize(long_text).duration_seconds >= 3.0
        future = speech_service.speak(long_text)
        assert future is not None
        metrics = wait_for_playback_start(speech_service)

        playback_returned_after = stop_one_second_after_playback_start(
            speech_service,
            future,
            metrics,
        )

        assert playback_returned_after <= INTERRUPTION_LIMIT_SECONDS
    finally:
        speech_service.shutdown()


def test_new_utterance_interrupts_real_previous_playback_within_limit() -> None:
    """Dispatching a replacement returns the old real WinMM buffer promptly."""
    speech_service = create_playable_speech_service()
    long_text = IDLE_UTTERANCES[7].text

    try:
        first_future = speech_service.speak(long_text)
        assert first_future is not None
        first_metrics = wait_for_playback_start(speech_service)
        replacement_at = first_metrics.playback_started_at + STOP_AFTER_PLAYBACK_SECONDS
        time.sleep(max(0.0, replacement_at - time.perf_counter()))

        second_future = speech_service.speak("新的声音现在开始。")
        assert second_future is not None
        first_future.result(timeout=INTERRUPTION_LIMIT_SECONDS)
        first_playback_returned_after = time.perf_counter() - first_metrics.playback_started_at
        second_metrics = wait_for_playback_start(
            speech_service,
            after=first_metrics.playback_started_at,
        )

        assert first_playback_returned_after <= INTERRUPTION_LIMIT_SECONDS
        assert second_metrics.playback_started_at >= replacement_at
        speech_service.stop()
        second_future.result(timeout=INTERRUPTION_LIMIT_SECONDS)
    finally:
        speech_service.shutdown()
