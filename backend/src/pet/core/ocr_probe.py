"""Offline Windows.Media.Ocr probe for retained game capture frames."""

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable, Mapping, Sequence
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
from typing import Any

from PIL import Image

from pet.core.capture import (
    AdaptiveFrameSelector,
    ReplayFrameTime,
    _load_replay_frame_times,
    _replay_session_monotonic_origin,
)


BACKEND_DIRECTORY = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_ROOT = BACKEND_DIRECTORY / "eval-reports"
DEFAULT_LANGUAGE = "zh-Hans-CN"
DEFAULT_SAMPLE_STRIDE = 10
POWERSHELL_EXECUTABLE = "powershell.exe"
FRAME_PATTERN = re.compile(r"raw-(\d+)-")
GRID_COLUMNS = 9
GRID_ROWS = 16


class OcrProbeError(RuntimeError):
    """A local OCR probe failure safe to show to the operator."""


@dataclass(frozen=True, slots=True)
class SegmentRange:
    start: float
    end: float | None

    def contains(self, value: float) -> bool:
        return value >= self.start and (self.end is None or value < self.end)


@dataclass(frozen=True, slots=True)
class OcrLanguage:
    language_tag: str
    display_name: str
    native_name: str


@dataclass(frozen=True, slots=True)
class SampledFrame:
    path: Path
    frame_number: int
    timing: ReplayFrameTime
    relative_seconds: float
    confirmed_grid: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float | None = None
    quad: tuple[tuple[float, float], ...] | None = None


@dataclass(frozen=True, slots=True)
class OcrFrameResult:
    path: Path | None
    width: int
    height: int
    duration_ms: float
    recognize_ms: float
    lines: tuple[OcrLine, ...]
    error: str | None = None
    det_ms: float | None = None
    rec_ms: float | None = None
    cpu_core_seconds: float | None = None


PowerShellRunner = Callable[[str], bytes]


_WINRT_OCR_SCRIPT = r'''
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
$ocrType = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$languageType = [Windows.Globalization.Language,Windows.Foundation,ContentType=WindowsRuntime]
$storageFileType = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$streamType = [Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$decoderType = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
$bitmapType = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Foundation,ContentType=WindowsRuntime]
$resultType = [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
$requestPath = Decode-Value "__REQUEST_PATH_BASE64__"
$request = Get-Content -LiteralPath $requestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$available = @($ocrType::AvailableRecognizerLanguages | ForEach-Object {
  [PSCustomObject]@{
    language_tag = $_.LanguageTag
    display_name = $_.DisplayName
    native_name = $_.NativeName
  }
})
$language = New-Object Windows.Globalization.Language([string]$request.language)
$engine = $ocrType::TryCreateFromLanguage($language)
if ($null -eq $engine) { throw "OCR language unavailable: $($request.language)" }
$frames = @()
foreach ($path in @($request.frames)) {
  $frameWatch = [Diagnostics.Stopwatch]::StartNew()
  $stream = $null
  $bitmap = $null
  try {
    $file = Await-WinRt ($storageFileType::GetFileFromPathAsync([string]$path)) $storageFileType
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
          confidence = $null
        }
      }
    })
    $frameWatch.Stop()
    $frames += [PSCustomObject]@{
      path = [string]$path
      width = $bitmap.PixelWidth
      height = $bitmap.PixelHeight
      duration_ms = $frameWatch.Elapsed.TotalMilliseconds
      recognize_ms = $recognizeWatch.Elapsed.TotalMilliseconds
      lines = $lines
      error = $null
    }
  } catch {
    $frameWatch.Stop()
    $frames += [PSCustomObject]@{
      path = [string]$path
      width = 0
      height = 0
      duration_ms = $frameWatch.Elapsed.TotalMilliseconds
      recognize_ms = 0.0
      lines = @()
      error = $_.Exception.Message
    }
  } finally {
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
  }
}
$payload = [PSCustomObject]@{
  available_languages = $available
  requested_language = [string]$request.language
  max_image_dimension = $ocrType::MaxImageDimension
  confidence_available = $false
  frames = $frames
}
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::Out.Write(($payload | ConvertTo-Json -Depth 8 -Compress))
'''


_WINRT_LANGUAGE_SCRIPT = r'''
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$ocrType = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$languages = @($ocrType::AvailableRecognizerLanguages | ForEach-Object {
  [PSCustomObject]@{
    language_tag = $_.LanguageTag
    display_name = $_.DisplayName
    native_name = $_.NativeName
  }
})
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::Out.Write(($languages | ConvertTo-Json -Compress))
'''


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Windows 内置 OCR 离线录像探针")
    parser.add_argument("--session", required=True, type=Path, help="会话目录或其 raw 目录")
    parser.add_argument("--sample-stride", type=int, default=DEFAULT_SAMPLE_STRIDE)
    parser.add_argument("--lang", action="append", help="WinRT OCR 语言标签，可重复")
    parser.add_argument("--segment", type=_parse_segment, help="相对秒范围：起,止")
    parser.add_argument("--label")
    parser.add_argument("--answer-key", type=Path)
    parser.add_argument("--output-dir", type=Path)
    return parser


def _parse_segment(value: str) -> SegmentRange:
    parts = tuple(part.strip() for part in value.split(","))
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--segment 格式必须为 起,止")
    try:
        start = float(parts[0])
        end = float(parts[1])
    except ValueError as error:
        raise argparse.ArgumentTypeError("--segment 起止必须是秒数") from error
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise argparse.ArgumentTypeError("--segment 必须满足 0 <= 起 < 止")
    return SegmentRange(start, end)


def _run_powershell(script: str) -> bytes:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        completed = subprocess.run(
            [
                POWERSHELL_EXECUTABLE,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise OcrProbeError(f"无法启动 PowerShell WinRT OCR：{error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise OcrProbeError(f"PowerShell WinRT OCR 失败：{detail or completed.returncode}")
    return completed.stdout


def _json_payload(output: bytes, label: str) -> object:
    try:
        return json.loads(output.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OcrProbeError(f"{label}没有返回有效 UTF-8 JSON：{error}") from error


def enumerate_ocr_languages(runner: PowerShellRunner = _run_powershell) -> tuple[OcrLanguage, ...]:
    raw = _json_payload(runner(_WINRT_LANGUAGE_SCRIPT), "OCR 语言枚举")
    items = raw if isinstance(raw, list) else [raw]
    languages: list[OcrLanguage] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        tag = item.get("language_tag")
        display = item.get("display_name")
        native = item.get("native_name")
        if all(isinstance(value, str) for value in (tag, display, native)):
            languages.append(OcrLanguage(str(tag), str(display), str(native)))
    if not languages:
        raise OcrProbeError("Windows.Media.Ocr 未枚举出任何可用语言")
    return tuple(languages)


def _language_available(requested: str, languages: Sequence[OcrLanguage]) -> bool:
    normalized = requested.casefold()
    return any(
        item.language_tag.casefold() == normalized
        or item.language_tag.casefold().startswith(f"{normalized}-")
        or normalized.startswith(f"{item.language_tag.casefold()}-")
        for item in languages
    )


def _missing_language_message(language: str, available: Sequence[OcrLanguage]) -> str:
    tags = ", ".join(item.language_tag for item in available)
    return (
        f"本机缺少 OCR 语言 {language}；当前仅有：{tags}。"
        "请在 Windows 设置 > 时间和语言 > 语言和区域中添加对应语言及语言包；"
        "管理员也可先用 Get-WindowsCapability -Online 查询 Language.OCR 能力，"
        "再按系统返回的准确能力名安装。不得用英文 OCR 结果替代中文能力。"
    )


def _resolve_raw_directory(value: Path) -> tuple[Path, Path]:
    resolved = value.resolve()
    if resolved.name.casefold() == "raw" and resolved.is_dir():
        return resolved.parent, resolved
    raw = resolved / "raw"
    if raw.is_dir():
        return resolved, raw
    raise OcrProbeError(f"找不到 raw 目录：{value}")


def _frame_number(path: Path) -> int:
    match = FRAME_PATTERN.match(path.name)
    if match is None:
        raise OcrProbeError(f"raw 文件名无法解析帧号：{path.name}")
    return int(match.group(1))


def _prepare_samples(
    raw_directory: Path,
    stride: int,
    segment: SegmentRange | None,
) -> tuple[tuple[SampledFrame, ...], int]:
    paths = tuple(sorted(raw_directory.glob("raw-*.jpg")))
    if not paths:
        raise OcrProbeError(f"raw 目录没有 JPEG：{raw_directory}")
    timings = _load_replay_frame_times(raw_directory, paths)
    origin = _replay_session_monotonic_origin(raw_directory, timings)
    selector = AdaptiveFrameSelector()
    samples: list[SampledFrame] = []
    included = 0
    for path, timing in zip(paths, timings, strict=True):
        relative = timing.monotonic_seconds - origin
        if segment is not None and segment.end is not None and relative >= segment.end:
            break
        with Image.open(path) as source:
            bitmap = source.convert("RGB")
        observation = selector.observe(bitmap, timing.monotonic_seconds)
        bitmap.close()
        # Preserve the detector's full-session baseline state before sampling a clip.
        if segment is not None and relative < segment.start:
            continue
        if included % stride == 0:
            samples.append(
                SampledFrame(
                    path,
                    _frame_number(path),
                    timing,
                    relative,
                    observation.decision.confirmed_region_grid,
                )
            )
        included += 1
    if not included:
        raise OcrProbeError("指定片段没有 raw 帧")
    return tuple(samples), included


def _recording_hash(raw_directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(raw_directory.glob("raw-*.jpg")):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def run_winrt_ocr(
    samples: Sequence[SampledFrame],
    language: str,
    request_directory: Path,
    runner: PowerShellRunner = _run_powershell,
) -> tuple[tuple[OcrLanguage, ...], int, tuple[OcrFrameResult, ...]]:
    request_directory.mkdir(parents=True, exist_ok=True)
    request = {
        "language": language,
        "frames": [str(item.path) for item in samples],
    }
    request_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="ocr-request-",
            dir=request_directory,
            delete=False,
        ) as stream:
            json.dump(request, stream, ensure_ascii=False)
            request_path = Path(stream.name)
        encoded_path = base64.b64encode(str(request_path).encode("utf-8")).decode("ascii")
        script = _WINRT_OCR_SCRIPT.replace("__REQUEST_PATH_BASE64__", encoded_path)
        raw = _json_payload(runner(script), "WinRT OCR")
    finally:
        if request_path is not None:
            request_path.unlink(missing_ok=True)
    if not isinstance(raw, Mapping):
        raise OcrProbeError("WinRT OCR 顶层响应不是对象")
    languages_raw = raw.get("available_languages")
    language_items = languages_raw if isinstance(languages_raw, list) else [languages_raw]
    languages = tuple(
        OcrLanguage(
            str(item.get("language_tag", "")),
            str(item.get("display_name", "")),
            str(item.get("native_name", "")),
        )
        for item in language_items
        if isinstance(item, Mapping) and item.get("language_tag")
    )
    frames_raw = raw.get("frames")
    frame_items = frames_raw if isinstance(frames_raw, list) else [frames_raw]
    results: list[OcrFrameResult] = []
    for item in frame_items:
        if not isinstance(item, Mapping):
            continue
        lines_raw = item.get("lines")
        line_items = lines_raw if isinstance(lines_raw, list) else ([] if lines_raw is None else [lines_raw])
        lines = tuple(
            OcrLine(
                str(line.get("text", "")),
                float(line.get("x", 0.0)),
                float(line.get("y", 0.0)),
                float(line.get("width", 0.0)),
                float(line.get("height", 0.0)),
                None,
            )
            for line in line_items
            if isinstance(line, Mapping) and str(line.get("text", "")).strip()
        )
        error_value = item.get("error")
        results.append(
            OcrFrameResult(
                Path(str(item.get("path", ""))),
                int(item.get("width", 0)),
                int(item.get("height", 0)),
                float(item.get("duration_ms", 0.0)),
                float(item.get("recognize_ms", 0.0)),
                lines,
                str(error_value) if error_value is not None else None,
            )
        )
    expected = [item.path.resolve() for item in samples]
    actual = [item.path.resolve() for item in results]
    if actual != expected:
        raise OcrProbeError("WinRT OCR 返回帧顺序或数量与请求不一致")
    max_dimension = int(raw.get("max_image_dimension", 0))
    return languages, max_dimension, tuple(results)


def _grid_rect(cell: str) -> tuple[float, float, float, float]:
    match = re.fullmatch(r"r(\d+)c(\d+)", cell)
    if match is None:
        raise OcrProbeError(f"无法解析 confirmed 格子：{cell}")
    row = int(match.group(1)) - 1
    column = int(match.group(2)) - 1
    if not 0 <= row < GRID_ROWS or not 0 <= column < GRID_COLUMNS:
        raise OcrProbeError(f"confirmed 格子越界：{cell}")
    return (
        column / GRID_COLUMNS,
        row / GRID_ROWS,
        1.0 / GRID_COLUMNS,
        1.0 / GRID_ROWS,
    )


def confirmed_overlap_ratio(line: OcrLine, confirmed_grid: Sequence[str]) -> float:
    line_area = max(line.width, 0.0) * max(line.height, 0.0)
    if line_area <= 0.0 or not confirmed_grid:
        return 0.0
    total = 0.0
    line_right = line.x + line.width
    line_bottom = line.y + line.height
    for cell in confirmed_grid:
        x, y, width, height = _grid_rect(cell)
        overlap_width = max(0.0, min(line_right, x + width) - max(line.x, x))
        overlap_height = max(0.0, min(line_bottom, y + height) - max(line.y, y))
        total += overlap_width * overlap_height
    return min(total / line_area, 1.0)


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _heatmap(results: Sequence[OcrFrameResult]) -> tuple[tuple[int, ...], ...]:
    values = [[0, 0, 0] for _ in range(3)]
    for result in results:
        for line in result.lines:
            column = min(int((line.x + line.width / 2.0) * 3), 2)
            row = min(int((line.y + line.height / 2.0) * 3), 2)
            values[max(row, 0)][max(column, 0)] += 1
    return tuple(tuple(row) for row in values)


def _manual_indices(count: int, wanted: int = 10) -> tuple[int, ...]:
    if count <= wanted:
        return tuple(range(count))
    return tuple(round(index * (count - 1) / (wanted - 1)) for index in range(wanted))


def _write_outputs(
    output_directory: Path,
    *,
    label: str,
    session_directory: Path,
    raw_directory: Path,
    language: str,
    available_languages: Sequence[OcrLanguage],
    max_image_dimension: int,
    samples: Sequence[SampledFrame],
    included_frame_count: int,
    results: Sequence[OcrFrameResult],
    stride: int,
    segment: SegmentRange | None,
    answer_key: Path | None,
    hash_before: str,
    hash_after: str,
) -> dict[str, object]:
    output_directory.mkdir(parents=True, exist_ok=False)
    by_path = {result.path.resolve(): result for result in results}
    csv_path = output_directory / "ocr-probe.csv"
    line_count = 0
    overlap_count = 0
    character_count = 0
    overlap_character_count = 0
    overlap_area_values: list[float] = []
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            (
                "frame_number",
                "relative_seconds",
                "raw_file",
                "image_width",
                "image_height",
                "duration_ms",
                "recognize_ms",
                "line_index",
                "text",
                "bbox_x",
                "bbox_y",
                "bbox_width",
                "bbox_height",
                "confidence",
                "confirmed_grid_count",
                "overlaps_confirmed",
                "confirmed_overlap_ratio",
                "error",
            )
        )
        for sample in samples:
            result = by_path[sample.path.resolve()]
            rows: Sequence[OcrLine | None] = result.lines or (None,)
            for index, line in enumerate(rows, start=1):
                overlap = confirmed_overlap_ratio(line, sample.confirmed_grid) if line else 0.0
                if line is not None:
                    line_count += 1
                    character_count += len(line.text)
                    overlap_count += int(overlap > 0.0)
                    overlap_character_count += len(line.text) if overlap > 0.0 else 0
                    overlap_area_values.append(overlap)
                writer.writerow(
                    (
                        sample.frame_number,
                        f"{sample.relative_seconds:.3f}",
                        sample.path.name,
                        result.width,
                        result.height,
                        f"{result.duration_ms:.3f}",
                        f"{result.recognize_ms:.3f}",
                        index if line is not None else "",
                        line.text if line is not None else "",
                        f"{line.x:.6f}" if line is not None else "",
                        f"{line.y:.6f}" if line is not None else "",
                        f"{line.width:.6f}" if line is not None else "",
                        f"{line.height:.6f}" if line is not None else "",
                        "",
                        len(sample.confirmed_grid),
                        str(overlap > 0.0).lower() if line is not None else "",
                        f"{overlap:.6f}" if line is not None else "",
                        result.error or "",
                    )
                )
    durations = [result.duration_ms for result in results if result.error is None]
    recognize_durations = [result.recognize_ms for result in results if result.error is None]
    per_frame_lines = [len(result.lines) for result in results]
    per_frame_characters = [sum(len(line.text) for line in result.lines) for result in results]
    heatmap = _heatmap(results)
    stats: dict[str, object] = {
        "label": label,
        "session": str(session_directory),
        "raw_directory": str(raw_directory),
        "language": language,
        "available_languages": [
            {
                "language_tag": item.language_tag,
                "display_name": item.display_name,
                "native_name": item.native_name,
            }
            for item in available_languages
        ],
        "route": "PowerShell -> WinRT Windows.Media.Ocr",
        "max_image_dimension": max_image_dimension,
        "confidence_available": False,
        "sample_stride": stride,
        "segment": (
            {"start": segment.start, "end": segment.end} if segment is not None else None
        ),
        "included_frame_count": included_frame_count,
        "sampled_frame_count": len(samples),
        "successful_frame_count": len(durations),
        "error_frame_count": len(results) - len(durations),
        "duration_ms": {
            "median": statistics.median(durations) if durations else None,
            "p90": _percentile(durations, 0.90),
            "max": max(durations) if durations else None,
        },
        "recognize_ms": {
            "median": statistics.median(recognize_durations) if recognize_durations else None,
            "p90": _percentile(recognize_durations, 0.90),
            "max": max(recognize_durations) if recognize_durations else None,
        },
        "text_line_count": line_count,
        "frames_with_text": sum(bool(result.lines) for result in results),
        "frames_with_text_rate": (
            sum(bool(result.lines) for result in results) / len(results) if results else None
        ),
        "text_character_count": character_count,
        "text_characters_per_frame": {
            "mean": statistics.mean(per_frame_characters) if per_frame_characters else 0.0,
            "median": statistics.median(per_frame_characters) if per_frame_characters else 0.0,
            "p90": _percentile(per_frame_characters, 0.90),
            "max": max(per_frame_characters) if per_frame_characters else 0,
        },
        "text_lines_per_frame": {
            "mean": statistics.mean(per_frame_lines) if per_frame_lines else 0.0,
            "median": statistics.median(per_frame_lines) if per_frame_lines else 0.0,
            "max": max(per_frame_lines) if per_frame_lines else 0,
        },
        "lines_overlapping_confirmed": overlap_count,
        "line_overlap_rate": overlap_count / line_count if line_count else None,
        "text_characters_overlapping_confirmed": overlap_character_count,
        "text_character_overlap_rate": (
            overlap_character_count / character_count if character_count else None
        ),
        "mean_confirmed_overlap_ratio": (
            statistics.mean(overlap_area_values) if overlap_area_values else None
        ),
        "text_center_heatmap_3x3": heatmap,
        "recording_hash_before": hash_before,
        "recording_hash_after": hash_after,
        "recording_hash_matches": hash_before == hash_after,
    }
    (output_directory / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    md_lines = [
        f"# {label} OCR 探针",
        "",
        f"- 通路：PowerShell → WinRT Windows.Media.Ocr",
        f"- 语言：`{language}`；本机可用："
        + "、".join(f"`{item.language_tag}`" for item in available_languages),
        "- 置信度：Windows.Media.Ocr 的行/词对象不提供置信度，本报告留空。",
        f"- 原始帧：{included_frame_count}；stride={stride}；抽样：{len(samples)}。",
        f"- 整帧总耗时中位/P90/最大：{statistics.median(durations):.1f}/"
        f"{_percentile(durations, 0.90):.1f}/{max(durations):.1f} ms。" if durations else "- 无成功帧。",
        f"- 录像哈希前后：`{hash_before}` / `{hash_after}`；一致={hash_before == hash_after}。",
        "",
    ]
    for sample in samples:
        result = by_path[sample.path.resolve()]
        md_lines.extend(
            [
                f"## 帧 {sample.frame_number}｜T+{sample.relative_seconds:.1f}s｜"
                f"{result.duration_ms:.1f} ms",
                "",
                f"原图：[{sample.path.name}]({sample.path.as_posix()})",
                "",
            ]
        )
        if result.error:
            md_lines.append(f"- OCR 错误：{result.error}")
        elif not result.lines:
            md_lines.append("- 未识别出文字行。")
        else:
            for line in result.lines:
                overlap = confirmed_overlap_ratio(line, sample.confirmed_grid)
                md_lines.append(
                    f"- `{line.text}`｜框 ({line.x:.4f}, {line.y:.4f}, "
                    f"{line.width:.4f}, {line.height:.4f})｜"
                    f"confirmed 重叠 {overlap:.1%}｜置信度：不可用"
                )
        md_lines.append("")
    (output_directory / "ocr-probe.md").write_text(
        "\n".join(md_lines).rstrip() + "\n", encoding="utf-8"
    )

    manual_lines = [
        f"# {label} OCR 人工对照抽样",
        "",
        "本表只并列 raw 帧与 OCR 原文，不判定命中、漏检或错读。",
        "",
    ]
    if answer_key is not None:
        manual_lines.extend([f"答案键草稿：[{answer_key.name}]({answer_key.resolve().as_posix()})", ""])
    manual_lines.extend(
        [
            "| 帧号 | T+秒 | raw 截图 | OCR 输出 |",
            "|---:|---:|---|---|",
        ]
    )
    for index in _manual_indices(len(samples)):
        sample = samples[index]
        result = by_path[sample.path.resolve()]
        texts = "<br>".join(line.text.replace("|", "\\|") for line in result.lines) or "（无）"
        manual_lines.append(
            f"| {sample.frame_number} | {sample.relative_seconds:.1f} | "
            f"[{sample.path.name}]({sample.path.as_posix()}) | {texts} |"
        )
    (output_directory / "manual-review.md").write_text(
        "\n".join(manual_lines).rstrip() + "\n", encoding="utf-8"
    )
    return stats


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    arguments = build_parser().parse_args(argv)
    if arguments.sample_stride <= 0:
        print("--sample-stride 必须为正整数", file=sys.stderr)
        return 2
    try:
        session_directory, raw_directory = _resolve_raw_directory(arguments.session)
        languages = enumerate_ocr_languages()
        requested_languages = tuple(arguments.lang or (DEFAULT_LANGUAGE,))
        for language in requested_languages:
            if not _language_available(language, languages):
                raise OcrProbeError(_missing_language_message(language, languages))
        samples, included = _prepare_samples(
            raw_directory, arguments.sample_stride, arguments.segment
        )
        hash_before = _recording_hash(raw_directory)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        label = arguments.label or session_directory.name
        root = (
            arguments.output_dir.resolve()
            if arguments.output_dir is not None
            else DEFAULT_OUTPUT_ROOT / f"ocr-probe-{stamp}" / label
        )
        if root.exists():
            raise OcrProbeError(f"输出目录已存在：{root}")
        all_stats: list[dict[str, object]] = []
        for language in requested_languages:
            destination = root if len(requested_languages) == 1 else root / language
            runtime_languages, max_dimension, results = run_winrt_ocr(
                samples, language, root.parent
            )
            hash_after = _recording_hash(raw_directory)
            all_stats.append(
                _write_outputs(
                    destination,
                    label=label,
                    session_directory=session_directory,
                    raw_directory=raw_directory,
                    language=language,
                    available_languages=runtime_languages,
                    max_image_dimension=max_dimension,
                    samples=samples,
                    included_frame_count=included,
                    results=results,
                    stride=arguments.sample_stride,
                    segment=arguments.segment,
                    answer_key=arguments.answer_key,
                    hash_before=hash_before,
                    hash_after=hash_after,
                )
            )
        print("OCR 语言：" + "、".join(item.language_tag for item in languages))
        print(f"抽样：{len(samples)}/{included} 帧；输出：{root}")
        for stats in all_stats:
            timing = stats["duration_ms"]
            assert isinstance(timing, Mapping)
            print(
                f"{stats['language']}: 行数 {stats['text_line_count']}；"
                f"耗时中位/P90/最大 {float(timing['median']):.1f}/"
                f"{float(timing['p90']):.1f}/{float(timing['max']):.1f} ms"
            )
        return 0
    except (OcrProbeError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"OCR 探针未执行：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
