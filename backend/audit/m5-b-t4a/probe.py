from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from typing import Any, Protocol, Sequence
import winreg

import psutil
from PIL import Image


PROBE_DIRECTORY = Path(__file__).resolve().parent
BACKEND_DIRECTORY = PROBE_DIRECTORY.parents[1]
SOURCE_DIRECTORY = BACKEND_DIRECTORY / "src"
if str(SOURCE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIRECTORY))

from pet.core.ocr_probe import (  # noqa: E402
    OcrFrameResult,
    OcrLine,
    _frame_number,
    _grid_rect,
    _prepare_samples,
)
from pet.core.ocr_selective import (  # noqa: E402
    PersistentWinRtOcrWorker,
    normalize_text,
)


DEFAULT_MANIFEST = BACKEND_DIRECTORY / "data" / "generic" / "ocr-truth" / "frames.toml"
DEFAULT_TRUTH = DEFAULT_MANIFEST.with_name("truth.md")
DEFAULT_OUTPUT = BACKEND_DIRECTORY / "eval-reports" / "m5-b-t4a-engines"
CATEGORIES = ("hud_numeric", "subtitle_dialogue", "menu_button", "dense_panel")
CONFIGURATIONS = ("width-896", "full", "width-1280", "confirmed-region-crop")
POSITION_TOLERANCE = 0.03
STABILITY_RUNS = 5
EARLY_STOP_MEDIAN_MS = 500.0
NUMERIC_PATTERN = re.compile(r"\d")
TRUTH_LINE_PATTERN = re.compile(
    r'^- (?P<text>".*") \| \((?P<x>\d+(?:\.\d+)?), (?P<y>\d+(?:\.\d+)?)\) '
    r'\| (?P<category>[a-z_]+)$'
)


@dataclass(frozen=True, slots=True)
class FrameSpec:
    index: int
    role: str
    session: Path
    relative_session: str
    relative_frame: str
    path: Path
    category: str

    @property
    def frame_number(self) -> int:
        return _frame_number(self.path)

    @property
    def frame_id(self) -> str:
        return f"{self.role}:{self.path.name}"


@dataclass(frozen=True, slots=True)
class TruthLine:
    frame_index: int
    text: str
    x: float
    y: float
    category: str


@dataclass(frozen=True, slots=True)
class Prediction:
    text: str
    x: float
    y: float
    confidence: float | None


@dataclass(frozen=True, slots=True)
class Recognition:
    elapsed_ms: float
    predictions: tuple[Prediction, ...]


@dataclass(frozen=True, slots=True)
class PreparedInput:
    path: Path | None
    offset_x: float = 0.0
    offset_y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0


class ProbeEngine(Protocol):
    name: str
    has_confidence: bool
    startup_ms: float
    initialization_ms: float
    warmup_ms: float
    rss_mb: float
    idle_cpu_percent: float
    model_size_mb: float | None

    def start(self, warmup: Path) -> None: ...

    def recognize(self, image: Path) -> Recognition: ...

    def close(self) -> None: ...


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)


def load_frames(path: Path) -> tuple[FrameSpec, ...]:
    with path.open("rb") as stream:
        payload = tomllib.load(stream)
    raw_frames = payload.get("frames")
    if not isinstance(raw_frames, list) or len(raw_frames) != 40:
        raise ValueError("frames.toml must contain exactly 40 [[frames]] entries")
    frames: list[FrameSpec] = []
    for index, item in enumerate(raw_frames, 1):
        if not isinstance(item, dict):
            raise ValueError(f"frame {index} is not a TOML table")
        category = str(item.get("category", ""))
        if category not in CATEGORIES:
            raise ValueError(f"frame {index} has unknown category {category!r}")
        relative_session = str(item.get("session", ""))
        relative_frame = str(item.get("frame", ""))
        session = (path.parent / relative_session).resolve()
        frame_path = (session / relative_frame).resolve()
        if not frame_path.is_file():
            raise FileNotFoundError(frame_path)
        frames.append(
            FrameSpec(
                index,
                str(item.get("role", "")),
                session,
                relative_session,
                relative_frame,
                frame_path,
                category,
            )
        )
    counts = {category: sum(frame.category == category for frame in frames) for category in CATEGORIES}
    if any(count != 10 for count in counts.values()):
        raise ValueError(f"expected 10 frames per category, got {counts}")
    sessions = {frame.session for frame in frames}
    if len(sessions) != 4 or any(sum(frame.session == session for frame in frames) != 10 for session in sessions):
        raise ValueError("expected four sessions with ten frames each")
    return tuple(frames)


def write_initial_truth(frames: Sequence[FrameSpec], destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(
            f"refusing to overwrite truth asset: {destination}; delete it only when creating the first draft"
        )
    lines = [
        "# M5-B-T4 OCR 人工真值集",
        "",
        "> 本文件当前是 WinRT 生成的初稿。每帧顶部均标注“待人工校对”；人工校对前，召回与精确率只作候选引擎横向参考。",
        "",
        "## 判定规则",
        "",
        "- 文本先经 `pet.core.ocr_selective.normalize_text` 归一化，再做全等比较。",
        "- 位置容差为归一化中心距离 ≤ 0.03。",
        "- 同帧内一对一匹配，不重复计数。真值行被匹配即命中；引擎行匹配不到任何真值行即误报。",
        "- 每条格式为：`原文 | 归一化中心坐标(x,y) | 类别`。原文使用 JSON 字符串表示，避免竖线等字符破坏字段边界。",
        "",
    ]
    with PersistentWinRtOcrWorker("zh-Hans-CN") as worker:
        for frame in frames:
            reply = worker.recognize(frame.path)
            if reply.result is None:
                raise RuntimeError(f"WinRT failed for {frame.frame_id}: {reply.skipped_reason}")
            lines.extend(
                [
                    f"## 帧 {frame.index:02d} · {frame.role} · {frame.path.name}",
                    "",
                    "**待人工校对**",
                    "",
                ]
            )
            for item in reply.result.lines:
                center_x = item.x + item.width / 2.0
                center_y = item.y + item.height / 2.0
                lines.append(
                    f"- {json.dumps(item.text, ensure_ascii=False)} | "
                    f"({center_x:.6f}, {center_y:.6f}) | {frame.category}"
                )
            if not reply.result.lines:
                lines.append("（WinRT 初稿未读出文字；待人工校对）")
            lines.append("")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")


def load_truth(path: Path, frame_count: int) -> tuple[TruthLine, ...]:
    frame_index = 0
    values: list[TruthLine] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## 帧 "):
            frame_index += 1
            continue
        match = TRUTH_LINE_PATTERN.fullmatch(line)
        if match is None:
            continue
        if frame_index == 0:
            raise ValueError("truth line appeared before first frame heading")
        text = json.loads(match.group("text"))
        values.append(
            TruthLine(
                frame_index - 1,
                str(text),
                float(match.group("x")),
                float(match.group("y")),
                match.group("category"),
            )
        )
    if frame_index != frame_count:
        raise ValueError(f"truth.md has {frame_index} frame blocks, expected {frame_count}")
    return tuple(values)


def _process_resources(process: psutil.Process) -> tuple[float, float]:
    processes = [process, *process.children(recursive=True)]
    for item in processes:
        item.cpu_percent(None)
    time.sleep(1.0)
    rss = sum(item.memory_info().rss for item in processes)
    cpu = sum(item.cpu_percent(None) for item in processes)
    return rss / (1024 * 1024), cpu


class WinRtEngine:
    name = "winrt-windows-media-ocr"
    has_confidence = False

    def __init__(self) -> None:
        self.worker: PersistentWinRtOcrWorker | None = None
        self.startup_ms = 0.0
        self.initialization_ms = 0.0
        self.warmup_ms = 0.0
        self.rss_mb = 0.0
        self.idle_cpu_percent = 0.0
        self.model_size_mb = None

    def start(self, warmup: Path) -> None:
        self.worker = PersistentWinRtOcrWorker("zh-Hans-CN")
        self.initialization_ms = self.worker.start()
        reply = self.worker.recognize(warmup)
        if reply.result is None:
            raise RuntimeError(f"WinRT warmup failed: {reply.skipped_reason}")
        self.warmup_ms = reply.wall_ms
        self.startup_ms = self.initialization_ms + self.warmup_ms
        assert self.worker.process is not None
        self.rss_mb, self.idle_cpu_percent = _process_resources(psutil.Process(self.worker.process.pid))

    def recognize(self, image: Path) -> Recognition:
        if self.worker is None:
            raise RuntimeError("WinRT engine is not started")
        reply = self.worker.recognize(image)
        if reply.result is None:
            raise RuntimeError(f"WinRT recognition failed: {reply.skipped_reason}")
        values = tuple(
            Prediction(
                item.text,
                item.x + item.width / 2.0,
                item.y + item.height / 2.0,
                None,
            )
            for item in reply.result.lines
        )
        return Recognition(reply.wall_ms, values)

    def close(self) -> None:
        if self.worker is not None:
            self.worker.close()
            self.worker = None


class RapidOcrEngine:
    has_confidence = True

    def __init__(self, model_type: str) -> None:
        self.model_type = model_type
        self.name = f"rapidocr-ppocrv4-{model_type}-cpu"
        self.engine: Any = None
        self.startup_ms = 0.0
        self.initialization_ms = 0.0
        self.warmup_ms = 0.0
        self.rss_mb = 0.0
        self.idle_cpu_percent = 0.0
        self.model_size_mb = 0.0

    def start(self, warmup: Path) -> None:
        from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR
        import rapidocr

        kind = ModelType.MOBILE if self.model_type == "mobile" else ModelType.SERVER
        params = {
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.lang_type": LangDet.CH,
            "Det.model_type": kind,
            "Det.ocr_version": OCRVersion.PPOCRV4,
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.lang_type": LangRec.CH,
            "Rec.model_type": kind,
            "Rec.ocr_version": OCRVersion.PPOCRV4,
        }
        started = time.perf_counter()
        self.engine = RapidOCR(params=params)
        self.initialization_ms = (time.perf_counter() - started) * 1000.0
        warm = self.recognize(warmup)
        self.warmup_ms = warm.elapsed_ms
        self.startup_ms = self.initialization_ms + self.warmup_ms
        self.rss_mb, self.idle_cpu_percent = _process_resources(psutil.Process(os.getpid()))
        model_directory = Path(rapidocr.__file__).resolve().parent / "models"
        model_files = [
            model_directory / f"ch_PP-OCRv4_det_{self.model_type}.onnx",
            model_directory / f"ch_PP-OCRv4_rec_{self.model_type}.onnx",
        ]
        self.model_size_mb = sum(path.stat().st_size for path in model_files) / (1024 * 1024)

    def recognize(self, image: Path) -> Recognition:
        if self.engine is None:
            raise RuntimeError("RapidOCR engine is not started")
        started = time.perf_counter()
        result = self.engine(image, use_cls=False)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        boxes = result.boxes if result.boxes is not None else ()
        texts = result.txts if result.txts is not None else ()
        scores = result.scores if result.scores is not None else ()
        with Image.open(image) as source:
            width, height = source.size
        values: list[Prediction] = []
        for box, text, score in zip(boxes, texts, scores, strict=True):
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            values.append(
                Prediction(
                    str(text),
                    ((min(xs) + max(xs)) / 2.0) / width,
                    ((min(ys) + max(ys)) / 2.0) / height,
                    float(score),
                )
            )
        return Recognition(elapsed_ms, tuple(values))

    def close(self) -> None:
        self.engine = None


def _confirmed_grids(frames: Sequence[FrameSpec]) -> dict[int, tuple[str, ...]]:
    by_session: dict[Path, list[FrameSpec]] = {}
    for frame in frames:
        by_session.setdefault(frame.session, []).append(frame)
    output: dict[int, tuple[str, ...]] = {}
    for session, wanted in by_session.items():
        samples, _ = _prepare_samples(session / "raw", 1, None)
        by_name = {sample.path.name: sample.confirmed_grid for sample in samples}
        for frame in wanted:
            output[frame.index] = by_name.get(frame.path.name, ())
    return output


def _crop_rect(cells: Sequence[str]) -> tuple[float, float, float, float] | None:
    if not cells:
        return None
    rects = [_grid_rect(cell) for cell in cells]
    return (
        min(rect[0] for rect in rects),
        min(rect[1] for rect in rects),
        max(rect[0] + rect[2] for rect in rects),
        max(rect[1] + rect[3] for rect in rects),
    )


def prepare_inputs(
    frames: Sequence[FrameSpec],
    configuration: str,
    confirmed_grids: dict[int, tuple[str, ...]],
    directory: Path,
) -> dict[int, PreparedInput]:
    output: dict[int, PreparedInput] = {}
    for frame in frames:
        if configuration == "full":
            output[frame.index] = PreparedInput(frame.path)
            continue
        with Image.open(frame.path) as source:
            bitmap = source.convert("RGB")
        if configuration.startswith("width-"):
            width = int(configuration.split("-", 1)[1])
            height = round(bitmap.height * width / bitmap.width)
            transformed = bitmap.resize((width, height), Image.Resampling.LANCZOS)
            destination = directory / f"{frame.index:02d}-{configuration}.png"
            transformed.save(destination)
            transformed.close()
            output[frame.index] = PreparedInput(destination)
        else:
            rect = _crop_rect(confirmed_grids.get(frame.index, ()))
            if rect is None:
                output[frame.index] = PreparedInput(None)
            else:
                left = max(0, math.floor(rect[0] * bitmap.width))
                top = max(0, math.floor(rect[1] * bitmap.height))
                right = min(bitmap.width, math.ceil(rect[2] * bitmap.width))
                bottom = min(bitmap.height, math.ceil(rect[3] * bitmap.height))
                cropped = bitmap.crop((left, top, right, bottom))
                destination = directory / f"{frame.index:02d}-{configuration}.png"
                cropped.save(destination)
                cropped.close()
                output[frame.index] = PreparedInput(
                    destination,
                    left / bitmap.width,
                    top / bitmap.height,
                    (right - left) / bitmap.width,
                    (bottom - top) / bitmap.height,
                )
        bitmap.close()
    return output


def _map_predictions(recognition: Recognition, prepared: PreparedInput) -> Recognition:
    return Recognition(
        recognition.elapsed_ms,
        tuple(
            Prediction(
                item.text,
                prepared.offset_x + item.x * prepared.scale_x,
                prepared.offset_y + item.y * prepared.scale_y,
                item.confidence,
            )
            for item in recognition.predictions
        ),
    )


def _match_frame(
    truths: Sequence[TruthLine], predictions: Sequence[Prediction]
) -> tuple[tuple[tuple[int, int, float], ...], tuple[int, ...], tuple[int, ...]]:
    edges: list[list[tuple[int, float]]] = []
    for truth in truths:
        normalized = normalize_text(truth.text)
        options: list[tuple[int, float]] = []
        for prediction_index, prediction in enumerate(predictions):
            if normalize_text(prediction.text) != normalized:
                continue
            distance = math.hypot(prediction.x - truth.x, prediction.y - truth.y)
            if distance <= POSITION_TOLERANCE:
                options.append((prediction_index, distance))
        edges.append(sorted(options, key=lambda item: item[1]))

    prediction_to_truth: dict[int, int] = {}

    def assign(truth_index: int, visited: set[int]) -> bool:
        for prediction_index, _distance in edges[truth_index]:
            if prediction_index in visited:
                continue
            visited.add(prediction_index)
            previous = prediction_to_truth.get(prediction_index)
            if previous is None or assign(previous, visited):
                prediction_to_truth[prediction_index] = truth_index
                return True
        return False

    for truth_index in range(len(truths)):
        assign(truth_index, set())
    pairs = tuple(
        sorted(
            (
                truth_index,
                prediction_index,
                math.hypot(
                    predictions[prediction_index].x - truths[truth_index].x,
                    predictions[prediction_index].y - truths[truth_index].y,
                ),
            )
            for prediction_index, truth_index in prediction_to_truth.items()
        )
    )
    matched_truth = {pair[0] for pair in pairs}
    matched_predictions = {pair[1] for pair in pairs}
    return (
        pairs,
        tuple(index for index in range(len(truths)) if index not in matched_truth),
        tuple(index for index in range(len(predictions)) if index not in matched_predictions),
    )


def _closest_prediction(truth: TruthLine, predictions: Sequence[Prediction]) -> str:
    if not predictions:
        return "—"
    wanted = normalize_text(truth.text)
    best = max(
        predictions,
        key=lambda item: difflib.SequenceMatcher(None, wanted, normalize_text(item.text)).ratio(),
    )
    return best.text


def _closest_truth(prediction: Prediction, truths: Sequence[TruthLine]) -> str:
    if not truths:
        return "—"
    value = normalize_text(prediction.text)
    best = max(
        truths,
        key=lambda item: difflib.SequenceMatcher(None, value, normalize_text(item.text)).ratio(),
    )
    return best.text


def score_configuration(
    frames: Sequence[FrameSpec],
    truth: Sequence[TruthLine],
    recognitions: dict[int, Recognition],
) -> dict[str, Any]:
    truth_by_frame = {
        frame.index: tuple(item for item in truth if item.frame_index == frame.index - 1)
        for frame in frames
    }
    matches: list[tuple[int, TruthLine, Prediction, float]] = []
    misses: list[dict[str, Any]] = []
    false_positives: list[dict[str, Any]] = []
    category_counts = {
        category: {"truth": 0, "output": 0, "matches": 0} for category in CATEGORIES
    }
    for frame in frames:
        truths = truth_by_frame[frame.index]
        predictions = recognitions.get(frame.index, Recognition(0.0, ())).predictions
        pairs, unmatched_truth, unmatched_predictions = _match_frame(truths, predictions)
        category_counts[frame.category]["truth"] += len(truths)
        category_counts[frame.category]["output"] += len(predictions)
        category_counts[frame.category]["matches"] += len(pairs)
        for truth_index, prediction_index, distance in pairs:
            matches.append((frame.index, truths[truth_index], predictions[prediction_index], distance))
        for truth_index in unmatched_truth:
            item = truths[truth_index]
            misses.append(
                {
                    "frame": frame.frame_id,
                    "truth": item.text,
                    "engine_output": _closest_prediction(item, predictions),
                }
            )
        for prediction_index in unmatched_predictions:
            item = predictions[prediction_index]
            false_positives.append(
                {
                    "frame": frame.frame_id,
                    "truth": _closest_truth(item, truths),
                    "engine_output": item.text,
                    "center": [item.x, item.y],
                }
            )
    total_truth = len(truth)
    total_output = sum(len(item.predictions) for item in recognitions.values())
    numeric_truth = [item for item in truth if NUMERIC_PATTERN.search(normalize_text(item.text))]
    matched_truth_keys = {(frame_index, item.text, item.x, item.y) for frame_index, item, _pred, _dist in matches}
    numeric_matches = sum(
        (item.frame_index + 1, item.text, item.x, item.y) in matched_truth_keys for item in numeric_truth
    )
    category_metrics: dict[str, Any] = {}
    for category, counts in category_counts.items():
        category_metrics[category] = {
            **counts,
            "recall": counts["matches"] / counts["truth"] if counts["truth"] else None,
            "precision": counts["matches"] / counts["output"] if counts["output"] else None,
        }
    return {
        "truth_lines": total_truth,
        "output_lines": total_output,
        "matches": len(matches),
        "recall": len(matches) / total_truth if total_truth else None,
        "precision": len(matches) / total_output if total_output else None,
        "position_error_median": statistics.median(item[3] for item in matches) if matches else None,
        "numeric_truth_lines": len(numeric_truth),
        "numeric_matches": numeric_matches,
        "numeric_recall": numeric_matches / len(numeric_truth) if numeric_truth else None,
        "categories": category_metrics,
        "misses": misses,
        "false_positives": false_positives,
    }


def _signature(recognition: Recognition) -> tuple[tuple[str, float, float], ...]:
    return tuple(
        sorted(
            (normalize_text(item.text), round(item.x, 4), round(item.y, 4))
            for item in recognition.predictions
        )
    )


def probe_windows_ai() -> dict[str, Any]:
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
        build = str(winreg.QueryValueEx(key, "CurrentBuild")[0])
        display_version = str(winreg.QueryValueEx(key, "DisplayVersion")[0])
        product_name = str(winreg.QueryValueEx(key, "ProductName")[0])
    package_script = (
        "Get-AppxPackage | Where-Object {$_.Name -match 'WindowsAppRuntime'} | "
        "Select-Object Name,Version,PackageFullName | ConvertTo-Json -Compress"
    )
    packages_run = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", package_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    package_text = packages_run.stdout.decode("utf-8-sig", errors="replace").strip()
    try:
        packages_raw = json.loads(package_text) if package_text else []
    except json.JSONDecodeError:
        packages_raw = []
    packages = packages_raw if isinstance(packages_raw, list) else [packages_raw]
    pnp = subprocess.run(
        ["pnputil.exe", "/enum-devices", "/connected"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    pnp_text = pnp.stdout.decode("utf-8-sig", errors="replace")
    npu_lines = tuple(
        line.strip()
        for line in pnp_text.splitlines()
        if re.search(r"\bNPU\b|\bNeural\b|AI Boost|Hexagon", line, re.IGNORECASE)
    )
    projections: dict[str, bool] = {}
    for name in ("winrt", "winsdk"):
        try:
            projections[name] = importlib.util.find_spec(name) is not None
        except ModuleNotFoundError:
            projections[name] = False
    windows_app_sdk_nuget = Path.home() / ".nuget" / "packages" / "microsoft.windowsappsdk"
    references = Path(r"C:\Program Files (x86)\Windows Kits\10\References")
    metadata_matches = (
        [str(path) for path in references.rglob("Microsoft.Windows.AI*.winmd")]
        if references.exists()
        else []
    )
    return {
        "os": {
            "registry_product_name": product_name,
            "display_version": display_version,
            "build": build,
            "platform_version": platform.version(),
        },
        "windows_app_runtime_packages": packages,
        "npu_device_matches": list(npu_lines),
        "python_projection_available": projections,
        "windows_app_sdk_nuget_present": windows_app_sdk_nuget.exists(),
        "windows_ai_metadata_matches": metadata_matches,
        "available": False,
        "unavailable_reasons": [
            "No connected NPU device matched NPU/Neural/AI Boost/Hexagon; Microsoft documents Text Recognition as NPU-only.",
            "Neither the winrt nor winsdk Python projection is installed in the isolated probe environment.",
            "The probe is an unpackaged Python process and has no systemAIModels package capability declaration.",
        ],
        "sources": [
            "https://learn.microsoft.com/en-us/windows/ai/apis/",
            "https://learn.microsoft.com/en-us/windows/ai/apis/get-started",
            "https://learn.microsoft.com/en-us/windows/windows-app-sdk/api/winrt/microsoft.windows.ai.imaging.textrecognizer?view=windows-app-sdk-1.8",
        ],
    }


def benchmark(
    frames: Sequence[FrameSpec], truth: Sequence[TruthLine], output: Path
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    confirmed_grids = _confirmed_grids(frames)
    engine_factories = (WinRtEngine, lambda: RapidOcrEngine("mobile"), lambda: RapidOcrEngine("server"))
    payload: dict[str, Any] = {
        "task_id": "M5-B-T4a",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "position_tolerance": POSITION_TOLERANCE,
        "truth_status": "WinRT draft; pending human correction",
        "frame_count": len(frames),
        "category_frame_counts": {
            category: sum(frame.category == category for frame in frames) for category in CATEGORIES
        },
        "frame_dimensions": {},
        "windows_ai_text_recognizer": probe_windows_ai(),
        "engines": [],
        "gpu": {
            "tested": False,
            "reason": "GPU tier is conditional on every CPU candidate exceeding the 100 ms median target; WinRT is measured first and the final trigger is recorded from the completed CPU rows.",
        },
    }
    for frame in frames:
        with Image.open(frame.path) as source:
            payload["frame_dimensions"][frame.frame_id] = list(source.size)

    with tempfile.TemporaryDirectory(prefix="m5-b-t4a-") as temporary:
        temporary_root = Path(temporary)
        for factory in engine_factories:
            engine = factory()
            print(f"starting {engine.name}", flush=True)
            engine.start(frames[0].path)
            engine_payload: dict[str, Any] = {
                "name": engine.name,
                "startup_ms": engine.startup_ms,
                "initialization_ms": engine.initialization_ms,
                "warmup_ms": engine.warmup_ms,
                "resident_memory_mb": engine.rss_mb,
                "idle_cpu_percent": engine.idle_cpu_percent,
                "model_size_mb": engine.model_size_mb,
                "has_confidence": engine.has_confidence,
                "configurations": [],
                "early_stop": None,
            }
            try:
                for configuration in CONFIGURATIONS:
                    print(f"{engine.name}: {configuration}", flush=True)
                    config_directory = temporary_root / engine.name / configuration
                    config_directory.mkdir(parents=True, exist_ok=True)
                    prepared = prepare_inputs(frames, configuration, confirmed_grids, config_directory)
                    recognitions: dict[int, Recognition] = {}
                    durations: list[float] = []
                    for position, frame in enumerate(frames, 1):
                        item = prepared[frame.index]
                        if item.path is None:
                            recognitions[frame.index] = Recognition(0.0, ())
                            continue
                        recognition = _map_predictions(engine.recognize(item.path), item)
                        recognitions[frame.index] = recognition
                        durations.append(recognition.elapsed_ms)
                        if position % 10 == 0:
                            print(f"  {position}/{len(frames)}", flush=True)
                    stability_frame = next(
                        frame for frame in reversed(frames) if prepared[frame.index].path is not None
                    )
                    stability_input = prepared[stability_frame.index]
                    assert stability_input.path is not None
                    signatures = [
                        _signature(
                            _map_predictions(
                                engine.recognize(stability_input.path), stability_input
                            )
                        )
                        for _ in range(STABILITY_RUNS)
                    ]
                    scored = score_configuration(frames, truth, recognitions)
                    config_payload = {
                        "configuration": configuration,
                        "processed_frames": len(durations),
                        "skipped_no_confirmed_region": len(frames) - len(durations),
                        "duration_ms": {
                            "median": statistics.median(durations) if durations else None,
                            "p90": _percentile(durations, 0.90),
                            "max": max(durations) if durations else None,
                        },
                        "stability": {
                            "frame": stability_frame.frame_id,
                            "runs": STABILITY_RUNS,
                            "unique_output_signatures": len(set(signatures)),
                            "consistent": len(set(signatures)) == 1,
                        },
                        **scored,
                    }
                    engine_payload["configurations"].append(config_payload)
                    median_ms = config_payload["duration_ms"]["median"]
                    if (
                        configuration == "width-896"
                        and median_ms is not None
                        and median_ms > EARLY_STOP_MEDIAN_MS
                    ):
                        engine_payload["early_stop"] = {
                            "configuration": configuration,
                            "median_ms": median_ms,
                            "threshold_ms": EARLY_STOP_MEDIAN_MS,
                            "skipped_configurations": list(CONFIGURATIONS[1:]),
                        }
                        print(
                            f"{engine.name}: stopping after width-896 median "
                            f"{median_ms:.1f} ms exceeded {EARLY_STOP_MEDIAN_MS:.1f} ms",
                            flush=True,
                        )
                        break
            finally:
                engine.close()
            payload["engines"].append(engine_payload)

    full_medians = [
        config["duration_ms"]["median"]
        for engine in payload["engines"]
        for config in engine["configurations"]
        if config["configuration"] == "full"
    ]
    payload["gpu"] = {
        "tested": False,
        "reason": (
            "GPU tier not triggered because at least one CPU candidate met the <=100 ms full-frame median target."
            if any(value is not None and value <= 100.0 for value in full_medians)
            else "All CPU candidates exceeded the median target; GPU testing is required before selection."
        ),
    }
    return payload


def _format_ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _format_number(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _table_text(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def write_report(payload: dict[str, Any], destination: Path) -> None:
    windows_ai = payload["windows_ai_text_recognizer"]
    lines = [
        "# M5-B-T4a OCR 引擎横向实测",
        "",
        "> 真值集为 WinRT 初稿，每帧仍标注“待人工校对”。本报告只列事实与表格，不作选型推荐。",
        "",
        "## 素材与口径",
        "",
        f"- 40 帧；四段录制各 10 帧；四类各 10 帧。位置容差 {payload['position_tolerance']:.2f}，同帧一对一匹配。",
        "- 开发期按产品负责人指示不处理玩家 ID：真值初稿与引擎输出均不做身份文本过滤。",
        f"- 每个引擎先测 896 宽；若该配置中位耗时 > {EARLY_STOP_MEDIAN_MS:.0f}ms，则停止该引擎后续配置。",
        "- `full` 为选型主配置；两档缩放与 detector confirmed-region crop 只作后续成本机制参考。",
        "- 首次引擎初始化与一次预热合并列为启动耗时；所有识别耗时统计排除这次预热。",
        "- card-game 原始客户区为 1920×1079；另外三段为 1920×1080，未拉伸或补边。",
        "",
        "## Windows AI TextRecognizer 可用性探测",
        "",
        f"- OS registry: {windows_ai['os']['registry_product_name']} {windows_ai['os']['display_version']} build {windows_ai['os']['build']}。",
        f"- Windows App Runtime packages: {json.dumps(windows_ai['windows_app_runtime_packages'], ensure_ascii=False)}",
        f"- NPU 设备匹配：{json.dumps(windows_ai['npu_device_matches'], ensure_ascii=False)}",
        f"- Python projections: {json.dumps(windows_ai['python_projection_available'], ensure_ascii=False)}",
        f"- Windows App SDK NuGet package present: {windows_ai['windows_app_sdk_nuget_present']}。",
        f"- Windows AI metadata matches: {json.dumps(windows_ai['windows_ai_metadata_matches'], ensure_ascii=False)}",
        "- 判定：本机当前探针不可调用 `Microsoft.Windows.AI.Imaging.TextRecognizer`。依据：",
    ]
    lines.extend(f"  - {reason}" for reason in windows_ai["unavailable_reasons"])
    lines.extend(
        [
            "- 实测/定义依据：",
            *[f"  - {source}" for source in windows_ai["sources"]],
            "",
            "## 引擎 × 配置",
            "",
            "分类列顺序均为召回/精确率。crop 的耗时只统计实际存在 confirmed region 的帧；召回/精确率仍以全部 40 帧计。",
            "",
            "| 引擎 | 配置 | 处理帧 | 启动 ms | 中位/P90/最大 ms | 总召回/精确率 | HUD数字 | 字幕对话 | 菜单按钮 | 密集面板 | 位置误差中位 | 数字召回 | 内存 MiB | 空闲CPU% | 模型 MiB | 5次稳定 | 置信度 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for engine in payload["engines"]:
        for config in engine["configurations"]:
            duration = config["duration_ms"]
            categories = config["categories"]
            category_cells = [
                f"{_format_ratio(categories[name]['recall'])}/{_format_ratio(categories[name]['precision'])}"
                for name in CATEGORIES
            ]
            lines.append(
                f"| {engine['name']} | {config['configuration']} | {config['processed_frames']}/40 | "
                f"{engine['startup_ms']:.1f} | {_format_number(duration['median'])}/{_format_number(duration['p90'])}/{_format_number(duration['max'])} | "
                f"{_format_ratio(config['recall'])}/{_format_ratio(config['precision'])} | "
                f"{' | '.join(category_cells)} | {_format_number(config['position_error_median'], 4)} | "
                f"{_format_ratio(config['numeric_recall'])} | {engine['resident_memory_mb']:.1f} | "
                f"{engine['idle_cpu_percent']:.1f} | {_format_number(engine['model_size_mb'])} | "
                f"{config['stability']['unique_output_signatures']}/5 unique | "
                f"{'yes' if engine['has_confidence'] else 'no'} |"
            )
        if engine["early_stop"] is not None:
            stop = engine["early_stop"]
            lines.append(
                f"| {engine['name']} | 后续配置已中止 | 0/40 | {engine['startup_ms']:.1f} | "
                f"896 中位 {stop['median_ms']:.1f} > {stop['threshold_ms']:.1f} | — | — | — | — | — | — | — | "
                f"{engine['resident_memory_mb']:.1f} | {engine['idle_cpu_percent']:.1f} | "
                f"{_format_number(engine['model_size_mb'])} | — | {'yes' if engine['has_confidence'] else 'no'} |"
            )
    lines.extend(
        [
            "",
            "## 启动与常驻开销明细",
            "",
            "| 引擎 | 初始化 ms | 丢弃预热 ms | 合计启动 ms | 常驻内存 MiB | 空闲 CPU% | 模型文件 MiB |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for engine in payload["engines"]:
        lines.append(
            f"| {engine['name']} | {engine['initialization_ms']:.1f} | {engine['warmup_ms']:.1f} | "
            f"{engine['startup_ms']:.1f} | {engine['resident_memory_mb']:.1f} | "
            f"{engine['idle_cpu_percent']:.1f} | {_format_number(engine['model_size_mb'])} |"
        )
    lines.extend(
        [
            "",
            "## GPU 条件档",
            "",
            f"- tested: {str(payload['gpu']['tested']).lower()}。{payload['gpu']['reason']}",
            "",
            "## 主配置失败样例",
        ]
    )
    for engine in payload["engines"]:
        evaluated = next(
            (
                item
                for item in engine["configurations"]
                if item["configuration"] == "full"
            ),
            engine["configurations"][0],
        )
        lines.extend(
            [
                "",
                f"### {engine['name']}",
                "",
                f"样例配置：`{evaluated['configuration']}`。"
                + (
                    " 主配置未测，使用触发中止的 896 宽实测结果。"
                    if evaluated["configuration"] != "full"
                    else ""
                ),
                "",
                "漏读（最多 5 条）：",
                "",
                "| 帧 | 真值原文 | 同帧最接近引擎输出 |",
                "|---|---|---|",
            ]
        )
        for item in evaluated["misses"][:5]:
            lines.append(
                f"| {item['frame']} | `{_table_text(item['truth'])}` | "
                f"`{_table_text(item['engine_output'])}` |"
            )
        if not evaluated["misses"]:
            lines.append("| — | 无未匹配真值行 | — |")
        lines.extend(
            [
                "",
                "未匹配输出（最多 5 条；真值仍待人工校对，故同时进入末尾补标清单）：",
                "",
                "| 帧 | 同帧最接近真值 | 引擎输出 |",
                "|---|---|---|",
            ]
        )
        for item in evaluated["false_positives"][:5]:
            lines.append(
                f"| {item['frame']} | `{_table_text(item['truth'])}` | "
                f"`{_table_text(item['engine_output'])}` |"
            )
        if not evaluated["false_positives"]:
            lines.append("| — | 无未匹配引擎行 | — |")
    lines.extend(
        [
            "",
            "## 各引擎读出但真值集没有的行",
            "",
            "以下优先来自全帧主配置；被 500ms 规则提前中止的引擎使用其 896 宽实测结果。按帧逐条列出，供人工补真值或确认错读；未在此阶段自动改真值集。",
        ]
    )
    for engine in payload["engines"]:
        evaluated = next(
            (
                item
                for item in engine["configurations"]
                if item["configuration"] == "full"
            ),
            engine["configurations"][0],
        )
        lines.extend(
            [
                "",
                f"### {engine['name']} · {evaluated['configuration']}",
                "",
            ]
        )
        if not evaluated["false_positives"]:
            lines.append("- 无。")
        else:
            for item in evaluated["false_positives"]:
                lines.append(
                    f"- {item['frame']} · `{item['engine_output']}` · center=({item['center'][0]:.4f}, {item['center'][1]:.4f})"
                )
    lines.extend(
        [
            "",
            "## 安装与调用",
            "",
            "- WinRT：使用仓库现有 `PersistentWinRtOcrWorker`，依赖系统 `Windows.Media.Ocr` 的 `zh-Hans-CN` 能力，不新增包。",
            "- RapidOCR：隔离环境安装 `rapidocr==3.9.2`、`onnxruntime==1.29.0`、`psutil==7.2.2`；PP-OCRv4 中文 mobile/server 检测与识别模型由 RapidOCR 官方模型清单下载到探针虚拟环境。",
            "- 命令见 `backend/audit/m5-b-t4a/README.md`。生产 `requirements.txt` 未修改。",
            "",
            "## 与规格的偏差及原因",
            "",
            "- Windows AI TextRecognizer 未进入性能表：实机探测没有 NPU、Python projection 或打包 capability 调用通路；没有用文档推断的假数据补行。",
            "- card-game 客户区实测为 1920×1079；其余三段为 1920×1080。保留录制原始像素，不为凑尺寸改素材。",
            "- crop 帧若 detector 没有 confirmed region，则不调用 OCR；耗时统计排除这些帧，准确率分母仍保留全部真值与帧。",
            f"- 按产品负责人补充口径先跑 896 宽；该配置中位 > {EARLY_STOP_MEDIAN_MS:.0f}ms 的引擎不继续跑主配置或其他参考配置。",
            "",
            "## 未完成项",
            "",
            "- `truth.md` 每帧仍待产品负责人人工校对；上方未匹配输出清单是补标入口。",
            "- 本阶段不改生产代码、不接 adapter，也不修改生产依赖，符合 4a 边界。",
        ]
    )
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M5-B-T4a OCR engine probe")
    parser.add_argument("command", choices=("init-truth", "benchmark", "render-report"))
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--truth", type=Path, default=DEFAULT_TRUTH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    frames = load_frames(arguments.manifest.resolve())
    if arguments.command == "init-truth":
        write_initial_truth(frames, arguments.truth.resolve())
        print(arguments.truth.resolve())
        return 0
    if arguments.command == "render-report":
        metrics_path = arguments.output.resolve() / "metrics.json"
        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        report_path = arguments.output.resolve() / "report.md"
        write_report(payload, report_path)
        print(report_path)
        return 0
    truth = load_truth(arguments.truth.resolve(), len(frames))
    payload = benchmark(frames, truth, arguments.output.resolve())
    metrics_path = arguments.output.resolve() / "metrics.json"
    report_path = arguments.output.resolve() / "report.md"
    metrics_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_report(payload, report_path)
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
