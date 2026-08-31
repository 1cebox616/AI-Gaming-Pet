"""Calibrate full-frame perceptual hashes on retained production recordings."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
import csv
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import time
import tomllib

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
DEFAULT_OUTPUT = BACKEND_DIRECTORY / "eval-reports" / "m5-b-t2-2"
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
TRUTH_PATH = BACKEND_DIRECTORY / "data" / "generic" / "scene-truth" / "spans.toml"


class SceneEvaluationError(RuntimeError):
    """Raised when retained calibration inputs violate the expected format."""


@dataclass(frozen=True, slots=True)
class SelectedFrame:
    path: Path
    raw_sequence: int
    relative_seconds: float
    recording_seconds: float
    captured_at: datetime
    detector_large: bool


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
    frames: tuple[SelectedFrame, ...]
    polling_frames: tuple[PollingFrame, ...]


@dataclass(frozen=True, slots=True)
class TruthSpan:
    recording: str
    start: float
    end: float
    screen: str


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
    all_adjacent_distances: tuple[int, ...]
    noise_floor_distances: tuple[int, ...]
    curve: tuple[CurvePoint, ...]
    threshold_lower: int
    threshold_upper: int
    plateau_start: int | None
    plateau_end: int | None
    plateau_width_ratio: float
    raw_stable_min_frames: int | None
    stable_min_frames: int
    elimination_reason: str | None

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
    captured_rows: list[tuple[int, float, datetime, str, bool, bool]] = []
    with metrics_path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("是否取得画面") != "是":
                continue
            sequence = int(row["序号"])
            captured_at = datetime.fromisoformat(row["时间"])
            monotonic = float(row.get("单调秒") or captured_at.timestamp())
            change_ratio = float(
                row.get("confirmed块占比")
                or row.get("确实变了的块占比")
                or 0.0
            )
            captured_rows.append(
                (
                    sequence,
                    monotonic,
                    captured_at,
                    row.get("判定原因") or "",
                    row.get("是否落盘") == "是",
                    change_ratio > 0.50,
                )
            )
    if not captured_rows:
        raise SceneEvaluationError(f"recording has no captured frames: {path}")
    polling_origin = captured_rows[0][1]
    selected_origin = next(
        (monotonic for _seq, monotonic, _wall, _reason, selected, _large in captured_rows if selected),
        None,
    )
    if selected_origin is None:
        raise SceneEvaluationError(f"recording has no selected frames: {path}")
    frames: list[SelectedFrame] = []
    polling_frames: list[PollingFrame] = []
    for (
        sequence,
        monotonic,
        captured_at,
        detector_reason,
        selected,
        detector_large,
    ) in captured_rows:
        raw_path = raw_paths.get(sequence)
        if raw_path is None:
            raise SceneEvaluationError(
                f"captured frame {sequence} is absent from raw archive: {path}"
            )
        polling_relative_seconds = monotonic - polling_origin
        polling_frames.append(
            PollingFrame(
                path=raw_path,
                raw_sequence=sequence,
                relative_seconds=polling_relative_seconds,
                captured_at=captured_at,
                detector_reason=detector_reason,
                selected=selected,
            )
        )
        if selected:
            frames.append(
                SelectedFrame(
                    path=raw_path,
                    raw_sequence=sequence,
                    relative_seconds=monotonic - selected_origin,
                    recording_seconds=polling_relative_seconds,
                    captured_at=captured_at,
                    detector_large=detector_large,
                )
            )
    if not frames:
        raise SceneEvaluationError(f"recording has no selected frames: {path}")
    label = payload.get("标签")
    return Recording(
        path=path,
        label=str(label) if label else path.name,
        title=startup["title"],
        frames=tuple(frames),
        polling_frames=tuple(polling_frames),
    )


def load_truth_spans(path: Path) -> tuple[TruthSpan, ...]:
    if not path.is_file():
        return ()
    with path.open("rb") as stream:
        payload = tomllib.load(stream)
    raw_spans = payload.get("spans", payload.get("span"))
    if not isinstance(raw_spans, list):
        raise SceneEvaluationError("scene truth must contain [[spans]] entries")
    spans: list[TruthSpan] = []
    for raw in raw_spans:
        if not isinstance(raw, dict):
            raise SceneEvaluationError("scene truth span must be a TOML table")
        span = TruthSpan(
            recording=str(raw["recording"]),
            start=float(raw["start"]),
            end=float(raw["end"]),
            screen=str(raw["screen"]),
        )
        if span.start < 0 or span.end <= span.start:
            raise SceneEvaluationError("scene truth span needs 0 <= start < end")
        spans.append(span)
    return tuple(spans)


def analyze_variants(
    recordings: Sequence[Recording], truth_spans: Sequence[TruthSpan] = ()
) -> tuple[VariantResult, ...]:
    polling_hashes: dict[tuple[HashKind, HashBits], list[list[str]]] = {
        variant: [[] for _ in recordings] for variant in VARIANTS
    }
    timings: dict[tuple[HashKind, HashBits], list[float]] = {
        variant: [] for variant in VARIANTS
    }
    for recording_index, recording in enumerate(recordings):
        for frame in recording.polling_frames:
            with Image.open(frame.path) as source:
                image = source.convert("RGB")
            for variant in VARIANTS:
                started = time.perf_counter()
                value = perceptual_hash(image, *variant)
                timings[variant].append((time.perf_counter() - started) * 1000.0)
                polling_hashes[variant][recording_index].append(value)
            image.close()

    results: list[VariantResult] = []
    for kind, bits in VARIANTS:
        grouped_polling_hashes = tuple(
            tuple(items) for items in polling_hashes[(kind, bits)]
        )
        grouped_hashes = tuple(
            tuple(
                value
                for value, frame in zip(group, recording.polling_frames, strict=True)
                if frame.selected
            )
            for group, recording in zip(
                grouped_polling_hashes, recordings, strict=True
            )
        )
        all_adjacent = tuple(
            hamming(left, right)
            for group in grouped_polling_hashes
            for left, right in zip(group, group[1:])
        )
        if truth_spans:
            noise_floor = tuple(
                distance
                for group, recording in zip(
                    grouped_polling_hashes, recordings, strict=True
                )
                for distance in _truth_noise_floor_distances(
                    group, recording, truth_spans
                )
            )
        else:
            noise_floor = tuple(
                distance
                for group, recording in zip(
                    grouped_polling_hashes, recordings, strict=True
                )
                for distance in _noise_floor_distances(
                    group,
                    tuple(
                        frame.detector_reason
                        for frame in recording.polling_frames
                    ),
                )
            )
        if not noise_floor:
            raise SceneEvaluationError("no detector no_change adjacency pairs were found")
        lower, upper, interval_error = _threshold_interval(noise_floor, bits)
        points: list[CurvePoint] = []
        for threshold in range(upper + 1):
            _, candidate_stable = _bounded_stable_min_frames(
                grouped_hashes, threshold
            )
            points.append(
                _curve_point(grouped_hashes, threshold, candidate_stable)
            )
        if interval_error is not None:
            start = None
            end = None
            raw_stable = None
            stable_min_frames = 3
            elimination_reason = interval_error
        else:
            start, end = _stable_plateau(points, lower, upper)
            raw_stable, stable_min_frames = _bounded_stable_min_frames(
                grouped_hashes, start
            )
            elimination_reason = None
        results.append(
            VariantResult(
                kind=kind,
                bits=bits,
                hashes=grouped_hashes,
                elapsed_ms=tuple(timings[(kind, bits)]),
                all_adjacent_distances=all_adjacent,
                noise_floor_distances=noise_floor,
                curve=tuple(points),
                threshold_lower=lower,
                threshold_upper=upper,
                plateau_start=start,
                plateau_end=end,
                plateau_width_ratio=(
                    (end - start) / bits
                    if start is not None and end is not None
                    else 0.0
                ),
                raw_stable_min_frames=raw_stable,
                stable_min_frames=stable_min_frames,
                elimination_reason=elimination_reason,
            )
        )
    return tuple(results)


def choose_default(results: Sequence[VariantResult]) -> VariantResult:
    candidates = [
        result
        for result in results
        if result.elimination_reason is None and result.median_elapsed_ms < 1.0
    ]
    if not candidates:
        raise SceneEvaluationError(
            "no hash variant has a valid threshold interval and median time below 1ms"
        )
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
    noise_floor_distances: Sequence[int], bits: HashBits
) -> tuple[int, int, str | None]:
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


def _truth_noise_floor_distances(
    hashes: Sequence[str],
    recording: Recording,
    truth_spans: Sequence[TruthSpan],
) -> tuple[int, ...]:
    if len(hashes) != len(recording.polling_frames):
        raise ValueError("polling hashes and frames must have equal length")
    identifiers = {
        recording.path.name,
        recording.label,
        str(recording.path),
        recording.path.as_posix(),
    }
    matching = [span for span in truth_spans if span.recording in identifiers]
    distances: list[int] = []
    for index in range(1, len(hashes)):
        previous = recording.polling_frames[index - 1].relative_seconds
        current = recording.polling_frames[index].relative_seconds
        if any(
            span.start <= previous <= span.end
            and span.start <= current <= span.end
            for span in matching
        ):
            distances.append(hamming(hashes[index - 1], hashes[index]))
    return tuple(distances)


def _stable_plateau(
    points: Sequence[CurvePoint], lower: int, upper: int
) -> tuple[int, int]:
    eligible = [point for point in points if lower <= point.threshold <= upper]
    if not eligible:
        raise ValueError("stable plateau interval must not be empty")
    runs: list[tuple[int, int]] = []
    start = eligible[0].threshold
    previous = eligible[0]
    for point in eligible[1:]:
        if point.stable_cluster_count != previous.stable_cluster_count:
            runs.append((start, previous.threshold))
            start = point.threshold
        previous = point
    runs.append((start, previous.threshold))
    return max(runs, key=lambda run: (run[1] - run[0], -run[0]))


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


def _bounded_stable_min_frames(
    hashes: Sequence[Sequence[str]], threshold: int
) -> tuple[int, int]:
    raw = _stable_min_frames(hashes, threshold)
    return raw, min(10, max(3, raw))


def _percentile(values: Sequence[int] | Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=np.float64), percentile))


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
    if default.plateau_start is None:
        raise ValueError("production replay requires a calibrated threshold")
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
    truth_spans = load_truth_spans(TRUTH_PATH)
    results = analyze_variants(recordings, truth_spans)
    default = choose_default(results)
    title_map = WindowTitleMap.load()
    identities = tuple(
        _recording_identity(recording, title_map) for recording in recordings
    )
    old_card_counts, old_directories = _old_card_inventory(
        recordings, memory_root
    )
    _reset_calibration_cards(
        memory_root,
        old_directories,
        tuple(game_id for game_id, _display_name in identities),
    )
    repository = GameCardRepository(memory_root)
    production_rows: list[str] = []
    persistence_rows: list[str] = []
    new_card_counts: dict[str, int] = {}

    for index, (recording, identity) in enumerate(
        zip(recordings, identities, strict=True)
    ):
        game_id, _display_name = identity
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
        if representative_root.is_dir():
            shutil.rmtree(representative_root)
        representative_root.mkdir(parents=True, exist_ok=True)
        production_rows.extend(
            _production_report_rows(
                recording,
                session,
                clusterer,
                members,
                corroboration,
                representative_root,
                card_min_visits=card_min_visits,
                card_min_span_seconds=card_min_span_seconds,
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
        new_card_counts[recording.label] = len(session.card.scenes)

    report = _render_report(
        recordings,
        results,
        default,
        production_rows,
        persistence_rows,
        old_card_counts=old_card_counts,
        new_card_counts=new_card_counts,
        noise_source=("人工区间" if truth_spans else "检测器 no_change 邻接对"),
        truth_spans=truth_spans,
        card_min_visits=card_min_visits,
        card_min_span_seconds=card_min_span_seconds,
    )
    (output / "report.md").write_text(report, encoding="utf-8", newline="\n")
    return default


def _recording_identity(
    recording: Recording, title_map: WindowTitleMap
) -> tuple[str, str]:
    try:
        game_name = title_map.identify(recording.title, "")
    except ValueError:
        # Legacy capture session.json did not persist process_name.  Live
        # production still follows the process-name fallback exactly.
        game_name = recording.title
    return slugify_game_id(game_name, recording.title), recording.title


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


def _supervised_quality(
    recordings: Sequence[Recording],
    result: VariantResult,
    truth_spans: Sequence[TruthSpan],
) -> tuple[float, float]:
    if result.plateau_start is None:
        raise ValueError("supervised quality requires a calibrated threshold")
    purity_correct = 0
    purity_total = 0
    separated = 0
    separation_total = 0
    for recording, hashes in zip(recordings, result.hashes, strict=True):
        clusterer = SceneClusterer(result.plateau_start, result.stable_min_frames)
        assignments = [
            clusterer.observe(value, frame.relative_seconds).cluster_id
            for value, frame in zip(hashes, recording.frames, strict=True)
        ]
        identifiers = {
            recording.path.name,
            recording.label,
            str(recording.path),
            recording.path.as_posix(),
        }
        summaries: list[tuple[TruthSpan, int]] = []
        for span in truth_spans:
            if span.recording not in identifiers:
                continue
            members = [
                assignment
                for assignment, frame in zip(
                    assignments, recording.frames, strict=True
                )
                if span.start <= frame.recording_seconds <= span.end
            ]
            if not members:
                continue
            counts = Counter(members)
            dominant, count = counts.most_common(1)[0]
            purity_correct += count
            purity_total += len(members)
            summaries.append((span, dominant))
        for index, (left_span, left_cluster) in enumerate(summaries):
            for right_span, right_cluster in summaries[index + 1 :]:
                if left_span.screen == right_span.screen:
                    continue
                separation_total += 1
                separated += left_cluster != right_cluster
    purity = purity_correct / purity_total if purity_total else 0.0
    separation = separated / separation_total if separation_total else 0.0
    return purity, separation


def _production_report_rows(
    recording: Recording,
    session: GameCardSession,
    clusterer: SceneClusterer,
    members: dict[int, list[SelectedFrame]],
    corroboration: Counter[str],
    representative_root: Path,
    *,
    card_min_visits: int,
    card_min_span_seconds: float,
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
    promoted_clusters = [
        cluster
        for cluster in clusterer.clusters
        if cluster.stable
        and cluster.seen_count >= card_min_visits
        and cluster.span_seconds >= card_min_span_seconds
    ]
    if not promoted_clusters:
        rows.extend(
            (
                "- 本段按新阈值得到零个可升格场景；未调参凑数，也不生成代表帧。",
                "",
            )
        )
    for cluster in promoted_clusters:
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
    old_card_counts: dict[str, int],
    new_card_counts: dict[str, int],
    noise_source: str,
    truth_spans: Sequence[TruthSpan],
    card_min_visits: int,
    card_min_span_seconds: float,
) -> str:
    if default.plateau_start is None or default.plateau_end is None:
        raise ValueError("report default must have a calibrated plateau")
    lines = [
        "# M5-B-T2-2 场景指纹重校准报告",
        "",
        "## 输入与方法",
        "",
        f"- 噪声底口径：**{noise_source}**。人工真值优先；本次若无真值，就只取 detector_reason=`no_change` 的轮询帧与其直接前一帧。",
        "- 六个变体都对全部轮询帧计算指纹；生产聚簇和稳定帧数仍只使用检测器选中帧，在线行为未改。",
        "- 阈值下限为噪声底 P95，上限为位数的 25%；平台按 [下限, 上限] 内稳定簇数不变的最宽连续区间定义，取其起点。",
        "- stable_min_frames 取选中帧连续超阈值段 P90 向上取整，再限制到 [3, 10]。",
        "- slug 规则：NFKC 后小写，只保留 ASCII 字母数字，其他字符折叠为 `-`；无字符剩余时用 display_name UTF-8 SHA-1 前 8 位生成 `g-<hash>`。",
        "- 录制身份限制：两段旧 session.json 未保存 process_name；查表未命中时，离线卡以原始标题生成 slug。线上生产仍严格用进程名生成未命中 slug。",
        "",
    ]
    lines.extend(
        f"- `{recording.path}`：{len(recording.polling_frames)} 张轮询帧 / {len(recording.frames)} 张选中帧"
        for recording in recordings
    )
    lines.extend(
        (
            "",
            "## 两种相邻距离口径并排",
            "",
            "| 变体 | 噪声底样本 | 噪声底中位 / P90 / P95 / P99 | 全体样本 | 全体中位 / P90 / P95 / P99 |",
            "|---|---:|---:|---:|---:|",
        )
    )
    for result in results:
        noise = result.noise_floor_distances
        all_pairs = result.all_adjacent_distances
        lines.append(
            f"| {result.kind}/{result.bits} | {len(noise)} | "
            f"{_percentile(noise, 50):.2f} / {_percentile(noise, 90):.2f} / {_percentile(noise, 95):.2f} / {_percentile(noise, 99):.2f} | "
            f"{len(all_pairs)} | {_percentile(all_pairs, 50):.2f} / {_percentile(all_pairs, 90):.2f} / {_percentile(all_pairs, 95):.2f} / {_percentile(all_pairs, 99):.2f} |"
        )
    lines.extend(
        (
            "",
            "## 六变体定值表",
            "",
            "| 变体 | [下限, 上限] | 稳定簇平台 | 归一化宽度 | 单帧中位 | stable 原值→定值 | 结论 |",
            "|---|---:|---:|---:|---:|---:|---|",
        )
    )
    for result in results:
        platform = (
            "淘汰"
            if result.plateau_start is None
            else f"{result.plateau_start}–{result.plateau_end}"
        )
        stable = (
            "—"
            if result.raw_stable_min_frames is None
            else f"{result.raw_stable_min_frames}→{result.stable_min_frames}"
        )
        conclusion = result.elimination_reason or (
            "候选" if result.median_elapsed_ms < 1.0 else "超过 1ms，不参与变体选择"
        )
        lines.append(
            f"| {result.kind}/{result.bits} | [{result.threshold_lower}, {result.threshold_upper}] | {platform} | "
            f"{result.plateau_width_ratio:.4%} | {result.median_elapsed_ms:.4f}ms | {stable} | {conclusion} |"
        )
    eliminated = [result for result in results if result.elimination_reason]
    lines.extend(("", "淘汰名单：", ""))
    if eliminated:
        lines.extend(
            f"- {result.kind}/{result.bits}：{result.elimination_reason}"
            for result in eliminated
        )
    else:
        lines.append("- 无变体因阈值区间为空被淘汰。")
    lines.extend(("", "## 监督复核", ""))
    if truth_spans:
        purity, separation = _supervised_quality(
            recordings, default, truth_spans
        )
        lines.append(
            f"人工区间 {len(truth_spans)} 个；默认值纯度 {purity:.4%}，分离度 {separation:.4%}，乘积 {purity * separation:.4%}。定值未因监督结果自动改动。"
        )
    else:
        lines.append(
            "`data/generic/scene-truth/spans.toml` 不存在，未由 agent 生成；本次采用 no_change 代理口径，无监督冲突可报告。"
        )
    lines.extend(
        (
            "",
            "## 四个默认值逐条推导",
            "",
            f"- `hash_kind={default.kind}`、`hash_bits={default.bits}`：未淘汰且单帧中位 {default.median_elapsed_ms:.4f}ms < 1ms 的候选中，稳定簇平台归一化宽度 {default.plateau_width_ratio:.4%} 最大。",
            f"- `hamming_threshold={default.plateau_start}`：噪声底 P95 下限 {default.threshold_lower}、25% 上限 {default.threshold_upper}，稳定簇平台为 {default.plateau_start}–{default.plateau_end}，按规则取平台起点。",
            f"- `stable_min_frames={default.stable_min_frames}`：阈值 {default.plateau_start} 下连续超阈值段 P90 向上取整为 {default.raw_stable_min_frames}，再限制到 [3, 10]。",
            f"- 待实测占位：card_min_visits={card_min_visits}、card_min_span_seconds={card_min_span_seconds:g}、card_flush_seconds=120。",
            "",
            "## 新旧卡场景数",
            "",
        )
    )
    lines.extend(("| 录制 | 旧卡 | 新卡 |", "|---|---:|---:|"))
    lines.extend(
        f"| {recording.label} | {old_card_counts.get(recording.label, 0)} | {new_card_counts.get(recording.label, 0)} |"
        for recording in recordings
    )
    lines.extend(("", "## 新默认值生产逻辑", ""))
    lines.extend(production_rows)
    lines.extend(("## 跨会话持久化对照", ""))
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
        results = analyze_variants(recordings, load_truth_spans(TRUTH_PATH))
        print(
            json.dumps(
                [
                    {
                        "hash_kind": result.kind,
                        "hash_bits": result.bits,
                        "noise_samples": len(result.noise_floor_distances),
                        "noise_p95": _percentile(
                            result.noise_floor_distances, 95
                        ),
                        "all_p95": _percentile(
                            result.all_adjacent_distances, 95
                        ),
                        "threshold_interval": [
                            result.threshold_lower,
                            result.threshold_upper,
                        ],
                        "plateau": [result.plateau_start, result.plateau_end],
                        "plateau_width_ratio": result.plateau_width_ratio,
                        "raw_stable_min_frames": result.raw_stable_min_frames,
                        "stable_min_frames": result.stable_min_frames,
                        "median_elapsed_ms": result.median_elapsed_ms,
                        "elimination_reason": result.elimination_reason,
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
                "noise_p95": _percentile(default.noise_floor_distances, 95),
                "median_elapsed_ms": default.median_elapsed_ms,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
