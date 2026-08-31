"""Regenerate game cards through semantic scene verification."""

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
from pet.core.llm import LlmDispatchStats, LlmError, OpenRouterClient
from pet.core.scene_fingerprint import SceneCluster, SceneClusterer
from pet.games.generic.adapter import WindowTitleMap
from pet.games.generic.deep_read import DeepReadResult, DeepVisionReader
from pet.games.generic.eval.scene_fingerprint_eval import (
    BACKEND_DIRECTORY,
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

DEFAULT_OUTPUT = BACKEND_DIRECTORY / "eval-reports" / "m5-b-t2-5"


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
    matches_existing: bool | None
    candidate_scene_id: str | None
    candidate_label: str | None
    action: str | None
    evidence_id: str | None
    model: str | None
    provider: str | None
    cost_usd: float
    latency_ms: float | None
    validation_error: str | None
    error: str | None
    error_metadata: dict[str, object] | None
    representative_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecordingResult:
    recording: Recording
    game_id: str
    clusters: tuple[SceneCluster, ...]
    promoted_cluster_ids: tuple[int, ...]
    attempts: tuple[AttemptResult, ...]
    card_path: Path
    old_scene_count: int


def _load_recorded_verifications(
    paths: Sequence[Path],
) -> dict[tuple[str, int], EvidenceEvent]:
    recorded: dict[tuple[str, int], EvidenceEvent] = {}
    for path in paths:
        recording_label = path.parent.parent.name
        for line in path.read_text(encoding="utf-8").splitlines():
            event = EvidenceEvent.model_validate_json(line)
            if not isinstance(event.payload, SceneVerifiedPayload):
                continue
            if event.outcome != "ok":
                continue
            key = (recording_label, event.payload.session_cluster_id)
            if key in recorded:
                raise ValueError(
                    "multiple recorded decisions supplied for "
                    f"{recording_label} session:c{event.payload.session_cluster_id}"
                )
            recorded[key] = event
    return recorded


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
                    matches_existing=decision.matches_existing,
                    candidate_scene_id=decision.candidate_scene_id,
                    candidate_label=decision.candidate_label,
                    action=decision.action,
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
                outcome="failed" if decision.action == "rejected" else "ok",
            )
        )
        if decision.accepted:
            assert decision.applied_label is not None
            assert decision.applied_annotation is not None
            assert decision.applied_label_status is not None
            session.record_verification(
                trigger.cluster,
                SceneCardVerification(
                    label=decision.applied_label,
                    annotation=decision.applied_annotation,
                    modality=decision.modality,
                    label_status=decision.applied_label_status,
                    needs_review=decision.needs_review,
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
            matches_existing=decision.matches_existing,
            candidate_scene_id=decision.candidate_scene_id,
            candidate_label=decision.candidate_label,
            action=decision.action,
            evidence_id=evidence_id,
            model=deep_result.result.model,
            provider=deep_result.result.provider,
            cost_usd=deep_result.cost_usd,
            latency_ms=deep_result.result.latency_seconds * 1000.0,
            validation_error=decision.validation_error,
            error=None,
            error_metadata=None,
            representative_paths=representative_paths,
        )
    except Exception as error:
        metadata = error.metadata() if isinstance(error, LlmError) else None
        return AttemptResult(
            cluster_id=trigger.cluster.cluster_id,
            accepted=False,
            label=None,
            annotation=None,
            modality=None,
            matches_existing=None,
            candidate_scene_id=None,
            candidate_label=None,
            action=None,
            evidence_id=None,
            model=None,
            provider=error.provider if isinstance(error, LlmError) else None,
            cost_usd=0.0,
            latency_ms=(
                error.latency_seconds * 1000.0
                if isinstance(error, LlmError)
                and error.latency_seconds is not None
                else None
            ),
            validation_error=None,
            error=(error.diagnostic() if isinstance(error, LlmError) else str(error)),
            error_metadata=metadata,
            representative_paths=representative_paths,
        )


def _apply_recorded_trigger(
    *,
    source_event: EvidenceEvent,
    store: EvidenceStore,
    session: GameCardSession,
    recording: Recording,
    trigger: NamingTrigger,
    output: Path,
) -> AttemptResult:
    representative_paths = _copy_representatives(output, recording, trigger)
    payload = source_event.payload
    if not isinstance(payload, SceneVerifiedPayload) or source_event.outcome != "ok":
        raise ValueError("recorded scene verification must be an accepted event")
    if payload.session_cluster_id != trigger.cluster.cluster_id:
        raise ValueError("recorded scene verification references the wrong cluster")
    if payload.action != "new":
        raise ValueError("from-zero recorded regeneration requires action=new")
    anchor = trigger.frames[-1]
    evidence_id = store.new_evidence_id(anchor.root_capture_id, "deep")
    root_capture_ids = [item.root_capture_id for item in trigger.frames]
    store.append(
        EvidenceEvent(
            evidence_id=evidence_id,
            source="deep",
            kind="scene_verified",
            root_capture_id=anchor.root_capture_id,
            observed_at=anchor.frame.relative_seconds,
            learned_at=max(anchor.frame.relative_seconds, trigger.cluster.last_seen),
            scope=None,
            payload=payload.model_copy(update={"root_capture_ids": root_capture_ids}),
            derived_from=[
                item.fingerprint_evidence_id for item in trigger.frames
            ],
            context_version=None,
            outcome="ok",
        )
    )
    needs_review = payload.modality == "uncertain"
    session.record_verification(
        trigger.cluster,
        SceneCardVerification(
            label=payload.label,
            annotation=payload.annotation,
            modality=payload.modality,
            label_status="uncertain" if needs_review else "named",
            needs_review=needs_review,
            verified_at=datetime.now(timezone.utc),
            evidence_id=evidence_id,
        ),
    )
    return AttemptResult(
        cluster_id=trigger.cluster.cluster_id,
        accepted=True,
        label=payload.label,
        annotation=payload.annotation,
        modality=payload.modality,
        matches_existing=payload.matches_existing,
        candidate_scene_id=payload.candidate_scene_id,
        candidate_label=payload.candidate_label,
        action=payload.action,
        evidence_id=evidence_id,
        model=payload.model,
        provider=payload.provider,
        cost_usd=payload.cost_usd,
        latency_ms=payload.latency_ms,
        validation_error=None,
        error=None,
        error_metadata=None,
        representative_paths=representative_paths,
    )


async def run_evaluation(
    recordings: Sequence[Recording],
    output: Path,
    memory_root: Path,
    *,
    reset_cards: bool = True,
    cluster_ids: frozenset[int] | None = None,
    recorded_evidence_paths: Sequence[Path] = (),
    recorded_only: bool = False,
) -> tuple[RecordingResult, ...]:
    configuration = load_config(strict=True)
    generic = configuration.games["generic"].generic
    naming = generic.scene.naming
    profile = configuration.llm.profiles[naming.llm_profile]
    effective = resolve_llm_profile(configuration.llm, naming.llm_profile)
    recorded = _load_recorded_verifications(recorded_evidence_paths)
    if recorded_only and not recorded:
        raise ValueError("recorded-only regeneration requires scene_verified evidence")
    reader: DeepVisionReader | None = None
    if not recorded_only:
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
    old_counts, old_directories = _old_card_inventory(recordings, memory_root)
    if reset_cards:
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
    dispatch_stats: tuple[LlmDispatchStats, ...] = ()
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
                if cluster_ids is not None:
                    triggers = tuple(
                        trigger
                        for trigger in triggers
                        if trigger.cluster.cluster_id in cluster_ids
                    )
                attempts_list: list[AttemptResult] = []
                for trigger in triggers:
                    source_event = recorded.get(
                        (recording.label, trigger.cluster.cluster_id)
                    )
                    if source_event is not None:
                        attempts_list.append(
                            _apply_recorded_trigger(
                                source_event=source_event,
                                store=store,
                                session=session,
                                recording=recording,
                                trigger=trigger,
                                output=output,
                            )
                        )
                    elif recorded_only:
                        raise ValueError(
                            "missing recorded decision for "
                            f"{recording.label} session:c{trigger.cluster.cluster_id}"
                        )
                    else:
                        assert reader is not None
                        attempts_list.append(
                            await _name_trigger(
                                reader=reader,
                                store=store,
                                session=session,
                                recording=recording,
                                trigger=trigger,
                                output=output,
                                send_width=naming.upload_width,
                            )
                        )
                attempts = tuple(attempts_list)
            finally:
                store.close()
            session.flush(clusterer.clusters)
            promoted_hashes = {scene.representative_hash for scene in session.card.scenes}
            promoted = tuple(
                cluster.cluster_id
                for cluster in clusterer.clusters
                if cluster.representative_hash in promoted_hashes
            )
            results.append(
                RecordingResult(
                    recording=recording,
                    game_id=game_id,
                    clusters=clusterer.clusters,
                    promoted_cluster_ids=promoted,
                    attempts=attempts,
                    card_path=memory_root / game_id / "gamecard.json",
                    old_scene_count=old_counts[recording.label],
                )
            )
    finally:
        if reader is not None:
            snapshot = reader.dispatch_stats()
            dispatch_stats = (snapshot,) if snapshot is not None else ()
            reader.close()
    (output / "report.md").write_text(
        _render_report(results, dispatch_stats), encoding="utf-8", newline="\n"
    )
    return tuple(results)


def _render_report(
    results: Sequence[RecordingResult],
    dispatch_stats: Sequence[LlmDispatchStats] = (),
) -> str:
    total_cost = sum(
        attempt.cost_usd for result in results for attempt in result.attempts
    )
    rate_limit_count = sum(item.rate_limit_count for item in dispatch_stats)
    cooldown_seconds = sum(item.cooldown_seconds for item in dispatch_stats)
    cooldown_drop_count = sum(item.cooldown_drop_count for item in dispatch_stats)
    lines = [
        "# M5-B-T2-5 场景命名收尾报告",
        "",
        "## 实现边界与代表帧",
        "",
        "- 指纹参数保持 `pHash64 / Hamming <= 8 / stable=4s`，本任务未改聚簇或稳定门。",
        "- 稳定门由全部轮询帧推进；场景指纹核查模型在过门时使用该连续段内已有、且具备合法 `root_capture_id` 的检测器选中帧。",
        "- 代表帧最多三张，按当时可用帧的首／中／末选取；少于三张时去重后全部使用。上传宽度为 1920px，16:9 输入即 1080p。",
        "- 每会话请求上限为 8：四段校准录像单段最多 4 个稳定簇，取两倍余量，同时给异常簇风暴设置花费边界。",
        "- 本批保留录制没有同时间线的稳定 OCR 产物，因此请求未附 OCR；在线原语保留该输入槽位。",
        "- 场景指纹核查模型不联网、不做跨簇归并、不做 variants；模型只提议，代码检查稳定门、中文标签和长度后执行。",
        "- 上卡门为：稳定簇取得有效命名即可。驻留与访问次数只记录；label 词汇不参与上卡判断，反复出现的战斗界面也可以入卡。",
        "- 场景命名提示词措辞未修改。",
        (
            "- 限流响应 / 冷却累计 / 冷却丢弃："
            f"{rate_limit_count} / {cooldown_seconds:.3f}s / {cooldown_drop_count}。"
        ),
        "",
        "## 四张卡汇总",
        "",
        "| 录像 | 旧场景数 | 新场景数 | 命名数 | uncertain | 被拒判决 | 核查失败 |",
        "|---|---:|---:|---:|---:|---:|---:|",
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
            f"| {result.recording.label} | {result.old_scene_count} | {len(scenes)} | {named} | "
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
                    f"  - 场景指纹核查证据：{', '.join(scene['deep_evidence_ids']) or '无'}",
                )
            )
        lines.append("")
    lines.extend(("## 每次场景指纹核查与花费", "", "| 录像 | 簇 | 结果 | modality | 模型 / 上游 | 延迟 | 花费 |", "|---|---|---|---|---|---:|---:|"))
    for result in results:
        for attempt in result.attempts:
            outcome = (
                f"接受（{attempt.action}）"
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
            "## 错误元数据",
            "",
        )
    )
    errors = [
        (result.recording.label, attempt)
        for result in results
        for attempt in result.attempts
        if attempt.error_metadata is not None
    ]
    if errors:
        for recording_label, attempt in errors:
            lines.append(
                f"- {recording_label} / session:c{attempt.cluster_id}："
                + json.dumps(
                    attempt.error_metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    else:
        lines.append("- 无 LLM 错误。")
    lines.extend(
        (
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
            "{existing_card_candidate_or_none}",
            "请只根据这些画面、文字与给出的当前命名完成场景确认或命名。",
            "```",
            "",
            "## 偏差与未完成项",
            "",
            "- 相对原 M5-B-T2-5 规格第 2 条的偏差：依产品负责人后续指令，删除 label 词汇过滤；所有稳定且取得有效命名的簇均可上卡。",
            "- OCR 是可选输入；本批录像没有与选中帧对齐的稳定 OCR 流，故未附加，也未重新跑 OCR。",
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
    parser.add_argument(
        "--preserve-cards",
        action="store_true",
        help="keep successful names from earlier independent sessions when a later check fails",
    )
    parser.add_argument(
        "--cluster-id",
        action="append",
        type=int,
        help="evaluate only this session cluster (repeatable; offline calibration only)",
    )
    parser.add_argument(
        "--recorded-evidence",
        action="append",
        type=Path,
        help="reuse accepted scene_verified rows from this real replay evidence file",
    )
    parser.add_argument(
        "--recorded-only",
        action="store_true",
        help="fail instead of calling the model when a stable cluster lacks recorded evidence",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    recordings = tuple(
        load_recording(path) for path in tuple(arguments.recording or DEFAULT_RECORDINGS)
    )
    results = asyncio.run(
        run_evaluation(
            recordings,
            arguments.output,
            arguments.memory_dir,
            reset_cards=not arguments.preserve_cards,
            cluster_ids=(
                frozenset(arguments.cluster_id)
                if arguments.cluster_id is not None
                else None
            ),
            recorded_evidence_paths=tuple(arguments.recorded_evidence or ()),
            recorded_only=arguments.recorded_only,
        )
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
