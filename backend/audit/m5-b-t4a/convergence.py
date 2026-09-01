from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time
import tomllib
import unicodedata
from typing import Any, Sequence

import psutil
from PIL import Image


PROBE_VERSION = 2
PROBE_DIRECTORY = Path(__file__).resolve().parent
BACKEND_DIRECTORY = PROBE_DIRECTORY.parents[1]
MANIFEST_PATH = BACKEND_DIRECTORY / "data" / "generic" / "ocr-truth" / "frames.toml"
REFERENCE_PATH = MANIFEST_PATH.with_name("reference-anchors.json")
DEFAULT_OUTPUT = BACKEND_DIRECTORY / "eval-reports" / "m5-b-t4a2"
CATEGORIES = ("hud_numeric", "subtitle_dialogue", "menu_button", "dense_panel")
CANDIDATES = ("T", "H")
THREAD_LEVELS = ("default", "4", "2", "1")
INPUT_PATHS = ("file", "memory")
BASE_DETECTOR_SIZES = ("original", "max1536")
CONDITIONAL_DETECTOR_SIZE = "max1280"
DETECTOR_SIZES = (*BASE_DETECTOR_SIZES, CONDITIONAL_DETECTOR_SIZE)
ROUNDS = 3
WARMUP_FRAMES = 3
MB = 1024 * 1024


def _normalize(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value)
        if not character.isspace()
    ).casefold()


def _percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile of an empty sequence")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[lower])
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower))


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    return {
        "mean": round(statistics.fmean(values), 6),
        "p50": round(statistics.median(values), 6),
        "p90": round(_percentile(values, 0.9), 6),
        "max": round(max(values), 6),
        "samples": [round(value, 6) for value in values],
    }


def _load_frames() -> list[dict[str, Any]]:
    with MANIFEST_PATH.open("rb") as stream:
        manifest = tomllib.load(stream)
    reference = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
    if reference.get("status") != "model-vision adjudicated; not human gold":
        raise ValueError("reference anchor status changed")
    expected_matching = (
        "same-frame NFKC + whitespace removal + casefold exact line match; position not used"
    )
    if reference.get("matching") != expected_matching:
        raise ValueError("reference anchor matching rule changed")
    reference_by_index = {int(item["index"]): item for item in reference["frames"]}
    frames: list[dict[str, Any]] = []
    for index, item in enumerate(manifest["frames"], 1):
        path = (MANIFEST_PATH.parent / item["session"] / item["frame"]).resolve()
        wanted = reference_by_index[index]
        if item["category"] != wanted["category"]:
            raise ValueError(f"category mismatch at frame {index}")
        with Image.open(path) as image:
            width, height = image.size
            image_format = image.format
        frames.append(
            {
                "index": index,
                "category": item["category"],
                "path": path,
                "relative_path": str(path.relative_to(BACKEND_DIRECTORY)),
                "width": width,
                "height": height,
                "format": image_format,
                "truth": list(wanted["lines"]),
            }
        )
    if len(frames) != 40 or len(reference_by_index) != 40:
        raise ValueError("M5-B-T4a2 requires exactly 40 frames")
    anchor_count = sum(len(frame["truth"]) for frame in frames)
    if anchor_count != 324:
        raise ValueError(f"reference anchor count changed: {anchor_count} != 324")
    return frames


def _score_lines(truth_lines: Sequence[str], output_lines: Sequence[str]) -> dict[str, Any]:
    truth = [_normalize(value) for value in truth_lines]
    output = [_normalize(value) for value in output_lines]
    exact = sum((Counter(truth) & Counter(output)).values())
    numeric_truth = [value for value in truth if any(character.isdigit() for character in value)]
    numeric_output_counts = Counter(output)
    numeric_exact = 0
    for value in numeric_truth:
        if numeric_output_counts[value] > 0:
            numeric_exact += 1
            numeric_output_counts[value] -= 1
    return {
        "truth_lines": len(truth),
        "output_lines": len(output),
        "exact_matches": exact,
        "numeric_truth_lines": len(numeric_truth),
        "numeric_exact_matches": numeric_exact,
    }


def _aggregate_scores(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    truth_lines = sum(int(item["truth_lines"]) for item in samples)
    exact_matches = sum(int(item["exact_matches"]) for item in samples)
    numeric_truth = sum(int(item["numeric_truth_lines"]) for item in samples)
    numeric_exact = sum(int(item["numeric_exact_matches"]) for item in samples)
    return {
        "truth_lines": truth_lines,
        "output_lines": sum(int(item["output_lines"]) for item in samples),
        "exact_matches": exact_matches,
        "strict_recall": exact_matches / truth_lines,
        "numeric_truth_lines": numeric_truth,
        "numeric_exact_matches": numeric_exact,
        "numeric_strict_recall": numeric_exact / numeric_truth,
    }


def _cpu_seconds(process: psutil.Process) -> float:
    processes = [process]
    try:
        processes.extend(process.children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    total = 0.0
    for item in processes:
        try:
            cpu = item.cpu_times()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        total += float(cpu.user + cpu.system)
    return total


def _memory_bgr(path: Path) -> Any:
    import cv2
    import numpy as np

    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _configuration_id(
    candidate: str, threads: str, input_path: str, detector_size: str
) -> str:
    return f"{candidate}-threads-{threads}-{input_path}-{detector_size}"


def _engine_params(
    candidate: str, threads: str, detector_size: str
) -> dict[str, Any]:
    from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion

    if candidate not in CANDIDATES:
        raise ValueError(f"unknown candidate: {candidate}")
    detector_model = ModelType.TINY if candidate == "T" else ModelType.SMALL
    params: dict[str, Any] = {
        "Global.use_cls": False,
        "Global.log_level": "error",
        "Det.engine_type": EngineType.OPENVINO,
        "Det.lang_type": LangDet.CH,
        "Det.model_type": detector_model,
        "Det.ocr_version": OCRVersion.PPOCRV6,
        "Rec.engine_type": EngineType.OPENVINO,
        "Rec.lang_type": LangRec.CH,
        "Rec.model_type": ModelType.TINY,
        "Rec.ocr_version": OCRVersion.PPOCRV6,
    }
    if threads != "default":
        params["EngineConfig.openvino.inference_num_threads"] = int(threads)
    if detector_size in {"max1536", "max1280"}:
        params["Det.limit_type"] = "max"
        params["Det.limit_side_len"] = int(detector_size.removeprefix("max"))
    elif detector_size != "original":
        raise ValueError(f"unknown detector size: {detector_size}")
    return params


def _recognize_frame(
    engine: Any,
    frame: dict[str, Any],
    input_path: str,
    process: psutil.Process,
) -> dict[str, Any]:
    image: Any = frame["path"]
    if input_path == "memory":
        image = _memory_bgr(frame["path"])
    elif input_path != "file":
        raise ValueError(f"unknown input path: {input_path}")

    cpu_before = _cpu_seconds(process)
    wall_started = time.perf_counter()
    result = engine(image, use_cls=False)
    ocr_finished = time.perf_counter()

    boxes = result.boxes if result.boxes is not None else ()
    texts = result.txts if result.txts is not None else ()
    confidences = result.scores if result.scores is not None else ()
    width = int(frame["width"])
    height = int(frame["height"])
    lines: list[dict[str, Any]] = []
    for box, text, confidence in zip(boxes, texts, confidences, strict=True):
        quad = [[float(point[0]) / width, float(point[1]) / height] for point in box]
        xs = [point[0] for point in quad]
        ys = [point[1] for point in quad]
        lines.append(
            {
                "text": str(text),
                "confidence": round(float(confidence), 6),
                "quad": [[round(x, 6), round(y, 6)] for x, y in quad],
                "bbox": {
                    "x": round(min(xs), 6),
                    "y": round(min(ys), 6),
                    "width": round(max(xs) - min(xs), 6),
                    "height": round(max(ys) - min(ys), 6),
                },
            }
        )
    postprocess_finished = time.perf_counter()
    cpu_after = _cpu_seconds(process)
    wall_finished = time.perf_counter()

    wall_seconds = wall_finished - wall_started
    cpu_core_seconds = max(0.0, cpu_after - cpu_before)
    elapse = result.elapse_list or (None, None, None)
    det_ms = float(elapse[0] or 0.0) * 1000.0
    rec_ms = float(elapse[2] or 0.0) * 1000.0
    postprocess_ms = (postprocess_finished - ocr_finished) * 1000.0
    wall_ms = wall_seconds * 1000.0
    framework_overhead_ms = wall_ms - det_ms - rec_ms - postprocess_ms
    score = _score_lines(frame["truth"], [line["text"] for line in lines])
    memory = process.memory_info()
    return {
        "index": frame["index"],
        "category": frame["category"],
        "path": frame["relative_path"],
        "width": width,
        "height": height,
        "format": frame["format"],
        "wall_ms": round(wall_ms, 6),
        "det_ms": round(det_ms, 6),
        "rec_ms": round(rec_ms, 6),
        "postprocess_ms": round(postprocess_ms, 6),
        "framework_overhead_ms": round(framework_overhead_ms, 6),
        "cpu_core_seconds": round(cpu_core_seconds, 6),
        "concurrent_cores": round(cpu_core_seconds / wall_seconds, 6),
        "rss_mb": round(float(memory.rss) / MB, 6),
        "lines": lines,
        **score,
    }


def run_worker(
    candidate: str,
    threads: str,
    input_path: str,
    detector_size: str,
    round_number: int,
    result_path: Path,
) -> dict[str, Any]:
    from rapidocr import RapidOCR

    frames = _load_frames()
    process = psutil.Process(os.getpid())
    initialized_at = time.perf_counter()
    engine = RapidOCR(params=_engine_params(candidate, threads, detector_size))
    initialization_ms = (time.perf_counter() - initialized_at) * 1000.0

    warmups: list[float] = []
    for frame in frames[:WARMUP_FRAMES]:
        image: Any = frame["path"]
        if input_path == "memory":
            image = _memory_bgr(frame["path"])
        started = time.perf_counter()
        engine(image, use_cls=False)
        warmups.append((time.perf_counter() - started) * 1000.0)

    stable_rss_before_mb = float(process.memory_info().rss) / MB
    samples: list[dict[str, Any]] = []
    for position, frame in enumerate(frames, 1):
        samples.append(_recognize_frame(engine, frame, input_path, process))
        if position % 10 == 0:
            print(
                f"worker {candidate}/{threads}/{input_path}/{detector_size} "
                f"round {round_number}: {position}/40",
                flush=True,
            )
    memory = process.memory_info()
    stable_rss_samples = [stable_rss_before_mb, *[float(item["rss_mb"]) for item in samples]]
    payload = {
        "probe_version": PROBE_VERSION,
        "task_id": "M5-B-T4a2",
        "candidate": candidate,
        "candidate_description": (
            "PP-OCRv6 tiny detector + tiny recognizer"
            if candidate == "T"
            else "PP-OCRv6 small detector + tiny recognizer"
        ),
        "threads": threads,
        "input_path": input_path,
        "detector_size": detector_size,
        "round": round_number,
        "configuration_id": _configuration_id(candidate, threads, input_path, detector_size),
        "frame_count": len(samples),
        "warmup_frames": WARMUP_FRAMES,
        "initialization_ms": round(initialization_ms, 6),
        "warmup_ms": [round(value, 6) for value in warmups],
        "startup_ms": round(initialization_ms + warmups[0], 6),
        "steady_rss_mb": round(statistics.median(stable_rss_samples), 6),
        "peak_rss_mb": round(float(memory.peak_wset) / MB, 6),
        "samples": samples,
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def _all_configurations(
    detector_sizes: Sequence[str] = BASE_DETECTOR_SIZES,
) -> list[tuple[str, str, str, str]]:
    return [
        (candidate, threads, input_path, detector_size)
        for candidate in CANDIDATES
        for threads in THREAD_LEVELS
        for input_path in INPUT_PATHS
        for detector_size in detector_sizes
    ]


def _interleaved_orders() -> list[list[tuple[str, str, str, str]]]:
    baseline = [(candidate, "default", "file", "original") for candidate in CANDIDATES]
    remaining = [item for item in _all_configurations() if item not in baseline]
    random.Random(50402).shuffle(remaining)
    first = [*baseline, *remaining]
    offsets = (0, 11, 23)
    return [first[offset:] + first[:offset] for offset in offsets]


def _conditional_interleaved_orders() -> list[list[tuple[str, str, str, str]]]:
    configurations = _all_configurations((CONDITIONAL_DETECTOR_SIZE,))
    random.Random(50403).shuffle(configurations)
    offsets = (0, 5, 11)
    return [
        configurations[offset:] + configurations[:offset] for offset in offsets
    ]


def _valid_existing_run(path: Path, expected: tuple[str, str, str, str], round_number: int) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    candidate, threads, input_path, detector_size = expected
    return (
        payload.get("probe_version") == PROBE_VERSION
        and payload.get("candidate") == candidate
        and payload.get("threads") == threads
        and payload.get("input_path") == input_path
        and payload.get("detector_size") == detector_size
        and payload.get("round") == round_number
        and payload.get("frame_count") == 40
    )


def _aggregate_configuration(runs: Sequence[dict[str, Any]]) -> dict[str, Any]:
    first = runs[0]
    samples = [sample for run in runs for sample in run["samples"]]
    scores = _aggregate_scores(samples)
    categories = {
        category: _aggregate_scores(
            [sample for sample in samples if sample["category"] == category]
        )
        for category in CATEGORIES
    }
    wall = [float(sample["wall_ms"]) for sample in samples]
    cpu = [float(sample["cpu_core_seconds"]) for sample in samples]
    concurrent = [float(sample["concurrent_cores"]) for sample in samples]
    round_p50 = [
        statistics.median(float(sample["wall_ms"]) for sample in run["samples"])
        for run in runs
    ]
    return {
        "candidate": first["candidate"],
        "candidate_description": first["candidate_description"],
        "threads": first["threads"],
        "input_path": first["input_path"],
        "detector_size": first["detector_size"],
        "configuration_id": first["configuration_id"],
        "rounds": len(runs),
        "formal_calls": len(samples),
        "overall": scores,
        "categories": categories,
        "timing_ms": {
            "wall": _distribution(wall),
            "det": _distribution([float(sample["det_ms"]) for sample in samples]),
            "rec": _distribution([float(sample["rec_ms"]) for sample in samples]),
            "postprocess": _distribution(
                [float(sample["postprocess_ms"]) for sample in samples]
            ),
            "framework_overhead": _distribution(
                [float(sample["framework_overhead_ms"]) for sample in samples]
            ),
        },
        "cpu_core_seconds": _distribution(cpu),
        "peak_concurrent_cores": round(max(concurrent), 6),
        "rss_mb": {
            "steady": round(statistics.median(float(run["steady_rss_mb"]) for run in runs), 6),
            "peak": round(max(float(run["peak_rss_mb"]) for run in runs), 6),
        },
        "startup_ms": _distribution([float(run["startup_ms"]) for run in runs]),
        "initialization_ms": _distribution(
            [float(run["initialization_ms"]) for run in runs]
        ),
        "warmup_ms": _distribution(
            [float(value) for run in runs for value in run["warmup_ms"]]
        ),
        "round_p50_ms": [round(value, 6) for value in round_p50],
        "round_p50_spread_ms": round(max(round_p50) - min(round_p50), 6),
    }


def _eligibility(row: dict[str, Any]) -> tuple[bool, bool]:
    follows = float(row["timing_ms"]["wall"]["p90"]) < 500.0
    resources = (
        float(row["cpu_core_seconds"]["mean"]) <= 0.5
        and float(row["peak_concurrent_cores"]) <= 2.0
        and float(row["rss_mb"]["steady"]) <= 600.0
    )
    return follows, resources


def _mark_criteria(rows: list[dict[str, Any]]) -> None:
    eligible = [row for row in rows if all(_eligibility(row))]
    highest_recall = (
        max(float(row["overall"]["strict_recall"]) for row in eligible)
        if eligible
        else None
    )
    for row in rows:
        follows, resources = _eligibility(row)
        recall_highest = (
            highest_recall is not None
            and math.isclose(
                float(row["overall"]["strict_recall"]), highest_recall, abs_tol=1e-12
            )
        )
        row["criteria"] = {
            "p90_under_500ms": follows,
            "resources": resources,
            "average_cpu_at_most_half_core": float(row["cpu_core_seconds"]["mean"]) <= 0.5,
            "peak_concurrency_at_most_two_cores": float(row["peak_concurrent_cores"]) <= 2.0,
            "steady_rss_at_most_600mb": float(row["rss_mb"]["steady"]) <= 600.0,
            "highest_recall_among_time_and_resource_eligible": recall_highest,
            "all_three": follows and resources and recall_highest,
        }


def _control_row(rows: Sequence[dict[str, Any]], candidate: str) -> dict[str, Any]:
    candidates = [row for row in rows if row["candidate"] == candidate]
    eligible = [row for row in candidates if all(_eligibility(row))]
    pool = eligible or [row for row in candidates if _eligibility(row)[0]] or candidates
    return max(
        pool,
        key=lambda row: (
            float(row["overall"]["strict_recall"]),
            -float(row["cpu_core_seconds"]["mean"]),
            -float(row["timing_ms"]["wall"]["p50"]),
        ),
    )


def aggregate_runs(
    output: Path,
    orders: Sequence[Sequence[tuple[str, str, str, str]]],
    conditional_orders: Sequence[Sequence[tuple[str, str, str, str]]],
) -> dict[str, Any]:
    by_configuration: dict[str, list[dict[str, Any]]] = {}
    for order_group in (orders, conditional_orders):
        for round_number, order in enumerate(order_group, 1):
            for candidate, threads, input_path, detector_size in order:
                config_id = _configuration_id(candidate, threads, input_path, detector_size)
                path = output / "runs" / f"round-{round_number:02d}-{config_id}.json"
                run = json.loads(path.read_text(encoding="utf-8"))
                by_configuration.setdefault(config_id, []).append(run)
    rows = [_aggregate_configuration(by_configuration[config_id]) for config_id in sorted(by_configuration)]
    _mark_criteria(rows)
    controls = {
        candidate: _control_row(rows, candidate)["configuration_id"] for candidate in CANDIDATES
    }
    return {
        "schema_version": 1,
        "task_id": "M5-B-T4a2",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "truth": {
            "path": str(REFERENCE_PATH.relative_to(BACKEND_DIRECTORY)),
            "status": "independent model-vision reference anchors; not human gold",
            "anchor_count": 324,
            "matching": "same-frame NFKC + whitespace removal + casefold exact whole-line",
            "precision": None,
            "precision_reason": "The independent reference is intentionally non-exhaustive.",
        },
        "machine": {
            "physical_cores": psutil.cpu_count(logical=False),
            "logical_cores": psutil.cpu_count(logical=True),
            "platform": sys.platform,
        },
        "round_count": ROUNDS,
        "warmup_frames_per_run": WARMUP_FRAMES,
        "interleaved_orders": [
            [
                _configuration_id(candidate, threads, input_path, detector_size)
                for candidate, threads, input_path, detector_size in order
            ]
            for order in orders
        ],
        "conditional_1280_reason": (
            "max1536 produced exactly the same strict recall as original for both "
            "candidates across every thread and input-path cell, so the specification's "
            "explicit recall-headroom condition for one 1280 tier was met."
        ),
        "conditional_1280_interleaved_orders": [
            [
                _configuration_id(candidate, threads, input_path, detector_size)
                for candidate, threads, input_path, detector_size in order
            ]
            for order in conditional_orders
        ],
        "control_configuration_by_candidate": controls,
        "configurations": rows,
    }


def _pct(value: float) -> str:
    return f"{value:.1%}"


def _yes(value: bool | None) -> str:
    if value is None:
        return "—"
    return "是" if value else "否"


def _delta(value: float, baseline: float, suffix: str = "") -> str:
    return f"{value - baseline:+.3f}{suffix}"


def _find_row(
    rows: Sequence[dict[str, Any]],
    candidate: str,
    threads: str,
    input_path: str,
    detector_size: str,
) -> dict[str, Any]:
    return next(
        row
        for row in rows
        if row["candidate"] == candidate
        and row["threads"] == threads
        and row["input_path"] == input_path
        and row["detector_size"] == detector_size
    )


def _single_variable_table(
    rows: Sequence[dict[str, Any]], candidate: str, variable: str, control: dict[str, Any]
) -> list[str]:
    levels = {
        "threads": THREAD_LEVELS,
        "input_path": INPUT_PATHS,
        "detector_size": DETECTOR_SIZES,
    }[variable]
    baseline_level = {"threads": "default", "input_path": "file", "detector_size": "original"}[
        variable
    ]
    selected: list[dict[str, Any]] = []
    for level in levels:
        values = {
            "threads": str(control["threads"]),
            "input_path": str(control["input_path"]),
            "detector_size": str(control["detector_size"]),
        }
        values[variable] = level
        selected.append(_find_row(rows, candidate, **values))
    baseline = next(row for row in selected if row[variable] == baseline_level)
    output = [
        f"固定项：threads={control['threads']}、input={control['input_path']}、detector={control['detector_size']}；仅 `{variable}` 改变。差值相对 `{baseline_level}`。",
        "",
        "| 档位 | 严格召回 | P50/P90 ms | CPU mean/P50/P90 核秒 | 峰值并发核 | RSS peak MB | ΔP50 | ΔCPU mean | Δ召回 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in selected:
        recall = float(row["overall"]["strict_recall"])
        base_recall = float(baseline["overall"]["strict_recall"])
        wall = row["timing_ms"]["wall"]
        cpu = row["cpu_core_seconds"]
        output.append(
            f"| {row[variable]} | {_pct(recall)} | {wall['p50']:.1f}/{wall['p90']:.1f} | "
            f"{cpu['mean']:.3f}/{cpu['p50']:.3f}/{cpu['p90']:.3f} | "
            f"{row['peak_concurrent_cores']:.2f} | {row['rss_mb']['peak']:.1f} | "
            f"{_delta(float(wall['p50']), float(baseline['timing_ms']['wall']['p50']), ' ms')} | "
            f"{_delta(float(cpu['mean']), float(baseline['cpu_core_seconds']['mean']), ' 核')} | "
            f"{_delta((recall - base_recall) * 100.0, 0.0, ' pp')} |"
        )
    return output


def write_report(payload: dict[str, Any], destination: Path) -> None:
    rows = payload["configurations"]
    lines = [
        "# M5-B-T4a2 OCR 引擎收口实测",
        "",
        "> 40 张原始 1080p/1079p 帧；324 条独立视觉参考 anchor；该参考非人工穷举 gold，只用于相对召回比较，不计算正式 precision。",
        "",
        "## 结论边界",
        "",
        "- 本轮只完成 4a-2；没有进入 4b、没有修改生产 OCR 或 adapter。",
        "- 判据：P90 < 500ms；平均 CPU ≤0.5 核、单次峰值并发 ≤2 核、RSS peak ≤600MB；在前两项内召回最高。",
        "- CPU 核秒来自每次 OCR 调用前后的进程 CPU 时间差，不由墙钟耗时推算。平均值按 1Hz 调用等价为平均占用核数。",
        "- 每个配置独立进程、3 帧预热丢弃；基础 32 个配置交错运行 3 轮，每行 120 次正式 OCR。",
        "- file 路径把 JPEG 解码计入调用；memory 路径在计时前用同一 JPEG 解码成 BGR numpy 数组，每次只保留当前帧，模拟 WGC 内存位图而不人为把 40 帧常驻内存。",
        "- `original` 保持 RapidOCR 原始 detector 行为；`max1536` 只设置 detector 的 max side，recognizer 仍从原始分辨率图裁字。",
        "- 条件追加 `max1280`：max1536 在两候选的所有线程/输入组合中均与原图召回完全相同，满足规格所写的“1536 仍有明显召回余量”条件；1280 的 16 个配置另行交错 3 轮。",
        "",
        "## 主表",
        "",
        "| 候选 | 线程 | 输入 | detector | 总召回 | 数字 | HUD | 字幕 | 菜单 | 密集 | wall P50/P90/max ms | det/rec/post/框架中位 ms | CPU mean/P50/P90 核秒 | 峰值并发核 | RSS steady/peak MB | 启动P50 ms |",
        "|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        overall = row["overall"]
        categories = row["categories"]
        wall = row["timing_ms"]["wall"]
        timing = row["timing_ms"]
        cpu = row["cpu_core_seconds"]
        lines.append(
            f"| {row['candidate']} | {row['threads']} | {row['input_path']} | {row['detector_size']} | "
            f"{_pct(overall['strict_recall'])} | {_pct(overall['numeric_strict_recall'])} | "
            f"{_pct(categories['hud_numeric']['strict_recall'])} | "
            f"{_pct(categories['subtitle_dialogue']['strict_recall'])} | "
            f"{_pct(categories['menu_button']['strict_recall'])} | "
            f"{_pct(categories['dense_panel']['strict_recall'])} | "
            f"{wall['p50']:.1f}/{wall['p90']:.1f}/{wall['max']:.1f} | "
            f"{timing['det']['p50']:.1f}/{timing['rec']['p50']:.1f}/"
            f"{timing['postprocess']['p50']:.3f}/{timing['framework_overhead']['p50']:.1f} | "
            f"{cpu['mean']:.3f}/{cpu['p50']:.3f}/{cpu['p90']:.3f} | "
            f"{row['peak_concurrent_cores']:.2f} | "
            f"{row['rss_mb']['steady']:.1f}/{row['rss_mb']['peak']:.1f} | "
            f"{row['startup_ms']['p50']:.1f} |"
        )

    lines.extend(["", "## 单变量：线程数", ""])
    for candidate in CANDIDATES:
        control = next(
            row
            for row in rows
            if row["configuration_id"] == payload["control_configuration_by_candidate"][candidate]
        )
        lines.extend([f"### 候选 {candidate}", ""])
        lines.extend(_single_variable_table(rows, candidate, "threads", control))
        lines.append("")

    lines.extend(["## 单变量：输入路径", ""])
    for candidate in CANDIDATES:
        control = next(
            row
            for row in rows
            if row["configuration_id"] == payload["control_configuration_by_candidate"][candidate]
        )
        lines.extend([f"### 候选 {candidate}", ""])
        lines.extend(_single_variable_table(rows, candidate, "input_path", control))
        lines.append("")

    lines.extend(["## 单变量：detector 尺寸", ""])
    for candidate in CANDIDATES:
        control = next(
            row
            for row in rows
            if row["configuration_id"] == payload["control_configuration_by_candidate"][candidate]
        )
        lines.extend([f"### 候选 {candidate}", ""])
        lines.extend(_single_variable_table(rows, candidate, "detector_size", control))
        lines.append("")

    lines.extend(
        [
            "## 达标表",
            "",
            "第三条只在同时满足节奏与资源的配置中比较召回；若无资源合格项则显示“—”。本表不推荐选型。",
            "",
            "| 配置 | P90<500 | CPU均值≤0.5 | 峰值≤2 | 稳态RSS≤600 | 资源合格 | 合格组召回最高 | 三条同时满足 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rows:
        criteria = row["criteria"]
        lines.append(
            f"| {row['configuration_id']} | {_yes(criteria['p90_under_500ms'])} | "
            f"{_yes(criteria['average_cpu_at_most_half_core'])} | "
            f"{_yes(criteria['peak_concurrency_at_most_two_cores'])} | "
            f"{_yes(criteria['steady_rss_at_most_600mb'])} | {_yes(criteria['resources'])} | "
            f"{_yes(criteria['highest_recall_among_time_and_resource_eligible'] if any(all(_eligibility(item)) for item in rows) else None)} | "
            f"{_yes(criteria['all_three'])} |"
        )

    lines.extend(
        [
            "",
            "## 噪声核对",
            "",
            "同配置三轮 P50 的离散度定义为 max−min。候选间差异按相同 threads/input/detector 的 T 与 H 汇总 P50 之差计算；若任一侧轮间离散度大于候选差异，该组标为“本轮不可分辨”。",
            "",
            "| 线程/输入/detector | T 三轮P50 | H 三轮P50 | T/H离散度 ms | 候选差异 ms | 判定 |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for threads in THREAD_LEVELS:
        for input_path in INPUT_PATHS:
            for detector_size in DETECTOR_SIZES:
                t_row = _find_row(rows, "T", threads, input_path, detector_size)
                h_row = _find_row(rows, "H", threads, input_path, detector_size)
                difference = abs(
                    float(t_row["timing_ms"]["wall"]["p50"])
                    - float(h_row["timing_ms"]["wall"]["p50"])
                )
                indistinguishable = max(
                    float(t_row["round_p50_spread_ms"]),
                    float(h_row["round_p50_spread_ms"]),
                ) > difference
                verdict = "该组对比在本轮不可分辨" if indistinguishable else "差异大于轮间离散"
                lines.append(
                    f"| {threads}/{input_path}/{detector_size} | "
                    f"{' / '.join(f'{value:.1f}' for value in t_row['round_p50_ms'])} | "
                    f"{' / '.join(f'{value:.1f}' for value in h_row['round_p50_ms'])} | "
                    f"{t_row['round_p50_spread_ms']:.1f}/{h_row['round_p50_spread_ms']:.1f} | "
                    f"{difference:.1f} | {verdict} |"
                )

    lines.extend(["", "## 交错运行顺序", ""])
    for index, order in enumerate(payload["interleaved_orders"], 1):
        lines.append(f"- 基础矩阵第 {index} 轮：" + " → ".join(order))
    for index, order in enumerate(payload["conditional_1280_interleaved_orders"], 1):
        lines.append(f"- 条件 1280 矩阵第 {index} 轮：" + " → ".join(order))

    passed = [row["configuration_id"] for row in rows if row["criteria"]["all_three"]]
    lines.extend(
        [
            "",
            "## 一句话事实",
            "",
            (
                "同时满足三条判据的配置：" + "、".join(passed) + "。"
                if passed
                else "没有配置同时满足三条判据。"
            ),
            "",
            "## 与规格的偏差及原因",
            "",
            "- 按规格的显式例外追加了 1280：1536 的召回在基础矩阵中与原图逐格相同。未测试 v4、反向混合、stream、rec_batch、INT8/NNCF 或 GPU。",
            "",
            "## 未完成项",
            "",
            "- 未进入 4b；未测游戏运行中的帧时间与 1% low。",
            "- 324 条 anchor 不是人工穷举 gold，因此没有正式 precision，不能据此最终定型引擎。",
        ]
    )
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_benchmark(output: Path) -> Path:
    _load_frames()
    output.mkdir(parents=True, exist_ok=True)
    orders = _interleaved_orders()
    conditional_orders = _conditional_interleaved_orders()
    all_order_groups = (orders, conditional_orders)
    total = sum(len(group) * len(group[0]) for group in all_order_groups)
    position = 0
    for order_group in all_order_groups:
        for round_number, order in enumerate(order_group, 1):
            for configuration in order:
                position += 1
                candidate, threads, input_path, detector_size = configuration
                config_id = _configuration_id(candidate, threads, input_path, detector_size)
                result_path = output / "runs" / f"round-{round_number:02d}-{config_id}.json"
                if _valid_existing_run(result_path, configuration, round_number):
                    print(f"[{position}/{total}] reuse {result_path.name}", flush=True)
                    continue
                print(f"[{position}/{total}] run {result_path.name}", flush=True)
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "worker",
                        "--candidate",
                        candidate,
                        "--threads",
                        threads,
                        "--input-path",
                        input_path,
                        "--detector-size",
                        detector_size,
                        "--round",
                        str(round_number),
                        "--result-path",
                        str(result_path),
                    ],
                    check=True,
                )
    payload = aggregate_runs(output, orders, conditional_orders)
    metrics_path = output / "metrics.json"
    report_path = output / "report.md"
    metrics_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_report(payload, report_path)
    return report_path


def render_report(output: Path) -> Path:
    metrics_path = output / "metrics.json"
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    report_path = output / "report.md"
    write_report(payload, report_path)
    return report_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M5-B-T4a2 OCR convergence probe")
    parser.add_argument("command", choices=("benchmark", "worker", "render-report"))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--candidate", choices=CANDIDATES)
    parser.add_argument("--threads", choices=THREAD_LEVELS)
    parser.add_argument("--input-path", choices=INPUT_PATHS)
    parser.add_argument("--detector-size", choices=DETECTOR_SIZES)
    parser.add_argument("--round", type=int)
    parser.add_argument("--result-path", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "benchmark":
        print(run_benchmark(arguments.output.resolve()))
        return 0
    if arguments.command == "render-report":
        print(render_report(arguments.output.resolve()))
        return 0
    required = {
        "candidate": arguments.candidate,
        "threads": arguments.threads,
        "input_path": arguments.input_path,
        "detector_size": arguments.detector_size,
        "round": arguments.round,
        "result_path": arguments.result_path,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ValueError(f"worker arguments missing: {', '.join(missing)}")
    run_worker(
        arguments.candidate,
        arguments.threads,
        arguments.input_path,
        arguments.detector_size,
        arguments.round,
        arguments.result_path.resolve(),
    )
    print(arguments.result_path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
