"""Deterministic full-frame perceptual hashes and session-local clustering."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal, Sequence

import numpy as np
import numpy.typing as npt
from PIL import Image

HashKind = Literal["ahash", "dhash", "phash"]
HashBits = Literal[64, 256]


def perceptual_hash(image: Image.Image, kind: HashKind, bits: HashBits) -> str:
    """Return a fixed-width hexadecimal perceptual hash for one in-memory frame."""
    side = _hash_side(bits)
    if image.width <= 0 or image.height <= 0:
        raise ValueError("scene fingerprint image must not be empty")
    if kind == "ahash":
        values = _resize_gray(image, side, side)
        hash_bits = values >= float(values.mean())
    elif kind == "dhash":
        values = _resize_gray(image, side + 1, side)
        hash_bits = values[:, 1:] >= values[:, :-1]
    elif kind == "phash":
        dct_side = side * 4
        values = _resize_gray(image, dct_side, dct_side).astype(np.float32)
        basis = _dct_basis(dct_side)
        low_frequency = (basis @ values @ basis.T)[:side, :side]
        median = float(np.median(low_frequency.reshape(-1)[1:]))
        hash_bits = low_frequency >= median
    else:
        raise ValueError(f"unsupported scene hash kind: {kind}")
    packed = np.packbits(hash_bits.reshape(-1), bitorder="big")
    return packed.tobytes().hex()


def hamming(left: str, right: str) -> int:
    """Return the bit distance between equal-width hexadecimal hashes."""
    if len(left) != len(right) or len(left) not in {16, 64}:
        raise ValueError("scene hashes must have the same 64-bit or 256-bit width")
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except ValueError as error:
        raise ValueError("scene hashes must be hexadecimal") from error


def _hash_side(bits: int) -> int:
    if bits == 64:
        return 8
    if bits == 256:
        return 16
    raise ValueError("scene hash bits must be 64 or 256")


def _resize_gray(image: Image.Image, width: int, height: int) -> npt.NDArray[np.uint8]:
    # BOX averaging and the RGB-to-luma transform are both linear.  Reducing a
    # large RGB frame first avoids converting two million pixels when the hash
    # consumes at most 64x64, without adding an encoded-image round trip.
    reduce_factor = max(
        1,
        min(
            image.width // max(width * 4, 1),
            image.height // max(height * 4, 1),
        ),
    )
    reduced = image.reduce(reduce_factor) if reduce_factor > 1 else image
    resized = reduced.convert("L").resize(
        (width, height),
        Image.Resampling.LANCZOS,
    )
    return np.asarray(resized, dtype=np.uint8)


@lru_cache(maxsize=2)
def _dct_basis(size: int) -> npt.NDArray[np.float32]:
    positions = np.arange(size, dtype=np.float32)
    frequencies = positions[:, np.newaxis]
    basis = np.cos(np.pi * (positions + 0.5) * frequencies / size).astype(
        np.float32
    )
    basis[0, :] *= np.sqrt(1.0 / size)
    basis[1:, :] *= np.sqrt(2.0 / size)
    basis.setflags(write=False)
    return basis


@dataclass(frozen=True, slots=True)
class CardSceneReference:
    scene_id: str
    representative_hash: str


@dataclass(frozen=True, slots=True)
class CardCandidate:
    scene_id: str
    distance: int


@dataclass(slots=True)
class VisitSpan:
    start: float
    end: float
    selected_frame_count: int = 1


@dataclass(slots=True)
class SceneCluster:
    cluster_id: int
    representative_hash: str
    first_seen: float
    last_seen: float
    seen_count: int = 1
    visit_spans: list[VisitSpan] = field(default_factory=list)
    distances: list[int] = field(default_factory=lambda: [0])
    evidence_ids: list[str] = field(default_factory=list)
    card_candidate: CardCandidate | None = None
    stable: bool = False
    max_consecutive_frames: int = 1

    @property
    def span_seconds(self) -> float:
        return self.last_seen - self.first_seen


@dataclass(frozen=True, slots=True)
class SceneFingerprintMatch:
    cluster_id: int
    distance: int
    is_new_cluster: bool
    switched_from: int | None
    stable: bool
    card_candidate: CardCandidate | None


class SceneClusterer:
    """Cluster selected frames within one session without changing representatives."""

    def __init__(
        self,
        hamming_threshold: int,
        stable_min_frames: int,
        card_scenes: Sequence[CardSceneReference] = (),
    ) -> None:
        if hamming_threshold < 0:
            raise ValueError("hamming threshold must be nonnegative")
        if stable_min_frames < 1:
            raise ValueError("stable_min_frames must be positive")
        self.hamming_threshold = hamming_threshold
        self.stable_min_frames = stable_min_frames
        self._card_scenes = tuple(card_scenes)
        self._clusters: list[SceneCluster] = []
        self._previous_cluster_id: int | None = None
        self._consecutive_frames = 0
        self._last_observed_at: float | None = None

    @property
    def clusters(self) -> tuple[SceneCluster, ...]:
        return tuple(self._clusters)

    def observe(self, fingerprint: str, observed_at: float) -> SceneFingerprintMatch:
        if observed_at < 0:
            raise ValueError("scene observed_at must be nonnegative")
        if self._last_observed_at is not None and observed_at < self._last_observed_at:
            raise ValueError("scene observations must arrive in content-time order")
        self._last_observed_at = observed_at
        cluster, distance = self._nearest_cluster(fingerprint)
        is_new = cluster is None or distance > self.hamming_threshold
        if is_new:
            cluster = self._new_cluster(fingerprint, observed_at)
            distance = 0
        else:
            self._append_member(cluster, observed_at, distance)

        previous = self._previous_cluster_id
        switched_from = previous if previous is not None and previous != cluster.cluster_id else None
        if previous == cluster.cluster_id:
            self._consecutive_frames += 1
        else:
            self._consecutive_frames = 1
        self._previous_cluster_id = cluster.cluster_id
        cluster.max_consecutive_frames = max(
            cluster.max_consecutive_frames,
            self._consecutive_frames,
        )
        if self._consecutive_frames >= self.stable_min_frames:
            cluster.stable = True
        return SceneFingerprintMatch(
            cluster_id=cluster.cluster_id,
            distance=distance,
            is_new_cluster=is_new,
            switched_from=switched_from,
            stable=self._consecutive_frames >= self.stable_min_frames,
            card_candidate=cluster.card_candidate,
        )

    def record_evidence(self, cluster_id: int, evidence_id: str) -> None:
        cluster = self._cluster(cluster_id)
        if evidence_id not in cluster.evidence_ids:
            cluster.evidence_ids.append(evidence_id)

    def _nearest_cluster(self, fingerprint: str) -> tuple[SceneCluster | None, int]:
        if not self._clusters:
            return None, 0
        distances = [
            hamming(fingerprint, cluster.representative_hash)
            for cluster in self._clusters
        ]
        best_index = min(range(len(distances)), key=distances.__getitem__)
        return self._clusters[best_index], distances[best_index]

    def _new_cluster(self, fingerprint: str, observed_at: float) -> SceneCluster:
        candidate = self._nearest_card_scene(fingerprint)
        cluster = SceneCluster(
            cluster_id=len(self._clusters) + 1,
            representative_hash=fingerprint,
            first_seen=observed_at,
            last_seen=observed_at,
            visit_spans=[VisitSpan(start=observed_at, end=observed_at)],
            card_candidate=candidate,
        )
        self._clusters.append(cluster)
        return cluster

    def _append_member(
        self,
        cluster: SceneCluster,
        observed_at: float,
        distance: int,
    ) -> None:
        cluster.seen_count += 1
        cluster.last_seen = observed_at
        cluster.distances.append(distance)
        if self._previous_cluster_id == cluster.cluster_id:
            span = cluster.visit_spans[-1]
            span.end = observed_at
            span.selected_frame_count += 1
        else:
            cluster.visit_spans.append(VisitSpan(start=observed_at, end=observed_at))

    def _nearest_card_scene(self, fingerprint: str) -> CardCandidate | None:
        comparable = tuple(
            scene
            for scene in self._card_scenes
            if len(scene.representative_hash) == len(fingerprint)
        )
        if not comparable:
            return None
        distances = [
            hamming(fingerprint, scene.representative_hash)
            for scene in comparable
        ]
        best_index = min(range(len(distances)), key=distances.__getitem__)
        distance = distances[best_index]
        if distance > self.hamming_threshold:
            return None
        return CardCandidate(
            scene_id=comparable[best_index].scene_id,
            distance=distance,
        )

    def _cluster(self, cluster_id: int) -> SceneCluster:
        if cluster_id < 1 or cluster_id > len(self._clusters):
            raise ValueError(f"unknown scene cluster id: {cluster_id}")
        return self._clusters[cluster_id - 1]
