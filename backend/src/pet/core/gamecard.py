"""Typed, atomic persistence for the cross-session generic game card."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pet.core.scene_fingerprint import CardSceneReference, SceneCluster

GAMECARD_VERSION = 2
KNOWLEDGE_ATTEMPT_HISTORY_LIMIT = 20  # 待实测：先限制跨会话卡文件无限增长。

CANONICAL_PC_INPUT_PATTERN = (
    r"^(?:(?:[A-Z]|[0-9]|F(?:[1-9]|1[0-9]|2[0-4])|Mouse[1-5])|"
    r"(?:Space|Tab|Escape|Enter|Backspace|CapsLock|LeftShift|RightShift|"
    r"LeftCtrl|RightCtrl|LeftAlt|RightAlt|ArrowUp|ArrowDown|ArrowLeft|"
    r"ArrowRight|MouseLeft|MouseRight|MouseMiddle|MouseWheelUp|"
    r"MouseWheelDown|MouseMove|Backquote|Minus|Equals|LeftBracket|"
    r"RightBracket|Backslash|Semicolon|Apostrophe|Comma|Period|Slash))"
    r"(?:\+(?:(?:[A-Z]|[0-9]|F(?:[1-9]|1[0-9]|2[0-4])|Mouse[1-5])|"
    r"(?:Space|Tab|Escape|Enter|Backspace|CapsLock|LeftShift|RightShift|"
    r"LeftCtrl|RightCtrl|LeftAlt|RightAlt|ArrowUp|ArrowDown|ArrowLeft|"
    r"ArrowRight|MouseLeft|MouseRight|MouseMiddle|MouseWheelUp|"
    r"MouseWheelDown|MouseMove|Backquote|Minus|Equals|LeftBracket|"
    r"RightBracket|Backslash|Semicolon|Apostrophe|Comma|Period|Slash)))*$"
)

GameKnowledgeMode = Literal["web", "knowledge"]
GameKnowledgeOutcome = Literal[
    "ok",
    "failed",
    "cooldown_drop",
    "timeout",
    "schema_reject",
]
GameKnowledgeStatus = Literal["initialized", "refreshed", "stale"]
GameKnowledgeWriteAction = Literal["initialized", "refreshed", "kept_previous"]


class GameCardModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GameKnowledgeSystem(GameCardModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class GameKnowledgeGameplay(GameCardModel):
    player_goal: str = Field(min_length=1)
    core_loop: str = Field(min_length=1)
    major_systems: list[GameKnowledgeSystem] = Field(min_length=4, max_length=10)
    modes_and_structure: str = Field(min_length=1)


class GameKnowledgeBackground(GameCardModel):
    setting_and_premise: str = Field(min_length=1)
    release_and_service_status: str = Field(min_length=1)


class GameKnowledgeContent(GameCardModel):
    """The complete V3 shelf-one answer; partial answers are never valid."""

    genre: list[str] = Field(min_length=1, max_length=5)
    perspective: str = Field(min_length=1)
    game_overview: str = Field(min_length=1)
    gameplay: GameKnowledgeGameplay
    background: GameKnowledgeBackground
    default_pc_keybinds: dict[
        str,
        str,
    ] = Field(default_factory=dict, max_length=40)

    @model_validator(mode="after")
    def validate_nonempty_text_and_keybinds(self) -> GameKnowledgeContent:
        strings = [
            *self.genre,
            self.perspective,
            self.game_overview,
            self.gameplay.player_goal,
            self.gameplay.core_loop,
            self.gameplay.modes_and_structure,
            self.background.setting_and_premise,
            self.background.release_and_service_status,
        ]
        for system in self.gameplay.major_systems:
            strings.extend((system.name, system.description))
        if any(not value.strip() for value in strings):
            raise ValueError("game knowledge text fields must not be blank")
        for action, input_name in self.default_pc_keybinds.items():
            if not action.strip():
                raise ValueError("game knowledge keybind actions must not be blank")
            if re.fullmatch(CANONICAL_PC_INPUT_PATTERN, input_name) is None:
                raise ValueError(
                    f"game knowledge keybind {action!r} is not canonical: {input_name!r}"
                )
        return self


class GameKnowledgeAttempt(GameCardModel):
    attempted_at: datetime
    result: GameKnowledgeOutcome
    failure_reason: str | None = None

    @model_validator(mode="after")
    def validate_failure_reason(self) -> GameKnowledgeAttempt:
        if self.result == "ok" and self.failure_reason is not None:
            raise ValueError("successful knowledge attempt cannot have a failure reason")
        if self.result != "ok" and not (self.failure_reason or "").strip():
            raise ValueError("failed knowledge attempt requires a failure reason")
        return self


class GameKnowledge(GameCardModel):
    content: GameKnowledgeContent | None
    status: GameKnowledgeStatus
    checked_at: datetime
    model: str = Field(min_length=1)
    mode: GameKnowledgeMode
    request_id: str = Field(min_length=1)
    attempts: list[GameKnowledgeAttempt] = Field(
        min_length=1,
        max_length=KNOWLEDGE_ATTEMPT_HISTORY_LIMIT,
    )

    @model_validator(mode="after")
    def validate_content_status(self) -> GameKnowledge:
        if self.status in {"initialized", "refreshed"} and self.content is None:
            raise ValueError("successful knowledge status requires complete content")
        return self


class GameCardScene(GameCardModel):
    scene_id: str = Field(pattern=r"^scene:s[1-9]\d*$")
    representative_hash: str = Field(pattern=r"^(?:[0-9a-f]{16}|[0-9a-f]{64})$")
    label: str | None = None
    label_status: Literal["unnamed", "named", "uncertain"] = "unnamed"
    needs_review: bool = False
    annotation: str | None = None
    dwell_seconds: float = Field(ge=0.0)
    visit_count: int = Field(ge=1)
    sessions_seen: int = Field(ge=1)
    first_seen: datetime
    last_seen: datetime
    evidence_ids: list[str]
    verified_at: datetime | None = None
    deep_evidence_ids: list[str] = Field(default_factory=list)


class SceneCardVerification(GameCardModel):
    """One code-validated naming decision waiting for card promotion."""

    label: str = Field(min_length=1, max_length=24)
    annotation: str = Field(min_length=1, max_length=160)
    modality: Literal["observed", "inferred", "uncertain"]
    label_status: Literal["named", "uncertain"]
    needs_review: bool
    verified_at: datetime
    evidence_id: str = Field(min_length=1)


class GameCardInit(GameCardModel):
    initialized_at: datetime
    source_recordings: list[str]
    version: int = Field(default=GAMECARD_VERSION, ge=1)


class GameCard(GameCardModel):
    game_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    knowledge: GameKnowledge | None = None
    scenes: list[GameCardScene]
    # B-T3c owns the element contract. Until then this field is deliberately empty.
    hud_elements: list[object] = Field(default_factory=list, max_length=0)
    init: GameCardInit


def slugify_game_id(value: str, display_name: str | None = None) -> str:
    """Return an ASCII-only slug, hashing the display name when none remains."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters: list[str] = []
    pending_hyphen = False
    for character in normalized:
        if character.isascii() and character.isalnum():
            if pending_hyphen and characters:
                characters.append("-")
            characters.append(character)
            pending_hyphen = False
        else:
            pending_hyphen = True
    slug = "".join(characters).strip("-")
    if not slug:
        source = display_name if display_name is not None else value
        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
        return f"g-{digest}"
    return slug


def render_gamecard_markdown(card: GameCard) -> str:
    """Render the disposable human view directly from typed JSON data."""
    lines = [
        f"# 游戏卡：{card.display_name}",
        "",
        f"- 游戏 ID：`{card.game_id}`",
        f"- 初始化时间：{_iso(card.init.initialized_at)}",
        f"- 版本：{card.init.version}",
        "",
        "## 游戏知识",
        "",
    ]
    if card.knowledge is None:
        lines.extend(("尚未尝试联网初始化。", ""))
    else:
        knowledge = card.knowledge
        lines.extend(
            (
                f"- 状态：{knowledge.status}",
                f"- 最近核查：{_iso(knowledge.checked_at)}",
                f"- 模型：`{knowledge.model}`",
                f"- 模式：{knowledge.mode}",
                f"- 请求 ID：`{knowledge.request_id}`",
                "",
            )
        )
        if knowledge.content is None:
            lines.extend(("没有通过完整 V3 合同的有效内容。", ""))
        else:
            content = knowledge.content
            lines.extend(
                (
                    f"- 类型：{'、'.join(content.genre)}",
                    f"- 视角：{content.perspective}",
                    f"- 游戏概述：{content.game_overview}",
                    f"- 玩家目标：{content.gameplay.player_goal}",
                    f"- 核心循环：{content.gameplay.core_loop}",
                    f"- 模式与结构：{content.gameplay.modes_and_structure}",
                    f"- 背景前提：{content.background.setting_and_premise}",
                    f"- 发售与运营：{content.background.release_and_service_status}",
                    "",
                    "### 主要系统",
                    "",
                )
            )
            lines.extend(
                f"- {system.name}：{system.description}"
                for system in content.gameplay.major_systems
            )
            lines.extend(("", "### PC 默认键位", ""))
            if content.default_pc_keybinds:
                lines.extend(
                    f"- {action}：`{input_name}`"
                    for action, input_name in content.default_pc_keybinds.items()
                )
            else:
                lines.append("没有可确认的默认键位。")
            lines.append("")
        lines.extend(("### 核查尝试", ""))
        for attempt in knowledge.attempts:
            detail = (
                f"；原因：{attempt.failure_reason}"
                if attempt.failure_reason is not None
                else ""
            )
            lines.append(
                f"- {_iso(attempt.attempted_at)}：{attempt.result}{detail}"
            )
        lines.append("")
    lines.extend(
        (
        "## 场景",
        "",
        )
    )
    if not card.scenes:
        lines.append("暂无已升格场景。")
    for scene in card.scenes:
        lines.extend(
            (
                f"### {scene.scene_id}",
                "",
                f"- 标签：{scene.label or '未标注'}",
                f"- 标签状态：{scene.label_status}",
                f"- 待复查：{'是' if scene.needs_review else '否'}",
                f"- 注释：{scene.annotation or '未标注'}",
                f"- 代表指纹：`{scene.representative_hash}`",
                f"- 首次见到：{_iso(scene.first_seen)}",
                f"- 最近见到：{_iso(scene.last_seen)}",
                f"- 累计驻留：{scene.dwell_seconds:.3f} 秒",
                f"- 累计访问段数：{scene.visit_count}",
                f"- 会话簇次数：{scene.sessions_seen}",
                "- 证据样本："
                + (", ".join(f"`{item}`" for item in scene.evidence_ids) or "无"),
                f"- 命名时间：{_iso(scene.verified_at) if scene.verified_at else '未命名'}",
                "- 场景指纹核查证据："
                + (", ".join(f"`{item}`" for item in scene.deep_evidence_ids) or "无"),
                "",
            )
        )
    lines.extend(("## HUD 元素", "", "暂无。", ""))
    if card.init.source_recordings:
        lines.extend(("## 来源录制", ""))
        lines.extend(f"- `{path}`" for path in card.init.source_recordings)
        lines.append("")
    return "\n".join(lines)


def render_game_knowledge_short_view(
    content: GameKnowledgeContent | None,
    token_limit: int,
) -> str:
    """Render a conservative model context that cannot exceed the token cap.

    Without adding a model-specific tokenizer, UTF-8 byte length is used as a
    strict upper bound: a byte-fallback tokenizer cannot emit more tokens than
    input bytes. The default cap remains marked as pending measurement in config.
    """
    if token_limit < 1:
        raise ValueError("game knowledge short-view token limit must be positive")
    if content is None:
        return ""
    systems = "；".join(
        f"{item.name}：{item.description}" for item in content.gameplay.major_systems
    )
    keybinds = "；".join(
        f"{action}={input_name}"
        for action, input_name in content.default_pc_keybinds.items()
    )
    sections = (
        f"【游戏知识｜推测背景】{content.game_overview}",
        f"类型：{'、'.join(content.genre)}；视角：{content.perspective}",
        f"目标：{content.gameplay.player_goal}",
        f"循环：{content.gameplay.core_loop}",
        f"系统：{systems}",
        f"结构：{content.gameplay.modes_and_structure}",
        f"背景：{content.background.setting_and_premise}",
        f"运营：{content.background.release_and_service_status}",
        f"默认键位：{keybinds}" if keybinds else "默认键位：无可确认条目",
    )
    return _truncate_utf8("\n".join(sections), token_limit)


class GameCardRepository:
    """Load and atomically replace one JSON source plus its Markdown view."""

    def __init__(self, memory_root: Path) -> None:
        self.memory_root = memory_root

    def load_or_create(
        self,
        game_id: str,
        display_name: str,
        initialized_at: datetime,
        *,
        source_recording: str | None = None,
    ) -> GameCard:
        path = self._directory(game_id) / "gamecard.json"
        if path.is_file():
            card = GameCard.model_validate_json(path.read_text(encoding="utf-8"))
            if card.game_id != game_id:
                raise ValueError(
                    f"game card path identity {game_id!r} conflicts with {card.game_id!r}"
                )
            return card
        recordings = [source_recording] if source_recording is not None else []
        return GameCard(
            game_id=game_id,
            display_name=display_name,
            knowledge=None,
            scenes=[],
            hud_elements=[],
            init=GameCardInit(
                initialized_at=_utc(initialized_at),
                source_recordings=recordings,
                version=GAMECARD_VERSION,
            ),
        )

    def load(self, game_id: str) -> GameCard:
        path = self._directory(game_id) / "gamecard.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        card = GameCard.model_validate_json(path.read_text(encoding="utf-8"))
        if card.game_id != game_id:
            raise ValueError(
                f"game card path identity {game_id!r} conflicts with {card.game_id!r}"
            )
        return card

    def write(self, card: GameCard) -> None:
        directory = self._directory(card.game_id)
        directory.mkdir(parents=True, exist_ok=True)
        json_text = json.dumps(
            card.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        markdown = render_gamecard_markdown(card)
        _atomic_write(directory / "gamecard.json", json_text)
        _atomic_write(directory / "gamecard.md", markdown)

    def card_references(self, card: GameCard) -> tuple[CardSceneReference, ...]:
        return tuple(
            CardSceneReference(
                scene_id=scene.scene_id,
                representative_hash=scene.representative_hash,
            )
            for scene in card.scenes
        )

    def record_knowledge_attempt(
        self,
        card: GameCard,
        *,
        checked_at: datetime,
        model: str,
        mode: GameKnowledgeMode,
        request_id: str,
        outcome: GameKnowledgeOutcome,
        failure_reason: str | None,
        content: GameKnowledgeContent | None,
    ) -> tuple[GameCard, GameKnowledgeWriteAction]:
        """Atomically keep or replace the complete shelf-one answer."""
        path = self._directory(card.game_id) / "gamecard.json"
        working = self.load(card.game_id) if path.is_file() else card.model_copy(deep=True)
        previous_content = (
            working.knowledge.content if working.knowledge is not None else None
        )
        if outcome == "ok":
            if content is None:
                raise ValueError("successful game knowledge attempt requires content")
            status: GameKnowledgeStatus = (
                "initialized" if previous_content is None else "refreshed"
            )
            action: GameKnowledgeWriteAction = status
            next_content = content
            provenance_checked_at = _utc(checked_at)
            provenance_model = model
            provenance_mode = mode
            provenance_request_id = request_id
        else:
            if content is not None:
                raise ValueError("failed game knowledge attempt cannot provide content")
            status = "stale"
            action = "kept_previous"
            next_content = previous_content
            if working.knowledge is not None and previous_content is not None:
                # ROOT CAUSE: a failed refresh must not make retained content look
                # as though it came from the failed request.  These four fields
                # remain the provenance of the current complete content; the new
                # failure is represented by attempts[] and session evidence.
                provenance_checked_at = working.knowledge.checked_at
                provenance_model = working.knowledge.model
                provenance_mode = working.knowledge.mode
                provenance_request_id = working.knowledge.request_id
            else:
                # A first-ever failed attempt has no successful provenance to
                # retain.  Its metadata identifies the only attempted lookup.
                provenance_checked_at = _utc(checked_at)
                provenance_model = model
                provenance_mode = mode
                provenance_request_id = request_id
        attempts = (
            list(working.knowledge.attempts)
            if working.knowledge is not None
            else []
        )
        attempts.append(
            GameKnowledgeAttempt(
                attempted_at=_utc(checked_at),
                result=outcome,
                failure_reason=failure_reason,
            )
        )
        attempts = attempts[-KNOWLEDGE_ATTEMPT_HISTORY_LIMIT:]
        working.knowledge = GameKnowledge(
            content=next_content,
            status=status,
            checked_at=provenance_checked_at,
            model=provenance_model,
            mode=provenance_mode,
            request_id=provenance_request_id,
            attempts=attempts,
        )
        self.write(working)
        return working, action

    def _directory(self, game_id: str) -> Path:
        if slugify_game_id(game_id) != game_id:
            raise ValueError("game_id must already be a normalized slug")
        return self.memory_root / game_id


class GameCardSession:
    """Merge qualifying session clusters without double-counting periodic flushes."""

    def __init__(
        self,
        repository: GameCardRepository,
        card: GameCard,
        started_at: datetime,
        *,
        source_recording: str | None = None,
    ) -> None:
        self.repository = repository
        self.card = card
        self.started_at = _utc(started_at)
        self.source_recording = source_recording
        self._cluster_scene_ids: dict[int, str] = {}
        self._flushed_dwell_seconds: dict[int, float] = {}
        self._flushed_visit_counts: dict[int, int] = {}
        self._verifications: dict[int, SceneCardVerification] = {}

    def flush(self, clusters: Sequence[SceneCluster]) -> GameCard:
        path = self.repository._directory(self.card.game_id) / "gamecard.json"
        working = (
            self.repository.load(self.card.game_id)
            if path.is_file()
            else self.card.model_copy(deep=True)
        )
        cluster_scene_ids = dict(self._cluster_scene_ids)
        flushed_dwell_seconds = dict(self._flushed_dwell_seconds)
        flushed_visit_counts = dict(self._flushed_visit_counts)
        if self.source_recording is not None and self.source_recording not in working.init.source_recordings:
            working.init.source_recordings.append(self.source_recording)

        for cluster in clusters:
            if not self._qualifies(cluster):
                continue
            scene_id = cluster_scene_ids.get(cluster.cluster_id)
            first_flush = scene_id is None
            created_scene = False
            if first_flush:
                candidate_id = (
                    cluster.card_candidate.scene_id
                    if cluster.card_candidate is not None
                    else None
                )
                scene = _scene_by_id(working.scenes, candidate_id)
                if scene is None:
                    scene_id = _next_scene_id(working.scenes)
                    scene = GameCardScene(
                        scene_id=scene_id,
                        representative_hash=cluster.representative_hash,
                        label=None,
                        label_status="unnamed",
                        needs_review=False,
                        annotation=None,
                        dwell_seconds=cluster.dwell_seconds,
                        visit_count=cluster.visit_count,
                        sessions_seen=1,
                        first_seen=self._absolute(cluster.first_seen),
                        last_seen=self._absolute(cluster.last_seen),
                        evidence_ids=[],
                        verified_at=None,
                        deep_evidence_ids=[],
                    )
                    working.scenes.append(scene)
                    created_scene = True
                else:
                    scene_id = scene.scene_id
                cluster_scene_ids[cluster.cluster_id] = scene_id
                if not created_scene:
                    scene.sessions_seen += 1
            else:
                scene = _scene_by_id(working.scenes, scene_id)
                if scene is None:
                    raise ValueError(f"flushed scene disappeared from game card: {scene_id}")

            previous_dwell = flushed_dwell_seconds.get(cluster.cluster_id, 0.0)
            previous_visits = flushed_visit_counts.get(cluster.cluster_id, 0)
            if created_scene:
                previous_dwell = cluster.dwell_seconds
                previous_visits = cluster.visit_count
            dwell_delta = cluster.dwell_seconds - previous_dwell
            visit_delta = cluster.visit_count - previous_visits
            if dwell_delta < -1e-9:
                raise ValueError("session cluster dwell_seconds must be append-only")
            if visit_delta < 0:
                raise ValueError("session cluster visit_count must be append-only")
            scene.dwell_seconds += max(0.0, dwell_delta)
            scene.visit_count += visit_delta
            scene.last_seen = max(scene.last_seen, self._absolute(cluster.last_seen))
            for evidence_id in cluster.evidence_ids:
                if evidence_id not in scene.evidence_ids and len(scene.evidence_ids) < 5:
                    scene.evidence_ids.append(evidence_id)
            verification = self._verifications.get(cluster.cluster_id)
            if verification is not None:
                _apply_verification(scene, verification)
            flushed_dwell_seconds[cluster.cluster_id] = cluster.dwell_seconds
            flushed_visit_counts[cluster.cluster_id] = cluster.visit_count

        working.scenes.sort(key=lambda scene: _scene_number(scene.scene_id))
        self.repository.write(working)
        self.card = working
        self._cluster_scene_ids = cluster_scene_ids
        self._flushed_dwell_seconds = flushed_dwell_seconds
        self._flushed_visit_counts = flushed_visit_counts
        return working

    def needs_verification(self, cluster: SceneCluster) -> bool:
        """Return whether this session cluster has no existing naming decision."""
        return cluster.cluster_id not in self._verifications

    def named_candidate(self, cluster: SceneCluster) -> GameCardScene | None:
        """Return the named card candidate that a new session must recheck."""
        candidate_id = (
            cluster.card_candidate.scene_id
            if cluster.card_candidate is not None
            else self._cluster_scene_ids.get(cluster.cluster_id)
        )
        scene = _scene_by_id(self.card.scenes, candidate_id)
        if scene is None or scene.label_status == "unnamed":
            return None
        if scene.label is None or scene.annotation is None:
            raise ValueError("named game-card scene lacks label or annotation")
        return scene

    def record_verification(
        self,
        cluster: SceneCluster,
        verification: SceneCardVerification,
    ) -> None:
        """Stage an accepted decision and apply it if the scene is promoted."""
        if not cluster.stable:
            raise ValueError("scene verification requires a stable cluster")
        if cluster.cluster_id in self._verifications:
            raise ValueError("scene cluster already has a verification decision")
        self._verifications[cluster.cluster_id] = verification
        self.flush((cluster,))

    def mark_candidate_for_review(
        self,
        cluster: SceneCluster,
        evidence_id: str,
    ) -> None:
        """Retain an existing name while marking an uncertain mismatch for review."""
        candidate_id = (
            cluster.card_candidate.scene_id
            if cluster.card_candidate is not None
            else self._cluster_scene_ids.get(cluster.cluster_id)
        )
        path = self.repository._directory(self.card.game_id) / "gamecard.json"
        working = (
            self.repository.load(self.card.game_id)
            if path.is_file()
            else self.card.model_copy(deep=True)
        )
        scene = _scene_by_id(working.scenes, candidate_id)
        if scene is None or scene.label_status == "unnamed":
            raise ValueError("scene review marker requires a named card candidate")
        scene.needs_review = True
        if evidence_id not in scene.deep_evidence_ids:
            scene.deep_evidence_ids.append(evidence_id)
        self.repository.write(working)
        self.card = working

    def _qualifies(self, cluster: SceneCluster) -> bool:
        verification = self._verifications.get(cluster.cluster_id)
        return cluster.stable and verification is not None

    def _absolute(self, relative_seconds: float) -> datetime:
        return self.started_at + timedelta(seconds=relative_seconds)


def _atomic_write(path: Path, text: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _scene_by_id(
    scenes: Sequence[GameCardScene], scene_id: str | None
) -> GameCardScene | None:
    if scene_id is None:
        return None
    return next((scene for scene in scenes if scene.scene_id == scene_id), None)


def _next_scene_id(scenes: Sequence[GameCardScene]) -> str:
    next_number = max((_scene_number(scene.scene_id) for scene in scenes), default=0) + 1
    return f"scene:s{next_number}"


def _scene_number(scene_id: str) -> int:
    match = re.fullmatch(r"scene:s([1-9]\d*)", scene_id)
    if match is None:
        raise ValueError(f"invalid game-card scene id: {scene_id}")
    return int(match.group(1))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("game-card timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _truncate_utf8(text: str, maximum_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return text
    return encoded[:maximum_bytes].decode("utf-8", errors="ignore").rstrip()


def _apply_verification(
    scene: GameCardScene,
    verification: SceneCardVerification,
) -> None:
    scene.label = verification.label
    scene.label_status = verification.label_status
    scene.needs_review = verification.needs_review
    scene.annotation = verification.annotation
    scene.verified_at = _utc(verification.verified_at)
    if verification.evidence_id not in scene.deep_evidence_ids:
        scene.deep_evidence_ids.append(verification.evidence_id)
