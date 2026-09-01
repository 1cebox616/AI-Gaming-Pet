"""Reproduce the M5-B-T4b fixed-engine offline OCR evidence report."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import tomllib
from typing import Any, Sequence

import numpy as np
from PIL import Image
import psutil

from pet.core.belief import EvidenceEvent, EvidenceStore, OcrFramePayload, TextObservedPayload
from pet.core.ocr_rapid import (
    DETECTOR_MODEL_NAME,
    ENGINE_NAME,
    RECOGNIZER_MODEL_NAME,
    RapidOcrEngine,
)
from pet.core.ocr_selective import TextLineCache, normalize_text


BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]
SESSIONS = (
    "20260827-171815",
    "20260827-203925",
    "20260827-215554",
    "20260827-220206",
)
TRUTH_DIRECTORY = BACKEND_DIRECTORY / "data" / "generic" / "ocr-truth"
DEFAULT_OUTPUT = BACKEND_DIRECTORY / "eval-reports" / "m5-b-t4b-minimal"


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _image_bgr(path: Path) -> np.ndarray:
    with Image.open(path) as source:
        return np.asarray(source.convert("RGB"), dtype=np.uint8)[:, :, ::-1].copy()


def _raw_path(session: Path, sequence: int) -> Path:
    matches = tuple((session / "raw").glob(f"raw-{sequence:06d}-*"))
    if len(matches) != 1:
        raise RuntimeError(f"expected one raw frame for {session.name} #{sequence}, got {len(matches)}")
    return matches[0]


def _event(
    store: EvidenceStore,
    *,
    root: str,
    observed_at: float,
    payload: OcrFramePayload | TextObservedPayload,
) -> EvidenceEvent:
    return EvidenceEvent(
        evidence_id=store.new_evidence_id(root, "ocr"),
        source="ocr",
        kind="ocr_frame" if isinstance(payload, OcrFramePayload) else "text_observed",
        root_capture_id=root,
        observed_at=observed_at,
        learned_at=observed_at + (
            payload.elapsed_ms / 1000.0 if isinstance(payload, OcrFramePayload) else 0.0
        ),
        scope=None,
        payload=payload,
        derived_from=[],
        context_version=None,
        outcome="ok",
    )


def _replay_session(engine: RapidOcrEngine, session: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    with (session / "metrics.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    captured = [row for row in rows if row["是否取得画面"] == "是"]
    selected = [row for row in rows if row["是否落盘"] == "是"]
    origin = float(selected[0]["单调秒"])
    cache = TextLineCache()
    process = psutil.Process()
    store = EvidenceStore.open(output)
    samples: list[dict[str, Any]] = []
    durations: list[float] = []
    det_values: list[float] = []
    rec_values: list[float] = []
    cpu_values: list[float] = []
    rss_values: list[float] = [process.memory_info().rss / 1024**2]
    change_counts: Counter[str] = Counter()
    line_total = 0
    late = 0
    for position, row in enumerate(selected, 1):
        sequence = int(row["序号"])
        frame_path = _raw_path(session, sequence)
        result = engine.recognize(_image_bgr(frame_path))
        observed_at = float(row["单调秒"]) - origin
        root = f"f{sequence}"
        trigger = "heartbeat" if row["是否强制落盘"] == "是" else "detector"
        is_late = result.duration_ms >= 1000.0
        late += int(is_late)
        store.append(
            _event(
                store,
                root=root,
                observed_at=observed_at,
                payload=OcrFramePayload(
                    engine=ENGINE_NAME,
                    num_threads=2,
                    det_limit_side_len=1280,
                    recognized_line_count=len(result.lines),
                    elapsed_ms=result.duration_ms,
                    det_ms=result.det_ms,
                    rec_ms=result.rec_ms,
                    cpu_core_seconds=result.cpu_core_seconds,
                    trigger=trigger,
                    outcome_detail="late" if is_late else "ok",
                ),
            )
        )
        if not is_late:
            diff = cache.update(result.lines)
            for item in diff.lines:
                change = {"added": "new", "changed": "changed", "unchanged": "stable"}[item.kind]
                text = normalize_text(item.text)
                if not text or item.line.width <= 0 or item.line.height <= 0:
                    continue
                payload = TextObservedPayload(
                    text=text,
                    bbox=(item.line.x, item.line.y, item.line.x + item.line.width, item.line.y + item.line.height),
                    quad=item.line.quad,
                    change=change,
                    previous_text=normalize_text(item.previous_text) if item.previous_text else None,
                    streak=item.streak,
                    engine=ENGINE_NAME,
                    engine_confidence=item.line.confidence,
                )
                store.append(_event(store, root=root, observed_at=observed_at, payload=payload))
                change_counts[change] += 1
                line_total += 1
                if len(samples) < 20:
                    samples.append({"frame": root, "raw": frame_path.name, "text": text, "change": change})
            for item in diff.gone:
                payload = TextObservedPayload(
                    text=normalize_text(item.text),
                    bbox=(item.line.x, item.line.y, item.line.x + item.line.width, item.line.y + item.line.height),
                    quad=item.line.quad,
                    change="gone",
                    previous_text=None,
                    streak=item.streak,
                    engine=ENGINE_NAME,
                    engine_confidence=item.line.confidence,
                )
                store.append(_event(store, root=root, observed_at=observed_at, payload=payload))
                change_counts["gone"] += 1
                line_total += 1
        durations.append(result.duration_ms)
        if result.det_ms is not None:
            det_values.append(result.det_ms)
        if result.rec_ms is not None:
            rec_values.append(result.rec_ms)
        if result.cpu_core_seconds is not None:
            cpu_values.append(result.cpu_core_seconds)
        rss_values.append(process.memory_info().rss / 1024**2)
        if position % 50 == 0:
            print(f"{session.name}: {position}/{len(selected)}", flush=True)
    store.close()
    return {
        "session": session.name,
        "captured_frames": len(captured),
        "ocr_frames": len(selected),
        "skipped_frames": len(captured) - len(selected),
        "heartbeat_frames": sum(row["是否强制落盘"] == "是" for row in selected),
        "late_frames": late,
        "elapsed_ms": {"median": statistics.median(durations), "p90": _percentile(durations, 0.9), "max": max(durations)},
        "det_ms": {"median": statistics.median(det_values), "p90": _percentile(det_values, 0.9)},
        "rec_ms": {"median": statistics.median(rec_values), "p90": _percentile(rec_values, 0.9)},
        "cpu_core_seconds": {"median": statistics.median(cpu_values), "p90": _percentile(cpu_values, 0.9)},
        "rss_mb": {"steady_median": statistics.median(rss_values), "peak": max(rss_values)},
        "text_observed_count": line_total,
        "text_observed_per_ocr_frame": line_total / len(selected),
        "change_counts": dict(change_counts),
        "samples": samples,
    }


def _anchor_score(engine: RapidOcrEngine) -> dict[str, Any]:
    with (TRUTH_DIRECTORY / "frames.toml").open("rb") as stream:
        frame_specs = tomllib.load(stream)["frames"]
    truth = json.loads((TRUTH_DIRECTORY / "reference-anchors.json").read_text(encoding="utf-8"))
    truth_by_index = {index: frame["lines"] for index, frame in enumerate(truth["frames"], 1)}
    matched = 0
    total = 0
    outputs: list[dict[str, Any]] = []
    for index, spec in enumerate(frame_specs, 1):
        path = (TRUTH_DIRECTORY / spec["session"] / spec["frame"]).resolve()
        result = engine.recognize(_image_bgr(path))
        predictions = {normalize_text(line.text) for line in result.lines}
        anchors = truth_by_index[index]
        hits = [anchor for anchor in anchors if normalize_text(anchor) in predictions]
        matched += len(hits)
        total += len(anchors)
        outputs.append({"index": index, "path": str(path), "lines": [line.text for line in result.lines], "matched": len(hits), "anchors": len(anchors)})
    return {"matched": matched, "total": total, "recall": matched / total, "outputs": outputs}


def _decode_rate() -> dict[str, Any]:
    evidence = BACKEND_DIRECTORY / "eval-reports" / "observation-replay-20260829-181302" / "slay-the-spire-2-1080p-1fps-20260827-220206" / "evidence.jsonl"
    values: list[float] = []
    excluded = 0
    if evidence.is_file():
        for event in EvidenceStore.read(evidence):
            payload = event.payload
            if event.kind != "fast_observation":
                continue
            if payload.output_tokens is None or payload.ttft_ms is None or payload.latency_ms <= payload.ttft_ms:  # type: ignore[union-attr]
                excluded += 1
                continue
            values.append(payload.output_tokens / ((payload.latency_ms - payload.ttft_ms) / 1000.0))  # type: ignore[union-attr]
    return {
        "source": str(evidence),
        "included": len(values),
        "excluded": excluded,
        "median_tokens_per_second": statistics.median(values) if values else None,
        "p90_tokens_per_second": _percentile(values, 0.9),
    }


def _write_report(output: Path, payload: dict[str, Any]) -> None:
    rows = payload["sessions"]
    lines = [
        "# M5-B-T4b 最简单遍式 OCR 离线重放",
        "",
        "## 固定配置与资产",
        "",
        "- 引擎：RapidOCR PP-OCRv6 tiny detector + tiny recognizer，OpenVINO CPU；全帧、单遍、内存 BGR numpy；方向分类器配置与调用均关闭。",
        "- 参数：`Det.limit_type=max`、`Det.limit_side_len=1280`、`num_threads=2`；OpenVINO、OpenCV 与 OMP 三处同限。",
        "- 模型由 `rapidocr==3.9.2` 官方 wheel 的模型缓存离线复制到 `backend/models/ocr/`；运行时使用显式本地路径，缺文件直接禁用，不下载。",
        f"- `{DETECTOR_MODEL_NAME}`：1,829,618 bytes；`{RECOGNIZER_MODEL_NAME}`：4,489,813 bytes；合计 6,319,431 bytes（6.03 MiB）。OpenVINO 逐节点检查到 float32 与 int64，当前权重为 FP32，不是 FP16。",
        "- 现有 Pillow/numpy/zbl 只能提供图像与位图，不能识字；WinRT OCR 是旧原型且不是 4a-2 定型引擎，因此新增 RapidOCR/OpenVINO。OpenCV 是 RapidOCR 预处理与线程上限所需，psutil 是进程 CPU/RSS 取样所需。",
        "",
        "## 四段 1080p 录像",
        "",
        "| 录像 | 抓帧 | OCR/跳过 | 心跳 | P50/P90/max ms | det P50/P90 | rec P50/P90 | late | CPU核秒 P50/P90 | RSS稳态/峰值 MiB | text/帧 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['session']} | {row['captured_frames']} | {row['ocr_frames']}/{row['skipped_frames']} | {row['heartbeat_frames']} | "
            f"{row['elapsed_ms']['median']:.1f}/{row['elapsed_ms']['p90']:.1f}/{row['elapsed_ms']['max']:.1f} | "
            f"{row['det_ms']['median']:.1f}/{row['det_ms']['p90']:.1f} | {row['rec_ms']['median']:.1f}/{row['rec_ms']['p90']:.1f} | "
            f"{row['late_frames']}/{row['ocr_frames']} | {row['cpu_core_seconds']['median']:.3f}/{row['cpu_core_seconds']['p90']:.3f} | "
            f"{row['rss_mb']['steady_median']:.1f}/{row['rss_mb']['peak']:.1f} | {row['text_observed_per_ocr_frame']:.2f} |"
        )
    total_captured = sum(row["captured_frames"] for row in rows)
    total_ocr = sum(row["ocr_frames"] for row in rows)
    total_heartbeat = sum(row["heartbeat_frames"] for row in rows)
    changes = Counter()
    for row in rows:
        changes.update(row["change_counts"])
    lines.extend([
        "",
        f"- 合计抓帧 {total_captured}，OCR {total_ocr}，跳过 {total_captured-total_ocr}；运行率 {total_ocr/total_captured:.1%}，跳过率 {(total_captured-total_ocr)/total_captured:.1%}；heartbeat {total_heartbeat}/{total_ocr}。",
        f"- change 分布：`new={changes['new']}`、`changed={changes['changed']}`、`gone={changes['gone']}`、`stable={changes['stable']}`。",
        "",
        "## 40 帧 / 324 anchor",
        "",
        f"- 生产实现复跑：{payload['anchors']['matched']}/{payload['anchors']['total']}，召回 {payload['anchors']['recall']:.1%}；4a-2 定型值 83.3%。差异为 {(payload['anchors']['recall']-0.833)*100:+.1f} 个百分点。",
        "- 本轮与 4a-2 同为 T + 2 threads + memory + max1280；模型文件来自同一 RapidOCR 3.9.2 缓存，评分继续使用同一份 reference-anchors.json。",
        "",
        "## 快线输出解码速率补账",
        "",
        f"- 同录制 `20260827-220206` 的既有 evidence.jsonl：纳入 {payload['decode']['included']} 帧，排除 ttft/输出 token 缺失或非正解码区间 {payload['decode']['excluded']} 帧；中位/P90 = {payload['decode']['median_tokens_per_second']:.1f}/{payload['decode']['p90_tokens_per_second']:.1f} token/s。",
        "",
        "## 20 条 text_observed 人工抽查样本",
        "",
        "| 帧 | change | 引擎原文经 normalize_text |",
        "|---|---|---|",
    ])
    samples = [sample for row in rows for sample in row["samples"]][:20]
    lines.extend(f"| {sample['frame']} | {sample['change']} | {sample['text'].replace('|', '\\|')} |" for sample in samples)
    lines.extend([
        "",
        "## 口径",
        "",
        "- OCR/跳过直接读取各录像 metrics.csv 的 detector 落盘判定；普通 no_change/suppressed 帧不 OCR、不推进缓存、不写 ocr_frame。",
        "- late 以生产 1 Hz 节奏的 1000ms 截止线计；late 不写 text_observed。CPU 核心秒数为每次 recognize 前后进程 user+system CPU 时间差。",
        "- RSS 稳态为初始化后逐帧样本中位，峰值为同批最大；进程同时承载评测脚本，因此是生产引擎的同进程口径。",
        "- 本报告只列事实与表格，不作线程数推荐。",
    ])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    engine = RapidOcrEngine(
        model_dir=BACKEND_DIRECTORY / "models" / "ocr",
        num_threads=2,
        det_limit_side_len=1280,
    )
    engine.start()
    try:
        sessions = [
            _replay_session(
                engine,
                BACKEND_DIRECTORY / "recordings" / "capture" / session,
                args.output / session,
            )
            for session in SESSIONS
        ]
        payload = {"sessions": sessions, "anchors": _anchor_score(engine), "decode": _decode_rate()}
    finally:
        engine.close()
    (args.output / "metrics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _write_report(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
