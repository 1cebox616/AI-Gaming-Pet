"""Replay the four calibrated recordings through stable-cluster scene naming."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from pet.core.belief import (
    EvidenceEvent,
    EvidenceStore,
    SceneFingerprintPayload,
    SceneVerifiedPayload,
)
from pet.core.config import load_config, resolve_llm_profile
from pet.core.gamecard import (
    GameCardRepository,
    GameCardSession,
    SceneCardVerification,
)
from pet.core.llm import OpenRouterClient
from pet.core.scene_fingerprint import SceneCluster, SceneClusterer
from pet.games.generic.adapter import WindowTitleMap
from pet.games.generic.deep_read import DeepReadResult, DeepVisionReader
from pet.games.generic.eval.scene_fingerprint_eval import (
    BACKEND_DIRECTORY,
    CARD_MIN_DWELL_SECONDS,
    DEFAULT_MEMORY,
    DEFAULT_RECORDINGS,
    HAMMING_THRESHOLD,
    HASH_BITS,
    HASH_KIND,
    STABLE_MIN_SECONDS,
    PollingFrame,
    Recording,
    _hash_recordings,
    _old_card_inventory,
    _recording_identity,
    _reset_calibration_cards,
    load_recording,
)
from pet.games.generic.scene_naming import (
    SCENE_NAMING_PROMPT_PATH,
    SceneNamingFrame,
    build_scene_naming_request,
    parse_scene_naming_proposal,
    validate_scene_naming_proposal,
)

DEFAULT_OUTPUT = BACKEND_DIRECTORY / "eval-reports" / "m5-b-t2-4"


@dataclass(frozen=True, slots=True)
class TriggerFrame:
    frame: PollingFrame
    root_capture_id: str
    fingerprint_evidence_id: str


@dataclass(frozen=True, slots=True)
class NamingTrigger:
    cluster: SceneCluster
    frames: tuple[TriggerFrame, ...]


@dataclass(frozen=True, slots=True)
class AttemptResult:
    cluster_id: int
    accepted: bool
    label: str | None
    annotation: str | None
    modality: str | None
    evidence_id: str | None
    model: str | None
    provider: str | None
    cost_usd: float
    latency_ms: float | None
    validation_error: str | None
    error: str | None
    representative_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecordingResult:
    recording: Recording
    game_id: str
    clusters: tuple[SceneCluster, ...]
    promoted_cluster_ids: tuple[int, ...]
    attempts: tuple[AttemptResult, ...]
    card_path: Path


def _choose_trigger_frames(
    frames: Sequence[TriggerFrame],
    limit: int,
) -> tuple[TriggerFrame, ...]:
    if not frames:
        return ()
    if len(frames) <= limit:
        return tuple(frames)
    if limit == 1:
        return (frames[len(frames) // 2],)
    if limit == 2:
        return (frames[0], frames[-1])
    return (frames[0], frames[len(frames) // 2], frames[-1])


def _copy_representatives(
    output: Path,
    recording: Recording,
    trigger: NamingTrigger,
) -> tuple[str, ...]:
    directory = output / recording.label / "representatives"
    directory.mkdir(parents=True, exist_ok=True)
    labels = ("first", "middle", "last") if len(trigger.frames) == 3 else tuple(
        f"frame-{index + 1}" for index in range(len(trigger.frames))
    )
    paths: list[str] = []
    for label, item in zip(labels, trigger.frames, strict=True):
        destination = directory / f"cluster-{trigger.cluster.cluster_id}-{label}.jpg"
        shutil.copyfile(item.frame.path, destination)
        paths.append(str(destination.resolve()))
    return tuple(paths)


def _append_fingerprint(
    store: EvidenceStore,
    *,
    sequence: int,
    frame: PollingFrame,
    fingerprint: str,
    match: object,
    switched_from: int | None,
    elapsed_ms: float,
) -> str:
    from pet.core.scene_fingerprint import SceneFingerprintMatch

    if not isinstance(match, SceneFingerprintMatch):
        raise TypeError("scene fingerprint match has the wrong type")
    root_capture_id = f"f{sequence}"
    evidence_id = store.new_evidence_id(root_capture_id, "scene")
    candidate = match.card_candidate
    store.append(
        EvidenceEvent(
            evidence_id=evidence_id,
            source="scene",
            kind="scene_fingerprint",
            root_capture_id=root_capture_id,
            observed_at=frame.relative_seconds,
            learned_at=frame.relative_seconds,
            scope=None,
            payload=SceneFingerprintPayload(
                hash=fingerprint,
                cluster_id=match.cluster_id,
                distance=match.distance,
                is_new_cluster=match.is_new_cluster,
                switched_from=switched_from,
                stable=match.stable,
                card_candidate_scene_id=candidate.scene_id if candidate else None,
                card_candidate_distance=candidate.distance if candidate else None,
                elapsed_ms=elapsed_ms,
            ),
            derived_from=[],
            context_version=None,
            outcome="ok",
        )
    )
    return evidence_id


def _collect_triggers(
    recording: Recording,
    hashes: Sequence[str],
    timings: Sequence[float],
    clusterer: SceneClusterer,
    store: EvidenceStore,
    *,
    representative_limit: int,
    request_limit: int,
) -> tuple[NamingTrigger, ...]:
    triggers: list[NamingTrigger] = []
    attempted: set[int] = set()
    selected_sequence = 0
    previous_selected_cluster: int | None = None
    changed_since_selected = False
    current_cluster_id: int | None = None
    current_selected: list[TriggerFrame] = []
    for frame, fingerprint, elapsed_ms in zip(
        recording.polling_frames, hashes, timings, strict=True
    ):
        match = clusterer.observe(fingerprint, frame.relative_seconds)
        if match.switched_from is not None:
            changed_since_selected = True
        if current_cluster_id != match.cluster_id:
            current_cluster_id = match.cluster_id
            current_selected = []
        if frame.selected:
            selected_sequence += 1
            selected_switched_from = (
                previous_selected_cluster
                if changed_since_selected and previous_selected_cluster is not None
                else None
            )
            evidence_id = _append_fingerprint(
                store,
                sequence=selected_sequence,
                frame=frame,
                fingerprint=fingerprint,
                match=match,
                switched_from=selected_switched_from,
                elapsed_ms=elapsed_ms,
            )
            clusterer.record_evidence(match.cluster_id, evidence_id)
            current_selected.append(
                TriggerFrame(frame, f"f{selected_sequence}", evidence_id)
            )
            previous_selected_cluster = match.cluster_id
            changed_since_selected = False
        if (
            match.stable
            and match.cluster_id not in attempted
            and current_selected
            and len(triggers) < request_limit
        ):
            attempted.add(match.cluster_id)
            triggers.append(
                NamingTrigger(
                    clusterer.cluster(match.cluster_id),
                    _choose_trigger_frames(current_selected, representative_limit),
                )
            )
    return tuple(triggers)


async def _name_trigger(
    *,
    reader: DeepVisionReader,
    store: EvidenceStore,
    session: GameCardSession,
    recording: Recording,
    trigger: NamingTrigger,
    output: Path,
    send_width: int,
) -> AttemptResult:
    representative_paths = _copy_representatives(output, recording, trigger)
    try:
        request = build_scene_naming_request(
            game_name=recording.title,
            session_cluster_id=trigger.cluster.cluster_id,
            frames=tuple(
                SceneNamingFrame(item.root_capture_id, item.frame.path)
                for item in trigger.frames
            ),
            stable_ocr_lines=(),
            send_width=send_width,
        )
        deep_result = await reader.read(request)
        proposal = parse_scene_naming_proposal(deep_result.result.text)
        decision = validate_scene_naming_proposal(trigger.cluster, proposal)
        anchor = trigger.frames[-1]
        evidence_id = store.new_evidence_id(anchor.root_capture_id, "deep")
        store.append(
            EvidenceEvent(
                evidence_id=evidence_id,
                source="deep",
                kind="scene_verified",
                root_capture_id=anchor.root_capture_id,
                observed_at=anchor.frame.relative_seconds,
                learned_at=max(anchor.frame.relative_seconds, trigger.cluster.last_seen),
                scope=None,
                payload=SceneVerifiedPayload(
                    session_cluster_id=trigger.cluster.cluster_id,
                    label=decision.label,
                    annotation=decision.annotation,
                    modality=decision.modality,
                    root_capture_ids=[item.root_capture_id for item in trigger.frames],
                    model=deep_result.result.model,
                    provider=deep_result.result.provider,
                    prompt_tokens=deep_result.result.usage.prompt_tokens,
                    completion_tokens=deep_result.result.usage.completion_tokens,
                    cost_usd=deep_result.cost_usd,
                    latency_ms=deep_result.result.latency_seconds * 1000.0,
                    validation_error=decision.validation_error,
                ),
                derived_from=[
                    item.fingerprint_evidence_id for item in trigger.frames
                ],
                context_version=None,
                outcome="ok" if decision.accepted else "failed",
            )
        )
        if decision.accepted:
            session.record_verification(
                trigger.cluster,
                SceneCardVerification(
                    label=decision.label,
                    annotation=decision.annotation,
                    modality=decision.modality,
                    verified_at=datetime.now(timezone.utc),
                    evidence_id=evidence_id,
                ),
            )
        return AttemptResult(
            cluster_id=trigger.cluster.cluster_id,
            accepted=decision.accepted,
            label=decision.label,
            annotation=decision.annotation,
            modality=decision.modality,
            evidence_id=evidence_id,
            model=deep_result.result.model,
            provider=deep_result.result.provider,
            cost_usd=deep_result.cost_usd,
            latency_ms=deep_result.result.latency_seconds * 1000.0,
            validation_error=decision.validation_error,
            error=None,
            representative_paths=representative_paths,
        )
    except Exception as error:
        return AttemptResult(
            cluster_id=trigger.cluster.cluster_id,
            accepted=False,
            label=None,
            annotation=None,
            modality=None,
            evidence_id=None,
            model=None,
            provider=None,
            cost_usd=0.0,
            latency_ms=None,
            validation_error=None,
            error=str(error),
            representative_paths=representative_paths,
        )


async def run_evaluation(
    recordings: Sequence[Recording],
    output: Path,
    memory_root: Path,
) -> tuple[RecordingResult, ...]:
    configuration = load_config(strict=True)
    generic = configuration.games["generic"].generic
    naming = generic.scene.naming
    profile = configuration.llm.profiles[naming.llm_profile]
    effective = resolve_llm_profile(configuration.llm, naming.llm_profile)
    client = OpenRouterClient.from_profile(
        profile_name=naming.llm_profile,
        base_url=effective.base_url,
        api_key_env=effective.api_key_env,
        timeout_seconds=effective.timeout_seconds,
    )
    reader = DeepVisionReader(
        client,
        effective,
        input_price_per_million_usd=profile.input_price_per_million_usd,
        output_price_per_million_usd=profile.output_price_per_million_usd,
        reasoning_effort="none",
    )
    grouped_hashes, grouped_timings = _hash_recordings(recordings)
    title_map = WindowTitleMap.load()
    identities = tuple(
        _recording_identity(recording, title_map) for recording in recordings
    )
    _old_counts, old_directories = _old_card_inventory(recordings, memory_root)
    _reset_calibration_cards(
        memory_root,
        old_directories,
        tuple(game_id for game_id, _display_name in identities),
    )
    if output.is_dir():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    repository = GameCardRepository(memory_root)
    results: list[RecordingResult] = []
    try:
        for recording, hashes, timings, identity in zip(
            recordings, grouped_hashes, grouped_timings, identities, strict=True
        ):
            game_id, display_name = identity
            source = recording.path.relative_to(BACKEND_DIRECTORY).as_posix()
            card = repository.load_or_create(
                game_id,
                display_name,
                recording.polling_frames[0].captured_at,
                source_recording=source,
            )
            clusterer = SceneClusterer(
                HAMMING_THRESHOLD,
                STABLE_MIN_SECONDS,
                repository.card_references(card),
            )
            session = GameCardSession(
                repository,
                card,
                recording.polling_frames[0].captured_at,
                card_min_dwell_seconds=CARD_MIN_DWELL_SECONDS,
                source_recording=source,
            )
            replay_directory = output / recording.label / "production-replay"
            store = EvidenceStore.open(replay_directory)
            try:
                triggers = _collect_triggers(
                    recording,
                    hashes,
                    timings,
                    clusterer,
                    store,
                    representative_limit=naming.representative_frame_count,
                    request_limit=naming.max_requests_per_session,
                )
                attempts = tuple(
                    [
                        await _name_trigger(
                            reader=reader,
                            store=store,
                            session=session,
                            recording=recording,
                            trigger=trigger,
                            output=output,
                            send_width=generic.send_width,
                        )
                        for trigger in triggers
                    ]
                )
            finally:
                store.close()
            session.flush(clusterer.clusters)
            promoted = tuple(
                cluster.cluster_id
                for cluster in clusterer.clusters
                if cluster.dwell_seconds >= CARD_MIN_DWELL_SECONDS
            )
            results.append(
                RecordingResult(
                    recording=recording,
                    game_id=game_id,
                    clusters=clusterer.clusters,
                    promoted_cluster_ids=promoted,
                    attempts=attempts,
                    card_path=memory_root / game_id / "gamecard.json",
                )
            )
    finally:
        reader.close()
    (output / "report.md").write_text(
        _render_report(results), encoding="utf-8", newline="\n"
    )
    return tuple(results)


def _render_report(results: Sequence[RecordingResult]) -> str:
    total_cost = sum(
        attempt.cost_usd for result in results for attempt in result.attempts
    )
    lines = [
        "# M5-B-T2-4 场景命名报告",
        "",
        "## 实现边界与代表帧",
        "",
        "- 指纹参数保持 `pHash64 / Hamming <= 8 / stable=4s / dwell=8s`，本任务未改聚簇或升格规则。",
        "- 稳定门由全部轮询帧推进；深线在过门时使用该连续段内已有、且具备合法 `root_capture_id` 的检测器选中帧。",
        "- 代表帧最多三张，按当时可用帧的首／中／末选取；少于三张时去重后全部使用。上传宽度为 896px。",
        "- 每会话请求上限为 8：四段校准录像单段最多 4 个稳定簇，取两倍余量，同时给异常簇风暴设置花费边界。",
        "- 本批保留录制没有同时间线的稳定 OCR 产物，因此请求未附 OCR；在线原语保留该输入槽位。",
        "- 深线不联网、不做跨簇归并、不做 variants；模型只提议，代码检查稳定门、中文标签和长度后执行。",
        "",
        "## 四张卡汇总",
        "",
        "| 录像 | 场景数 | 命名数 | uncertain | 被拒判决 | 深读失败 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        card = json.loads(result.card_path.read_text(encoding="utf-8"))
        scenes = card["scenes"]
        named = sum(scene["label_status"] != "unnamed" for scene in scenes)
        uncertain = sum(scene["label_status"] == "uncertain" for scene in scenes)
        rejected = sum(
            attempt.validation_error is not None for attempt in result.attempts
        )
        failed = sum(attempt.error is not None for attempt in result.attempts)
        lines.append(
            f"| {result.recording.label} | {len(scenes)} | {named} | "
            f"{uncertain} | {rejected} | {failed} |"
        )
    lines.extend(("", "## 逐场景与代表帧", ""))
    for result in results:
        card = json.loads(result.card_path.read_text(encoding="utf-8"))
        attempts = {attempt.cluster_id: attempt for attempt in result.attempts}
        clusters = {cluster.representative_hash: cluster for cluster in result.clusters}
        lines.extend((f"### {result.recording.label}", ""))
        for scene in card["scenes"]:
            cluster = clusters[scene["representative_hash"]]
            attempt = attempts.get(cluster.cluster_id)
            paths = attempt.representative_paths if attempt is not None else ()
            lines.extend(
                (
                    f"- `{scene['scene_id']}` / `session:c{cluster.cluster_id}`："
                    f"{scene['label'] or '未命名'}（{scene['label_status']}）",
                    f"  - 注释：{scene['annotation'] or '无'}",
                    "  - 代表帧：" + (" / ".join(f"`{path}`" for path in paths) or "无"),
                    f"  - 深线证据：{', '.join(scene['deep_evidence_ids']) or '无'}",
                )
            )
        lines.append("")
    lines.extend(("## 每次深读与花费", "", "| 录像 | 簇 | 结果 | modality | 模型 / 上游 | 延迟 | 花费 |", "|---|---|---|---|---|---:|---:|"))
    for result in results:
        for attempt in result.attempts:
            outcome = (
                "接受"
                if attempt.accepted
                else (f"拒绝：{attempt.validation_error}" if attempt.validation_error else f"失败：{attempt.error}")
            )
            lines.append(
                f"| {result.recording.label} | session:c{attempt.cluster_id} | {outcome} | "
                f"{attempt.modality or '-'} | {attempt.model or '-'} / {attempt.provider or '-'} | "
                f"{attempt.latency_ms:.1f}ms | ${attempt.cost_usd:.6f} |"
                if attempt.latency_ms is not None
                else f"| {result.recording.label} | session:c{attempt.cluster_id} | {outcome} | - | - | - | $0.000000 |"
            )
    lines.extend(
        (
            f"| **合计** |  |  |  |  |  | **${total_cost:.6f}** |",
            "",
            "## 提示词全文",
            "",
            "```text",
            SCENE_NAMING_PROMPT_PATH.read_text(encoding="utf-8").strip(),
            "```",
            "",
            "用户消息模板：",
            "",
            "```text",
            "游戏名（由窗口标题确定）：{game_name}",
            "会话视觉簇：session:c{session_cluster_id}",
            "所看帧：{root_capture_ids}",
            "稳定 OCR 文字：{stable_ocr_lines_or_unavailable}",
            "请只根据这些画面和文字给出该视觉簇的场景命名。",
            "```",
            "",
            "## 偏差与未完成项",
            "",
            "- 与规格的偏差：无。OCR 是可选输入；本批录像没有与选中帧对齐的稳定 OCR 流，故未附加，也未重新跑 OCR。",
            "- 未完成项：无。",
            "",
        )
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recording", action="append", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    recordings = tuple(
        load_recording(path) for path in tuple(arguments.recording or DEFAULT_RECORDINGS)
    )
    results = asyncio.run(
        run_evaluation(recordings, arguments.output, arguments.memory_dir)
    )
    print(
        json.dumps(
            {
                "recordings": len(results),
                "scene_counts": [len(result.promoted_cluster_ids) for result in results],
                "attempt_counts": [len(result.attempts) for result in results],
                "total_cost_usd": sum(
                    attempt.cost_usd
                    for result in results
                    for attempt in result.attempts
                ),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
