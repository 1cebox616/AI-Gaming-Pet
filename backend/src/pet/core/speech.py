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
from typing import Any, Callable

from pet.core.config import SpeechConfig

POWERSHELL_EXECUTABLE = "powershell.exe"
POWERSHELL_TIMEOUT_SECONDS = 15
WAVE_FORMAT_PCM = 1
WAVE_MAPPER = 0xFFFFFFFF
MMSYSERR_NOERROR = 0
WAVERR_STILLPLAYING = 33
WHDR_DONE = 0x00000001
WAVE_OUT_POLL_INTERVAL_SECONDS = 0.005
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
    playback_started_at: float


@dataclass(frozen=True)
class _PcmWave:
    """Decoded PCM parameters and frames kept alive for one WinMM buffer."""

    channels: int
    sample_width_bytes: int
    frame_rate: int
    frames: bytes


class _WaveFormatEx(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort),
        ("nChannels", ctypes.c_ushort),
        ("nSamplesPerSec", ctypes.c_uint32),
        ("nAvgBytesPerSec", ctypes.c_uint32),
        ("nBlockAlign", ctypes.c_ushort),
        ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class _WaveHeader(ctypes.Structure):
    _fields_ = [
        ("lpData", ctypes.c_void_p),
        ("dwBufferLength", ctypes.c_uint32),
        ("dwBytesRecorded", ctypes.c_uint32),
        ("dwUser", ctypes.c_size_t),
        ("dwFlags", ctypes.c_uint32),
        ("dwLoops", ctypes.c_uint32),
        ("lpNext", ctypes.c_void_p),
        ("reserved", ctypes.c_size_t),
    ]


class _WindowsWavePlayer:
    """Play one in-memory PCM buffer with cross-thread WinMM cancellation."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._active_playback: tuple[int, object] | None = None

    def play(
        self,
        wav_bytes: bytes,
        can_start: Callable[[], bool],
        on_started: Callable[[float], None],
    ) -> None:
        """Queue one WAV buffer and block only until it finishes or is reset."""
        if sys.platform != "win32":
            logger.warning("system speech playback is only implemented for Windows")
            return

        pcm_wave = _decode_pcm_wav(wav_bytes)
        winmm = _configured_winmm()
        wave_format = _WaveFormatEx(
            wFormatTag=WAVE_FORMAT_PCM,
            nChannels=pcm_wave.channels,
            nSamplesPerSec=pcm_wave.frame_rate,
            nAvgBytesPerSec=(
                pcm_wave.frame_rate * pcm_wave.channels * pcm_wave.sample_width_bytes
            ),
            nBlockAlign=pcm_wave.channels * pcm_wave.sample_width_bytes,
            wBitsPerSample=pcm_wave.sample_width_bytes * 8,
            cbSize=0,
        )
        audio_buffer = ctypes.create_string_buffer(pcm_wave.frames, len(pcm_wave.frames))
        header = _WaveHeader(
            lpData=ctypes.cast(audio_buffer, ctypes.c_void_p),
            dwBufferLength=len(pcm_wave.frames),
            dwBytesRecorded=0,
            dwUser=0,
            dwFlags=0,
            dwLoops=0,
            lpNext=None,
            reserved=0,
        )
        device_handle = ctypes.c_void_p()
        playback_token = object()
        prepared = False

        try:
            with self._lock:
                if not can_start():
                    return

                _check_wave_out_result(
                    winmm.waveOutOpen(
                        ctypes.byref(device_handle),
                        WAVE_MAPPER,
                        ctypes.byref(wave_format),
                        0,
                        0,
                        0,
                    ),
                    "waveOutOpen",
                )
                if device_handle.value is None:
                    raise RuntimeError("waveOutOpen returned an empty device handle")
                self._active_playback = (device_handle.value, playback_token)

                _check_wave_out_result(
                    winmm.waveOutPrepareHeader(
                        device_handle,
                        ctypes.byref(header),
                        ctypes.sizeof(header),
                    ),
                    "waveOutPrepareHeader",
                )
                prepared = True
                _check_wave_out_result(
                    winmm.waveOutWrite(
                        device_handle,
                        ctypes.byref(header),
                        ctypes.sizeof(header),
                    ),
                    "waveOutWrite",
                )
                on_started(time.perf_counter())

            while not header.dwFlags & WHDR_DONE:
                time.sleep(WAVE_OUT_POLL_INTERVAL_SECONDS)
        finally:
            if device_handle.value is not None:
                self._release_playback(
                    winmm,
                    device_handle,
                    header,
                    prepared,
                    playback_token,
                    audio_buffer,
                )

    def stop(self) -> None:
        """Return the active WinMM buffer immediately from any calling thread."""
        if sys.platform != "win32":
            return

        with self._lock:
            if self._active_playback is None:
                return
            device_handle_value, _ = self._active_playback
            result = _configured_winmm().waveOutReset(ctypes.c_void_p(device_handle_value))
            if result != MMSYSERR_NOERROR:
                logger.warning("waveOutReset could not stop playback: %s", _wave_out_error(result))

    def _release_playback(
        self,
        winmm: Any,
        device_handle: ctypes.c_void_p,
        header: _WaveHeader,
        prepared: bool,
        playback_token: object,
        audio_buffer: ctypes.Array[ctypes.c_char],
    ) -> None:
        """Unprepare and close only after the driver has returned the live buffer."""
        with self._lock:
            if prepared and not header.dwFlags & WHDR_DONE:
                reset_result = winmm.waveOutReset(device_handle)
                if reset_result != MMSYSERR_NOERROR:
                    logger.warning(
                        "waveOutReset during playback cleanup failed: %s",
                        _wave_out_error(reset_result),
                    )

            if prepared:
                unprepare_result = winmm.waveOutUnprepareHeader(
                    device_handle,
                    ctypes.byref(header),
                    ctypes.sizeof(header),
                )
                if unprepare_result == WAVERR_STILLPLAYING:
                    winmm.waveOutReset(device_handle)
                    unprepare_result = winmm.waveOutUnprepareHeader(
                        device_handle,
                        ctypes.byref(header),
                        ctypes.sizeof(header),
                    )
                if unprepare_result != MMSYSERR_NOERROR:
                    logger.warning(
                        "waveOutUnprepareHeader failed: %s",
                        _wave_out_error(unprepare_result),
                    )

            close_result = winmm.waveOutClose(device_handle)
            if close_result != MMSYSERR_NOERROR:
                logger.warning("waveOutClose failed: %s", _wave_out_error(close_result))

            active_playback = self._active_playback
            if active_playback is not None and active_playback[1] is playback_token:
                self._active_playback = None

        # Keep an explicit reference through unprepare and close; the driver must not
        # observe a freed PCM buffer while it still owns the WAVEHDR.
        del audio_buffer


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
        self._enabled = self._configuration.enabled
        self._player = _WindowsWavePlayer()

    def load(self) -> bool:
        """Find and select an installed OneCore Chinese voice."""
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
        if not self._enabled:
            logger.info("speech is disabled by backend configuration")
        return True

    def speak(self, text: str) -> Future[None] | None:
        """Interrupt previous playback and asynchronously synthesize and play ``text``."""
        if not text.strip():
            logger.warning("ignoring an empty speech request")
            return None
        self.stop()
        request_started_at = time.perf_counter()
        with self._lock:
            if not self._enabled:
                return None
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

    def set_enabled(self, enabled: bool) -> None:
        """Apply the runtime speech switch, stopping playback when disabled."""
        with self._lock:
            changed = self._enabled != enabled
            self._enabled = enabled

        if changed and not enabled:
            self.stop()
        if changed:
            logger.info("runtime speech is now %s", "enabled" if enabled else "disabled")

    def is_enabled(self) -> bool:
        """Return the authoritative runtime speech state."""
        with self._lock:
            return self._enabled

    def stop(self) -> None:
        """Immediately stop active Windows WAV playback and invalidate pending clips."""
        with self._lock:
            self._generation += 1
            current_future = self._current_future
            self._current_future = None

        if current_future is not None:
            current_future.cancel()
        self._player.stop()

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
        self._executor.shutdown(wait=True, cancel_futures=True)

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

        try:
            self._player.play(
                audio.wav_bytes,
                lambda: self._is_current_generation(generation),
                lambda playback_started_at: self._record_playback_started(
                    synthesis_finished_at - synthesis_started_at,
                    request_started_at,
                    playback_started_at,
                ),
            )
        except (OSError, RuntimeError, ValueError, wave.Error) as error:
            logger.warning("system speech playback is unavailable; continuing silently: %s", error)

    def _is_current_generation(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and not self._is_shutdown

    def _record_playback_started(
        self,
        synthesis_seconds: float,
        request_started_at: float,
        playback_started_at: float,
    ) -> None:
        """Store metrics at the instant WinMM accepts the playback buffer."""
        with self._lock:
            self._last_metrics = SpeechMetrics(
                synthesis_seconds=synthesis_seconds,
                playback_start_latency_seconds=playback_started_at - request_started_at,
                playback_started_at=playback_started_at,
            )

        logger.info(
            "system speech playback started %.3f seconds after utterance dispatch",
            playback_started_at - request_started_at,
        )


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


def _decode_pcm_wav(wav_bytes: bytes) -> _PcmWave:
    """Extract the PCM frames and format required by the WinMM waveOut API."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
            if wav_file.getcomptype() != "NONE":
                raise ValueError("Windows waveOut playback requires uncompressed PCM audio")
            channels = wav_file.getnchannels()
            sample_width_bytes = wav_file.getsampwidth()
            frame_rate = wav_file.getframerate()
            frames = wav_file.readframes(wav_file.getnframes())
    except (EOFError, wave.Error) as error:
        raise ValueError("speech synthesis returned an invalid WAV container") from error

    if channels <= 0 or sample_width_bytes <= 0 or frame_rate <= 0 or not frames:
        raise ValueError("speech synthesis returned empty or invalid PCM audio")
    return _PcmWave(
        channels=channels,
        sample_width_bytes=sample_width_bytes,
        frame_rate=frame_rate,
        frames=frames,
    )


def _configured_winmm() -> Any:
    """Return WinMM with pointer-sized ctypes signatures declared explicitly."""
    winmm = ctypes.windll.winmm
    winmm.waveOutOpen.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint,
        ctypes.POINTER(_WaveFormatEx),
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.c_uint32,
    ]
    winmm.waveOutOpen.restype = ctypes.c_uint
    winmm.waveOutPrepareHeader.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_WaveHeader),
        ctypes.c_uint,
    ]
    winmm.waveOutPrepareHeader.restype = ctypes.c_uint
    winmm.waveOutWrite.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_WaveHeader),
        ctypes.c_uint,
    ]
    winmm.waveOutWrite.restype = ctypes.c_uint
    winmm.waveOutReset.argtypes = [ctypes.c_void_p]
    winmm.waveOutReset.restype = ctypes.c_uint
    winmm.waveOutUnprepareHeader.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_WaveHeader),
        ctypes.c_uint,
    ]
    winmm.waveOutUnprepareHeader.restype = ctypes.c_uint
    winmm.waveOutClose.argtypes = [ctypes.c_void_p]
    winmm.waveOutClose.restype = ctypes.c_uint
    winmm.waveOutGetErrorTextW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
    winmm.waveOutGetErrorTextW.restype = ctypes.c_uint
    return winmm


def _check_wave_out_result(result: int, operation: str) -> None:
    """Raise a useful error when one WinMM operation fails."""
    if result != MMSYSERR_NOERROR:
        raise OSError(f"{operation} failed: {_wave_out_error(result)}")


def _wave_out_error(result: int) -> str:
    """Translate one WinMM result code without introducing an audio dependency."""
    if sys.platform != "win32":
        return f"WinMM error {result}"

    message = ctypes.create_unicode_buffer(256)
    translated = _configured_winmm().waveOutGetErrorTextW(result, message, len(message))
    if translated == MMSYSERR_NOERROR and message.value:
        return f"{message.value} ({result})"
    return f"WinMM error {result}"


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
