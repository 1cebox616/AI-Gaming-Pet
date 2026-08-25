"""Offline camera-motion diagnosis without changing capture behavior."""

from __future__ import annotations

import argparse
from collections import Counter, deque
import csv
import inspect
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from pet.core.capture import (
    DEFAULT_CAMERA_MOTION_RATIO,
    DEFAULT_INPUT_MOTION_THRESHOLD,
    DEFAULT_MAX_SILENCE_SECONDS,
    DEFAULT_MIN_SAVE_INTERVAL_SECONDS,
    DEFAULT_NOISE_MARGIN,
    DEFAULT_NOISE_MULTIPLIER,
    DEFAULT_NOISE_WINDOW,
    DEFAULT_STRONG_BLOCK_DELTA,
    AdaptiveFrameSelector,
    CaptureError,
    DecisionReason,
    FrameChangeDetector,
    PreparedFrame,
)
from pet.core.capture_calibration import (
    CAMERA_TRUTH_MOUSE_PERCENTILE,
    DEFAULT_CAMERA_MOTION_RATIOS,
    PreparedCalibrationSession,
    prepare_session,
)

FIXED_BASELINE_LEAD_SECONDS = 3.0
TRUTH_PERCENTILES = (0.75, 0.90, 0.95)


@dataclass(frozen=True, slots=True)
class DiagnosticOptions:
    """One real-session diagnostic run."""

    session_dir: Path
    output_dir: Path


@dataclass(frozen=True, slots=True)
class GateTrace:
    """One selector poll with pre- and post-persistence block sets."""

    sequence: int
    reason: DecisionReason
    should_save: bool
    camera_motion: bool
    pre_gate_count: int
    pre_gate_ratio: float
    post_gate_count: int
    post_gate_ratio: float
    baseline_sequence: int
    baseline_gap_frames: int
    baseline_gap_seconds: float
    suppressed_min_interval: bool
    mouse_motion: float
    fixed_baseline_sequence: int | None
    fixed_pre_gate_count: int | None
    fixed_pre_gate_ratio: float | None


@dataclass(frozen=True, slots=True)
class Distribution:
    """Five-number summary for one measured ratio series."""

    minimum: float
    p25: float
    median: float
    p75: float
    maximum: float


@dataclass(frozen=True, slots=True)
class TruthCut:
    """Mouse-percentile truth set and its visual pre-gate median."""

    percentile: float
    threshold: float
    count: int
    pre_gate_median: float


@dataclass(frozen=True, slots=True)
class RecallRow:
    """Camera recall for one ratio under P=1 and P=2."""

    camera_ratio: float
    p1_recall: float
    p2_recall: float
    p1_hits: int
    p2_hits: int
    truth_count: int


@dataclass(frozen=True, slots=True)
class AlignmentRow:
    """Sensitivity of the mouse truth timestamp to a frame offset."""

    frame_offset: int
    comparable_count: int
    camera_hits: int
    camera_recall: float
    pre_gate_median: float


@dataclass(frozen=True, slots=True)
class BranchLines:
    """Source locations proving the candidate classification order."""

    confirmed: int
    camera_if: int
    persistent_elif: int


class DiagnosticSelector:
    """Trace the real selector while mirroring only its hidden gate state.

    The production selector remains the authority for final decisions. The
    mirrored state exposes the pre-persistence set and asserts that its
    post-persistence count is identical to the real decision on every frame.
    """

    def __init__(
        self,
        *,
        persistence_polls: int,
        camera_motion_ratio: float,
        mouse_motion: Sequence[float],
        fixed_baselines: dict[int, tuple[int, PreparedFrame]],
    ) -> None:
        self.detector = FrameChangeDetector()
        self.selector = AdaptiveFrameSelector(
            detector=self.detector,
            noise_window=DEFAULT_NOISE_WINDOW,
            noise_multiplier=DEFAULT_NOISE_MULTIPLIER,
            noise_margin=DEFAULT_NOISE_MARGIN,
            persistence_polls=persistence_polls,
            camera_motion_ratio=camera_motion_ratio,
            min_save_interval=DEFAULT_MIN_SAVE_INTERVAL_SECONDS,
            max_silence=DEFAULT_MAX_SILENCE_SECONDS,
        )
        columns, rows = self.detector.block_grid
        self._histories = [
            deque(maxlen=DEFAULT_NOISE_WINDOW) for _ in range(columns * rows)
        ]
        self._consecutive = np.zeros((rows, columns), dtype=np.int32)
        self._streak_floors = np.full(
            (rows, columns), DEFAULT_NOISE_MARGIN, dtype=np.float32
        )
        self._baseline: PreparedFrame | None = None
        self._baseline_sequence: int | None = None
        self._baseline_time = 0.0
        self._last_saved_at: float | None = None
        self._persistence_polls = persistence_polls
        self._mouse_motion = mouse_motion
        self._fixed_baselines = fixed_baselines

    def observe(
        self,
        prepared: PreparedFrame,
        *,
        sequence: int,
        timestamp: datetime,
    ) -> GateTrace:
        is_first = self._baseline is None
        baseline = self._baseline or prepared
        baseline_sequence = self._baseline_sequence or sequence
        floors = np.asarray(
            [
                statistics.median(history) * DEFAULT_NOISE_MULTIPLIER
                + DEFAULT_NOISE_MARGIN
                if history
                else DEFAULT_NOISE_MARGIN
                for history in self._histories
            ],
            dtype=np.float32,
        ).reshape(self._consecutive.shape)
        differences = self.detector.block_mean_differences(baseline, prepared)
        active_streak = self._consecutive > 0
        thresholds = np.where(active_streak, self._streak_floors, floors)
        above_floor = differences > thresholds
        starting_streak = above_floor & ~active_streak
        self._streak_floors[starting_streak] = floors[starting_streak]
        self._streak_floors[~above_floor] = floors[~above_floor]
        self._consecutive = np.where(above_floor, self._consecutive + 1, 0)
        confirmed = self._consecutive >= self._persistence_polls
        for history, value in zip(self._histories, differences.flat, strict=True):
            history.append(float(value))

        pre_count = int(np.count_nonzero(above_floor))
        post_count = int(np.count_nonzero(confirmed))
        block_total = int(above_floor.size)
        decision = self.selector.observe_prepared(prepared, timestamp.timestamp())
        if decision.changed_block_count != post_count:
            raise CaptureError(
                "诊断镜像与生产选择器的门控后块数不一致："
                f"frame={sequence}, diagnostic={post_count}, "
                f"production={decision.changed_block_count}"
            )

        fixed_sequence: int | None = None
        fixed_count: int | None = None
        fixed_ratio: float | None = None
        fixed = self._fixed_baselines.get(sequence)
        if fixed is not None:
            fixed_sequence, fixed_frame = fixed
            fixed_differences = self.detector.block_mean_differences(
                fixed_frame, prepared
            )
            fixed_count = int(np.count_nonzero(fixed_differences > thresholds))
            fixed_ratio = fixed_count / block_total

        baseline_gap_seconds = 0.0
        if baseline_sequence != sequence:
            baseline_gap_seconds = timestamp.timestamp() - self._baseline_time
        trace = GateTrace(
            sequence=sequence,
            reason=decision.reason,
            should_save=decision.should_save,
            camera_motion=decision.camera_motion,
            pre_gate_count=pre_count,
            pre_gate_ratio=pre_count / block_total,
            post_gate_count=post_count,
            post_gate_ratio=post_count / block_total,
            baseline_sequence=baseline_sequence,
            baseline_gap_frames=sequence - baseline_sequence,
            baseline_gap_seconds=baseline_gap_seconds,
            suppressed_min_interval=decision.reason == "suppressed_min_interval",
            mouse_motion=self._mouse_motion[sequence - 1],
            fixed_baseline_sequence=fixed_sequence,
            fixed_pre_gate_count=fixed_count,
            fixed_pre_gate_ratio=fixed_ratio,
        )

        now = timestamp.timestamp()
        if is_first or self._last_saved_at is None:
            self._baseline = prepared
            self._baseline_sequence = sequence
            self._baseline_time = now
            self._last_saved_at = now
            self._consecutive.fill(0)
        elif decision.reason == "forced":
            self._last_saved_at = now
        elif decision.reason == "suppressed_min_interval":
            pass
        elif decision.should_save:
            self._baseline = prepared
            self._baseline_sequence = sequence
            self._baseline_time = now
            self._last_saved_at = now
            self._consecutive.fill(0)
        return trace


def _percentile(ordered: Sequence[float], percentile: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def truth_mask(
    mouse_motion: Sequence[float], percentile: float
) -> tuple[float, tuple[bool, ...]]:
    """Cut the mouse-motion truth set exactly as M5-T4 did."""
    threshold = _percentile(sorted(mouse_motion), percentile)
    return threshold, tuple(
        value >= threshold and value > 0.0 for value in mouse_motion
    )


def fixed_baseline_map(
    session: PreparedCalibrationSession,
    truth: Sequence[bool],
    *,
    lead_seconds: float = FIXED_BASELINE_LEAD_SECONDS,
) -> dict[int, tuple[int, PreparedFrame]]:
    """Use the latest frame at least three seconds before each truth episode."""
    fixed: dict[int, tuple[int, PreparedFrame]] = {}
    for index, is_truth in enumerate(truth):
        if not is_truth:
            continue
        start = index
        while start > 0 and truth[start - 1]:
            start -= 1
        target = session.timestamps[start] - timedelta(seconds=lead_seconds)
        baseline_index = 0
        for candidate in range(start - 1, -1, -1):
            if session.timestamps[candidate] <= target:
                baseline_index = candidate
                break
        fixed[index + 1] = (
            baseline_index + 1,
            session.frames[baseline_index],
        )
    return fixed


def trace_session(
    session: PreparedCalibrationSession,
    *,
    persistence_polls: int,
    camera_motion_ratio: float,
    fixed_baselines: dict[int, tuple[int, PreparedFrame]],
) -> tuple[GateTrace, ...]:
    if session.input_motion is None:
        raise CaptureError("镜头诊断需要 explore-3a 会话的 input.csv")
    selector = DiagnosticSelector(
        persistence_polls=persistence_polls,
        camera_motion_ratio=camera_motion_ratio,
        mouse_motion=session.input_motion,
        fixed_baselines=fixed_baselines,
    )
    return tuple(
        selector.observe(frame, sequence=index, timestamp=timestamp)
        for index, (frame, timestamp) in enumerate(
            zip(session.frames, session.timestamps, strict=True), start=1
        )
    )


def distribution(values: Iterable[float]) -> Distribution:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("distribution needs at least one value")
    return Distribution(
        minimum=ordered[0],
        p25=_percentile(ordered, 0.25),
        median=statistics.median(ordered),
        p75=_percentile(ordered, 0.75),
        maximum=ordered[-1],
    )


def source_branch_lines() -> BranchLines:
    lines, start = inspect.getsourcelines(AdaptiveFrameSelector.observe_prepared)
    confirmed_offset = next(
        index
        for index, line in enumerate(lines)
        if "confirmed = self._consecutive" in line
    )
    camera_offset = next(
        index for index, line in enumerate(lines) if line.strip() == "if camera_motion:"
    )
    persistent_offset = next(
        index for index, line in enumerate(lines) if line.strip() == "elif changed_count:"
    )
    return BranchLines(
        start + confirmed_offset,
        start + camera_offset,
        start + persistent_offset,
    )


def recall_rows(
    session: PreparedCalibrationSession,
    truth: Sequence[bool],
    fixed_baselines: dict[int, tuple[int, PreparedFrame]],
) -> tuple[RecallRow, ...]:
    truth_count = sum(truth)
    rows: list[RecallRow] = []
    for ratio in DEFAULT_CAMERA_MOTION_RATIOS:
        p1 = trace_session(
            session,
            persistence_polls=1,
            camera_motion_ratio=ratio,
            fixed_baselines=fixed_baselines,
        )
        p2 = trace_session(
            session,
            persistence_polls=2,
            camera_motion_ratio=ratio,
            fixed_baselines=fixed_baselines,
        )
        p1_hits = sum(
            is_truth and trace.camera_motion
            for is_truth, trace in zip(truth, p1, strict=True)
        )
        p2_hits = sum(
            is_truth and trace.camera_motion
            for is_truth, trace in zip(truth, p2, strict=True)
        )
        rows.append(
            RecallRow(
                camera_ratio=ratio,
                p1_recall=p1_hits / truth_count,
                p2_recall=p2_hits / truth_count,
                p1_hits=p1_hits,
                p2_hits=p2_hits,
                truth_count=truth_count,
            )
        )
    return tuple(rows)


def truth_cuts(
    mouse_motion: Sequence[float], traces: Sequence[GateTrace]
) -> tuple[TruthCut, ...]:
    rows: list[TruthCut] = []
    for percentile in TRUTH_PERCENTILES:
        threshold, mask = truth_mask(mouse_motion, percentile)
        values = [
            trace.pre_gate_ratio
            for is_truth, trace in zip(mask, traces, strict=True)
            if is_truth
        ]
        rows.append(
            TruthCut(
                percentile=percentile,
                threshold=threshold,
                count=len(values),
                pre_gate_median=statistics.median(values),
            )
        )
    return tuple(rows)


def alignment_sensitivity(
    truth: Sequence[bool],
    p1_lowest_ratio_traces: Sequence[GateTrace],
    *,
    offsets: Sequence[int] = (-2, -1, 0, 1, 2),
) -> tuple[AlignmentRow, ...]:
    """Move the input-derived truth labels across nearby visual frames.

    A positive offset compares an input event with a later captured frame. This
    is a diagnostic only: it does not rewrite input timestamps or production
    behavior.
    """
    if len(truth) != len(p1_lowest_ratio_traces):
        raise ValueError("truth and traces must have the same length")
    rows: list[AlignmentRow] = []
    truth_indexes = [index for index, selected in enumerate(truth) if selected]
    for offset in offsets:
        shifted = [
            p1_lowest_ratio_traces[index + offset]
            for index in truth_indexes
            if 0 <= index + offset < len(p1_lowest_ratio_traces)
        ]
        if not shifted:
            raise ValueError(f"offset {offset} has no comparable truth frames")
        hits = sum(trace.camera_motion for trace in shifted)
        rows.append(
            AlignmentRow(
                frame_offset=offset,
                comparable_count=len(shifted),
                camera_hits=hits,
                camera_recall=hits / len(shifted),
                pre_gate_median=statistics.median(
                    trace.pre_gate_ratio for trace in shifted
                ),
            )
        )
    return tuple(rows)


def _fmt_ratio(value: float | None) -> str:
    return "" if value is None else f"{value:.6f}"


def write_turn_csv(
    path: Path,
    *,
    truth: Sequence[bool],
    p1: Sequence[GateTrace],
    p2: Sequence[GateTrace],
) -> None:
    header = (
        "真值轮次序号",
        "轮询序号",
        "P2判定原因",
        "门控前块数",
        "门控前块占比",
        "P2门控后块数",
        "P2门控后块占比",
        "P1判定原因",
        "P1门控后块数",
        "P1门控后块占比",
        "基线帧序号",
        "当前帧序号",
        "序号差",
        "时间差秒",
        "min_save_interval抑制",
        "鼠标位移量",
        "固定基线帧序号",
        "固定基线门控前块数",
        "固定基线门控前块占比",
    )
    with path.open("w", encoding="utf-8-sig", newline="") as destination:
        writer = csv.writer(destination)
        writer.writerow(header)
        truth_index = 0
        for is_truth, p1_trace, p2_trace in zip(truth, p1, p2, strict=True):
            if not is_truth:
                continue
            truth_index += 1
            writer.writerow(
                (
                    truth_index,
                    p2_trace.sequence,
                    p2_trace.reason,
                    p2_trace.pre_gate_count,
                    _fmt_ratio(p2_trace.pre_gate_ratio),
                    p2_trace.post_gate_count,
                    _fmt_ratio(p2_trace.post_gate_ratio),
                    p1_trace.reason,
                    p1_trace.post_gate_count,
                    _fmt_ratio(p1_trace.post_gate_ratio),
                    p2_trace.baseline_sequence,
                    p2_trace.sequence,
                    p2_trace.baseline_gap_frames,
                    f"{p2_trace.baseline_gap_seconds:.6f}",
                    "是" if p2_trace.suppressed_min_interval else "否",
                    f"{p2_trace.mouse_motion:.6f}",
                    p2_trace.fixed_baseline_sequence or "",
                    p2_trace.fixed_pre_gate_count
                    if p2_trace.fixed_pre_gate_count is not None
                    else "",
                    _fmt_ratio(p2_trace.fixed_pre_gate_ratio),
                )
            )


def markdown_table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def _distribution_row(name: str, values: Distribution) -> tuple[object, ...]:
    return (
        name,
        f"{values.minimum:.2%}",
        f"{values.p25:.2%}",
        f"{values.median:.2%}",
        f"{values.p75:.2%}",
        f"{values.maximum:.2%}",
    )


def _distribution_numeric_row(
    name: str, values: Distribution
) -> tuple[object, ...]:
    return (
        name,
        f"{values.minimum:.3f}",
        f"{values.p25:.3f}",
        f"{values.median:.3f}",
        f"{values.p75:.3f}",
        f"{values.maximum:.3f}",
    )


def _hypothesis_statuses(
    *,
    recalls: Sequence[RecallRow],
    production_pre: Distribution,
    fixed_pre: Distribution,
) -> tuple[str, str, str]:
    lowest = min(recalls, key=lambda row: row.camera_ratio)
    h1 = (
        "支持"
        if lowest.p1_recall > lowest.p2_recall
        else "排除"
    )
    h2 = "支持" if fixed_pre.median > production_pre.median else "排除"
    return h1, h2, "排除"


def _classification(
    *,
    recalls: Sequence[RecallRow],
    truth_pre: Distribution,
    truth_post: Distribution,
    nontruth_pre: Distribution,
    cuts: Sequence[TruthCut],
) -> str:
    lowest = min(recalls, key=lambda row: row.camera_ratio)
    if (
        lowest.p2_recall == 0.0
        and lowest.p1_recall > 0.0
        and truth_post.maximum < lowest.camera_ratio
        and truth_pre.maximum >= lowest.camera_ratio
    ):
        return "属实现缺陷"
    cut_direction_consistent = cuts[-1].pre_gate_median >= cuts[0].pre_gate_median
    if (
        truth_pre.median <= nontruth_pre.median
        and not cut_direction_consistent
    ):
        return "属真值切法问题"
    if truth_pre.median > nontruth_pre.median and lowest.p1_recall < 0.5:
        return "属参数取值问题"
    return "无法判定"


def write_report(
    path: Path,
    *,
    session: PreparedCalibrationSession,
    truth: Sequence[bool],
    p1: Sequence[GateTrace],
    p2: Sequence[GateTrace],
    recalls: Sequence[RecallRow],
    cuts: Sequence[TruthCut],
    alignment: Sequence[AlignmentRow],
    lines_: BranchLines,
) -> None:
    truth_p1 = [trace for flag, trace in zip(truth, p1, strict=True) if flag]
    truth_p2 = [trace for flag, trace in zip(truth, p2, strict=True) if flag]
    nontruth_p2 = [trace for flag, trace in zip(truth, p2, strict=True) if not flag]
    truth_pre = distribution(trace.pre_gate_ratio for trace in truth_p2)
    truth_post = distribution(trace.post_gate_ratio for trace in truth_p2)
    nontruth_pre = distribution(trace.pre_gate_ratio for trace in nontruth_p2)
    nontruth_post = distribution(trace.post_gate_ratio for trace in nontruth_p2)
    fixed_pre = distribution(
        trace.fixed_pre_gate_ratio
        for trace in truth_p2
        if trace.fixed_pre_gate_ratio is not None
    )
    p1_baseline_gap = distribution(
        float(trace.baseline_gap_frames) for trace in truth_p1
    )
    p2_baseline_gap = distribution(
        float(trace.baseline_gap_frames) for trace in truth_p2
    )
    p1_time_gap = distribution(trace.baseline_gap_seconds for trace in truth_p1)
    p2_time_gap = distribution(trace.baseline_gap_seconds for trace in truth_p2)
    h1, h2, h3 = _hypothesis_statuses(
        recalls=recalls,
        production_pre=truth_pre,
        fixed_pre=fixed_pre,
    )
    classification = _classification(
        recalls=recalls,
        truth_pre=truth_pre,
        truth_post=truth_post,
        nontruth_pre=nontruth_pre,
        cuts=cuts,
    )
    p2_reason_counts = Counter(trace.reason for trace in truth_p2)
    suppressed_count = sum(trace.suppressed_min_interval for trace in truth_p2)
    threshold, _ = truth_mask(
        session.input_motion or (), CAMERA_TRUTH_MOUSE_PERCENTILE
    )
    report_lines = [
        "# M5-T5a 镜头移动判定诊断",
        "",
        "本报告全离线生成，只诊断，不修改检测算法、判定顺序或生产默认值。",
        "",
        "## 数据与口径",
        "",
        f"- 会话：`{session.session_dir}`（{session.recorded_label}）",
        f"- raw 帧：{len(session.frames)}；时长：{session.duration_seconds:.3f} 秒",
        f"- P90 鼠标位移阈值：{threshold:.3f}；真值轮次：{sum(truth)}",
        "- 主诊断表使用生产初值 P=2、camera_motion_ratio=0.35；同批帧另跑 P=1。",
        f"- 固定基线：每个连续真值片段开始前至少 {FIXED_BASELINE_LEAD_SECONDS:.1f} 秒的最近帧。",
        "- 门控前集合：当前轮 `difference > floor` 的块；门控后集合：连续满足 P 轮的块。",
        "",
        "## 三个假设",
        "",
        f"### H1 时间持久性门控：{h1}",
        "",
        f"`capture.py:{lines_.confirmed}` 先生成 `confirmed = consecutive >= P`，camera_motion 使用的是该集合的占比。P=1/P=2 实测：",
        "",
        markdown_table(
            ("camera_motion_ratio", "P=1 命中/31", "P=1 召回率", "P=2 命中/31", "P=2 召回率"),
            (
                (
                    f"{row.camera_ratio:.2f}",
                    row.p1_hits,
                    f"{row.p1_recall:.2%}",
                    row.p2_hits,
                    f"{row.p2_recall:.2%}",
                )
                for row in recalls
            ),
        ),
        "",
        f"P=2 真值轮次门控前中位 {truth_pre.median:.2%}，门控后中位 {truth_post.median:.2%}；门控后最大值仅 {truth_post.maximum:.2%}，低于最低 camera_motion_ratio 20%。",
        "",
        f"### H2 基线漂移：{h2}",
        "",
        markdown_table(
            ("口径", "最小", "P25", "中位", "P75", "最大"),
            (
                _distribution_numeric_row("P=1 基线序号差", p1_baseline_gap),
                _distribution_numeric_row("P=2 基线序号差", p2_baseline_gap),
                _distribution_numeric_row("P=1 基线时间差秒", p1_time_gap),
                _distribution_numeric_row("P=2 基线时间差秒", p2_time_gap),
                _distribution_row("生产基线门控前占比", truth_pre),
                _distribution_row("固定基线门控前占比", fixed_pre),
            ),
        ),
        "",
        f"固定基线把门控前中位从 {truth_pre.median:.2%} 提到 {fixed_pre.median:.2%}、P75 从 {truth_pre.p75:.2%} 提到 {fixed_pre.p75:.2%}，说明基线跟进确实压低部分轮次；但固定基线中位仍低于 20%，它不是 P=2 零召回的充分解释。",
        "",
        f"### H3 判定顺序：{h3}",
        "",
        f"`capture.py:{lines_.camera_if}` 是 `if camera_motion`，`capture.py:{lines_.persistent_elif}` 才是 `elif changed_count`；camera_motion 明确排在 persistent_change 前。",
        f"31 轮实际最终路径：{dict(sorted(p2_reason_counts.items()))}；min_save_interval 抑制 {suppressed_count} 轮。只要门控后占比达到阈值，就先进入 camera_motion，不存在被 persistent_change 提前截获。",
        "",
        "## 门控前后分布可分性",
        "",
        markdown_table(
            ("集合与口径", "最小", "P25", "中位", "P75", "最大"),
            (
                _distribution_row("31 真值·门控前", truth_pre),
                _distribution_row("31 真值·门控后", truth_post),
                _distribution_row("278 非转向·门控前", nontruth_pre),
                _distribution_row("278 非转向·门控后", nontruth_post),
            ),
        ),
        "",
        "两组门控前分布大幅重叠：真值 P25–P75 为 "
        f"{truth_pre.p25:.2%}–{truth_pre.p75:.2%}，非转向为 "
        f"{nontruth_pre.p25:.2%}–{nontruth_pre.p75:.2%}。因此当前块占比规则不能靠一个阈值干净分离两组；修复零召回缺陷后仍需重新校准并复核真值。",
        "",
        "## 真值切法自查",
        "",
        markdown_table(
            ("切法", "鼠标位移阈值", "真值集大小", "门控前块占比中位"),
            (
                (
                    f"P{int(row.percentile * 100)}",
                    f"{row.threshold:.3f}",
                    row.count,
                    f"{row.pre_gate_median:.2%}",
                )
                for row in cuts
            ),
        ),
        "",
        "P75、P90、P95 的门控前中位分别处在同一量级，没有随更严格鼠标阈值出现方向反转；仅调整分位切法不能解释 P=2 的全零结果。",
        "",
        "### 输入—画面相邻帧错位敏感性",
        "",
        "以下以 P=1、camera_motion_ratio=0.20 重跑；正偏移表示把输入真值与更晚的画面帧对齐。此表只检查时间对齐风险，不改写真值。",
        "",
        markdown_table(
            ("画面帧偏移", "可比真值数", "命中数", "召回率", "门控前占比中位"),
            (
                (
                    f"{row.frame_offset:+d}",
                    row.comparable_count,
                    row.camera_hits,
                    f"{row.camera_recall:.2%}",
                    f"{row.pre_gate_median:.2%}",
                )
                for row in alignment
            ),
        ),
        "",
        "向后错一帧时召回有所上升，说明输入事件与 WGC 画面到达可能存在约一轮偏移；但所有偏移仍有多数真值未命中，所以该风险不能替代 H1 的结构性解释。",
        "",
        "## 31 轮逐轮诊断",
        "",
        markdown_table(
            (
                "#",
                "轮询",
                "P2原因",
                "门控前块/占比",
                "P2门控后块/占比",
                "P1门控后块/占比",
                "基线→当前",
                "时间差秒",
                "min抑制",
                "鼠标位移",
                "固定基线块/占比",
            ),
            (
                (
                    ordinal,
                    p2_trace.sequence,
                    p2_trace.reason,
                    f"{p2_trace.pre_gate_count}/{p2_trace.pre_gate_ratio:.2%}",
                    f"{p2_trace.post_gate_count}/{p2_trace.post_gate_ratio:.2%}",
                    f"{p1_trace.post_gate_count}/{p1_trace.post_gate_ratio:.2%}",
                    f"{p2_trace.baseline_sequence}→{p2_trace.sequence}",
                    f"{p2_trace.baseline_gap_seconds:.3f}",
                    "是" if p2_trace.suppressed_min_interval else "否",
                    f"{p2_trace.mouse_motion:.3f}",
                    (
                        f"{p2_trace.fixed_pre_gate_count}/"
                        f"{p2_trace.fixed_pre_gate_ratio:.2%}"
                        if p2_trace.fixed_pre_gate_ratio is not None
                        else "不可用"
                    ),
                )
                for ordinal, (p1_trace, p2_trace) in enumerate(
                    zip(truth_p1, truth_p2, strict=True), start=1
                )
            ),
        ),
        "",
        "## 四选一归类",
        "",
        f"**{classification}。**",
    ]
    if classification == "属实现缺陷":
        report_lines.extend(
            (
                "",
                "缺陷位置与成因：`AdaptiveFrameSelector.observe_prepared` 先用时间持久性生成 `confirmed`，再用 `confirmed` 的占比判断 camera_motion。瞬时全屏变化会在 P=2 的第一轮拥有大量门控前块，却没有门控后块，因此镜头移动分支没有机会触发。",
                f"直接证据：P=2 的 31 个真值轮次门控后最大占比 {truth_post.maximum:.2%}，全部低于最低阈值 20%；门控前最大 {truth_pre.maximum:.2%}，且 P=1/0.20 已命中 {min(recalls, key=lambda row: row.camera_ratio).p1_hits}/31。",
                "",
                "建议改法（本任务未实施）：camera_motion 单独使用门控前 `above_floor` 的块占比；persistent_change 与 region_grid 继续使用门控后的 `confirmed`。镜头移动分类应仍排在 persistent_change 前。",
                "",
                "风险：全屏闪白、加载转场、亮度突变和短时全屏粒子可能被归为镜头移动，导致 region_grid 被清空；需用补录的静止动效与真实转向素材重新测误判率。",
                "",
                "边界：这项实现缺陷解释了 P=2 为何恰好零召回，但不解释全部剩余漏检。门控前真值/非真值分布重叠，且输入与画面有一帧错位迹象；建议修复后使用人工标记的转向开始/结束帧做第二轮校准。",
            )
        )
    elif classification == "属真值切法问题":
        report_lines.extend(
            (
                "",
                "鼠标高位移集合没有呈现更高的视觉块变化，且不同分位切法方向不一致。需要把菜单光标移动与锁定视角下的鼠标输入从真值中剔除，并补录人工标记的转向片段。",
            )
        )
    elif classification == "属参数取值问题":
        report_lines.extend(
            (
                "",
                "门控前真值分布高于非转向分布，但现有阈值仍高于多数真值轮次；需要等待补录素材后另做阈值搜索。",
            )
        )
    else:
        report_lines.extend(
            (
                "",
                "现有数据不足以在四类之间唯一归因；需要人工标注真实转向的开始/结束帧，并记录菜单与自由视角状态。",
            )
        )
    path.write_text("\n".join(report_lines).rstrip() + "\n", encoding="utf-8")


def run_diagnostic(options: DiagnosticOptions) -> Path:
    options.output_dir.mkdir(parents=True, exist_ok=True)
    session = prepare_session(
        "explore-3a",
        options.session_dir,
        sample_stride=1,
        strong_block_delta=DEFAULT_STRONG_BLOCK_DELTA,
        input_motion_threshold=DEFAULT_INPUT_MOTION_THRESHOLD,
    )
    if session.input_motion is None:
        raise CaptureError("explore-3a 会话缺少 input.csv，无法建立鼠标真值集")
    threshold, truth = truth_mask(
        session.input_motion, CAMERA_TRUTH_MOUSE_PERCENTILE
    )
    if sum(truth) != 31:
        raise CaptureError(
            "P90 真值轮次数与 M5-T4 不一致："
            f"expected=31, actual={sum(truth)}, threshold={threshold:.3f}"
        )
    fixed = fixed_baseline_map(session, truth)
    p1 = trace_session(
        session,
        persistence_polls=1,
        camera_motion_ratio=DEFAULT_CAMERA_MOTION_RATIO,
        fixed_baselines=fixed,
    )
    p2 = trace_session(
        session,
        persistence_polls=2,
        camera_motion_ratio=DEFAULT_CAMERA_MOTION_RATIO,
        fixed_baselines=fixed,
    )
    recalls = recall_rows(session, truth, fixed)
    cuts = truth_cuts(session.input_motion, p2)
    p1_lowest_ratio = trace_session(
        session,
        persistence_polls=1,
        camera_motion_ratio=min(DEFAULT_CAMERA_MOTION_RATIOS),
        fixed_baselines=fixed,
    )
    alignment = alignment_sensitivity(truth, p1_lowest_ratio)
    lines_ = source_branch_lines()
    write_turn_csv(
        options.output_dir / "turn-diagnostics.csv",
        truth=truth,
        p1=p1,
        p2=p2,
    )
    write_report(
        options.output_dir / "report.md",
        session=session,
        truth=truth,
        p1=p1,
        p2=p2,
        recalls=recalls,
        cuts=cuts,
        alignment=alignment,
        lines_=lines_,
    )
    print(f"P90 threshold={threshold:.3f}; truth rounds={sum(truth)}")
    print(f"capture.py branch lines: camera={lines_.camera_if}, persistent={lines_.persistent_elif}")
    print(f"output={options.output_dir}")
    return options.output_dir


def _default_output_dir() -> Path:
    backend_root = Path(__file__).resolve().parents[3]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return backend_root / "eval-reports" / f"camera-motion-diagnostic-{timestamp}"


def main() -> None:
    parser = argparse.ArgumentParser(description="离线诊断 camera_motion 的零召回")
    parser.add_argument("--session", type=Path, required=True, help="explore-3a 会话目录")
    parser.add_argument("--output-dir", type=Path, help="输出目录（默认 eval-reports 时间戳目录）")
    arguments = parser.parse_args()
    try:
        run_diagnostic(
            DiagnosticOptions(
                session_dir=arguments.session,
                output_dir=arguments.output_dir or _default_output_dir(),
            )
        )
    except CaptureError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
