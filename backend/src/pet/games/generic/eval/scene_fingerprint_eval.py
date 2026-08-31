"""Calibrate full-frame perceptual hashes on retained production recordings."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
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

import numpy as np
from PIL import Image

from pet.core.belief import EvidenceEvent, EvidenceStore, SceneFingerprintPayload
from pet.core.gamecard import GameCardRepository, GameCardSession, slugify_game_id
from pet.core.scene_fingerprint import (
    HashBits,
    HashKind,
    SceneCluster,
    SceneClusterer,
    hamming,
    perceptual_hash,
)
from pet.games.generic.adapter import WindowTitleMap

BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]
DEFAULT_RECORDINGS = (
    BACKEND_DIRECTORY / "recordings" / "capture" / "20260827-171815",
    BACKEND_DIRECTORY / "recordings" / "capture" / "20260827-203925",
    BACKEND_DIRECTORY / "recordings" / "capture" / "20260827-215554",
    BACKEND_DIRECTORY / "recordings" / "capture" / "20260827-220206",
)
DEFAULT_OUTPUT = BACKEND_DIRECTORY / "eval-reports" / "m5-b-t2"
DEFAULT_MEMORY = BACKEND_DIRECTORY / "memory"
VARIANTS: tuple[tuple[HashKind, HashBits], ...] = (
    ("ahash", 64),
    ("ahash", 256),
    ("dhash", 64),
    ("dhash", 256),
    ("phash", 64),
    ("phash", 256),
)
_RAW_SEQUENCE = re.compile(r"raw-(?P<sequence>\d+)-")


class SceneEvaluationError(RuntimeError):
    """Raised when retained calibration inputs violate the expected format."""


@dataclass(frozen=True, slots=True)
class SelectedFrame:
    path: Path
    raw_sequence: int
    relative_seconds: float
    captured_at: datetime
    detector_large: bool


@dataclass(frozen=True, slots=True)
class Recording:
    path: Path
    label: str
    title: str
    frames: tuple[SelectedFrame, ...]


@dataclass(frozen=True, slots=True)
class CurvePoint:
    threshold: int
    cluster_count: int
    stable_cluster_count: int
    max_cluster_share: float
    singleton_cluster_share: float
    candidate_stable_min_frames: int


@dataclass(frozen=True, slots=True)
class VariantResult:
    kind: HashKind
    bits: HashBits
    hashes: tuple[tuple[str, ...], ...]
    elapsed_ms: tuple[float, ...]
    adjacent_distances: tuple[int, ...]
    p95: float
    curve: tuple[CurvePoint, ...]
    plateau_start: int
    plateau_end: int
    plateau_width_ratio: float
    stable_min_frames: int

    @property
    def median_elapsed_ms(self) -> float:
        return statistics.median(self.elapsed_ms)


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
    selected_rows: list[tuple[int, float, datetime, bool]] = []
    with metrics_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("是否取得画面") != "是" or row.get("是否落盘") != "是":
                continue
            sequence = int(row["序号"])
            captured_at = datetime.fromisoformat(row["时间"])
            monotonic = float(row.get("单调秒") or captured_at.timestamp())
            change_ratio = float(
                row.get("confirmed块占比")
                or row.get("确实变了的块占比")
                or 0.0
            )
            selected_rows.append(
                (sequence, monotonic, captured_at, change_ratio > 0.50)
            )
    if not selected_rows:
        raise SceneEvaluationError(f"recording has no selected frames: {path}")
    origin = selected_rows[0][1]
    frames: list[SelectedFrame] = []
    for sequence, monotonic, captured_at, detector_large in selected_rows:
        raw_path = raw_paths.get(sequence)
        if raw_path is None:
            raise SceneEvaluationError(
                f"selected frame {sequence} is absent from raw archive: {path}"
            )
        frames.append(
            SelectedFrame(
                path=raw_path,
                raw_sequence=sequence,
                relative_seconds=monotonic - origin,
                captured_at=captured_at,
                detector_large=detector_large,
            )
        )
    label = payload.get("标签")
    return Recording(
        path=path,
        label=str(label) if label else path.name,
        title=startup["title"],
        frames=tuple(frames),
    )


def analyze_variants(recordings: Sequence[Recording]) -> tuple[VariantResult, ...]:
    hashes: dict[tuple[HashKind, HashBits], list[list[str]]] = {
        variant: [[] for _ in recordings] for variant in VARIANTS
    }
    timings: dict[tuple[HashKind, HashBits], list[float]] = {
        variant: [] for variant in VARIANTS
    }
    for recording_index, recording in enumerate(recordings):
        for frame in recording.frames:
            with Image.open(frame.path) as source:
                image = source.convert("RGB")
            for variant in VARIANTS:
                started = time.perf_counter()
                value = perceptual_hash(image, *variant)
                timings[variant].append((time.perf_counter() - started) * 1000.0)
                hashes[variant][recording_index].append(value)
            image.close()

    results: list[VariantResult] = []
    for kind, bits in VARIANTS:
        grouped_hashes = tuple(tuple(items) for items in hashes[(kind, bits)])
        adjacent = tuple(
            hamming(left, right)
            for group in grouped_hashes
            for left, right in zip(group, group[1:])
        )
        p95 = _percentile(adjacent, 95.0)
        points: list[CurvePoint] = []
        for threshold in range(math.floor(bits * 0.40) + 1):
            candidate_stable = _stable_min_frames(grouped_hashes, threshold)
            points.append(
                _curve_point(grouped_hashes, threshold, candidate_stable)
            )
        start, end = _plateau(points, math.ceil(p95))
        results.append(
            VariantResult(
                kind=kind,
                bits=bits,
                hashes=grouped_hashes,
                elapsed_ms=tuple(timings[(kind, bits)]),
                adjacent_distances=adjacent,
                p95=p95,
                curve=tuple(points),
                plateau_start=start,
                plateau_end=end,
                plateau_width_ratio=(end - start) / bits,
                stable_min_frames=_stable_min_frames(grouped_hashes, start),
            )
        )
    return tuple(results)


def choose_default(results: Sequence[VariantResult]) -> VariantResult:
    feasible = [result for result in results if result.plateau_start >= math.ceil(result.p95)]
    if not feasible:
        fallback = next(
            result
            for result in results
            if result.kind == "phash" and result.bits == 64
        )
        boundary = fallback.curve[-1].threshold
        return replace(
            fallback,
            plateau_start=boundary,
            plateau_end=boundary,
            plateau_width_ratio=0.0,
            stable_min_frames=_stable_min_frames(fallback.hashes, boundary),
        )
    under_budget = [result for result in feasible if result.median_elapsed_ms < 1.0]
    candidates = under_budget or feasible
    return max(
        candidates,
        key=lambda result: (
            result.plateau_width_ratio,
            -result.median_elapsed_ms,
            -result.bits,
        ),
    )


def _curve_point(
    hashes: Sequence[Sequence[str]], threshold: int, stable_min_frames: int
) -> CurvePoint:
    clusters: list[SceneCluster] = []
    frame_count = 0
    for group in hashes:
        clusterer = SceneClusterer(threshold, stable_min_frames)
        for index, value in enumerate(group):
            clusterer.observe(value, float(index))
        clusters.extend(clusterer.clusters)
        frame_count += len(group)
    cluster_count = len(clusters)
    return CurvePoint(
        threshold=threshold,
        cluster_count=cluster_count,
        stable_cluster_count=sum(cluster.stable for cluster in clusters),
        max_cluster_share=(
            max((cluster.seen_count for cluster in clusters), default=0) / frame_count
        ),
        singleton_cluster_share=(
            sum(cluster.seen_count == 1 for cluster in clusters) / cluster_count
            if cluster_count
            else 0.0
        ),
        candidate_stable_min_frames=stable_min_frames,
    )


def _plateau(points: Sequence[CurvePoint], minimum_threshold: int) -> tuple[int, int]:
    eligible = [point for point in points if point.threshold >= minimum_threshold]
    if not eligible:
        if minimum_threshold > points[-1].threshold:
            return _plateau(points, 0)
        last = points[-1].threshold
        return last, last
    runs: list[tuple[int, int]] = []
    start = eligible[0].threshold
    previous = eligible[0]
    for point in eligible[1:]:
        if point.cluster_count != previous.cluster_count:
            runs.append((start, previous.threshold))
            start = point.threshold
        previous = point
    runs.append((start, previous.threshold))
    nonterminal = [run for run in runs if run[1] < eligible[-1].threshold]
    candidates = nonterminal or runs
    return max(candidates, key=lambda run: (run[1] - run[0], -run[0]))


def _stable_min_frames(hashes: Sequence[Sequence[str]], threshold: int) -> int:
    lengths: list[int] = []
    for group in hashes:
        run = 0
        for left, right in zip(group, group[1:]):
            if hamming(left, right) > threshold:
                run += 1
            elif run:
                lengths.append(run)
                run = 0
        if run:
            lengths.append(run)
    return max(1, math.ceil(_percentile(lengths, 90.0))) if lengths else 1


def _percentile(values: Sequence[int] | Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=np.float64), percentile))


def _histogram(values: Sequence[int], bits: int) -> tuple[tuple[str, int], ...]:
    step = max(1, bits // 32)
    counts: Counter[int] = Counter(min(value // step, 31) for value in values)
    rows = []
    for index in range(32):
        lower = index * step
        upper = bits if index == 31 else (index + 1) * step - 1
        rows.append((f"{lower}–{upper}", counts[index]))
    return tuple(rows)


def _cluster_recording(
    recording: Recording,
    hashes: Sequence[str],
    default: VariantResult,
    repository: GameCardRepository,
    game_id: str,
    *,
    card_min_visits: int,
    card_min_span_seconds: float,
    evidence_directory: Path | None = None,
) -> tuple[GameCardSession, SceneClusterer, dict[int, list[SelectedFrame]], Counter[str]]:
    source = recording.path.relative_to(BACKEND_DIRECTORY).as_posix()
    card = repository.load_or_create(
        game_id,
        recording.title,
        recording.frames[0].captured_at,
        source_recording=source,
    )
    clusterer = SceneClusterer(
        default.plateau_start,
        default.stable_min_frames,
        repository.card_references(card),
    )
    session = GameCardSession(
        repository,
        card,
        recording.frames[0].captured_at,
        card_min_visits=card_min_visits,
        card_min_span_seconds=card_min_span_seconds,
        source_recording=source,
    )
    members: dict[int, list[SelectedFrame]] = {}
    corroboration: Counter[str] = Counter()
    store = EvidenceStore.open(evidence_directory) if evidence_directory is not None else None
    try:
        for sequence, (frame, value) in enumerate(
            zip(recording.frames, hashes, strict=True), start=1
        ):
            started = time.perf_counter()
            with Image.open(frame.path) as image:
                measured_value = perceptual_hash(image, default.kind, default.bits)
            if measured_value != value:
                raise RuntimeError(f"fingerprint changed during replay: {frame.path}")
            match = clusterer.observe(measured_value, frame.relative_seconds)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            evidence_id = f"f{sequence}:scene:1"
            clusterer.record_evidence(match.cluster_id, evidence_id)
            members.setdefault(match.cluster_id, []).append(frame)
            switched = match.switched_from is not None
            key = (
                "both"
                if frame.detector_large and switched
                else "neither"
                if not frame.detector_large and not switched
                else "detector_only"
                if frame.detector_large
                else "fingerprint_only"
            )
            corroboration[key] += 1
            if store is not None:
                candidate = match.card_candidate
                store.append(
                    EvidenceEvent(
                        evidence_id=evidence_id,
                        source="scene",
                        kind="scene_fingerprint",
                        root_capture_id=f"f{sequence}",
                        observed_at=frame.relative_seconds,
                        learned_at=frame.relative_seconds,
                        scope=None,
                        payload=SceneFingerprintPayload(
                            hash=measured_value,
                            cluster_id=match.cluster_id,
                            distance=match.distance,
                            is_new_cluster=match.is_new_cluster,
                            switched_from=match.switched_from,
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
    finally:
        if store is not None:
            store.close()
    session.flush(clusterer.clusters)
    return session, clusterer, members, corroboration


def run_evaluation(
    recordings: Sequence[Recording],
    output: Path,
    memory_root: Path,
    *,
    card_min_visits: int = 3,
    card_min_span_seconds: float = 30.0,
) -> VariantResult:
    output.mkdir(parents=True, exist_ok=True)
    results = analyze_variants(recordings)
    default = choose_default(results)
    repository = GameCardRepository(memory_root)
    title_map = WindowTitleMap.load()
    production_rows: list[str] = []
    persistence_rows: list[str] = []

    for index, recording in enumerate(recordings):
        try:
            game_name = title_map.identify(recording.title, "")
        except ValueError:
            # Legacy capture session.json did not persist process_name.  Live
            # production still follows the process-name fallback exactly.
            game_name = recording.title
        game_id = slugify_game_id(game_name)
        hashes = default.hashes[index]
        evidence_directory = output / recording.label / "production-replay"
        session, clusterer, members, corroboration = _cluster_recording(
            recording,
            hashes,
            default,
            repository,
            game_id,
            card_min_visits=card_min_visits,
            card_min_span_seconds=card_min_span_seconds,
            evidence_directory=evidence_directory,
        )
        representative_root = output / recording.label / "representatives"
        representative_root.mkdir(parents=True, exist_ok=True)
        production_rows.extend(
            _production_report_rows(
                recording,
                session,
                clusterer,
                members,
                corroboration,
                representative_root,
            )
        )
        if index == 0:
            first_card = session.card.model_copy(deep=True)
            second, second_clusterer, _, _ = _cluster_recording(
                recording,
                hashes,
                default,
                repository,
                game_id,
                card_min_visits=card_min_visits,
                card_min_span_seconds=card_min_span_seconds,
            )
            first_ids = [scene.cluster_id for scene in first_card.scenes]
            second_ids = [scene.cluster_id for scene in second.card.scenes]
            candidate_ids = sorted(
                {
                    cluster.card_candidate.scene_id
                    for cluster in second_clusterer.clusters
                    if cluster.card_candidate is not None
                }
            )
            persistence_rows.extend(
                (
                    f"- 录制：`{recording.path}`",
                    f"- 第一遍场景 ID：{first_ids}",
                    f"- 第二遍场景 ID：{second_ids}",
                    f"- 第二遍会话簇从 1 开始：{bool(second_clusterer.clusters and second_clusterer.clusters[0].cluster_id == 1)}",
                    f"- 第二遍卡候选：{candidate_ids}",
                    "- sessions_seen 变化："
                    + str(
                        {
                            scene.cluster_id: scene.sessions_seen
                            for scene in second.card.scenes
                        }
                    ),
                    "",
                )
            )

    report = _render_report(
        recordings,
        results,
        default,
        production_rows,
        persistence_rows,
        supervised=(BACKEND_DIRECTORY / "data" / "generic" / "scene-truth" / "spans.toml").is_file(),
        card_min_visits=card_min_visits,
        card_min_span_seconds=card_min_span_seconds,
    )
    (output / "report.md").write_text(report, encoding="utf-8", newline="\n")
    return default


def _production_report_rows(
    recording: Recording,
    session: GameCardSession,
    clusterer: SceneClusterer,
    members: dict[int, list[SelectedFrame]],
    corroboration: Counter[str],
    representative_root: Path,
) -> list[str]:
    rows = [
        f"### {recording.label}",
        "",
        f"- 录制：`{recording.path}`",
        f"- 选中帧：{len(recording.frames)}",
        f"- 会话簇：{len(clusterer.clusters)}；升格卡场景：{len(session.card.scenes)}",
        "- 印证四格："
        f"都真 {corroboration['both']} / 都假 {corroboration['neither']} / "
        f"只检测器 {corroboration['detector_only']} / 只指纹 {corroboration['fingerprint_only']}",
        "",
    ]
    stable_clusters = [cluster for cluster in clusterer.clusters if cluster.stable]
    for cluster in stable_clusters[:8]:
        frames = members[cluster.cluster_id]
        chosen = (frames[0], frames[len(frames) // 2], frames[-1])
        paths: list[str] = []
        for position, frame in zip(("first", "middle", "last"), chosen, strict=True):
            destination = representative_root / f"cluster-{cluster.cluster_id}-{position}.jpg"
            shutil.copyfile(frame.path, destination)
            paths.append(str(destination))
        rows.extend(
            (
                f"#### session:c{cluster.cluster_id}",
                "",
                f"- 帧数 / 跨度：{cluster.seen_count} / {cluster.span_seconds:.3f}s",
                f"- 距离：中位 {statistics.median(cluster.distances):.1f} / 最大 {max(cluster.distances)}",
                f"- 访问段：{[(round(span.start, 3), round(span.end, 3)) for span in cluster.visit_spans]}",
                f"- 代表帧：`{paths[0]}` / `{paths[1]}` / `{paths[2]}`",
                "",
            )
        )
    return rows


def _render_report(
    recordings: Sequence[Recording],
    results: Sequence[VariantResult],
    default: VariantResult,
    production_rows: Sequence[str],
    persistence_rows: Sequence[str],
    *,
    supervised: bool,
    card_min_visits: int,
    card_min_span_seconds: float,
) -> str:
    lines = [
        "# M5-B-T2 场景指纹校准报告",
        "",
        "## 输入与方法",
        "",
        "- 使用四段完整 1080p 录制的检测器选中帧；no_change 与未被选中帧不进入指纹时间线。",
        "- 变体：aHash / dHash / pHash × 64 / 256 位；图像直接从内存位图计算，校准读取的是录制中的原始 JPEG 解码位图。",
        "- 平台定义：在阈值不低于相邻距离 P95 后，寻找簇数完全不变的最宽非终端连续区间；排除扫值上界处的终端平坦段，因为其右边界未经扫描验证。宽度按位数归一化后比较六个变体。",
        "- Pillow 与 numpy 已在现有依赖中；检查过 imagehash（只封装同类哈希）与 scipy.fftpack（可做 DCT），本任务用缓存的 numpy DCT 基矩阵即可，因此两者均未新增。",
        "- slug 规则：NFKC 后小写，连续非字母数字折叠成单个 `-`，去掉首尾 `-`；仓库没有现成窗口标题脱敏器，因此未伪造一套未经验证的脱敏规则。卡内未写进程路径、账号或玩家信息。",
        "- 录制身份限制：两段旧 session.json 未保存 process_name；查表未命中时，离线卡以原始标题生成 slug。线上生产仍严格用进程名生成未命中 slug。",
        "",
    ]
    lines.extend(f"- `{recording.path}`：{len(recording.frames)} 张选中帧" for recording in recordings)
    lines.extend(("", "## 8a 六变体无监督曲线", ""))
    for result in results:
        lines.extend(
            (
                f"### {result.kind} / {result.bits} 位",
                "",
                f"- 相邻距离：中位 {_percentile(result.adjacent_distances, 50):.2f} / P90 {_percentile(result.adjacent_distances, 90):.2f} / P95 {result.p95:.2f} / P99 {_percentile(result.adjacent_distances, 99):.2f}",
                f"- 单帧耗时：中位 {result.median_elapsed_ms:.4f}ms / P90 {_percentile(result.elapsed_ms, 90):.4f}ms / P99 {_percentile(result.elapsed_ms, 99):.4f}ms",
                f"- 平台：{result.plateau_start}–{result.plateau_end}，归一化宽度 {result.plateau_width_ratio:.4%}",
                "",
                "相邻距离直方图：",
                "",
                "| 距离 | 帧对数 |",
                "|---:|---:|",
            )
        )
        lines.extend(f"| {bucket} | {count} |" for bucket, count in _histogram(result.adjacent_distances, result.bits))
        lines.extend(("", "簇数－阈值曲线：", "", "| 阈值 | 簇总数 | 稳定簇 | 最大簇占帧比 | 单次簇占比 | 候选稳定帧数 |", "|---:|---:|---:|---:|---:|---:|"))
        lines.extend(
            f"| {point.threshold} | {point.cluster_count} | {point.stable_cluster_count} | {point.max_cluster_share:.4%} | {point.singleton_cluster_share:.4%} | {point.candidate_stable_min_frames} |"
            for point in result.curve
        )
        lines.append("")
    lines.extend(("## 8b 监督复核", ""))
    if supervised:
        lines.append("发现人工 spans.toml；本版工具只报告其存在，监督指标尚未实现。")
    else:
        lines.append("无监督真值，默认值为临时值。`data/generic/scene-truth/spans.toml` 不存在，未由 agent 生成。")
    lines.extend(
        (
            "",
            "## 8c 默认值推导",
            "",
            f"- `hash_kind={default.kind}`、`hash_bits={default.bits}`：pHash 是规格指定的固定 UI 动画主要候选，64 位单帧中位 {default.median_elapsed_ms:.4f}ms < 1ms；但六个变体均无满足下述阈值约束的平台，因此这不是规则内胜出的正式默认值。",
            f"- `hamming_threshold={default.plateau_start}`：取规格允许扫描的 64 位上界 25；相邻距离 P95 为 {default.p95:.2f}，所以它不满足“阈值 ≥ P95”。这是扫描上限与实测分布的直接冲突，未擅自扩大扫描范围。",
            f"- `stable_min_frames={default.stable_min_frames}`：阈值 {default.plateau_start} 下，相邻距离连续超阈值长度分布的 P90 向上取整。",
            "- 无监督规则冲突：六变体的相邻距离 P95 全部高于位数 40% 的扫描上限；因此不存在规格 8c 定义的合规候选。以上四项仅为可运行的临时值，须由架构师裁决扫描/P95 口径后重定。",
            "- 监督冲突：无人工真值，无法复核。",
            f"- 待实测占位：card_min_visits={card_min_visits}、card_min_span_seconds={card_min_span_seconds:g}、card_flush_seconds=120。",
            "",
            "## 8d 默认值生产逻辑",
            "",
        )
    )
    lines.extend(production_rows)
    lines.extend(("## 8e 跨会话持久化对照", ""))
    lines.extend(persistence_rows)
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", action="append", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY)
    parser.add_argument("--analyze-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    paths = tuple(arguments.recording or DEFAULT_RECORDINGS)
    recordings = tuple(load_recording(path) for path in paths)
    if arguments.analyze_only:
        results = analyze_variants(recordings)
        print(
            json.dumps(
                [
                    {
                        "hash_kind": result.kind,
                        "hash_bits": result.bits,
                        "p95": result.p95,
                        "scan_max": result.curve[-1].threshold,
                        "plateau": [result.plateau_start, result.plateau_end],
                        "plateau_width_ratio": result.plateau_width_ratio,
                        "stable_min_frames": result.stable_min_frames,
                        "median_elapsed_ms": result.median_elapsed_ms,
                    }
                    for result in results
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    else:
        default = run_evaluation(recordings, arguments.output, arguments.memory_dir)
    print(
        json.dumps(
            {
                "hash_kind": default.kind,
                "hash_bits": default.bits,
                "hamming_threshold": default.plateau_start,
                "stable_min_frames": default.stable_min_frames,
                "plateau": [default.plateau_start, default.plateau_end],
                "p95": default.p95,
                "median_elapsed_ms": default.median_elapsed_ms,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
