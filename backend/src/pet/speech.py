"""Local CPU speech synthesis and Windows audio playback for the pet."""

from __future__ import annotations

import argparse
import ctypes
import io
import logging
import sys
import time
import wave
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

KOKORO_REPOSITORY = "hexgrad/Kokoro-82M-v1.1-zh"
KOKORO_MODEL_FILE_NAME = "kokoro-v1_1-zh.pth"
KOKORO_VOICE_FILE_NAME = "zf_001.pt"
KOKORO_SAMPLE_RATE_HZ = 24_000
MODEL_DIRECTORY = Path(__file__).resolve().parents[2] / "models" / "kokoro-v1.1-zh"
MODEL_FILE_PATH = MODEL_DIRECTORY / KOKORO_MODEL_FILE_NAME
MODEL_CONFIG_PATH = MODEL_DIRECTORY / "config.json"
VOICE_FILE_PATH = MODEL_DIRECTORY / "voices" / KOKORO_VOICE_FILE_NAME
MODEL_DOWNLOAD_FILES: tuple[str, ...] = (
    "config.json",
    KOKORO_MODEL_FILE_NAME,
    f"voices/{KOKORO_VOICE_FILE_NAME}",
)


class SpeechUnavailableError(RuntimeError):
    """Raised when synthesis is requested before a usable model is loaded."""


@dataclass(frozen=True)
class SynthesizedAudio:
    """A complete PCM WAV clip produced by the local speech model."""

    wav_bytes: bytes
    duration_seconds: float


@dataclass(frozen=True)
class SpeechMetrics:
    """Timing collected for one asynchronous speech request."""

    synthesis_seconds: float
    playback_start_latency_seconds: float


class SpeechService:
    """Preload, synthesize, and interrupt local speech without blocking FastAPI."""

    def __init__(self) -> None:
        self._pipeline: Any | None = None
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pet-speech")
        self._lock = Lock()
        self._generation = 0
        self._current_future: Future[None] | None = None
        self._last_metrics: SpeechMetrics | None = None
        self._is_shutdown = False

    def load(self) -> bool:
        """Load the model and selected voice before clients can request speech."""
        with self._lock:
            if self._pipeline is not None:
                return True

        missing_files = [
            path
            for path in (MODEL_CONFIG_PATH, MODEL_FILE_PATH, VOICE_FILE_PATH)
            if not path.is_file()
        ]
        if missing_files:
            logger.error(
                "speech model is unavailable (%s); run `python -m pet.speech --install-model` from backend",
                ", ".join(str(path) for path in missing_files),
            )
            return False

        try:
            from kokoro import KModel, KPipeline

            model = KModel(
                repo_id=KOKORO_REPOSITORY,
                config=str(MODEL_CONFIG_PATH),
                model=str(MODEL_FILE_PATH),
            ).to("cpu").eval()
            pipeline = KPipeline(
                lang_code="z",
                repo_id=KOKORO_REPOSITORY,
                model=model,
                device="cpu",
            )
            pipeline.load_voice(str(VOICE_FILE_PATH))
        except Exception as error:
            logger.exception("speech model failed to load; continuing without audio: %s", error)
            return False

        with self._lock:
            if self._is_shutdown:
                logger.warning("speech model loaded after shutdown request; ignoring it")
                return False
            self._pipeline = pipeline

        if not _has_wave_output_device():
            logger.warning(
                "no Windows wave output device was detected; speech will synthesize but not play audio"
            )
        logger.info("local Kokoro Chinese speech model loaded on CPU")
        return True

    def speak(self, text: str) -> Future[None] | None:
        """Interrupt the previous clip and asynchronously synthesize and play ``text``."""
        if not text.strip():
            logger.warning("ignoring an empty speech request")
            return None

        self.stop()
        request_started_at = time.perf_counter()

        with self._lock:
            if self._pipeline is None:
                logger.warning("speech request ignored because the local model is unavailable")
                return None
            if self._is_shutdown:
                logger.warning("speech request ignored because the service is shutting down")
                return None

            self._generation += 1
            generation = self._generation
            future = self._executor.submit(
                self._synthesize_and_play,
                text,
                generation,
                request_started_at,
            )
            self._current_future = future
            return future

    def stop(self) -> None:
        """Immediately stop active Windows playback and invalidate pending clips."""
        with self._lock:
            self._generation += 1
            current_future = self._current_future
            self._current_future = None

        if current_future is not None:
            current_future.cancel()
        _stop_windows_playback()

    def synthesize(self, text: str) -> SynthesizedAudio:
        """Synchronously synthesize a WAV clip without accessing an audio device."""
        if not text.strip():
            raise ValueError("speech text must not be empty")

        with self._lock:
            pipeline = self._pipeline

        if pipeline is None:
            raise SpeechUnavailableError("local speech model is unavailable")

        pcm_frames = bytearray()
        frame_count = 0
        for result in pipeline(text, voice=str(VOICE_FILE_PATH)):
            audio = result.audio
            if audio is None:
                continue

            samples = audio.detach().cpu().clamp(-1, 1).mul(32767).short()
            pcm_frames.extend(samples.numpy().tobytes())
            frame_count += samples.numel()

        if frame_count == 0:
            raise RuntimeError("speech synthesis returned no audio frames")

        wav_stream = io.BytesIO()
        with wave.open(wav_stream, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(KOKORO_SAMPLE_RATE_HZ)
            wav_file.writeframes(pcm_frames)

        return SynthesizedAudio(
            wav_bytes=wav_stream.getvalue(),
            duration_seconds=frame_count / KOKORO_SAMPLE_RATE_HZ,
        )

    def last_metrics(self) -> SpeechMetrics | None:
        """Return timing captured for the latest clip that reached playback."""
        with self._lock:
            return self._last_metrics

    def shutdown(self) -> None:
        """Stop playback and release worker resources during backend shutdown."""
        self.stop()
        with self._lock:
            self._is_shutdown = True
            self._pipeline = None
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _synthesize_and_play(
        self,
        text: str,
        generation: int,
        request_started_at: float,
    ) -> None:
        synthesis_started_at = time.perf_counter()
        try:
            audio = self.synthesize(text)
        except (OSError, RuntimeError, ValueError) as error:
            logger.exception("speech synthesis failed; skipping this utterance: %s", error)
            return

        synthesis_finished_at = time.perf_counter()
        if not self._is_current_generation(generation):
            return

        playback_started_at = time.perf_counter()
        with self._lock:
            self._last_metrics = SpeechMetrics(
                synthesis_seconds=synthesis_finished_at - synthesis_started_at,
                playback_start_latency_seconds=playback_started_at - request_started_at,
            )

        logger.info(
            "speech playback started %.3f seconds after utterance dispatch",
            playback_started_at - request_started_at,
        )
        _play_windows_wav(audio.wav_bytes)

    def _is_current_generation(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and not self._is_shutdown


def install_speech_model() -> Sequence[Path]:
    """Download the exact licensed model files used by :class:`SpeechService`."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as error:
        raise RuntimeError(
            "speech dependencies are missing; install backend requirements before downloading the model"
        ) from error

    MODEL_DIRECTORY.mkdir(parents=True, exist_ok=True)
    downloaded_paths: list[Path] = []
    for file_name in MODEL_DOWNLOAD_FILES:
        try:
            downloaded_path = hf_hub_download(
                repo_id=KOKORO_REPOSITORY,
                filename=file_name,
                local_dir=MODEL_DIRECTORY,
            )
        except Exception as error:
            raise RuntimeError(
                "unable to download the Kokoro Chinese model; check your network connection and retry"
            ) from error
        downloaded_paths.append(Path(downloaded_path))

    return downloaded_paths


def _has_wave_output_device() -> bool:
    """Return whether the Windows wave API reports at least one output device."""
    if sys.platform != "win32":
        return False

    try:
        return ctypes.windll.winmm.waveOutGetNumDevs() > 0
    except (AttributeError, OSError) as error:
        logger.warning("could not query Windows wave output devices: %s", error)
        return False


def _play_windows_wav(wav_bytes: bytes) -> None:
    """Play one in-memory WAV synchronously from a dedicated worker thread."""
    if sys.platform != "win32":
        logger.warning("speech playback is only implemented for Windows")
        return

    try:
        import winsound

        winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)
    except (OSError, RuntimeError) as error:
        logger.warning("speech playback is unavailable; continuing without audio: %s", error)


def _stop_windows_playback() -> None:
    """Request immediate cancellation of the active Windows wave playback."""
    if sys.platform != "win32":
        return

    try:
        import winsound

        winsound.PlaySound(None, 0)
    except (OSError, RuntimeError) as error:
        logger.warning("could not stop Windows speech playback: %s", error)


def _run_model_installer() -> int:
    """Provide the documented one-time model installation command."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("huggingface_hub").setLevel(logging.WARNING)
    try:
        downloaded_paths = install_speech_model()
    except RuntimeError as error:
        logger.error("speech model installation failed: %s", error)
        return 1

    logger.info(
        "speech model installation completed: %s",
        ", ".join(str(path) for path in downloaded_paths),
    )
    return 0


def main() -> int:
    """Run the explicit one-time speech-model installer command."""
    parser = argparse.ArgumentParser(description="Install the local Kokoro Chinese speech model")
    parser.add_argument(
        "--install-model",
        action="store_true",
        help="download the model and selected voice into backend/models",
    )
    arguments = parser.parse_args()
    if not arguments.install_model:
        parser.error("pass --install-model to download the local speech model")
    return _run_model_installer()


if __name__ == "__main__":
    raise SystemExit(main())
