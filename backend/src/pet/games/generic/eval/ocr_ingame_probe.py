"""Measure detector-paced OCR and game frame times during a real play segment."""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import subprocess
import time
from typing import Any, Sequence

import numpy as np
import psutil

from pet.core.capture import AdaptiveFrameSelector, WindowsGraphicsCaptureBackend
from pet.core.ocr_rapid import RapidOcrEngine, set_current_thread_below_normal


BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]
DEFAULT_OUTPUT = BACKEND_DIRECTORY / "eval-reports" / "m5-b-t4b-ingame"
PRESENTMON_CANDIDATES = (
    Path(r"C:\Program Files\AMD\CNext\CNext\PresentMon-x64.exe"),
    Path(r"C:\Program Files\NVIDIA Corporation\FrameViewSDK\bin\PresentMon_x64.exe"),
)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _presentmon() -> Path:
    for candidate in PRESENTMON_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("PresentMon executable was not found")


def _worker(engine: RapidOcrEngine, image: np.ndarray) -> dict[str, Any]:
    set_current_thread_below_normal()
    result = engine.recognize(image)
    return {
        "elapsed_ms": result.duration_ms,
        "det_ms": result.det_ms,
        "rec_ms": result.rec_ms,
        "cpu_core_seconds": result.cpu_core_seconds,
        "line_count": len(result.lines),
    }


def _read_frame_times(path: Path) -> list[float]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    values = []
    for row in rows:
        raw = row.get("MsBetweenPresents") or row.get("msBetweenPresents")
        if raw:
            value = float(raw)
            if value > 0:
                values.append(value)
    return values


def run_segment(title: str | None, duration: int, threads: int, output: Path) -> None:
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    backend = WindowsGraphicsCaptureBackend(title, capture_cursor=False)
    selector = AdaptiveFrameSelector(region_sparsity_max=0.50)
    engine = None
    executor = None
    if threads:
        engine = RapidOcrEngine(
            model_dir=BACKEND_DIRECTORY / "models" / "ocr",
            num_threads=threads,
            det_limit_side_len=1280,
        )
        engine.start()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingame-ocr")
    presentmon_csv = output / "presentmon.csv"
    command = [
        str(_presentmon()),
        "--process_name",
        backend.target.process_name,
        "--output_file",
        str(presentmon_csv),
        "--timed",
        str(duration),
        "--terminate_after_timed",
        "--v1_metrics",
        "--no_console_stats",
        "--exclude_dropped",
        "--session_name",
        f"M5BT4b{threads}{int(time.time())}",
    ]
    monitor = subprocess.Popen(command)
    process = psutil.Process()
    psutil.cpu_percent(interval=None)
    started = time.perf_counter()
    future: Future[dict[str, Any]] | None = None
    calls: list[dict[str, Any]] = []
    selected = 0
    skipped = 0
    late = 0
    heartbeat = 0
    system_cpu: list[float] = []
    rss: list[float] = []
    try:
        while time.perf_counter() - started < duration:
            tick = time.perf_counter()
            if future is not None and future.done():
                calls.append(future.result())
                future = None
            frame = backend.capture_frame()
            if frame is not None:
                observation = selector.observe(frame.bitmap, frame.metadata.monotonic_seconds)
                if observation.decision.should_save:
                    selected += 1
                    heartbeat += int(observation.decision.forced)
                    if engine is not None and executor is not None:
                        if future is None:
                            pixels = np.asarray(frame.bitmap, dtype=np.uint8)
                            image = pixels[:, :, :3][:, :, ::-1].copy()
                            future = executor.submit(_worker, engine, image)
                        else:
                            late += 1
                else:
                    skipped += 1
                frame.bitmap.close()
            system_cpu.append(psutil.cpu_percent(interval=None))
            rss.append(process.memory_info().rss / 1024**2)
            time.sleep(max(0.0, 1.0 - (time.perf_counter() - tick)))
        if future is not None:
            calls.append(future.result())
    finally:
        monitor.wait(timeout=max(30, duration + 30))
        backend.close()
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
        if engine is not None:
            engine.close()
    frame_times = _read_frame_times(presentmon_csv)
    slow_count = max(1, round(len(frame_times) * 0.01)) if frame_times else 0
    slowest = sorted(frame_times, reverse=True)[:slow_count]
    elapsed_values = [item["elapsed_ms"] for item in calls]
    cpu_values = [item["cpu_core_seconds"] for item in calls if item["cpu_core_seconds"] is not None]
    payload = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "title": backend.target.title,
        "process_name": backend.target.process_name,
        "duration_seconds": duration,
        "num_threads": threads,
        "frame_time_collection": "PresentMon v1 metrics, displayed frames, MsBetweenPresents",
        "frame_samples": len(frame_times),
        "frame_time_ms": {
            "mean": statistics.mean(frame_times) if frame_times else None,
            "p99": _percentile(frame_times, 0.99),
            "one_percent_low_fps": 1000.0 / statistics.mean(slowest) if slowest else None,
        },
        "detector": {"selected": selected, "skipped": skipped, "heartbeat": heartbeat},
        "ocr": {
            "completed": len(calls),
            "late": late,
            "p50_ms": statistics.median(elapsed_values) if elapsed_values else None,
            "p90_ms": _percentile(elapsed_values, 0.9),
            "cpu_core_seconds_mean": statistics.mean(cpu_values) if cpu_values else None,
            "cpu_core_seconds_p50": statistics.median(cpu_values) if cpu_values else None,
            "cpu_core_seconds_p90": _percentile(cpu_values, 0.9),
            "rss_steady_median_mb": statistics.median(rss) if rss else None,
            "rss_peak_mb": max(rss) if rss else None,
        },
        "system_cpu_percent": {
            "mean": statistics.mean(system_cpu) if system_cpu else None,
            "p90": _percentile(system_cpu, 0.9),
        },
        "calls": calls,
    }
    (output / "segment.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_report(root: Path) -> None:
    segments = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*/segment.json"))]
    by_threads = {item["num_threads"]: item for item in segments}
    lines = [
        "# M5-B-T4b 游戏中资源实测",
        "",
        "| OCR线程 | 时长s | 帧样本 | 帧时间均值/P99 ms | 1% low fps | 系统CPU均值/P90 | OCR P50/P90 ms | CPU核秒均值/P50/P90 | RSS稳态/峰值 MiB | OCR完成/late |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in segments:
        frame = item["frame_time_ms"]
        ocr = item["ocr"]
        cpu = item["system_cpu_percent"]
        def value(number: float | None, digits: int = 1) -> str:
            return "—" if number is None else f"{number:.{digits}f}"
        lines.append(
            f"| {item['num_threads']} | {item['duration_seconds']} | {item['frame_samples']} | {value(frame['mean'])}/{value(frame['p99'])} | "
            f"{value(frame['one_percent_low_fps'])} | {value(cpu['mean'])}/{value(cpu['p90'])} | {value(ocr['p50_ms'])}/{value(ocr['p90_ms'])} | "
            f"{value(ocr['cpu_core_seconds_mean'],3)}/{value(ocr['cpu_core_seconds_p50'],3)}/{value(ocr['cpu_core_seconds_p90'],3)} | "
            f"{value(ocr['rss_steady_median_mb'])}/{value(ocr['rss_peak_mb'])} | {ocr['completed']}/{ocr['late']} |"
        )
    baseline = by_threads.get(0)
    default = by_threads.get(2)
    if baseline is not None and default is not None:
        base_frame = baseline["frame_time_ms"]
        default_frame = default["frame_time_ms"]
        lines.extend(
            [
                "",
                "## OCR 关闭与默认 2 线程的实测差值",
                "",
                f"- 帧时间均值：{default_frame['mean'] - base_frame['mean']:+.3f} ms；P99：{default_frame['p99'] - base_frame['p99']:+.3f} ms；1% low：{default_frame['one_percent_low_fps'] - base_frame['one_percent_low_fps']:+.3f} fps。",
                f"- 系统 CPU 均值：{default['system_cpu_percent']['mean'] - baseline['system_cpu_percent']['mean']:+.3f} 个百分点；进程 RSS 稳态中位：{default['ocr']['rss_steady_median_mb'] - baseline['ocr']['rss_steady_median_mb']:+.1f} MiB。",
            ]
        )
    if all(value in by_threads for value in (1, 2, 4)):
        lines.extend(
            [
                "",
                "## 线程增量（后档减前档）",
                "",
                "| 区间 | 每增加1线程的 OCR P50/P90 ms | 每增加1线程的 CPU核秒均值 | 每增加1线程的帧时间均值/P99 ms | 每增加1线程的1% low fps |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for before_threads, after_threads in ((1, 2), (2, 4)):
            before = by_threads[before_threads]
            after = by_threads[after_threads]
            width = after_threads - before_threads
            lines.append(
                f"| {before_threads}→{after_threads} | "
                f"{(after['ocr']['p50_ms'] - before['ocr']['p50_ms']) / width:+.1f}/"
                f"{(after['ocr']['p90_ms'] - before['ocr']['p90_ms']) / width:+.1f} | "
                f"{(after['ocr']['cpu_core_seconds_mean'] - before['ocr']['cpu_core_seconds_mean']) / width:+.3f} | "
                f"{(after['frame_time_ms']['mean'] - before['frame_time_ms']['mean']) / width:+.3f}/"
                f"{(after['frame_time_ms']['p99'] - before['frame_time_ms']['p99']) / width:+.3f} | "
                f"{(after['frame_time_ms']['one_percent_low_fps'] - before['frame_time_ms']['one_percent_low_fps']) / width:+.3f} |"
            )
    lines.extend([
        "",
        "- 帧时间：AMD PresentMon HEAD 03f47e75，`--v1_metrics --exclude_dropped`；均值/P99 来自 `MsBetweenPresents`。1% low = 最慢 1% displayed frame 的平均帧时间倒数。",
        "- 采样：WGC 与生产 AdaptiveFrameSelector 每秒一次；OCR 只处理 detector/heartbeat 选中帧，worker busy 时丢弃、不排队；worker 线程 BELOW_NORMAL。",
        "- OCR 关闭（线程 0）与 1/2/4 线程各为独立实玩段；报告只列事实与表格，不推荐线程数。",
        "- 产品负责人在开测时把规格中的每段至少 5 分钟改为每段 2 分钟；本轮四段均为 120 秒。",
        "- 四段是连续实玩但不是同一帧序列；场景与操作强度只能人工尽量保持，不能把段间帧时间差单独归因于 OCR。",
    ])
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title")
    parser.add_argument("--duration", type=int, default=300)
    parser.add_argument("--threads", type=int, choices=(0, 1, 2, 4))
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    if args.report:
        write_report(args.output)
        return 0
    if args.threads is None:
        parser.error("--threads is required for a segment")
    run_segment(args.title, args.duration, args.threads, args.output / f"threads-{args.threads}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
