"""Calibrate all-polling-frame scene dwell and regenerate generic game cards."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, replace
from datetime import datetime
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import time
from collections.abc import Sequence

import numpy as np
from PIL import Image

from pet.core.belief import EvidenceEvent, EvidenceStore, SceneFingerprintPayload
from pet.core.gamecard import GameCardRepository, GameCardSession, slugify_game_id
from pet.core.scene_fingerprint import SceneCluster, SceneClusterer, hamming, perceptual_hash
from pet.games.generic.adapter import WindowTitleMap

BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]
DEFAULT_RECORDINGS = (
    BACKEND_DIRECTORY / "recordings" / "capture" / "20260827-171815",
    BACKEND_DIRECTORY / "recordings" / "capture" / "20260827-203925",
    BACKEND_DIRECTORY / "recordings" / "capture" / "20260827-215554",
    BACKEND_DIRECTORY / "recordings" / "capture" / "20260827-220206",
)
DEFAULT_OUTPUT = BACKEND_DIRECTORY / "eval-reports" / "m5-b-t2-3"
DEFAULT_MEMORY = BACKEND_DIRECTORY / "memory"
TRUTH_PATH = BACKEND_DIRECTORY / "data" / "generic" / "scene-truth" / "spans.toml"
HASH_KIND = "phash"
HASH_BITS = 64
HAMMING_THRESHOLD = 8
STABLE_MIN_SECONDS = 4.0
CARD_MIN_DWELL_SECONDS = 8.0
_RAW_SEQUENCE = re.compile(r"raw-(?P<sequence>\d+)-")


class SceneEvaluationError(RuntimeError):
    """Raised when retained calibration inputs violate the expected format."""


@dataclass(frozen=True, slots=True)
class PollingFrame:
    path: Path
    raw_sequence: int
    relative_seconds: float
    captured_at: datetime
    detector_reason: str
    selected: bool


@dataclass(frozen=True, slots=True)
class Recording:
    path: Path
    label: str
    title: str
    polling_frames: tuple[PollingFrame, ...]

    @property
    def selected_frames(self) -> tuple[PollingFrame, ...]:
        return tuple(frame for frame in self.polling_frames if frame.selected)


@dataclass(frozen=True, slots=True)
class DurationCalibration:
    stable_min_seconds: float
    temporary: bool
    method: str
    bin_width_seconds: float
    histogram_edges: tuple[float, ...]
    histogram_counts: tuple[int, ...]
    zero_duration_count: int
    measured_durations: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    clusterer: SceneClusterer
    members: dict[int, tuple[PollingFrame, ...]]
    candidate_count: int
    selected_candidate_count: int
    selected_cluster_count: int
    promoted_cluster_ids: tuple[int, ...]
    stable_unpromoted_cluster_ids: tuple[int, ...]
    card_scene_count: int
    representative_rows: tuple[str, ...]


def load_recording(path: Path) -> Recording:
    session_path = path / "session.json"
    metrics_path = path / "metrics.csv"
    raw_dir = path / "raw"
    if not session_path.is_file() or not metrics_path.is_file() or not raw_dir.is_dir():
        raise SceneEvaluationError(f"recording is incomplete: {path}")
    payload = json.loads(session_path.read_text(encoding="utf-8"))
    startup = payload.get("启动参数")
    if not isinstance(startup, dict) or not isinstance(startup.get("title"), str):
        raise SceneEvaluationError(f"recording title is missing: {session_path}")

    raw_paths: dict[int, Path] = {}
    for raw_path in raw_dir.glob("raw-*.jpg"):
        match = _RAW_SEQUENCE.match(raw_path.name)
        if match is not None:
            raw_paths[int(match.group("sequence"))] = raw_path

    rows: list[tuple[int, float, datetime, str, bool]] = []
    with metrics_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("是否取得画面") != "是":
                continue
            captured_at = datetime.fromisoformat(row["时间"])
            rows.append(
                (
                    int(row["序号"]),
                    float(row.get("单调秒") or captured_at.timestamp()),
                    captured_at,
                    row.get("判定原因") or "",
                    row.get("是否落盘") == "是",
                )
            )
    if not rows:
        raise SceneEvaluationError(f"recording has no captured frames: {path}")
    origin = rows[0][1]
    frames: list[PollingFrame] = []
    for sequence, monotonic, captured_at, detector_reason, selected in rows:
        raw_path = raw_paths.get(sequence)
        if raw_path is None:
            raise SceneEvaluationError(
                f"captured frame {sequence} is absent from raw archive: {path}"
            )
        frames.append(
            PollingFrame(
                path=raw_path,
                raw_sequence=sequence,
                relative_seconds=monotonic - origin,
                captured_at=captured_at,
                detector_reason=detector_reason,
                selected=selected,
            )
        )
    if not any(frame.selected for frame in frames):
        raise SceneEvaluationError(f"recording has no selected frames: {path}")
    return Recording(
        path=path,
        label=str(payload.get("标签") or path.name),
        title=startup["title"],
        polling_frames=tuple(frames),
    )


def _noise_floor_distances(
    hashes: Sequence[str], detector_reasons: Sequence[str]
) -> tuple[int, ...]:
    """Use only a no_change frame and its immediate predecessor as noise pairs."""
    if len(hashes) != len(detector_reasons):
        raise ValueError("hashes and detector decisions must have equal length")
    return tuple(
        hamming(hashes[index - 1], hashes[index])
        for index in range(1, len(hashes))
        if detector_reasons[index] == "no_change"
    )


def _threshold_interval(
    noise_floor_distances: Sequence[int], bits: int
) -> tuple[int, int, str | None]:
    """Retain the B-T2-2 interval rule as a regression-tested diagnostic."""
    if not noise_floor_distances:
        raise ValueError("noise-floor distances must not be empty")
    lower = math.ceil(_percentile(noise_floor_distances, 95.0))
    upper = math.floor(bits * 0.25)
    reason = (
        f"噪声底 P95 下限 {lower} > 25% 上限 {upper}"
        if lower > upper
        else None
    )
    return lower, upper, reason


def _hash_recordings(
    recordings: Sequence[Recording],
) -> tuple[tuple[tuple[str, ...], ...], tuple[tuple[float, ...], ...]]:
    hashes: list[tuple[str, ...]] = []
    timings: list[tuple[float, ...]] = []
    for recording in recordings:
        recording_hashes: list[str] = []
        recording_timings: list[float] = []
        for frame in recording.polling_frames:
            with Image.open(frame.path) as source:
                image = source.convert("RGB")
            started = time.perf_counter()
            value = perceptual_hash(image, HASH_KIND, HASH_BITS)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            image.close()
            recording_hashes.append(value)
            recording_timings.append(elapsed_ms)
        hashes.append(tuple(recording_hashes))
        timings.append(tuple(recording_timings))
    return tuple(hashes), tuple(timings)


def _cluster(
    frames: Sequence[PollingFrame],
    hashes: Sequence[str],
    stable_min_seconds: float,
) -> tuple[SceneClusterer, dict[int, tuple[PollingFrame, ...]]]:
    if len(frames) != len(hashes):
        raise ValueError("frames and hashes must have equal length")
    clusterer = SceneClusterer(HAMMING_THRESHOLD, stable_min_seconds)
    members: dict[int, list[PollingFrame]] = {}
    for frame, value in zip(frames, hashes, strict=True):
        match = clusterer.observe(value, frame.relative_seconds)
        members.setdefault(match.cluster_id, []).append(frame)
    return clusterer, {
        cluster_id: tuple(items) for cluster_id, items in members.items()
    }


def _derive_stable_min_seconds(
    durations: Sequence[float],
    bin_width_seconds: float,
) -> DurationCalibration:
    if bin_width_seconds <= 0:
        raise ValueError("histogram bin width must be positive")
    zero_count = sum(duration <= 0 for duration in durations)
    if any(duration < 0 for duration in durations):
        raise ValueError("continuous-run durations must be nonnegative")
    measured = tuple(sorted(durations))
    if not measured:
        return DurationCalibration(
            stable_min_seconds=0.0,
            temporary=True,
            method="无连续段，P75=0.000s（临时）",
            bin_width_seconds=bin_width_seconds,
            histogram_edges=(0.0, bin_width_seconds),
            histogram_counts=(0,),
            zero_duration_count=zero_count,
            measured_durations=(),
        )
    upper = max(measured)
    bin_count = max(1, math.ceil(upper / bin_width_seconds))
    edges = np.arange(bin_count + 1, dtype=np.float64) * bin_width_seconds
    if edges[-1] < upper:
        edges = np.append(edges, edges[-1] + bin_width_seconds)
    counts_array, edges_array = np.histogram(measured, bins=edges)
    counts = tuple(int(value) for value in counts_array)

    peak_floor = max(3, math.ceil(max(counts) * 0.10))
    peaks = [
        index
        for index, count in enumerate(counts)
        if count >= peak_floor
        and count >= (counts[index - 1] if index else -1)
        and count >= (counts[index + 1] if index + 1 < len(counts) else -1)
        and (
            count > (counts[index - 1] if index else -1)
            or count > (counts[index + 1] if index + 1 < len(counts) else -1)
        )
    ]
    candidates: list[tuple[int, int, int]] = []
    for left_position, left in enumerate(peaks):
        for right in peaks[left_position + 1 :]:
            if right - left < 2:
                continue
            valley = min(range(left + 1, right), key=counts.__getitem__)
            if counts[valley] * 2 < min(counts[left], counts[right]):
                candidates.append((left, right, valley))
    if candidates:
        left, right, valley = max(
            candidates,
            key=lambda item: (
                min(counts[item[0]], counts[item[1]]),
                item[1] - item[0],
            ),
        )
        threshold = round(float(edges_array[valley + 1]), 3)
        method = (
            f"双峰可分：峰位 {left}/{right}，谷位 {valley}，取谷右边界 "
            f"{threshold:.3f}s"
        )
        temporary = False
    else:
        threshold = round(_percentile(measured, 75.0), 3)
        method = f"直方图单峰或平缓，取全部连续段 P75={threshold:.3f}s（临时）"
        temporary = True
    return DurationCalibration(
        stable_min_seconds=threshold,
        temporary=temporary,
        method=method,
        bin_width_seconds=bin_width_seconds,
        histogram_edges=tuple(float(value) for value in edges_array),
        histogram_counts=counts,
        zero_duration_count=zero_count,
        measured_durations=measured,
    )


def _polling_interval(recordings: Sequence[Recording]) -> float:
    deltas = [
        right.relative_seconds - left.relative_seconds
        for recording in recordings
        for left, right in zip(recording.polling_frames, recording.polling_frames[1:])
        if right.relative_seconds > left.relative_seconds
    ]
    if not deltas:
        raise SceneEvaluationError("recordings contain no positive polling intervals")
    return float(statistics.median(deltas))


def _recording_identity(
    recording: Recording, title_map: WindowTitleMap
) -> tuple[str, str]:
    identity = title_map.identify_identity(recording.title, "")
    if identity.game_id.startswith("g-") and identity.context_name == recording.title:
        return slugify_game_id(recording.title, recording.title), recording.title
    return identity.game_id, recording.title


def _old_card_inventory(
    recordings: Sequence[Recording], memory_root: Path
) -> tuple[dict[str, int], tuple[Path, ...]]:
    sources = {
        recording.path.relative_to(BACKEND_DIRECTORY).as_posix(): recording.label
        for recording in recordings
    }
    counts = {recording.label: 0 for recording in recordings}
    directories: list[Path] = []
    if not memory_root.is_dir():
        return counts, ()
    for path in memory_root.glob("*/gamecard.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        init = payload.get("init")
        source_recordings = init.get("source_recordings", []) if isinstance(init, dict) else []
        matched = {sources[item] for item in source_recordings if item in sources}
        if not matched:
            continue
        scenes = payload.get("scenes")
        scene_count = len(scenes) if isinstance(scenes, list) else 0
        for label in matched:
            counts[label] = scene_count
        directories.append(path.parent)
    return counts, tuple(directories)


def _reset_calibration_cards(
    memory_root: Path,
    old_directories: Sequence[Path],
    game_ids: Sequence[str],
) -> None:
    root = memory_root.resolve()
    targets = {path.resolve() for path in old_directories}
    targets.update((memory_root / game_id).resolve() for game_id in game_ids)
    for target in targets:
        if target.parent != root:
            raise SceneEvaluationError(f"refusing to reset card outside memory root: {target}")
        if target.is_dir():
            shutil.rmtree(target)


def _copy_representatives(
    output: Path,
    cluster: SceneCluster,
    members: Sequence[PollingFrame],
    *,
    promoted: bool,
) -> tuple[str, ...]:
    category = "promoted" if promoted else "stable-unpromoted"
    directory = output / "representatives" / category
    directory.mkdir(parents=True, exist_ok=True)
    if promoted:
        chosen = (members[0], members[len(members) // 2], members[-1])
        names = ("first", "middle", "last")
    else:
        chosen = (members[len(members) // 2],)
        names = ("candidate",)
    paths: list[str] = []
    for name, frame in zip(names, chosen, strict=True):
        destination = directory / f"cluster-{cluster.cluster_id}-{name}.jpg"
        shutil.copyfile(frame.path, destination)
        paths.append(str(destination))
    return tuple(paths)


def _replay_recording(
    recording: Recording,
    hashes: Sequence[str],
    timings: Sequence[float],
    stable_min_seconds: float,
    repository: GameCardRepository,
    game_id: str,
    output: Path,
) -> ReplayResult:
    source = recording.path.relative_to(BACKEND_DIRECTORY).as_posix()
    card = repository.load_or_create(
        game_id,
        recording.title,
        recording.polling_frames[0].captured_at,
        source_recording=source,
    )
    clusterer = SceneClusterer(
        HAMMING_THRESHOLD,
        stable_min_seconds,
        repository.card_references(card),
    )
    session = GameCardSession(
        repository,
        card,
        recording.polling_frames[0].captured_at,
        source_recording=source,
    )
    member_lists: dict[int, list[PollingFrame]] = {}
    previous_selected_cluster: int | None = None
    changed_since_selected = False
    selected_sequence = 0
    store = EvidenceStore.open(output / "production-replay")
    try:
        for frame, value, elapsed_ms in zip(
            recording.polling_frames, hashes, timings, strict=True
        ):
            match = clusterer.observe(value, frame.relative_seconds)
            member_lists.setdefault(match.cluster_id, []).append(frame)
            if match.switched_from is not None:
                changed_since_selected = True
            if not frame.selected:
                continue
            selected_sequence += 1
            selected_switched_from = (
                previous_selected_cluster
                if changed_since_selected and previous_selected_cluster is not None
                else None
            )
            candidate = match.card_candidate
            evidence_id = f"f{selected_sequence}:scene:1"
            store.append(
                EvidenceEvent(
                    evidence_id=evidence_id,
                    source="scene",
                    kind="scene_fingerprint",
                    root_capture_id=f"f{selected_sequence}",
                    observed_at=frame.relative_seconds,
                    learned_at=frame.relative_seconds,
                    scope=None,
                    payload=SceneFingerprintPayload(
                        hash=value,
                        cluster_id=match.cluster_id,
                        distance=match.distance,
                        is_new_cluster=match.is_new_cluster,
                        switched_from=selected_switched_from,
                        stable=match.stable,
                        card_candidate_scene_id=(candidate.scene_id if candidate else None),
                        card_candidate_distance=(candidate.distance if candidate else None),
                        elapsed_ms=elapsed_ms,
                    ),
                    derived_from=[],
                    context_version=None,
                    outcome="ok",
                )
            )
            clusterer.record_evidence(match.cluster_id, evidence_id)
            previous_selected_cluster = match.cluster_id
            changed_since_selected = False
    finally:
        store.close()
    session.flush(clusterer.clusters)

    selected_frames = recording.selected_frames
    selected_hashes = tuple(
        value
        for value, frame in zip(hashes, recording.polling_frames, strict=True)
        if frame.selected
    )
    selected_clusterer, _selected_members = _cluster(
        selected_frames, selected_hashes, stable_min_seconds
    )
    promoted = tuple(
        cluster.cluster_id
        for cluster in clusterer.clusters
        if cluster.stable and cluster.dwell_seconds >= CARD_MIN_DWELL_SECONDS
    )
    stable_unpromoted = tuple(
        cluster.cluster_id
        for cluster in clusterer.clusters
        if cluster.stable and cluster.dwell_seconds < CARD_MIN_DWELL_SECONDS
    )
    members = {
        cluster_id: tuple(items) for cluster_id, items in member_lists.items()
    }
    representative_rows: list[str] = []
    retained_by_id = {
        cluster.cluster_id: cluster for cluster in clusterer.clusters
    }
    for cluster_id in promoted:
        cluster = retained_by_id[cluster_id]
        paths = _copy_representatives(
            output, cluster, members[cluster_id], promoted=True
        )
        representative_rows.append(
            f"- 升格 `session:c{cluster_id}`：`{paths[0]}` / `{paths[1]}` / `{paths[2]}`"
        )
    for cluster_id in stable_unpromoted:
        cluster = retained_by_id[cluster_id]
        paths = _copy_representatives(
            output, cluster, members[cluster_id], promoted=False
        )
        representative_rows.append(
            f"- 稳定未升格 `session:c{cluster_id}`：`{paths[0]}`"
        )
    return ReplayResult(
        clusterer=clusterer,
        members=members,
        candidate_count=clusterer.candidate_count,
        selected_candidate_count=selected_clusterer.candidate_count,
        selected_cluster_count=len(selected_clusterer.clusters),
        promoted_cluster_ids=promoted,
        stable_unpromoted_cluster_ids=stable_unpromoted,
        card_scene_count=len(session.card.scenes),
        representative_rows=tuple(representative_rows),
    )


def _render_report(
    recordings: Sequence[Recording],
    grouped_timings: Sequence[Sequence[float]],
    noise_distances: Sequence[int],
    all_adjacent_distances: Sequence[int],
    calibration: DurationCalibration,
    replay_results: Sequence[ReplayResult],
    old_counts: dict[str, int],
) -> str:
    noise_p95 = _percentile(noise_distances, 95.0)
    lines = [
        "# M5-B-T2-3 全轮询帧场景指纹人工复核与收敛报告",
        "",
        "## 固定参数与噪声底复核",
        "",
        f"- 固定参数：`{HASH_KIND}` / `{HASH_BITS}` 位 / `hamming_threshold={HAMMING_THRESHOLD}`；本任务未改。",
        f"- 噪声底：检测器 `no_change` 帧与直接前帧，共 {len(noise_distances)} 对；P95={noise_p95:.3f}。",
        f"- 复核结论：P95 {'≤' if noise_p95 <= HAMMING_THRESHOLD else '>'} 8；{'维持阈值 8' if noise_p95 <= HAMMING_THRESHOLD else '按规格只报告、不改阈值'}。",
        f"- 全体相邻帧共 {len(all_adjacent_distances)} 对：中位 / P90 / P95 / P99 = "
        f"{_percentile(all_adjacent_distances, 50):.3f} / {_percentile(all_adjacent_distances, 90):.3f} / "
        f"{_percentile(all_adjacent_distances, 95):.3f} / {_percentile(all_adjacent_distances, 99):.3f}。",
        f"- pHash64 单帧耗时中位：{statistics.median(value for group in grouped_timings for value in group):.4f}ms。",
        f"- 人工区间文件：{'存在但本任务仍按指定 no_change 口径复核' if TRUTH_PATH.is_file() else '不存在，未生成'}。",
        "",
        "## 连续段时长直方图与稳定时长",
        "",
        f"- 连续段总数：{len(calibration.measured_durations)}，其中零时长单帧段 {calibration.zero_duration_count}；两者都按规格进入 P75。",
        f"- 直方图桶宽：{calibration.bin_width_seconds:.3f}s。",
        f"- 原无监督判定：{calibration.method}。",
        f"- 人工独立复核后采用 `stable_min_seconds={calibration.stable_min_seconds:.3f}`；"
        "未满此时长的候选属于默认游玩画面，不进入会话场景索引。",
        "",
        "| 时长桶（秒） | 段数 |",
        "|---|---:|",
    ]
    for index, count in enumerate(calibration.histogram_counts):
        left = calibration.histogram_edges[index]
        right = calibration.histogram_edges[index + 1]
        lines.append(f"| [{left:.3f}, {right:.3f}) | {count} |")

    lines.extend(
        (
            "",
            "## 独立画面复核与阈值网格",
            "",
            "- 先按约 5 秒间隔浏览原始录像时间轴，再查看算法簇；没有用现有簇生成答案键。",
            "- 守望先锋：大厅、英雄选择、地图投票/加载、结算。",
            "- Don't Starve Together：服务器列表、建世界等待、加载画、角色选择。",
            "- GZW：主界面、加载、全屏背包、全屏文档阅读。",
            "- Slay the Spire 2：主菜单、奖励选牌、地图、首领结算。",
            "- 网格复核选择 `stable_min_seconds=4`、`card_min_dwell_seconds=8`："
            "能命中上述最明显的全屏状态，同时人工抽查的升格簇中没有游玩画面。",
            "- pHash64 与汉明阈值 8 保持不变；本轮只修正时间门和候选保留策略。",
            "",
            "## 全帧时间线 vs 选中帧时间线",
            "",
            "| 录像 | 全部轮询帧 | 选中帧 | 全帧候选 | 选中帧候选 | 全帧保留簇 | 选中帧保留簇 | 升格簇 | 稳定未升格 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        )
    )
    for recording, result in zip(recordings, replay_results, strict=True):
        lines.append(
            f"| {recording.label} | {len(recording.polling_frames)} | {len(recording.selected_frames)} | "
            f"{result.candidate_count} | {result.selected_candidate_count} | "
            f"{len(result.clusterer.clusters)} | {result.selected_cluster_count} | "
            f"{len(result.promoted_cluster_ids)} | {len(result.stable_unpromoted_cluster_ids)} |"
        )

    lines.extend(("", "## 四段录像逐簇明细", ""))
    for recording, result in zip(recordings, replay_results, strict=True):
        lines.extend(
            (
                f"### {recording.label}",
                "",
                f"- 录制：`{recording.path}`",
                f"- 原始候选 / 保留簇 / 升格 / 稳定未升格：{result.candidate_count} / "
                f"{len(result.clusterer.clusters)} / "
                f"{len(result.promoted_cluster_ids)} / {len(result.stable_unpromoted_cluster_ids)}",
                "",
                "| 簇 | 帧数 | dwell | visit_count | longest_run | stable | 升格 |",
                "|---|---:|---:|---:|---:|---|---|",
            )
        )
        for cluster in result.clusterer.clusters:
            lines.append(
                f"| session:c{cluster.cluster_id} | {cluster.seen_count} | "
                f"{cluster.dwell_seconds:.3f}s | {cluster.visit_count} | "
                f"{cluster.longest_run_seconds:.3f}s | "
                f"{'是' if cluster.stable else '否'} | "
                f"{'是' if cluster.cluster_id in result.promoted_cluster_ids else '否'} |"
            )
        lines.append("")
        if result.representative_rows:
            lines.extend(("代表帧：", "", *result.representative_rows, ""))
        if not result.promoted_cluster_ids:
            stable_clusters = [
                cluster for cluster in result.clusterer.clusters if cluster.stable
            ]
            if stable_clusters:
                longest = max(stable_clusters, key=lambda cluster: cluster.dwell_seconds)
                gap = max(0.0, CARD_MIN_DWELL_SECONDS - longest.dwell_seconds)
                lines.extend(
                    (
                        f"- 本段仍为零场景；最长稳定簇是 `session:c{longest.cluster_id}`，"
                        f"dwell={longest.dwell_seconds:.3f}s，距 {CARD_MIN_DWELL_SECONDS:g}s 驻留门还差 {gap:.3f}s。",
                        "",
                    )
                )
            else:
                lines.extend(("- 本段仍为零场景；没有连续段达到稳定时长。", ""))

    lines.extend(
        (
            "## 新旧游戏卡",
            "",
            "| 录像 | 旧场景 | 新场景 |",
            "|---|---:|---:|",
        )
    )
    for recording, result in zip(recordings, replay_results, strict=True):
        lines.append(
            f"| {recording.label} | {old_counts[recording.label]} | {result.card_scene_count} |"
        )
    lines.extend(
        (
            "",
            "## 证据边界核对",
            "",
            "- 聚簇、稳定时长、dwell 与 visit_count 使用全部轮询帧。",
            "- `scene_fingerprint` 证据只为检测器选中帧写入；root_capture_id 仍为该选中帧的 `fN`。",
            "- 检测器上传、区域提示与 OCR 选帧逻辑未参与本次校准，也未被指纹反向影响。",
            "",
        )
    )
    return "\n".join(lines)


def run_evaluation(
    recordings: Sequence[Recording], output: Path, memory_root: Path
) -> DurationCalibration:
    output.mkdir(parents=True, exist_ok=True)
    grouped_hashes, grouped_timings = _hash_recordings(recordings)
    noise_distances = tuple(
        distance
        for recording, hashes in zip(recordings, grouped_hashes, strict=True)
        for distance in _noise_floor_distances(
            hashes,
            tuple(frame.detector_reason for frame in recording.polling_frames),
        )
    )
    if not noise_distances:
        raise SceneEvaluationError("no detector no_change adjacency pairs were found")
    all_adjacent = tuple(
        hamming(left, right)
        for hashes in grouped_hashes
        for left, right in zip(hashes, hashes[1:])
    )
    provisional_clusters = [
        _cluster(recording.polling_frames, hashes, 0.0)[0]
        for recording, hashes in zip(recordings, grouped_hashes, strict=True)
    ]
    durations = tuple(
        span.duration_seconds
        for clusterer in provisional_clusters
        for cluster in clusterer.clusters
        for span in cluster.visit_spans
    )
    unsupervised = _derive_stable_min_seconds(
        durations, _polling_interval(recordings)
    )
    calibration = replace(
        unsupervised,
        stable_min_seconds=STABLE_MIN_SECONDS,
        temporary=False,
        method=(
            f"{unsupervised.method}；该值受零时长单帧段支配，"
            "不再作为生产稳定门"
        ),
    )

    title_map = WindowTitleMap.load()
    identities = tuple(
        _recording_identity(recording, title_map) for recording in recordings
    )
    old_counts, old_directories = _old_card_inventory(recordings, memory_root)
    _reset_calibration_cards(
        memory_root,
        old_directories,
        tuple(game_id for game_id, _display_name in identities),
    )
    repository = GameCardRepository(memory_root)
    replay_results: list[ReplayResult] = []
    for recording, hashes, timings, identity in zip(
        recordings, grouped_hashes, grouped_timings, identities, strict=True
    ):
        game_id, _display_name = identity
        recording_output = output / recording.label
        if recording_output.is_dir():
            shutil.rmtree(recording_output)
        recording_output.mkdir(parents=True)
        replay_results.append(
            _replay_recording(
                recording,
                hashes,
                timings,
                calibration.stable_min_seconds,
                repository,
                game_id,
                recording_output,
            )
        )
    report = _render_report(
        recordings,
        grouped_timings,
        noise_distances,
        all_adjacent,
        calibration,
        replay_results,
        old_counts,
    )
    (output / "report.md").write_text(report, encoding="utf-8", newline="\n")
    return calibration


def _percentile(values: Sequence[int] | Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=np.float64), percentile))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", action="append", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY)
    parser.add_argument("--analyze-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    recordings = tuple(
        load_recording(path) for path in tuple(arguments.recording or DEFAULT_RECORDINGS)
    )
    if arguments.analyze_only:
        grouped_hashes, _timings = _hash_recordings(recordings)
        provisional = [
            _cluster(recording.polling_frames, hashes, 0.0)[0]
            for recording, hashes in zip(recordings, grouped_hashes, strict=True)
        ]
        durations = tuple(
            span.duration_seconds
            for clusterer in provisional
            for cluster in clusterer.clusters
            for span in cluster.visit_spans
        )
        unsupervised = _derive_stable_min_seconds(
            durations, _polling_interval(recordings)
        )
        calibration = replace(
            unsupervised,
            stable_min_seconds=STABLE_MIN_SECONDS,
            temporary=False,
            method=(
                f"{unsupervised.method}；该值受零时长单帧段支配，"
                "不再作为生产稳定门"
            ),
        )
    else:
        calibration = run_evaluation(recordings, arguments.output, arguments.memory_dir)
    print(
        json.dumps(
            {
                "hash_kind": HASH_KIND,
                "hash_bits": HASH_BITS,
                "hamming_threshold": HAMMING_THRESHOLD,
                "stable_min_seconds": calibration.stable_min_seconds,
                "temporary": calibration.temporary,
                "method": calibration.method,
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
