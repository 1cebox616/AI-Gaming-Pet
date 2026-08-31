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

from pydantic import BaseModel, ConfigDict, Field

from pet.core.scene_fingerprint import CardSceneReference, SceneCluster

GAMECARD_VERSION = 1


class GameCardModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GameCardScene(GameCardModel):
    cluster_id: str = Field(pattern=r"^scene:s[1-9]\d*$")
    label: str | None = None
    representative_hash: str = Field(pattern=r"^(?:[0-9a-f]{16}|[0-9a-f]{64})$")
    first_seen: datetime
    last_seen: datetime
    seen_count: int = Field(ge=1)
    visit_count: int = Field(ge=1)
    sessions_seen: int = Field(ge=1)
    evidence_ids: list[str]


class GameCardHudSlot(GameCardModel):
    slot_id: str
    bbox: tuple[float, float, float, float]
    semantic_role: str | None = None
    role_status: str | None = None
    evidence_ids: list[str]


class GameCardKeybind(GameCardModel):
    meaning: str
    source: Literal["default_table", "observed"]
    support_count: int = Field(ge=1)


class GameCardViewConstants(GameCardModel):
    yaw_deg_per_count: float | None = None
    user_sensitivity: float | None = None


class GameCardInit(GameCardModel):
    initialized_at: datetime
    source_recordings: list[str]
    version: int = Field(default=GAMECARD_VERSION, ge=1)


class GameCard(GameCardModel):
    game_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    genre: str | None = None
    perspective: str | None = None
    scenes: list[GameCardScene]
    hud_slots: list[GameCardHudSlot]
    keybinds: dict[str, GameCardKeybind]
    view_constants: GameCardViewConstants
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
        f"- 类型：{card.genre or '未学习'}",
        f"- 视角：{card.perspective or '未学习'}",
        "",
        "## 场景",
        "",
    ]
    if not card.scenes:
        lines.append("暂无已升格场景。")
    for scene in card.scenes:
        lines.extend(
            (
                f"### {scene.cluster_id}",
                "",
                f"- 标签：{scene.label or '未标注'}",
                f"- 代表指纹：`{scene.representative_hash}`",
                f"- 首次见到：{_iso(scene.first_seen)}",
                f"- 最近见到：{_iso(scene.last_seen)}",
                f"- 累计帧数：{scene.seen_count}",
                f"- 累计访问段数：{scene.visit_count}",
                f"- 会话簇次数：{scene.sessions_seen}",
                "- 证据样本："
                + (", ".join(f"`{item}`" for item in scene.evidence_ids) or "无"),
                "",
            )
        )
    lines.extend(("## HUD 槽位", "", "暂无。", "", "## 键位", "", "暂无。", ""))
    if card.init.source_recordings:
        lines.extend(("## 来源录制", ""))
        lines.extend(f"- `{path}`" for path in card.init.source_recordings)
        lines.append("")
    return "\n".join(lines)


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
            genre=None,
            perspective=None,
            scenes=[],
            hud_slots=[],
            keybinds={},
            view_constants=GameCardViewConstants(),
            init=GameCardInit(
                initialized_at=_utc(initialized_at),
                source_recordings=recordings,
                version=GAMECARD_VERSION,
            ),
        )

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
                scene_id=scene.cluster_id,
                representative_hash=scene.representative_hash,
            )
            for scene in card.scenes
        )

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
        card_min_dwell_seconds: float,
        source_recording: str | None = None,
    ) -> None:
        if card_min_dwell_seconds < 0:
            raise ValueError("card_min_dwell_seconds must be nonnegative")
        self.repository = repository
        self.card = card
        self.started_at = _utc(started_at)
        self.card_min_dwell_seconds = card_min_dwell_seconds
        self.source_recording = source_recording
        self._cluster_scene_ids: dict[int, str] = {}
        self._flushed_seen_counts: dict[int, int] = {}
        self._flushed_visit_counts: dict[int, int] = {}

    def flush(self, clusters: Sequence[SceneCluster]) -> GameCard:
        working = self.card.model_copy(deep=True)
        cluster_scene_ids = dict(self._cluster_scene_ids)
        flushed_seen_counts = dict(self._flushed_seen_counts)
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
                        cluster_id=scene_id,
                        label=None,
                        representative_hash=cluster.representative_hash,
                        first_seen=self._absolute(cluster.first_seen),
                        last_seen=self._absolute(cluster.last_seen),
                        seen_count=cluster.seen_count,
                        visit_count=cluster.visit_count,
                        sessions_seen=1,
                        evidence_ids=[],
                    )
                    working.scenes.append(scene)
                    created_scene = True
                else:
                    scene_id = scene.cluster_id
                cluster_scene_ids[cluster.cluster_id] = scene_id
                if not created_scene:
                    scene.sessions_seen += 1
            else:
                scene = _scene_by_id(working.scenes, scene_id)
                if scene is None:
                    raise ValueError(f"flushed scene disappeared from game card: {scene_id}")

            previous_count = flushed_seen_counts.get(cluster.cluster_id, 0)
            previous_visits = flushed_visit_counts.get(cluster.cluster_id, 0)
            if created_scene:
                previous_count = cluster.seen_count
                previous_visits = cluster.visit_count
            delta = cluster.seen_count - previous_count
            visit_delta = cluster.visit_count - previous_visits
            if delta < 0:
                raise ValueError("session cluster seen_count must be append-only")
            if visit_delta < 0:
                raise ValueError("session cluster visit_count must be append-only")
            scene.seen_count += delta
            scene.visit_count += visit_delta
            scene.last_seen = max(scene.last_seen, self._absolute(cluster.last_seen))
            for evidence_id in cluster.evidence_ids:
                if evidence_id not in scene.evidence_ids and len(scene.evidence_ids) < 5:
                    scene.evidence_ids.append(evidence_id)
            flushed_seen_counts[cluster.cluster_id] = cluster.seen_count
            flushed_visit_counts[cluster.cluster_id] = cluster.visit_count

        working.scenes.sort(key=lambda scene: _scene_number(scene.cluster_id))
        self.repository.write(working)
        self.card = working
        self._cluster_scene_ids = cluster_scene_ids
        self._flushed_seen_counts = flushed_seen_counts
        self._flushed_visit_counts = flushed_visit_counts
        return working

    def _qualifies(self, cluster: SceneCluster) -> bool:
        return (
            cluster.stable
            and cluster.dwell_seconds >= self.card_min_dwell_seconds
        )

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
    return next((scene for scene in scenes if scene.cluster_id == scene_id), None)


def _next_scene_id(scenes: Sequence[GameCardScene]) -> str:
    next_number = max((_scene_number(scene.cluster_id) for scene in scenes), default=0) + 1
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
