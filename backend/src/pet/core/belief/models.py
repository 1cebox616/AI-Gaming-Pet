"""Pydantic models for the version-one append-only evidence contract."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pet.core.gamecard import (
    GameKnowledgeMode,
    GameKnowledgeOutcome,
    GameKnowledgeWriteAction,
)

EvidenceSource = Literal[
    "fast",
    "deep",
    "ocr",
    "input",
    "mouse",
    "scene",
    "detector",
    "init",
    "gsi",
]
EvidenceOutcome = Literal["ok", "dropped", "superseded", "failed"]
ChangeReason = Literal["sparse", "coarse", "large", "forced"]
MouseMotionDirection = Literal[
    "left",
    "right",
    "up",
    "down",
    "horizontal",
    "vertical",
]
MouseMotionMagnitude = Literal["slight", "moderate", "large"]

_FRAME_ROOT_PATTERN = re.compile(r"f[1-9]\d*\Z")
_NON_FRAME_ID_PATTERN = re.compile(r"n(?P<sequence>\d{12}):(?P<source>[a-z][a-z0-9_]*)\Z")


class EvidenceModel(BaseModel):
    """Strict base so persisted evidence cannot silently lose unknown fields."""

    model_config = ConfigDict(extra="forbid")


class Scope(EvidenceModel):
    cells: list[str]
    grid: tuple[int, int]
    bbox: tuple[float, float, float, float]
    location: str
    area_ratio: float = Field(ge=0.0, le=100.0)

    @model_validator(mode="after")
    def validate_geometry(self) -> Scope:
        rows, columns = self.grid
        if rows <= 0 or columns <= 0:
            raise ValueError("scope grid dimensions must be positive")
        x0, y0, x1, y1 = self.bbox
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise ValueError("scope bbox must be normalized as x0,y0,x1,y1")
        if not self.cells:
            raise ValueError("scope must contain at least one confirmed cell")
        return self


class FastObservationPayload(EvidenceModel):
    text: str
    scene: str | None
    local: str | None
    speculation: str | None
    game: str
    latency_ms: float = Field(ge=0.0)
    ttft_ms: float | None = Field(default=None, ge=0.0)
    visible_output_tokens: int | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    truncated: bool
    cost_usd: float = Field(ge=0.0)
    actual_model: str | None
    actual_provider: str | None
    user_prompt: str | None
    drop_reason: str | None


class FrameMetricsPayload(EvidenceModel):
    reason: ChangeReason
    change_ratio: float = Field(ge=0.0, le=1.0)
    global_change: float = Field(ge=0.0, le=100.0)
    region_area_ratio: float | None = Field(default=None, ge=0.0, le=100.0)
    region_intensity: float | None = Field(default=None, ge=0.0, le=100.0)
    heartbeat: bool
    wall: str

    @model_validator(mode="after")
    def validate_region_pair(self) -> FrameMetricsPayload:
        if (self.region_area_ratio is None) != (self.region_intensity is None):
            raise ValueError("region area and intensity must either both exist or both be absent")
        if self.heartbeat != (self.reason == "forced"):
            raise ValueError("heartbeat must exactly match reason=forced")
        return self


class KeyWindowPayload(EvidenceModel):
    summary: str
    window_start: float | None = Field(default=None, ge=0.0)
    window_end: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_window(self) -> KeyWindowPayload:
        if self.window_start is not None and self.window_start > self.window_end:
            raise ValueError("key window start must not be later than its end")
        return self


class MouseMotionPayload(EvidenceModel):
    window_start: float | None = Field(default=None, ge=0.0)
    window_end: float = Field(ge=0.0)
    direction: MouseMotionDirection
    magnitude: MouseMotionMagnitude
    raw_count_total: int = Field(ge=1)
    estimated_degrees: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def validate_window(self) -> MouseMotionPayload:
        if self.window_start is not None and self.window_start > self.window_end:
            raise ValueError("mouse window start must not be later than its end")
        return self


class TextObservedPayload(EvidenceModel):
    text: str = Field(min_length=1)
    bbox: tuple[float, float, float, float]
    quad: tuple[tuple[float, float], ...] | None = None
    change: Literal["new", "changed", "gone", "stable"]
    previous_text: str | None = None
    streak: int = Field(ge=1)
    engine: str = Field(min_length=1)
    engine_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_text_observation(self) -> TextObservedPayload:
        x0, y0, x1, y1 = self.bbox
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise ValueError("OCR bbox must be normalized as x0,y0,x1,y1")
        if self.quad is not None:
            if len(self.quad) != 4:
                raise ValueError("OCR quad must contain exactly four points")
            if any(not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0) for x, y in self.quad):
                raise ValueError("OCR quad points must be normalized")
        if (self.change == "changed") != (self.previous_text is not None):
            raise ValueError("changed OCR text must be the only change with previous_text")
        return self


class OcrFramePayload(EvidenceModel):
    engine: str = Field(min_length=1)
    num_threads: int = Field(ge=1)
    det_limit_side_len: int = Field(ge=1)
    recognized_line_count: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0.0)
    det_ms: float | None = Field(default=None, ge=0.0)
    rec_ms: float | None = Field(default=None, ge=0.0)
    cpu_core_seconds: float | None = Field(default=None, ge=0.0)
    trigger: Literal["detector", "heartbeat"]
    outcome_detail: Literal["ok", "late", "skipped_disabled", "failed"]


class SceneFingerprintPayload(EvidenceModel):
    hash: str = Field(pattern=r"^(?:[0-9a-f]{16}|[0-9a-f]{64})$")
    cluster_id: int = Field(ge=1)
    distance: int = Field(ge=0)
    is_new_cluster: bool
    switched_from: int | None = Field(default=None, ge=1)
    stable: bool
    card_candidate_scene_id: str | None = Field(
        default=None,
        pattern=r"^scene:s[1-9]\d*$",
    )
    card_candidate_distance: int | None = Field(default=None, ge=0)
    elapsed_ms: float = Field(ge=0.0)

    @model_validator(mode="after")
    def validate_card_candidate(self) -> SceneFingerprintPayload:
        if (self.card_candidate_scene_id is None) != (
            self.card_candidate_distance is None
        ):
            raise ValueError("scene card candidate id and distance must coexist")
        return self


class SceneVerifiedPayload(EvidenceModel):
    session_cluster_id: int = Field(ge=1)
    label: str
    annotation: str
    modality: Literal["observed", "inferred", "uncertain"]
    matches_existing: bool | None
    candidate_scene_id: str | None = Field(
        default=None,
        pattern=r"^scene:s[1-9]\d*$",
    )
    candidate_label: str | None = None
    action: Literal["new", "reused", "replaced", "rejected"]
    root_capture_ids: list[str] = Field(min_length=1)
    model: str = Field(min_length=1)
    provider: str | None = None
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float = Field(ge=0.0)
    latency_ms: float = Field(ge=0.0)
    validation_error: str | None = None

    @model_validator(mode="after")
    def validate_capture_ids(self) -> SceneVerifiedPayload:
        if not self.root_capture_ids:
            raise ValueError("scene verification must cite at least one viewed frame")
        if any(
            not value.startswith("f") or not value[1:].isdigit()
            for value in self.root_capture_ids
        ):
            raise ValueError("scene verification root ids must use f<sequence>")
        if len(set(self.root_capture_ids)) != len(self.root_capture_ids):
            raise ValueError("scene verification root ids must be unique")
        has_candidate = self.candidate_scene_id is not None
        if has_candidate != (self.candidate_label is not None):
            raise ValueError("scene verification candidate id and label must coexist")
        if self.action == "new" and (has_candidate or self.matches_existing is not None):
            raise ValueError("new scene verification cannot cite a card candidate")
        if self.action == "reused" and (
            not has_candidate or self.matches_existing is not True
        ):
            raise ValueError("reused scene verification requires a matching candidate")
        if self.action == "replaced" and (
            not has_candidate or self.matches_existing is not False
        ):
            raise ValueError("replaced scene verification requires a rejected candidate")
        return self


class GameKnowledgePayload(EvidenceModel):
    game_id: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    mode: GameKnowledgeMode
    model: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    outcome: GameKnowledgeOutcome
    latency_ms: float = Field(ge=0.0)
    cost_usd: float = Field(ge=0.0)
    write_action: GameKnowledgeWriteAction
    actual_model: str | None = None
    provider: str | None = None
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    failure_reason: str | None = None
    normalization_actions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result(self) -> GameKnowledgePayload:
        if self.outcome == "ok":
            if self.write_action == "kept_previous":
                raise ValueError("successful knowledge evidence must update the card")
            if self.failure_reason is not None:
                raise ValueError("successful knowledge evidence cannot have failure_reason")
        else:
            if self.write_action != "kept_previous":
                raise ValueError("failed knowledge evidence must keep previous content")
            if not (self.failure_reason or "").strip():
                raise ValueError("failed knowledge evidence requires failure_reason")
        return self


EvidencePayload = (
    FastObservationPayload
    | FrameMetricsPayload
    | KeyWindowPayload
    | MouseMotionPayload
    | TextObservedPayload
    | OcrFramePayload
    | SceneFingerprintPayload
    | SceneVerifiedPayload
    | GameKnowledgePayload
)


class EvidenceEvent(EvidenceModel):
    evidence_id: str
    source: EvidenceSource
    kind: str
    root_capture_id: str | None
    observed_at: float = Field(ge=0.0)
    learned_at: float = Field(ge=0.0)
    scope: Scope | None
    payload: EvidencePayload
    derived_from: list[str]
    context_version: int | None = Field(default=None, ge=0)
    outcome: EvidenceOutcome

    @model_validator(mode="after")
    def validate_kind_and_outcome(self) -> EvidenceEvent:
        expected_payloads: dict[str, type[EvidenceModel]] = {
            "fast_observation": FastObservationPayload,
            "frame_metrics": FrameMetricsPayload,
            "key_window": KeyWindowPayload,
            "mouse_motion": MouseMotionPayload,
            "text_observed": TextObservedPayload,
            "ocr_frame": OcrFramePayload,
            "scene_fingerprint": SceneFingerprintPayload,
            "scene_verified": SceneVerifiedPayload,
            "game_knowledge": GameKnowledgePayload,
        }
        expected = expected_payloads.get(self.kind)
        if expected is None or not isinstance(self.payload, expected):
            raise ValueError(f"payload does not match evidence kind {self.kind!r}")
        expected_sources: dict[str, EvidenceSource] = {
            "fast_observation": "fast",
            "frame_metrics": "detector",
            "key_window": "input",
            "mouse_motion": "mouse",
            "text_observed": "ocr",
            "ocr_frame": "ocr",
            "scene_fingerprint": "scene",
            "scene_verified": "deep",
            "game_knowledge": "init",
        }
        if self.source != expected_sources[self.kind]:
            raise ValueError(f"source does not match evidence kind {self.kind!r}")
        if self.kind in {"mouse_motion", "game_knowledge"}:
            if self.root_capture_id is not None:
                raise ValueError("non-frame mouse evidence must not have root_capture_id")
            match = _NON_FRAME_ID_PATTERN.fullmatch(self.evidence_id)
            if (
                match is None
                or match.group("source") != self.source
                or int(match.group("sequence")) < 1
            ):
                raise ValueError(
                    "non-frame evidence_id must use n<12-digit sequence>:<source>"
                )
        else:
            if self.root_capture_id is None:
                raise ValueError("frame evidence requires root_capture_id")
            if _FRAME_ROOT_PATTERN.fullmatch(self.root_capture_id) is None:
                raise ValueError("frame root_capture_id must use f<positive sequence>")
            identifier_parts = self.evidence_id.split(":")
            if (
                len(identifier_parts) != 3
                or identifier_parts[0] != self.root_capture_id
                or identifier_parts[1] != self.source
                or not identifier_parts[2].isdigit()
                or int(identifier_parts[2]) < 1
            ):
                raise ValueError(
                    "evidence_id must match root_capture_id, source, and positive sequence"
                )
        if self.learned_at < self.observed_at:
            raise ValueError("learned_at must not precede observed_at")
        if self.kind not in {
            "fast_observation",
            "scene_verified",
            "game_knowledge",
        } and self.outcome != "ok":
            raise ValueError("mechanical evidence must have outcome=ok")
        if isinstance(self.payload, GameKnowledgePayload):
            expected_outcome: EvidenceOutcome = {
                "ok": "ok",
                "cooldown_drop": "dropped",
                "timeout": "dropped",
                "failed": "failed",
                "schema_reject": "failed",
            }[self.payload.outcome]
            if self.outcome != expected_outcome:
                raise ValueError(
                    "game knowledge event outcome does not match payload outcome"
                )
        if isinstance(self.payload, SceneVerifiedPayload):
            if self.outcome == "ok" and self.payload.validation_error is not None:
                raise ValueError("accepted scene verification cannot have validation_error")
            if self.outcome == "failed" and not self.payload.validation_error:
                raise ValueError("rejected scene verification needs validation_error")
            if self.outcome not in {"ok", "failed"}:
                raise ValueError("scene verification outcome must be ok or failed")
            if (self.payload.action == "rejected") != (self.outcome == "failed"):
                raise ValueError("rejected scene verification must use outcome=failed")
        if isinstance(self.payload, FastObservationPayload):
            if self.outcome == "ok":
                if not self.payload.text or self.payload.drop_reason is not None:
                    raise ValueError("successful fast evidence needs text and no drop reason")
            elif self.payload.text or not self.payload.drop_reason:
                raise ValueError("non-success fast evidence needs empty text and a drop reason")
        return self
