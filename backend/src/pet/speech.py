"""Windows OneCore speech synthesis and interruptible playback for the pet."""

from __future__ import annotations

import base64
import binascii
import ctypes
import io
import json
import logging
import subprocess
import sys
import time
import wave
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock

from pet.config import SpeechConfig

POWERSHELL_EXECUTABLE = "powershell.exe"
POWERSHELL_TIMEOUT_SECONDS = 15
PREFERRED_CHINESE_VOICE_NAMES: tuple[str, ...] = (
    "Microsoft Yaoyao",
    "Microsoft Huihui",
    "Microsoft Kangkang",
)
WINDOWS_CHINESE_VOICE_INSTALL_INSTRUCTIONS = (
    "install a Chinese text-to-speech voice in Settings > Time & language > "
    "Language & region > Chinese (Simplified, China) > Language options > "
    "Text-to-speech > Download"
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OneCoreVoice:
    """One Windows Runtime voice discovered from the local operating system."""

    name: str
    language: str


@dataclass(frozen=True)
class SynthesizedAudio:
    """A complete WAV clip synthesized without opening an audio output device."""

    wav_bytes: bytes
    duration_seconds: float


@dataclass(frozen=True)
class SpeechMetrics:
    """Timing collected for one asynchronous system-speech request."""

    synthesis_seconds: float
    playback_start_latency_seconds: float


class SpeechService:
    """Use the installed OneCore Chinese voice without blocking FastAPI."""

    def __init__(self, configuration: SpeechConfig | None = None) -> None:
        self._configuration = configuration or SpeechConfig()
        self._voice: OneCoreVoice | None = None
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pet-speech")
        self._lock = Lock()
        self._generation = 0
        self._current_future: Future[None] | None = None
        self._last_metrics: SpeechMetrics | None = None
        self._is_shutdown = False

    def load(self) -> bool:
        """Find and select an installed OneCore Chinese voice."""
        if not self._configuration.enabled:
            logger.info("speech is disabled by backend configuration")
            return True

        try:
            voices = _enumerate_onecore_voices()
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            logger.error("could not enumerate Windows OneCore voices: %s", error)
            return False

        chinese_voices = [voice for voice in voices if voice.language.lower().startswith("zh-")]
        if not chinese_voices:
            logger.warning(
                "no OneCore Chinese voice is installed; %s",
                WINDOWS_CHINESE_VOICE_INSTALL_INSTRUCTIONS,
            )
            return False

        selected_voice = _select_voice(
            chinese_voices,
            self._configuration.voice_name,
        )
        with self._lock:
            if self._is_shutdown:
                logger.warning("speech service was shut down before its voice finished loading")
                return False
            self._voice = selected_voice

        if not _has_wave_output_device():
            logger.warning(
                "no Windows wave output device was detected; speech will remain silent"
            )
        logger.info(
            "OneCore Chinese speech voice loaded: %s (%s)",
            selected_voice.name,
            selected_voice.language,
        )
        return True

    def speak(self, text: str) -> Future[None] | None:
        """Interrupt previous playback and asynchronously synthesize and play ``text``."""
        if not text.strip():
            logger.warning("ignoring an empty speech request")
            return None
        if not self._configuration.enabled:
            return None

        self.stop()
        request_started_at = time.perf_counter()
        with self._lock:
            if self._voice is None:
                logger.warning("speech request ignored because no OneCore Chinese voice is available")
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
        """Immediately stop active Windows WAV playback and invalidate pending clips."""
        with self._lock:
            self._generation += 1
            current_future = self._current_future
            self._current_future = None

        if current_future is not None:
            current_future.cancel()
        _stop_windows_playback()

    def synthesize(self, text: str) -> SynthesizedAudio:
        """Produce an in-memory WAV clip without using an audio output device."""
        if not text.strip():
            raise ValueError("speech text must not be empty")

        with self._lock:
            voice = self._voice

        if voice is None:
            raise RuntimeError("no OneCore Chinese voice is available")

        wav_bytes = _synthesize_onecore_wav(voice.name, text)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()

        if frame_rate <= 0 or frame_count <= 0:
            raise RuntimeError("OneCore synthesis returned an empty WAV clip")

        return SynthesizedAudio(
            wav_bytes=wav_bytes,
            duration_seconds=frame_count / frame_rate,
        )

    def shutdown(self) -> None:
        """Stop playback and release the background workers during backend shutdown."""
        self.stop()
        with self._lock:
            self._is_shutdown = True
            self._voice = None
        self._executor.shutdown(wait=False, cancel_futures=True)

    def last_metrics(self) -> SpeechMetrics | None:
        """Return timing captured for the latest request that reached playback."""
        with self._lock:
            return self._last_metrics

    def _synthesize_and_play(
        self,
        text: str,
        generation: int,
        request_started_at: float,
    ) -> None:
        if not self._is_current_generation(generation):
            return

        synthesis_started_at = time.perf_counter()
        try:
            audio = self.synthesize(text)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
            logger.error("system speech synthesis failed; skipping this utterance: %s", error)
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
            "system speech playback started %.3f seconds after utterance dispatch",
            playback_started_at - request_started_at,
        )
        _play_windows_wav(audio.wav_bytes)

    def _is_current_generation(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and not self._is_shutdown


def _select_voice(
    chinese_voices: list[OneCoreVoice],
    configured_voice_name: str,
) -> OneCoreVoice:
    """Use the configured Chinese voice when present, otherwise choose the best default."""
    requested_name = configured_voice_name.strip()
    if requested_name:
        configured_voice = next(
            (voice for voice in chinese_voices if voice.name == requested_name),
            None,
        )
        if configured_voice is not None:
            return configured_voice
        logger.warning(
            "configured Chinese speech voice %r is not installed; selecting automatically",
            requested_name,
        )

    return next(
        (
            voice
            for preferred_name in PREFERRED_CHINESE_VOICE_NAMES
            for voice in chinese_voices
            if voice.name == preferred_name
        ),
        chinese_voices[0],
    )


def _enumerate_onecore_voices() -> tuple[OneCoreVoice, ...]:
    """Return installed Windows Runtime voices without depending on a Python COM package."""
    output = _run_powershell(_ONECORE_ENUMERATION_SCRIPT)
    try:
        payload = json.loads(output.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError("OneCore voice enumeration returned invalid JSON") from error

    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise RuntimeError("OneCore voice enumeration did not return a list")

    voices: list[OneCoreVoice] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        language = item.get("language")
        if isinstance(name, str) and isinstance(language, str):
            voices.append(OneCoreVoice(name=name, language=language))
    return tuple(voices)


def _synthesize_onecore_wav(voice_name: str, text: str) -> bytes:
    """Use OneCore to synthesize text into WAV bytes, never opening an output device."""
    command = _ONECORE_SYNTHESIS_SCRIPT.replace(
        "__VOICE_NAME_BASE64__",
        _base64_utf8(voice_name),
    ).replace("__TEXT_BASE64__", _base64_utf8(text))
    wav_bytes = _run_powershell(command)
    try:
        return base64.b64decode(wav_bytes, validate=True)
    except (binascii.Error, ValueError) as error:
        raise RuntimeError("OneCore synthesis did not return valid WAV data") from error


def _run_powershell(script: str) -> bytes:
    """Run an encoded PowerShell command without exposing user text to its parser."""
    encoded_command = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        [
            POWERSHELL_EXECUTABLE,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded_command,
        ],
        check=False,
        capture_output=True,
        timeout=POWERSHELL_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Windows speech command failed with exit code {result.returncode}")
    return result.stdout.strip()


def _base64_utf8(value: str) -> str:
    """Encode one value safely for insertion into the encoded PowerShell command."""
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


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
    """Play an in-memory WAV synchronously from a dedicated worker thread."""
    if sys.platform != "win32":
        logger.warning("system speech playback is only implemented for Windows")
        return

    try:
        import winsound

        winsound.PlaySound(wav_bytes, winsound.SND_MEMORY)
    except (OSError, RuntimeError) as error:
        logger.warning("system speech playback is unavailable; continuing silently: %s", error)


def _stop_windows_playback() -> None:
    """Request immediate cancellation of active Windows WAV playback."""
    if sys.platform != "win32":
        return

    try:
        import winsound

        winsound.PlaySound(None, 0)
    except (OSError, RuntimeError) as error:
        logger.warning("could not stop system speech playback: %s", error)


_ONECORE_ENUMERATION_SCRIPT = r'''
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$speechType = [Windows.Media.SpeechSynthesis.SpeechSynthesizer,Windows.Media.SpeechSynthesis,ContentType=WindowsRuntime]
$voices = @($speechType::AllVoices | ForEach-Object {
  [PSCustomObject]@{ name = $_.DisplayName; language = $_.Language }
})
[Console]::Out.Write(($voices | ConvertTo-Json -Compress))
'''

_ONECORE_SYNTHESIS_SCRIPT = r'''
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
function Decode-Value([string]$value) {
  return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($value))
}
function Await-WinRt([object]$operation, [type]$resultType) {
  $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1
  } | Select-Object -First 1
  $task = $method.MakeGenericMethod($resultType).Invoke($null, @($operation))
  return $task.GetAwaiter().GetResult()
}
$speechType = [Windows.Media.SpeechSynthesis.SpeechSynthesizer,Windows.Media.SpeechSynthesis,ContentType=WindowsRuntime]
$streamType = [Windows.Media.SpeechSynthesis.SpeechSynthesisStream,Windows.Media.SpeechSynthesis,ContentType=WindowsRuntime]
$synthesizer = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
$voice = $speechType::AllVoices | Where-Object {
  $_.DisplayName -eq (Decode-Value "__VOICE_NAME_BASE64__")
} | Select-Object -First 1
if ($null -eq $voice) {
  throw "Requested OneCore voice is not installed"
}
$synthesizer.Voice = $voice
$stream = Await-WinRt (
  $synthesizer.SynthesizeTextToStreamAsync((Decode-Value "__TEXT_BASE64__"))
) $streamType
$reader = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
[void](Await-WinRt ($reader.LoadAsync([uint32]$stream.Size)) ([uint32]))
$bytes = New-Object byte[] ([int]$stream.Size)
$reader.ReadBytes($bytes)
[Console]::Out.Write([Convert]::ToBase64String($bytes))
$reader.Dispose()
$stream.Dispose()
$synthesizer.Dispose()
'''
