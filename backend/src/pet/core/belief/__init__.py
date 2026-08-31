"""Typed, append-only evidence primitives shared by visual adapters."""

from pet.core.belief.models import (
    ChangeReason,
    EvidenceEvent,
    EvidenceOutcome,
    FastObservationPayload,
    FrameMetricsPayload,
    KeyWindowPayload,
    MouseMotionPayload,
    OcrFramePayload,
    SceneFingerprintPayload,
    SceneVerifiedPayload,
    Scope,
    TextObservedPayload,
)
from pet.core.belief.render import (
    ObservationsMarkdownWriter,
    render_observations_markdown,
)
from pet.core.belief.store import EvidenceStore

__all__ = [
    "ChangeReason",
    "EvidenceEvent",
    "EvidenceOutcome",
    "EvidenceStore",
    "FastObservationPayload",
    "FrameMetricsPayload",
    "KeyWindowPayload",
    "MouseMotionPayload",
    "OcrFramePayload",
    "SceneFingerprintPayload",
    "SceneVerifiedPayload",
    "ObservationsMarkdownWriter",
    "Scope",
    "TextObservedPayload",
    "render_observations_markdown",
]
