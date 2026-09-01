"""Selective second-pass WinRT OCR mechanics for the offline T7.10b probe.

This module deliberately has no adapter integration.  It contains the reusable,
deterministic pieces needed to measure a possible future attachment contract.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from queue import Empty, Queue
import subprocess
import threading
import time
from typing import Any, Literal, Protocol, Sequence
import unicodedata

from pet.core.ocr_probe import OcrFrameResult, OcrLine, POWERSHELL_EXECUTABLE


LineChange = Literal["unchanged", "changed", "added"]


class OcrEngine(Protocol):
    """Small lifecycle shared by the production engine and WinRT probe worker."""

    def start(self) -> None: ...

    def recognize(self, image: Any, /) -> OcrFrameResult: ...

    def close(self) -> None: ...


_PERSISTENT_WORKER_SCRIPT = r'''
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
function Await-WinRt([object]$operation, [type]$resultType) {
  $method = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and $_.IsGenericMethodDefinition -and $_.GetParameters().Count -eq 1
  } | Select-Object -First 1
  $task = $method.MakeGenericMethod($resultType).Invoke($null, @($operation))
  return $task.GetAwaiter().GetResult()
}
$ocrType = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$languageType = [Windows.Globalization.Language,Windows.Foundation,ContentType=WindowsRuntime]
$storageFileType = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$streamType = [Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$decoderType = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
$bitmapType = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Foundation,ContentType=WindowsRuntime]
$resultType = [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
$language = New-Object Windows.Globalization.Language("__LANGUAGE__")
$engine = $ocrType::TryCreateFromLanguage($language)
if ($null -eq $engine) { throw "OCR language unavailable: __LANGUAGE__" }
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::Out.WriteLine('{"type":"ready"}')
[Console]::Out.Flush()
while ($null -ne ($line = [Console]::In.ReadLine())) {
  if ([string]::IsNullOrWhiteSpace($line)) { continue }
  $request = $line | ConvertFrom-Json
  if ($request.command -eq "stop") { break }
  $watch = [Diagnostics.Stopwatch]::StartNew()
  $stream = $null
  $bitmap = $null
  try {
    $file = Await-WinRt ($storageFileType::GetFileFromPathAsync([string]$request.path)) $storageFileType
    $stream = Await-WinRt ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) $streamType
    $decoder = Await-WinRt ($decoderType::CreateAsync($stream)) $decoderType
    $bitmap = Await-WinRt ($decoder.GetSoftwareBitmapAsync()) $bitmapType
    $recognizeWatch = [Diagnostics.Stopwatch]::StartNew()
    $result = Await-WinRt ($engine.RecognizeAsync($bitmap)) $resultType
    $recognizeWatch.Stop()
    $lines = @($result.Lines | ForEach-Object {
      $words = @($_.Words)
      if ($words.Count -gt 0) {
        $left = ($words | ForEach-Object { $_.BoundingRect.X } | Measure-Object -Minimum).Minimum
        $top = ($words | ForEach-Object { $_.BoundingRect.Y } | Measure-Object -Minimum).Minimum
        $right = ($words | ForEach-Object { $_.BoundingRect.X + $_.BoundingRect.Width } | Measure-Object -Maximum).Maximum
        $bottom = ($words | ForEach-Object { $_.BoundingRect.Y + $_.BoundingRect.Height } | Measure-Object -Maximum).Maximum
        [PSCustomObject]@{
          text = $_.Text
          x = [double]$left / [double]$bitmap.PixelWidth
          y = [double]$top / [double]$bitmap.PixelHeight
          width = [double]($right - $left) / [double]$bitmap.PixelWidth
          height = [double]($bottom - $top) / [double]$bitmap.PixelHeight
        }
      }
    })
    $watch.Stop()
    $response = [PSCustomObject]@{
      id = [int]$request.id
      path = [string]$request.path
      width = $bitmap.PixelWidth
      height = $bitmap.PixelHeight
      duration_ms = $watch.Elapsed.TotalMilliseconds
      recognize_ms = $recognizeWatch.Elapsed.TotalMilliseconds
      lines = $lines
      error = $null
    }
  } catch {
    $watch.Stop()
    $response = [PSCustomObject]@{
      id = [int]$request.id
      path = [string]$request.path
      width = 0
      height = 0
      duration_ms = $watch.Elapsed.TotalMilliseconds
      recognize_ms = 0.0
      lines = @()
      error = $_.Exception.Message
    }
  } finally {
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
  }
  [Console]::Out.WriteLine(($response | ConvertTo-Json -Depth 6 -Compress))
  [Console]::Out.Flush()
}
'''


class PersistentOcrError(RuntimeError):
    """A worker lifecycle or protocol error."""


@dataclass(frozen=True, slots=True)
class WorkerReply:
    result: OcrFrameResult | None
    wall_ms: float
    skipped_reason: str | None = None


class PersistentWinRtOcrWorker:
    """Legacy file-backed WinRT implementation of the OcrEngine lifecycle.

    Its probe-only ``recognize`` wrapper returns WorkerReply so the caller can
    retain restart diagnostics; ``WorkerReply.result`` is the protocol result.
    Production uses the numpy-backed RapidOcrEngine.
    """
    """One long-lived PowerShell/WinRT OCR engine with line-delimited JSON I/O."""

    def __init__(self, language: str = "zh-Hans-CN") -> None:
        self.language = language
        self.process: subprocess.Popen[str] | None = None
        self.startup_ms: float | None = None
        self.restart_count = 0
        self.budget_timeout_count = 0
        self.crash_restart_count = 0
        self._next_id = 1
        self._responses: Queue[str | None] = Queue()
        self._reader: threading.Thread | None = None
        self._restart_thread: threading.Thread | None = None
        self._restart_error: BaseException | None = None

    def start(self) -> float:
        if self.process is not None and self.process.poll() is None:
            return float(self.startup_ms or 0.0)
        script = _PERSISTENT_WORKER_SCRIPT.replace("__LANGUAGE__", self.language)
        import base64

        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        started = time.perf_counter()
        try:
            process = subprocess.Popen(
                [
                    POWERSHELL_EXECUTABLE,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encoded,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as error:
            raise PersistentOcrError(f"无法启动持久 OCR 工作进程：{error}") from error
        self.process = process
        responses: Queue[str | None] = Queue()
        self._responses = responses
        self._reader = threading.Thread(
            target=self._read_stdout, args=(process, responses), daemon=True
        )
        self._reader.start()
        try:
            ready = self._responses.get(timeout=10.0)
        except Empty as error:
            self._terminate()
            raise PersistentOcrError("持久 OCR 工作进程启动超时") from error
        if ready is None:
            detail = self._stderr_text()
            self._terminate()
            raise PersistentOcrError(f"持久 OCR 工作进程启动失败：{detail}")
        try:
            payload = json.loads(ready)
        except json.JSONDecodeError as error:
            self._terminate()
            raise PersistentOcrError(f"持久 OCR ready 响应无效：{ready}") from error
        if payload.get("type") != "ready":
            self._terminate()
            raise PersistentOcrError(f"持久 OCR ready 响应错误：{payload}")
        self.startup_ms = (time.perf_counter() - started) * 1000.0
        return self.startup_ms

    @staticmethod
    def _read_stdout(
        process: subprocess.Popen[str], responses: Queue[str | None]
    ) -> None:
        if process.stdout is None:
            responses.put(None)
            return
        try:
            for line in process.stdout:
                responses.put(line.rstrip("\r\n"))
        finally:
            responses.put(None)

    def _begin_restart(self) -> None:
        if self._restart_thread is not None and self._restart_thread.is_alive():
            return
        self._restart_error = None

        def restart() -> None:
            try:
                self.start()
            except BaseException as error:
                self._restart_error = error

        self._restart_thread = threading.Thread(target=restart, daemon=True)
        self._restart_thread.start()

    def wait_for_restart(self, timeout_seconds: float = 1.0) -> bool:
        """Use the virtual one-second inter-frame gap for off-path recovery."""
        thread = self._restart_thread
        if thread is not None:
            thread.join(timeout=max(timeout_seconds, 0.0))
        return (
            (thread is None or not thread.is_alive())
            and self._restart_error is None
            and self.process is not None
            and self.process.poll() is None
        )

    def _stderr_text(self) -> str:
        process = self.process
        if process is None or process.stderr is None or process.poll() is None:
            return ""
        return process.stderr.read().strip()

    def recognize(self, path: Path, timeout_ms: float | None = None) -> WorkerReply:
        if self._restart_thread is not None and self._restart_thread.is_alive():
            return WorkerReply(None, 0.0, "worker_restarting")
        if self._restart_error is not None:
            self._restart_error = None
            self._begin_restart()
            return WorkerReply(None, 0.0, "worker_restart_failed")
        if self.process is None or self.process.poll() is not None:
            self.restart_count += 1
            self.crash_restart_count += 1
            self._terminate()
            self._begin_restart()
            return WorkerReply(None, 0.0, "worker_restarted")
        assert self.process.stdin is not None
        request_id = self._next_id
        self._next_id += 1
        started = time.perf_counter()
        try:
            self.process.stdin.write(
                json.dumps(
                    {"id": request_id, "command": "recognize", "path": str(path.resolve())},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            self.process.stdin.flush()
            timeout = None if timeout_ms is None else max(timeout_ms, 1.0) / 1000.0
            raw = self._responses.get(timeout=timeout)
            if raw is None:
                raise PersistentOcrError("持久 OCR 工作进程意外退出")
        except Empty:
            wall_ms = (time.perf_counter() - started) * 1000.0
            self._terminate(wait=False)
            self.restart_count += 1
            self.budget_timeout_count += 1
            self._begin_restart()
            return WorkerReply(None, wall_ms, "budget_timeout")
        except (BrokenPipeError, OSError, PersistentOcrError):
            wall_ms = (time.perf_counter() - started) * 1000.0
            self._terminate(wait=False)
            self.restart_count += 1
            self.crash_restart_count += 1
            self._begin_restart()
            return WorkerReply(None, wall_ms, "worker_restarted")
        wall_ms = (time.perf_counter() - started) * 1000.0
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return WorkerReply(None, wall_ms, "invalid_json")
        if int(payload.get("id", -1)) != request_id:
            return WorkerReply(None, wall_ms, "protocol_mismatch")
        raw_lines = payload.get("lines")
        line_items = raw_lines if isinstance(raw_lines, list) else ([] if raw_lines is None else [raw_lines])
        lines = tuple(
            OcrLine(
                str(item.get("text", "")),
                float(item.get("x", 0.0)),
                float(item.get("y", 0.0)),
                float(item.get("width", 0.0)),
                float(item.get("height", 0.0)),
                None,
            )
            for item in line_items
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        )
        error_value = payload.get("error")
        result = OcrFrameResult(
            Path(str(payload.get("path", path))),
            int(payload.get("width", 0)),
            int(payload.get("height", 0)),
            float(payload.get("duration_ms", 0.0)),
            float(payload.get("recognize_ms", 0.0)),
            lines,
            str(error_value) if error_value is not None else None,
        )
        return WorkerReply(result, wall_ms, None if result.error is None else "ocr_error")

    def _terminate(self, *, wait: bool = True) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        if not wait:
            return
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        restart = self._restart_thread
        if restart is not None and restart.is_alive():
            restart.join(timeout=2.0)
        process = self.process
        if process is not None and process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write('{"command":"stop"}\n')
                process.stdin.flush()
                process.wait(timeout=2.0)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                self._terminate()
        self.process = None

    def __enter__(self) -> "PersistentWinRtOcrWorker":
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def normalize_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value)
        if not character.isspace()
    ).casefold()


def _cache_text_key(value: str) -> str:
    """Suppress OCR punctuation jitter without hiding letter/number changes."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    content = "".join(character for character in normalized if character.isalnum())
    return content or normalize_text(value)


@dataclass(frozen=True, slots=True)
class CachedLine:
    text: str
    line: OcrLine
    streak: int


@dataclass(frozen=True, slots=True)
class DiffLine:
    kind: LineChange
    text: str
    line: OcrLine
    streak: int
    previous_text: str | None = None


@dataclass(frozen=True, slots=True)
class DiffResult:
    lines: tuple[DiffLine, ...]
    disappeared: tuple[str, ...]
    stable_count: int
    scene_reset: bool
    gone: tuple[CachedLine, ...] = ()


def _line_center(line: OcrLine) -> tuple[float, float]:
    return line.x + line.width / 2.0, line.y + line.height / 2.0


def _position_distance(first: OcrLine, second: OcrLine) -> float:
    ax, ay = _line_center(first)
    bx, by = _line_center(second)
    return math.hypot(ax - bx, ay - by)


def _is_position_near(first: OcrLine, second: OcrLine) -> bool:
    first_left, first_right = first.x, first.x + first.width
    second_left, second_right = second.x, second.x + second.width
    first_top, first_bottom = first.y, first.y + first.height
    second_top, second_bottom = second.y, second.y + second.height
    horizontal = min(
        abs(first_left - second_left),
        abs(first_right - second_right),
        abs(_line_center(first)[0] - _line_center(second)[0]),
    )
    vertical = min(
        abs(first_top - second_top),
        abs(first_bottom - second_bottom),
        abs(_line_center(first)[1] - _line_center(second)[1]),
    )
    horizontal_threshold = max(0.025, min(max(first.width, second.width) * 0.35, 0.08))
    vertical_threshold = max(0.018, min(max(first.height, second.height) * 2.0, 0.06))
    return horizontal <= horizontal_threshold and vertical <= vertical_threshold


class TextLineCache:
    """Greedy position-neighbour cache with exact normalized content stability."""

    def __init__(self) -> None:
        self._lines: tuple[CachedLine, ...] = ()

    @property
    def lines(self) -> tuple[CachedLine, ...]:
        return self._lines

    def clear(self) -> None:
        self._lines = ()

    def update(self, lines: Sequence[OcrLine], *, scene_reset: bool = False) -> DiffResult:
        current = tuple(line for line in lines if normalize_text(line.text))
        if scene_reset:
            next_lines = tuple(CachedLine(line.text, line, 1) for line in current)
            self._lines = next_lines
            return DiffResult(
                tuple(DiffLine("added", item.text, item.line, item.streak) for item in next_lines),
                (),
                0,
                True,
                (),
            )

        unmatched = set(range(len(self._lines)))
        matched: dict[int, int] = {}
        # Prefer an exact-content positional match, then permit a same-position
        # content change.  This prevents adjacent HUD rows swapping identities.
        for current_index, line in enumerate(current):
            exact = [
                old_index
                for old_index in unmatched
                if _cache_text_key(self._lines[old_index].text) == _cache_text_key(line.text)
                and _is_position_near(self._lines[old_index].line, line)
            ]
            choices = exact or [
                old_index
                for old_index in unmatched
                if _is_position_near(self._lines[old_index].line, line)
            ]
            if choices:
                best = min(choices, key=lambda index: _position_distance(self._lines[index].line, line))
                matched[current_index] = best
                unmatched.remove(best)

        diff_lines: list[DiffLine] = []
        next_lines: list[CachedLine] = []
        for index, line in enumerate(current):
            old_index = matched.get(index)
            if old_index is None:
                cached = CachedLine(line.text, line, 1)
                kind: LineChange = "added"
            else:
                old = self._lines[old_index]
                same = _cache_text_key(old.text) == _cache_text_key(line.text)
                cached = CachedLine(line.text, line, old.streak + 1 if same else 1)
                kind = "unchanged" if same else "changed"
                previous_text = None if same else old.text
            next_lines.append(cached)
            diff_lines.append(
                DiffLine(
                    kind,
                    cached.text,
                    cached.line,
                    cached.streak,
                    previous_text if old_index is not None else None,
                )
            )
        disappeared = tuple(self._lines[index].text for index in sorted(unmatched))
        gone = tuple(self._lines[index] for index in sorted(unmatched))
        self._lines = tuple(next_lines)
        return DiffResult(
            tuple(diff_lines),
            disappeared,
            sum(item.streak >= 2 for item in next_lines),
            False,
            gone,
        )


@dataclass(frozen=True, slots=True)
class CropGroup:
    members: tuple[DiffLine, ...]
    left: int
    top: int
    right: int
    bottom: int
    scale: int
    score: float

    @property
    def output_pixels(self) -> int:
        return (
            max(self.right - self.left, 0)
            * max(self.bottom - self.top, 0)
            * self.scale
            * self.scale
        )


def _pixel_rect(line: OcrLine, width: int, height: int) -> tuple[float, float, float, float]:
    return (
        line.x * width,
        line.y * height,
        (line.x + line.width) * width,
        (line.y + line.height) * height,
    )


def _adjacent(first: DiffLine, second: DiffLine, width: int, height: int) -> bool:
    left_a, top_a, right_a, bottom_a = _pixel_rect(first.line, width, height)
    left_b, top_b, right_b, bottom_b = _pixel_rect(second.line, width, height)
    line_height = max(bottom_a - top_a, bottom_b - top_b, 1.0)
    vertical_gap = max(0.0, max(top_a, top_b) - min(bottom_a, bottom_b))
    horizontal_gap = max(0.0, max(left_a, left_b) - min(right_a, right_b))
    overlaps_x = min(right_a, right_b) - max(left_a, left_b) > 0
    return vertical_gap <= line_height * 0.75 and (overlaps_x or horizontal_gap <= line_height * 2.0)


def build_crop_groups(
    diff_lines: Sequence[DiffLine], image_width: int, image_height: int
) -> tuple[CropGroup, ...]:
    eligible = [
        item
        for item in diff_lines
        if item.kind in ("added", "changed")
        and 0 < item.line.height * image_height <= 64
    ]
    groups: list[list[DiffLine]] = []
    for item in sorted(eligible, key=lambda value: (value.line.y, value.line.x)):
        for group in groups:
            # A dense menu can form one transitive component spanning most of
            # the screen.  Merge neighbouring rows, but cap a crop at three
            # source lines so one call cannot quietly become another full-frame
            # OCR pass.
            if len(group) < 3 and any(
                _adjacent(item, member, image_width, image_height) for member in group
            ):
                group.append(item)
                break
        else:
            groups.append([item])

    crops: list[CropGroup] = []
    for members in groups:
        rects = [_pixel_rect(item.line, image_width, image_height) for item in members]
        max_height = max(bottom - top for _left, top, _right, bottom in rects)
        pad_x = max_height * 1.5
        pad_y = max_height
        left = max(0, math.floor(min(rect[0] for rect in rects) - pad_x))
        top = max(0, math.floor(min(rect[1] for rect in rects) - pad_y))
        right = min(image_width, math.ceil(max(rect[2] for rect in rects) + pad_x))
        bottom = min(image_height, math.ceil(max(rect[3] for rect in rects) + pad_y))
        scale = min(6, max(3, round(100 / max(max_height, 1.0))))
        # Pure geometry: larger text ranks first; upper two thirds gets a
        # modest deterministic bonus.  No recognized word affects priority.
        center_y = (top + bottom) / 2.0 / max(image_height, 1)
        location_weight = 1.25 if center_y <= 2.0 / 3.0 else 1.0
        score = max_height * location_weight
        crops.append(
            CropGroup(tuple(members), left, top, right, bottom, scale, score)
        )
    return tuple(sorted(crops, key=lambda item: (-item.score, item.top, item.left)))


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    selected: tuple[CropGroup, ...]
    skipped_count: int
    max_crops: int
    max_ms: float


class SceneResetRamp:
    """Exactly three reset-frame-inclusive relaxed-budget frames."""

    def __init__(self, frames: int = 3) -> None:
        self.frames = frames
        self.remaining = 0

    def consume(self, scene_reset: bool) -> bool:
        if scene_reset:
            self.remaining = self.frames
        ramp = self.remaining > 0
        if self.remaining:
            self.remaining -= 1
        return ramp


def select_crop_budget(
    groups: Sequence[CropGroup], *, ramp: bool
) -> BudgetDecision:
    max_crops = 4 if ramp else 2
    max_ms = 250.0 if ramp else 120.0
    selected = tuple(groups[:max_crops])
    return BudgetDecision(selected, max(0, len(groups) - len(selected)), max_crops, max_ms)


# T7.10b v3 exposed 581/833ms synchronous resize overshoots from very large
# merged crops.  A conservative geometry-only admission rate keeps resize work
# inside the remaining wall budget without inspecting recognized semantics.
RESIZE_OUTPUT_PIXELS_PER_MS = 4_000
SECOND_PASS_SAFETY_MARGIN_MS = 20.0


def crop_fits_remaining_budget(group: CropGroup, remaining_ms: float) -> bool:
    usable_ms = remaining_ms - SECOND_PASS_SAFETY_MARGIN_MS
    return usable_ms > 0 and group.output_pixels <= usable_ms * RESIZE_OUTPUT_PIXELS_PER_MS


@dataclass(frozen=True, slots=True)
class CadenceResult:
    same_frame_hit_rate: float
    late_rate: float
    same_frame_hits: int
    late_frames: int
    frame_count: int
    maximum_queue_delay_ms: float


def simulate_nonblocking_cadence(
    durations_ms: Sequence[float], *, interval_ms: float = 1000.0, wait_ms: float = 150.0
) -> CadenceResult:
    """Model a serial OCR worker while the cadence thread never waits for it."""
    previous_finish = 0.0
    hits = 0
    maximum_queue_delay = 0.0
    for index, duration in enumerate(durations_ms):
        arrival = index * interval_ms
        started = max(arrival, previous_finish)
        maximum_queue_delay = max(maximum_queue_delay, started - arrival)
        previous_finish = started + max(duration, 0.0)
        hits += previous_finish <= arrival + wait_ms
    count = len(durations_ms)
    late = count - hits
    return CadenceResult(
        hits / count if count else 0.0,
        late / count if count else 0.0,
        hits,
        late,
        count,
        maximum_queue_delay,
    )


class OcrCallable(Protocol):
    def __call__(self) -> Any: ...


def prove_thread_submission_is_nonblocking(callback: OcrCallable) -> tuple[float, threading.Thread]:
    """Start OCR work and return immediately; used by tests and the report proof."""
    thread = threading.Thread(target=callback, daemon=True)
    started = time.perf_counter()
    thread.start()
    return (time.perf_counter() - started) * 1000.0, thread
