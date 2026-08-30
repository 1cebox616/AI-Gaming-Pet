"""Pydantic models for the version-one append-only evidence contract."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


EvidencePayload = (
    FastObservationPayload
    | FrameMetricsPayload
    | KeyWindowPayload
    | TextObservedPayload
    | OcrFramePayload
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
            "text_observed": TextObservedPayload,
            "ocr_frame": OcrFramePayload,
        }
        expected = expected_payloads.get(self.kind)
        if expected is None or not isinstance(self.payload, expected):
            raise ValueError(f"payload does not match evidence kind {self.kind!r}")
        expected_sources: dict[str, EvidenceSource] = {
            "fast_observation": "fast",
            "frame_metrics": "detector",
            "key_window": "input",
            "text_observed": "ocr",
            "ocr_frame": "ocr",
        }
        if self.source != expected_sources[self.kind]:
            raise ValueError(f"source does not match evidence kind {self.kind!r}")
        if self.root_capture_id is None:
            raise ValueError("frame evidence requires root_capture_id")
        identifier_parts = self.evidence_id.split(":")
        if (
            len(identifier_parts) != 3
            or identifier_parts[0] != self.root_capture_id
            or identifier_parts[1] != self.source
            or not identifier_parts[2].isdigit()
            or int(identifier_parts[2]) < 1
        ):
            raise ValueError("evidence_id must match root_capture_id, source, and positive sequence")
        if self.learned_at < self.observed_at:
            raise ValueError("learned_at must not precede observed_at")
        if self.kind != "fast_observation" and self.outcome != "ok":
            raise ValueError("mechanical evidence must have outcome=ok")
        if isinstance(self.payload, FastObservationPayload):
            if self.outcome == "ok":
                if not self.payload.text or self.payload.drop_reason is not None:
                    raise ValueError("successful fast evidence needs text and no drop reason")
            elif self.payload.text or not self.payload.drop_reason:
                raise ValueError("non-success fast evidence needs empty text and a drop reason")
        return self
