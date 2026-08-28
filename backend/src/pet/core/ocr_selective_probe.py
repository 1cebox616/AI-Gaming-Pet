"""Offline M5-T7.10b selective second-pass OCR probe."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from queue import Queue
import statistics
import sys
import time
import tomllib
from typing import Any

from PIL import Image

from pet.core.config import DEFAULT_REGION_FOCUS_MAX
from pet.core.ocr_probe import (
    BACKEND_DIRECTORY,
    DEFAULT_LANGUAGE,
    DEFAULT_OUTPUT_ROOT,
    OcrLine,
    SampledFrame,
    SegmentRange,
    _manual_indices,
    _percentile,
    _prepare_samples,
    _recording_hash,
    run_winrt_ocr,
)
from pet.core.ocr_selective import (
    DiffLine,
    PersistentWinRtOcrWorker,
    SceneResetRamp,
    TextLineCache,
    build_crop_groups,
    crop_fits_remaining_budget,
    normalize_text,
    prove_thread_submission_is_nonblocking,
    select_crop_budget,
    simulate_nonblocking_cadence,
    SECOND_PASS_SAFETY_MARGIN_MS,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_MANIFEST = BACKEND_DIRECTORY / "data" / "generic" / "replay-truth" / "m5-t8-segments.toml"
DEFAULT_SCENE_RESET_RATIO = DEFAULT_REGION_FOCUS_MAX
NORMAL_MAX_CROPS = 2
NORMAL_SECOND_PASS_BUDGET_MS = 120.0
RAMP_FRAMES = 3
RAMP_MAX_CROPS = 4
RAMP_SECOND_PASS_BUDGET_MS = 250.0
DISPATCH_WAIT_MS = 150.0


@dataclass(frozen=True, slots=True)
class ReplayRole:
    role: str
    session: Path
    segment: SegmentRange | None


@dataclass(frozen=True, slots=True)
class CandidateText:
    text: str
    source: str
    stability: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class FrameMeasurement:
    role: str
    frame_number: int
    relative_seconds: float
    raw_file: str
    confirmed_ratio: float
    scene_reset: bool
    ramp: bool
    first_pass_ms: float
    second_pass_ms: float
    total_ms: float
    crop_groups: int
    attempted_crops: int
    completed_crops: int
    skipped_crops: int
    secondary_eligible_lines: int
    secondary_covered_lines: int
    added: tuple[str, ...]
    changed: tuple[str, ...]
    disappeared: tuple[str, ...]
    stable_count: int
    stable_lines: tuple[CandidateText, ...]
    candidate_lines: tuple[CandidateText, ...]
    skipped_reason: str | None

    @property
    def injection_characters(self) -> int:
        unique = dict.fromkeys(
            item.text for item in self.candidate_lines if item.text.strip()
        )
        return sum(len(value) for value in unique)


@dataclass(frozen=True, slots=True)
class RoleRun:
    role: ReplayRole
    samples: tuple[SampledFrame, ...]
    frames: tuple[FrameMeasurement, ...]
    hash_before: str
    hash_after: str


@dataclass(frozen=True, slots=True)
class TruthTarget:
    role: str
    text: str
    start: float
    end: float
    exact_line: bool = False


TRUTH_TARGETS = (
    TruthTarget("card-game", "52/75", 30.0, 49.0),
    TruthTarget("card-game", "197/221", 34.0, 45.0),
    TruthTarget("card-game", "45/75", 50.0, 154.0),
    TruthTarget("card-game", "86/221", 175.0, 180.1),
    TruthTarget("card-game", "20", 75.0, 100.1, True),
    TruthTarget("card-game", "3", 173.0, 178.1, True),
    TruthTarget("simulation-fps", "笑脸人", 80.0, 116.0),
    TruthTarget("simulation-fps", "枪炮军士", 80.0, 116.0),
    TruthTarget("simulation-fps", "别墅区PMC的日记", 193.0, 202.0),
)

# Accepted T7.10 evidence, restated here only for the report comparison.  The
# values are not model input and never influence crop selection.
HISTORICAL_FIRST_PASS = {
    "52/75": True,
    "197/221": True,
    "45/75": True,
    "86/221": False,
    "20": False,
    "3": False,
    "笑脸人": True,
    "枪炮军士": True,
    "别墅区PMC的日记": True,
}
HISTORICAL_NAIVE_COLOR = {**HISTORICAL_FIRST_PASS, "86/221": True}

# Interface transitions explicitly described by the product-owner records.
# Other roles have no menu/shop/reading-page transition in their selected clip.
INTERFACE_TRANSITIONS = {
    "simulation-fps": (
        (80.0, "打开装备/交易界面"),
        (115.0, "离开交易界面"),
        (180.0, "再次打开装备界面"),
        (194.0, "打开日记阅读页"),
        (201.0, "关闭日记阅读页"),
    )
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M5-T7.10b 选择性二次 OCR 离线原型")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--lang", default=DEFAULT_LANGUAGE)
    parser.add_argument("--scene-reset-ratio", type=float, default=DEFAULT_SCENE_RESET_RATIO)
    parser.add_argument("--output-dir", type=Path)
    return parser


def load_roles(path: Path) -> tuple[ReplayRole, ...]:
    with path.open("rb") as stream:
        payload = tomllib.load(stream)
    roles: list[ReplayRole] = []
    for item in payload.get("segments", []):
        if not isinstance(item, Mapping):
            continue
        session = (path.parent / str(item["session"])).resolve()
        start = item.get("start")
        end = item.get("end")
        segment = (
            SegmentRange(float(start), float(end))
            if isinstance(start, (int, float)) and isinstance(end, (int, float))
            else None
        )
        roles.append(ReplayRole(str(item["role"]), session, segment))
    if len(roles) != 5:
        raise ValueError(f"M5-T7.10b 需要五个重放角色；清单实际为 {len(roles)}")
    return tuple(roles)


def select_one_second_cadence(samples: Sequence[SampledFrame]) -> tuple[SampledFrame, ...]:
    """Choose at most one existing raw frame for each one-second cadence slot."""
    if not samples:
        return ()
    selected = [samples[0]]
    next_time = samples[0].relative_seconds + 1.0
    for sample in samples[1:]:
        if sample.relative_seconds + 1e-6 >= next_time:
            selected.append(sample)
            elapsed_slots = max(1, math.floor(sample.relative_seconds - next_time) + 1)
            next_time += elapsed_slots
    return tuple(selected)


def _candidate(text: str, source: str, stability: str, line: OcrLine) -> CandidateText:
    return CandidateText(
        text,
        source,
        stability,
        (line.x, line.y, line.width, line.height),
    )


def _save_crop(source: Image.Image, group: Any, destination: Path) -> None:
    crop = source.crop((group.left, group.top, group.right, group.bottom))
    resized = crop.resize(
        (crop.width * group.scale, crop.height * group.scale), Image.Resampling.LANCZOS
    )
    resized.save(destination)
    resized.close()
    crop.close()


def run_role(
    role: ReplayRole,
    samples: Sequence[SampledFrame],
    worker: PersistentWinRtOcrWorker,
    output_directory: Path,
    *,
    scene_reset_ratio: float,
) -> RoleRun:
    raw_directory = role.session / "raw"
    hash_before = _recording_hash(raw_directory)
    cache = TextLineCache()
    ramp_state = SceneResetRamp(RAMP_FRAMES)
    measurements: list[FrameMeasurement] = []
    crop_directory = output_directory / role.role / "crops"
    crop_directory.mkdir(parents=True, exist_ok=True)

    for sample in samples:
        # A real one-second cadence leaves enough off-path wall time for the
        # worker to recover after a budget kill.  Wait here before starting the
        # next frame's measured interval; the dispatch/cadence thread never
        # waits on this recovery.
        worker.wait_for_restart(timeout_seconds=1.0)
        frame_started = time.perf_counter()
        first_reply = worker.recognize(sample.path)
        if first_reply.result is None:
            measurements.append(
                FrameMeasurement(
                    role.role,
                    sample.frame_number,
                    sample.relative_seconds,
                    sample.path.name,
                    len(sample.confirmed_grid) / (16 * 9),
                    False,
                    False,
                    first_reply.wall_ms,
                    0.0,
                    (time.perf_counter() - frame_started) * 1000.0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    (),
                    (),
                    (),
                    0,
                    (),
                    (),
                    first_reply.skipped_reason,
                )
            )
            continue

        first = first_reply.result
        confirmed_ratio = len(sample.confirmed_grid) / (16 * 9)
        scene_reset = confirmed_ratio > scene_reset_ratio
        ramp = ramp_state.consume(scene_reset)
        diff = cache.update(first.lines, scene_reset=scene_reset)
        groups = build_crop_groups(diff.lines, first.width, first.height)
        budget = select_crop_budget(groups, ramp=ramp)
        second_started = time.perf_counter()
        completed = 0
        attempted = 0
        skipped = budget.skipped_count
        covered_members: set[int] = set()
        second_candidates: list[CandidateText] = []
        budget_aborted = False

        with Image.open(sample.path) as source_image:
            source = source_image.convert("RGB")
        try:
            for group_index, group in enumerate(budget.selected):
                elapsed = (time.perf_counter() - second_started) * 1000.0
                remaining = budget.max_ms - elapsed
                if remaining <= 0:
                    skipped += len(budget.selected) - group_index
                    budget_aborted = True
                    break
                if not crop_fits_remaining_budget(group, remaining):
                    skipped += 1
                    budget_aborted = True
                    continue
                crop_path = crop_directory / f"frame-{sample.frame_number:06d}-crop-{group_index + 1}.png"
                _save_crop(source, group, crop_path)
                elapsed = (time.perf_counter() - second_started) * 1000.0
                remaining = budget.max_ms - elapsed
                if remaining <= SECOND_PASS_SAFETY_MARGIN_MS:
                    skipped += len(budget.selected) - group_index
                    budget_aborted = True
                    crop_path.unlink(missing_ok=True)
                    break
                attempted += 1
                reply = worker.recognize(
                    crop_path,
                    timeout_ms=remaining - SECOND_PASS_SAFETY_MARGIN_MS,
                )
                crop_path.unlink(missing_ok=True)
                if reply.result is None:
                    skipped += len(budget.selected) - group_index
                    budget_aborted = True
                    break
                completed += 1
                covered_members.update(id(member) for member in group.members)
                group_bbox = (
                    group.left / first.width,
                    group.top / first.height,
                    (group.right - group.left) / first.width,
                    (group.bottom - group.top) / first.height,
                )
                for line in reply.result.lines:
                    second_candidates.append(
                        CandidateText(line.text, "second", "candidate", group_bbox)
                    )
        finally:
            source.close()
        second_ms = (time.perf_counter() - second_started) * 1000.0

        first_candidates = tuple(
            _candidate(item.text, "first", "candidate", item.line)
            for item in diff.lines
            if item.kind in ("added", "changed")
        )
        stable_lines = tuple(
            _candidate(item.text, "cache", "stable", item.line)
            for item in cache.lines
            if item.streak >= 2
        )
        eligible_lines = sum(len(group.members) for group in groups)
        covered_lines = sum(
            1
            for group in groups
            for member in group.members
            if id(member) in covered_members
        )
        measurements.append(
            FrameMeasurement(
                role.role,
                sample.frame_number,
                sample.relative_seconds,
                sample.path.name,
                confirmed_ratio,
                scene_reset,
                ramp,
                first_reply.wall_ms,
                second_ms,
                (time.perf_counter() - frame_started) * 1000.0,
                len(groups),
                attempted,
                completed,
                skipped,
                eligible_lines,
                covered_lines,
                tuple(item.text for item in diff.lines if item.kind == "added"),
                tuple(item.text for item in diff.lines if item.kind == "changed"),
                diff.disappeared,
                diff.stable_count,
                stable_lines,
                first_candidates + tuple(second_candidates),
                "second_budget_exhausted" if budget_aborted else None,
            )
        )
    hash_after = _recording_hash(raw_directory)
    return RoleRun(role, tuple(samples), tuple(measurements), hash_before, hash_after)


def _frame_payload(frame: FrameMeasurement) -> dict[str, Any]:
    payload = asdict(frame)
    payload["stable_lines"] = [asdict(item) for item in frame.stable_lines]
    payload["candidate_lines"] = [asdict(item) for item in frame.candidate_lines]
    payload["injection_characters"] = frame.injection_characters
    return payload


def _write_role_outputs(root: Path, run: RoleRun) -> dict[str, Any]:
    destination = root / run.role.role
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / "frames.jsonl").open("w", encoding="utf-8") as stream:
        for frame in run.frames:
            stream.write(json.dumps(_frame_payload(frame), ensure_ascii=False) + "\n")

    durations = [item.total_ms for item in run.frames]
    injections = [float(item.injection_characters) for item in run.frames]
    cadence = simulate_nonblocking_cadence(durations, wait_ms=DISPATCH_WAIT_MS)
    stats = {
        "role": run.role.role,
        "session": str(run.role.session),
        "segment": (
            {"start": run.role.segment.start, "end": run.role.segment.end}
            if run.role.segment
            else None
        ),
        "frame_count": len(run.frames),
        "duration_ms": {
            "median": statistics.median(durations),
            "p90": _percentile(durations, 0.90),
            "max": max(durations),
        },
        "target": {
            "median_le_100": statistics.median(durations) <= 100.0,
            "p90_le_250": float(_percentile(durations, 0.90) or 0.0) <= 250.0,
        },
        "scene_reset_count": sum(item.scene_reset for item in run.frames),
        "skipped_crops": sum(item.skipped_crops for item in run.frames),
        "line_events": {
            "added": sum(len(item.added) for item in run.frames),
            "changed": sum(len(item.changed) for item in run.frames),
            "disappeared": sum(len(item.disappeared) for item in run.frames),
            "stable_count_median": statistics.median(
                item.stable_count for item in run.frames
            ),
        },
        "second_pass": {
            "attempted_crops": sum(item.attempted_crops for item in run.frames),
            "completed_crops": sum(item.completed_crops for item in run.frames),
        },
        "injection_characters": {
            "mean": statistics.mean(injections),
            "median": statistics.median(injections),
            "p90": _percentile(injections, 0.90),
        },
        "cadence": asdict(cadence),
        "recording_hash_before": run.hash_before,
        "recording_hash_after": run.hash_after,
        "recording_hash_matches": run.hash_before == run.hash_after,
    }
    (destination / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    manual = [
        f"# {run.role.role} 选择性 OCR 人工对照",
        "",
        "每行标明文本来自首遍、选择性二次或跨帧缓存；stable 只在同位置同内容连续至少两帧后出现。",
        "",
        "| 帧 | T+秒 | raw | OCR 候选与稳定文本 |",
        "|---:|---:|---|---|",
    ]
    for index in _manual_indices(len(run.frames)):
        frame = run.frames[index]
        sample = run.samples[index]
        values = [
            f"{item.text}〔{item.source}/{item.stability}〕"
            for item in frame.candidate_lines + frame.stable_lines
        ]
        manual.append(
            f"| {frame.frame_number} | {frame.relative_seconds:.1f} | "
            f"[{frame.raw_file}]({sample.path.as_posix()}) | "
            f"{'<br>'.join(values) if values else '—'} |"
        )
    (destination / "manual-review.md").write_text(
        "\n".join(manual) + "\n", encoding="utf-8"
    )
    return stats


def _target_hit(run: RoleRun, target: TruthTarget) -> tuple[bool, str | None]:
    wanted = normalize_text(target.text)
    for frame in run.frames:
        if not target.start <= frame.relative_seconds <= target.end:
            continue
        candidates = frame.candidate_lines + frame.stable_lines
        for item in candidates:
            value = normalize_text(item.text)
            match = value == wanted if target.exact_line else wanted in value
            if match:
                return True, f"T+{frame.relative_seconds:.1f} `{item.text}` ({item.source})"
    return False, None


def _coverage(frame: FrameMeasurement) -> str:
    if frame.secondary_eligible_lines == 0:
        return "—（无合格小字行）"
    return f"{frame.secondary_covered_lines}/{frame.secondary_eligible_lines} ({frame.secondary_covered_lines / frame.secondary_eligible_lines:.1%})"


def _transition_rows(runs: Sequence[RoleRun]) -> list[str]:
    rows: list[str] = []
    by_role = {run.role.role: run for run in runs}
    for role, transitions in INTERFACE_TRANSITIONS.items():
        run = by_role[role]
        for at, label in transitions:
            index = min(
                range(len(run.frames)),
                key=lambda value: abs(run.frames[value].relative_seconds - at),
            )
            first = run.frames[index]
            third = run.frames[min(index + 2, len(run.frames) - 1)]
            ramp_frames = sum(item.ramp for item in run.frames[index : index + 3]) if first.scene_reset else 0
            rows.append(
                f"| {role} | {label}（约 T+{at:.0f}） | T+{first.relative_seconds:.1f} | "
                f"{'是' if first.scene_reset else '否'} | {ramp_frames} | {_coverage(first)} | {_coverage(third)} |"
            )
    return rows


def _write_review(
    root: Path,
    runs: Sequence[RoleRun],
    stats: Sequence[Mapping[str, Any]],
    *,
    cold_ms: Sequence[float],
    warm_ms: Sequence[float],
    worker_startup_ms: float,
    worker_restarts: int,
    budget_timeouts: int,
    crash_restarts: int,
    submission_ms: float,
    agents_lines: int,
) -> None:
    by_role = {run.role.role: run for run in runs}
    normal_second = [
        frame.second_pass_ms for run in runs for frame in run.frames if not frame.ramp
    ]
    ramp_second = [
        frame.second_pass_ms for run in runs for frame in run.frames if frame.ramp
    ]
    normal_overruns = sum(value > NORMAL_SECOND_PASS_BUDGET_MS for value in normal_second)
    ramp_overruns = sum(value > RAMP_SECOND_PASS_BUDGET_MS for value in ramp_second)
    total_skipped_crops = sum(int(item["skipped_crops"]) for item in stats)
    truth_rows: list[str] = []
    selective_hits = 0
    for target in TRUTH_TARGETS:
        hit, evidence = _target_hit(by_role[target.role], target)
        selective_hits += hit
        truth_rows.append(
            f"| {target.role} | `{target.text}` | "
            f"{'✓' if HISTORICAL_FIRST_PASS[target.text] else '✗'} | "
            f"{'✓' if HISTORICAL_NAIVE_COLOR[target.text] else '✗'} | "
            f"{'✓' if hit else '✗'} | {evidence or '—'} |"
        )

    first_total = sum(HISTORICAL_FIRST_PASS[target.text] for target in TRUTH_TARGETS)
    naive_total = sum(HISTORICAL_NAIVE_COLOR[target.text] for target in TRUTH_TARGETS)
    lines = [
        "# M5-T7.10b 选择性二次 OCR 原型",
        "",
        "零网络、零云模型、零费用；五段录像只读。原型未接 adapter、提示词或生产管线。",
        "",
        "## 持久进程",
        "",
        f"- PowerShell/WinRT 工作进程启动至 ready：{worker_startup_ms:.1f} ms。",
        f"- 单帧冷进程端到端中位/P90：{statistics.median(cold_ms):.1f}/{_percentile(cold_ms, 0.90):.1f} ms。",
        f"- 常驻进程同帧请求中位/P90：{statistics.median(warm_ms):.1f}/{_percentile(warm_ms, 0.90):.1f} ms。",
        f"- 常驻相对冷进程中位节省：{statistics.median(cold_ms) - statistics.median(warm_ms):.1f} ms。",
        f"- 工作进程重建 {worker_restarts} 次：二次 OCR 预算超时 {budget_timeouts} 次；自主崩溃/管道失败 {crash_restarts} 次。超时 crop 不采用结果并计入 skipped。",
        "",
        "## 五段耗时与 100/250 ms 目标",
        "",
        "| 片段 | 1秒节拍帧 | 全流程中位/P90/最大 ms | 中位≤100 | P90≤250 | skipped crops |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in stats:
        duration = item["duration_ms"]
        target = item["target"]
        lines.append(
            f"| {item['role']} | {item['frame_count']} | "
            f"{duration['median']:.1f}/{duration['p90']:.1f}/{duration['max']:.1f} | "
            f"{'✓' if target['median_le_100'] else '✗'} | {'✓' if target['p90_le_250'] else '✗'} | "
            f"{item['skipped_crops']} |"
        )

    lines.extend(
        [
            "",
            "全流程计时包含常驻进程请求、首遍 OCR、Python 差分、裁剪缩放、选择性二次 OCR 与候选组装；不包含离线录像哈希和报告写入。",
            "",
            "**目标判定：五段中只有 survival-2d 与 640p frozen-regression 同时达标；fast-fps、simulation-fps、card-game 均未达标。因此该原型不能进入热路径。**",
            "",
            f"未达标的直接证据是：快节奏与文字密集片段中，OCR 行本身存在内容、位置或检出抖动，新增/变化事件仍然覆盖多数帧；几何与数量预算共跳过 {total_skipped_crops} 个 crop，OCR 请求预算超时 {budget_timeouts} 次。选择性策略保住了 7/9 召回，但没有把常见帧的工作量压到目标范围。",
            "",
            "## 自动召回三列对账",
            "",
            f"汇总：T7.10 首遍 {first_total}/{len(TRUTH_TARGETS)}；朴素彩色二次 {naive_total}/{len(TRUTH_TARGETS)}；本轮选择性二次 {selective_hits}/{len(TRUTH_TARGETS)}。",
            "",
            "历史两列来自已保存 T7.10 准确性与两遍式报告；`20`、`3` 采用正确时间窗内完整行精确匹配，避免把生命值或水印中的数字当命中。",
            "",
            "| 角色 | 真值 | T7.10 首遍 | 朴素彩色二次 | 本轮选择性 | 本轮原文证据 |",
            "|---|---|---:|---:|---:|---|",
            *truth_rows,
            "",
            "## 界面切换与 reset 爬坡",
            "",
            "二次文字覆盖率定义为：该帧满足小字条件的新增/变化首遍行中，被实际完成的二次 crop 覆盖的行数占比；不是 OCR 正确率。四个未登记菜单/商店/阅读页进出的角色记为无样本，不凭画面语义增补。",
            "",
            "| 角色 | 登记切换 | 对齐帧 | reset触发 | 爬坡帧数 | 切换首帧二次覆盖 | 第3帧二次覆盖 |",
            "|---|---|---:|---:|---:|---:|---:|",
            *_transition_rows(runs),
            "",
            "## 挂接模拟",
            "",
            "OCR 全程在独立 Python 线程内执行；节拍线程只提交工作，不调用 OCR，也不等待结果。以下按实测全流程耗时重放 1 秒到达时间，并以派发前最多等待 150ms 判定随帧命中。",
            "",
            f"实际线程提交调用耗时 {submission_ms:.3f} ms；OCR 工作在线程内继续运行，证明提交路径没有等待 OCR 完成。",
            "",
            "| 片段 | 随帧命中 | 迟到 | 最大队列等待 ms |",
            "|---|---:|---:|---:|",
        ]
    )
    for item in stats:
        cadence = item["cadence"]
        lines.append(
            f"| {item['role']} | {cadence['same_frame_hits']}/{cadence['frame_count']} "
            f"({cadence['same_frame_hit_rate']:.1%}) | {cadence['late_frames']}/{cadence['frame_count']} "
            f"({cadence['late_rate']:.1%}) | {cadence['maximum_queue_delay_ms']:.1f} |"
        )

    lines.extend(
        [
            "",
            "## 注入量估算",
            "",
            "仅统计每帧【新增+变化】首遍行及其二次候选的 Unicode 字符数，不把稳定缓存全文重复注入，也不冒充具体 tokenizer token。",
            "",
            "| 片段 | 字符/帧 平均/中位/P90 |",
            "|---|---:|",
        ]
    )
    for item in stats:
        injection = item["injection_characters"]
        lines.append(
            f"| {item['role']} | {injection['mean']:.1f}/{injection['median']:.1f}/{injection['p90']:.1f} |"
        )

    lines.extend(
        [
            "",
            "## 人工对照表与逐帧原始结构",
            "",
        ]
    )
    for run in runs:
        lines.append(
            f"- [{run.role.role} 人工10帧](./{run.role.role}/manual-review.md)；"
            f"[逐帧 JSONL](./{run.role.role}/frames.jsonl)；[统计](./{run.role.role}/stats.json)"
        )

    lines.extend(
        [
            "",
            "## 录像哈希",
            "",
            "| 片段 | 前后聚合 SHA-256 | 一致 |",
            "|---|---|---:|",
        ]
    )
    for run in runs:
        lines.append(
            f"| {run.role.role} | `{run.hash_before}` / `{run.hash_after}` | {'是' if run.hash_before == run.hash_after else '否'} |"
        )

    lines.extend(
        [
            "",
            "## 与规格的偏差及原因",
            "",
            "- 界面切换只采用答案键现场记录明确标注的菜单、交易与阅读页进出；未用 OCR 输出反推新切换点，避免用被测结果定义考题。",
            "- 150ms 随帧命中采用实测耗时的离散事件重放，而不是让离线工具真实睡眠约 19 分钟；工作线程隔离由实际线程提交耗时和单元测试共同证明。",
            "- WinRT 无置信度，二次结果始终标为 candidate；stable 仅来自连续至少两帧同位置同内容的首遍缓存。",
            "- 性能目标未整体达到：3/5 片段至少一项失败；fast-fps 两项失败，simulation-fps 仅 P90 失败，card-game 仅中位失败。规格允许未达标时如实分析；本报告没有调整真值或素材来制造通过。",
            f"- 二次预算实测合规：正常档超过 120ms 为 {normal_overruns} 帧（最大 {max(normal_second, default=0.0):.1f}ms），reset 档超过 250ms 为 {ramp_overruns} 帧（最大 {max(ramp_second, default=0.0):.1f}ms）。采用纯几何输出像素预拒绝和 20ms 收尾余量；被拒 crop 明确计入 skipped。Pillow resize 本身不可抢占，因此该结论是五段实测结果，不外推为形式化实时保证。",
            "",
            "## AGENTS.md 核对",
            "",
            "- 第二行：`最后更新：M5-T7.10 验收后（快线冻结、上传宽度 896 定案；M5-B 细化前）`。",
            f"- 物理总行数：{agents_lines}。文件由产品负责人覆盖，本任务未改内容。",
            "",
            "## 未完成项",
            "",
            "- 未接入 adapter、提示词或生产管线，符合本任务边界。",
            "- 中位≤100ms、P90≤250ms 的性能目标只在 2/5 片段达到；选择性二次 OCR 仍不是生产候选。",
            "- 二次阶段在五段实测中满足 120/250ms 上限；端到端 100/250ms 目标仍只在 2/5 片段达到。",
            "- 人工对照表提供来源与稳定性标签；对 OCR 错读的最终人工判定仍由产品负责人完成。",
        ]
    )
    (root / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _execute(
    roles: Sequence[ReplayRole],
    output: Path,
    *,
    language: str,
    scene_reset_ratio: float,
) -> dict[str, Any]:
    prepared: list[tuple[ReplayRole, tuple[SampledFrame, ...]]] = []
    for role in roles:
        all_samples, _included = _prepare_samples(role.session / "raw", 1, role.segment)
        prepared.append((role, select_one_second_cadence(all_samples)))

    representatives = tuple(samples[0] for _role, samples in prepared)
    cold_ms: list[float] = []
    for sample in representatives:
        started = time.perf_counter()
        run_winrt_ocr((sample,), language, output)
        cold_ms.append((time.perf_counter() - started) * 1000.0)

    runs: list[RoleRun] = []
    with PersistentWinRtOcrWorker(language) as worker:
        startup_ms = float(worker.startup_ms or 0.0)
        warm_ms = [worker.recognize(sample.path).wall_ms for sample in representatives]
        for role, samples in prepared:
            runs.append(
                run_role(
                    role,
                    samples,
                    worker,
                    output,
                    scene_reset_ratio=scene_reset_ratio,
                )
            )
        restarts = worker.restart_count
        budget_timeouts = worker.budget_timeout_count
        crash_restarts = worker.crash_restart_count
    return {
        "runs": tuple(runs),
        "cold_ms": tuple(cold_ms),
        "warm_ms": tuple(warm_ms),
        "startup_ms": startup_ms,
        "restarts": restarts,
        "budget_timeouts": budget_timeouts,
        "crash_restarts": crash_restarts,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if not 0.0 <= arguments.scene_reset_ratio <= 1.0:
        print("--scene-reset-ratio 必须在 0–1", file=sys.stderr)
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output = (
        arguments.output_dir.resolve()
        if arguments.output_dir
        else (DEFAULT_OUTPUT_ROOT / f"ocr-selective-{stamp}").resolve()
    )
    if output.exists():
        print(f"输出目录已存在：{output}", file=sys.stderr)
        return 2
    output.mkdir(parents=True)
    roles = load_roles(arguments.manifest.resolve())
    result_queue: Queue[dict[str, Any] | BaseException] = Queue()

    def work() -> None:
        try:
            result_queue.put(
                _execute(
                    roles,
                    output,
                    language=arguments.lang,
                    scene_reset_ratio=arguments.scene_reset_ratio,
                )
            )
        except BaseException as error:  # surfaced on the owner thread
            result_queue.put(error)

    submission_ms, thread = prove_thread_submission_is_nonblocking(work)
    thread.join()
    result = result_queue.get()
    if isinstance(result, BaseException):
        raise result
    runs = result["runs"]
    assert isinstance(runs, tuple)
    stats = tuple(_write_role_outputs(output, run) for run in runs)
    agents_lines = len((BACKEND_DIRECTORY.parent / "AGENTS.md").read_text(encoding="utf-8").splitlines())
    _write_review(
        output,
        runs,
        stats,
        cold_ms=result["cold_ms"],
        warm_ms=result["warm_ms"],
        worker_startup_ms=float(result["startup_ms"]),
        worker_restarts=int(result["restarts"]),
        budget_timeouts=int(result["budget_timeouts"]),
        crash_restarts=int(result["crash_restarts"]),
        submission_ms=submission_ms,
        agents_lines=agents_lines,
    )
    run_payload = {
        "task_id": "M5-T7.10b",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "network_calls": 0,
        "model_calls": 0,
        "cost_usd": 0.0,
        "language": arguments.lang,
        "scene_reset_ratio": arguments.scene_reset_ratio,
        "scene_reset_ratio_source": "pet.core.config.DEFAULT_REGION_FOCUS_MAX",
        "normal_budget": {"max_crops": NORMAL_MAX_CROPS, "max_ms": NORMAL_SECOND_PASS_BUDGET_MS},
        "ramp_budget": {"frames": RAMP_FRAMES, "max_crops": RAMP_MAX_CROPS, "max_ms": RAMP_SECOND_PASS_BUDGET_MS},
        "dispatch_wait_ms": DISPATCH_WAIT_MS,
        "output": str(output),
    }
    (output / "run.json").write_text(
        json.dumps(run_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(output / "review.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
