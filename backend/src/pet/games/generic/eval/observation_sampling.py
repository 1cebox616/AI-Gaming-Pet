"""Build a deterministic, mechanically stratified frame annotation sample."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
import random
import re
import shutil
import sys
from typing import Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from pet.core.belief import Scope
from pet.core.config import AdapterConfig, load_config
from pet.core.llm import _prepare_image_upload
from pet.games.generic.adapter import WindowTitleMap, _change_reason, _focus_scope
from pet.games.generic.eval.observation_replay import (
    PRODUCTION_SEND_WIDTH,
    PreparedReplay,
    ReplayFrameSpec,
    _prepare_replay,
)
from pet.games.generic.eval.scene_fingerprint_eval import (
    STABLE_MIN_SECONDS,
    SceneEvaluationError,
    _cluster,
    _hash_recordings,
    load_recording,
)

BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]
DEFAULT_OUTPUT = BACKEND_DIRECTORY / "eval-reports" / "w-t0"
OCR_DENSE_THRESHOLD = 12  # This value awaits measurement in W-T0c.
MANIFEST_SCHEMA_VERSION = "1.0"
_RAW_SEQUENCE = re.compile(r"raw-(?P<sequence>\d+)-")

UploadBranch = Literal["focus", "coarse", "heartbeat"]


class SamplingError(RuntimeError):
    """Raised when a requested sample cannot be built honestly."""


class ManifestImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManifestFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frame_id: str
    root_capture_id: str
    session: str
    observed_at: float = Field(ge=0.0)
    frame_seq: int = Field(gt=0)
    upload_branch: UploadBranch
    scope: Scope | None
    strata: list[str]
    scene_cluster_id: str | None
    ocr_box_count: int | None = Field(default=None, ge=0)
    upload: ManifestImage
    full: ManifestImage
    manually_included: bool = False


class SampleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = MANIFEST_SCHEMA_VERSION
    seed: int
    requested_target: int = Field(ge=1)
    actual_count: int = Field(ge=1)
    recording_count: int = Field(ge=1)
    single_recording: bool
    production_send_width: int = PRODUCTION_SEND_WIDTH
    ocr_dense_threshold: int | None
    ocr_dense_threshold_status: str | None
    scene_stratum_status: str
    ocr_stratum_status: str
    shortages: list[str]
    frames: list[ManifestFrame]


@dataclass(frozen=True, slots=True)
class Candidate:
    session_path: Path
    session: str
    spec: ReplayFrameSpec
    frame_seq: int
    branch: UploadBranch
    scope: Scope | None
    strata: tuple[str, ...]
    scene_cluster_id: str | None = None
    ocr_box_count: int | None = None
    manually_included: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return self.session, self.frame_seq


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _sequence(path: Path) -> int:
    match = _RAW_SEQUENCE.match(path.name)
    if match is None:
        raise SamplingError(f"raw frame filename has no sequence: {path.name}")
    return int(match.group("sequence"))


def _branch_and_scope(spec: ReplayFrameSpec, region_focus_max: float) -> tuple[UploadBranch, Scope | None]:
    reason = _change_reason(spec.forced, spec.change_ratio)
    if reason == "forced":
        return "heartbeat", None
    if spec.confirmed_region and spec.change_ratio <= region_focus_max:
        return "focus", _focus_scope(spec.confirmed_region)
    return "coarse", None


def _load_ocr_counts(session: Path) -> dict[str, int] | None:
    evidence = (
        BACKEND_DIRECTORY
        / "eval-reports"
        / "m5-b-t4b-minimal"
        / session.name
        / "evidence.jsonl"
    )
    if not evidence.is_file():
        return None
    counts: dict[str, int] = {}
    try:
        with evidence.open(encoding="utf-8") as source:
            for line in source:
                payload = json.loads(line)
                if payload.get("kind") != "ocr_frame" or payload.get("outcome") != "ok":
                    continue
                root = payload.get("root_capture_id")
                body = payload.get("payload")
                if isinstance(root, str) and isinstance(body, dict):
                    count = body.get("recognized_line_count")
                    if isinstance(count, int) and count >= 0:
                        counts[root] = count
    except (OSError, json.JSONDecodeError) as error:
        raise SamplingError(f"cannot read OCR evidence {evidence}: {error}") from error
    return counts


def _scene_metadata(
    session: Path,
    uploaded_sequences: set[int],
) -> tuple[dict[int, tuple[str, bool]], set[int]]:
    recording = load_recording(session)
    hashes = _hash_recordings((recording,))[0][0]
    clusterer, members = _cluster(recording.polling_frames, hashes, STABLE_MIN_SECONDS)
    retained = {cluster.cluster_id: cluster.stable for cluster in clusterer.clusters}
    by_sequence: dict[int, tuple[str, bool]] = {}
    ordered: list[tuple[int, int]] = []
    for cluster_id, frames in members.items():
        for frame in frames:
            by_sequence[frame.raw_sequence] = (
                f"{session.name}:c{cluster_id}",
                retained.get(cluster_id, False),
            )
            ordered.append((frame.raw_sequence, cluster_id))
    changed_since_upload = False
    prior_cluster: int | None = None
    first_after_epoch: set[int] = set()
    for sequence, cluster_id in sorted(ordered):
        if prior_cluster is not None and cluster_id != prior_cluster:
            changed_since_upload = True
        if sequence in uploaded_sequences:
            if changed_since_upload:
                first_after_epoch.add(sequence)
            changed_since_upload = False
        prior_cluster = cluster_id
    return by_sequence, first_after_epoch


def build_candidates(
    prepared: Sequence[PreparedReplay],
    *,
    region_focus_max: float,
    skip_ocr_stratum: bool,
) -> tuple[list[Candidate], str, str]:
    candidates: list[Candidate] = []
    scene_statuses: list[str] = []
    ocr_statuses: list[str] = []
    for replay in prepared:
        uploaded_sequences = {_sequence(spec.path) for spec in replay.selected}
        try:
            scene_by_sequence, first_after_epoch = _scene_metadata(
                replay.session,
                uploaded_sequences,
            )
            scene_statuses.append("available")
        except (SceneEvaluationError, OSError, ValueError) as error:
            scene_by_sequence = {}
            first_after_epoch = set()
            scene_statuses.append(f"unstratified: {error}")
        ocr_counts = None if skip_ocr_stratum else _load_ocr_counts(replay.session)
        ocr_statuses.append(
            "skipped" if skip_ocr_stratum else ("available" if ocr_counts is not None else "unstratified: no offline OCR evidence")
        )
        for spec in replay.selected:
            sequence = _sequence(spec.path)
            root = f"f{sequence}"
            branch, scope = _branch_and_scope(spec, region_focus_max)
            strata = [f"upload:{branch}"]
            scene = scene_by_sequence.get(sequence)
            if scene is not None:
                strata.append("scene:stable_interface" if scene[1] else "scene:default_gameplay")
            else:
                strata.append("scene:unstratified")
            if sequence in first_after_epoch:
                strata.append("scene:first_upload_after_epoch_change")
            count = ocr_counts.get(root) if ocr_counts is not None else None
            if count is None:
                strata.append("ocr:unstratified")
            elif count >= OCR_DENSE_THRESHOLD:
                strata.append("ocr:dense")
            else:
                strata.append("ocr:sparse")
            candidates.append(
                Candidate(
                    session_path=replay.session,
                    session=replay.session.name,
                    spec=spec,
                    frame_seq=sequence,
                    branch=branch,
                    scope=scope,
                    strata=tuple(strata),
                    scene_cluster_id=scene[0] if scene else None,
                    ocr_box_count=count,
                )
            )
    scene_status = "available" if all(value == "available" for value in scene_statuses) else "; ".join(scene_statuses)
    ocr_status = "available" if all(value == "available" for value in ocr_statuses) else "; ".join(ocr_statuses)
    return candidates, scene_status, ocr_status


def select_candidates(
    candidates: Sequence[Candidate],
    *,
    seed: int,
    target: int,
    include: Sequence[str] = (),
) -> tuple[list[Candidate], list[str]]:
    if not candidates:
        raise SamplingError("detector selected no upload candidates")
    if target < 1:
        raise SamplingError("target must be positive")
    include_set = set(include)
    known_roots = {f"f{item.frame_seq}" for item in candidates}
    missing = sorted(include_set - known_roots)
    if missing:
        raise SamplingError(f"manual include IDs are not upload candidates: {', '.join(missing)}")
    marked = [replace(item, manually_included=True) for item in candidates if f"f{item.frame_seq}" in include_set]
    if len(marked) > target:
        raise SamplingError("manual include count exceeds target")

    rng = random.Random(seed)
    ordered = list(candidates)
    rng.shuffle(ordered)
    selected: dict[tuple[str, int], Candidate] = {item.key: item for item in marked}
    shortages: list[str] = []

    def add_matching(
        name: str,
        desired: int,
        predicate: Callable[[Candidate], bool],
    ) -> None:
        matches = [item for item in ordered if predicate(item)]
        achievable = min(desired, len(matches), target)
        for item in matches:
            if len(selected) >= target or sum(predicate(value) for value in selected.values()) >= achievable:
                break
            selected.setdefault(item.key, item)
        actual = sum(predicate(value) for value in selected.values())
        if actual < desired:
            shortages.append(f"{name}: requested {desired}, available/selected {actual}")

    branch_minimum = math.ceil(target * 0.15)
    for branch in ("focus", "coarse", "heartbeat"):
        add_matching(f"upload:{branch}", branch_minimum, lambda item, value=branch: item.branch == value)
    add_matching(
        "scene:first_upload_after_epoch_change",
        min(8, target),
        lambda item: "scene:first_upload_after_epoch_change" in item.strata,
    )
    for stratum in ("scene:stable_interface", "scene:default_gameplay", "ocr:dense", "ocr:sparse"):
        if any(stratum in item.strata for item in candidates):
            add_matching(stratum, 1, lambda item, value=stratum: value in item.strata)
    for item in ordered:
        if len(selected) >= min(target, len(candidates)):
            break
        selected.setdefault(item.key, item)
    if len(candidates) < target:
        shortages.append(f"total: requested {target}, candidates {len(candidates)}")
    return sorted(selected.values(), key=lambda item: (item.session, item.frame_seq)), shortages


def _write_json(path: Path, payload: BaseModel) -> None:
    path.write_text(
        json.dumps(payload.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_sample(
    selected: Sequence[Candidate],
    *,
    output: Path,
    seed: int,
    requested_target: int,
    scene_status: str,
    ocr_status: str,
    shortages: Sequence[str],
) -> SampleManifest:
    output = output.resolve()
    upload_dir = output / "frames" / "upload"
    full_dir = output / "frames" / "full"
    upload_dir.mkdir(parents=True, exist_ok=True)
    full_dir.mkdir(parents=True, exist_ok=True)
    frames: list[ManifestFrame] = []
    session_names = sorted({item.session for item in selected})
    session_indexes = {name: index + 1 for index, name in enumerate(session_names)}
    for item in selected:
        frame_id = f"s{session_indexes[item.session]:03d}-f{item.frame_seq:06d}"
        filename = f"{frame_id}.jpg"
        full_path = full_dir / filename
        upload_path = upload_dir / filename
        shutil.copyfile(item.spec.path, full_path)
        with Image.open(full_path) as full_image:
            full_size = full_image.size
        upload_bytes, upload_size = _prepare_image_upload(
            item.spec.path,
            max_image_edge=None,
            target_width=min(PRODUCTION_SEND_WIDTH, full_size[0]),
            encoding="jpeg",
            jpeg_quality=85,
        )
        upload_path.write_bytes(upload_bytes)
        frames.append(
            ManifestFrame(
                frame_id=frame_id,
                root_capture_id=f"f{item.frame_seq}",
                session=item.session,
                observed_at=item.spec.relative_seconds,
                frame_seq=item.frame_seq,
                upload_branch=item.branch,
                scope=item.scope,
                strata=list(item.strata),
                scene_cluster_id=item.scene_cluster_id,
                ocr_box_count=item.ocr_box_count,
                upload=ManifestImage(path=f"frames/upload/{filename}", width=upload_size[0], height=upload_size[1], sha256=_sha256(upload_path)),
                full=ManifestImage(path=f"frames/full/{filename}", width=full_size[0], height=full_size[1], sha256=_sha256(full_path)),
                manually_included=item.manually_included,
            )
        )
    manifest = SampleManifest(
        seed=seed,
        requested_target=requested_target,
        actual_count=len(frames),
        recording_count=len(session_names),
        single_recording=len(session_names) == 1,
        ocr_dense_threshold=None if ocr_status.startswith("skipped") else OCR_DENSE_THRESHOLD,
        ocr_dense_threshold_status=None if ocr_status.startswith("skipped") else "awaits measurement in W-T0c",
        scene_stratum_status=scene_status,
        ocr_stratum_status=ocr_status,
        shortages=list(shortages),
        frames=frames,
    )
    _write_json(output / "sample_manifest.json", manifest)
    return manifest


def summarize(manifest: SampleManifest) -> str:
    branch = Counter(item.upload_branch for item in manifest.frames)
    strata = Counter(value for item in manifest.frames for value in item.strata)
    lines = [
        f"sample={manifest.actual_count}/{manifest.requested_target}; recordings={manifest.recording_count}",
        "branches=" + ", ".join(f"{name}:{branch[name]}" for name in ("focus", "coarse", "heartbeat")),
        "strata=" + ", ".join(f"{name}:{count}" for name, count in sorted(strata.items())),
        f"scene={manifest.scene_stratum_status}",
        f"ocr={manifest.ocr_stratum_status}",
    ]
    lines.extend(f"shortage={item}" for item in manifest.shortages)
    if manifest.single_recording:
        lines.append("recording_note=single recording; a second production-format top-down recording was unavailable")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a deterministic frame-annotation sample.")
    parser.add_argument("session", nargs="+", type=Path, help="Production-format capture session directory.")
    parser.add_argument("--seed", type=int, default=20260902)
    parser.add_argument("--target", type=int, default=60)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--include", default="", help="Comma-separated root capture IDs, for example f12,f30.")
    parser.add_argument("--skip-ocr-stratum", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.target < 40:
        print("--target must be at least 40", file=sys.stderr)
        return 2
    try:
        configuration = load_config(strict=True)
        generic = configuration.games.get("generic", AdapterConfig()).generic
        title_map = WindowTitleMap.load()
        prepared = tuple(
            _prepare_replay(path, PRODUCTION_SEND_WIDTH, None, title_map, generic.region_focus_max)
            for path in arguments.session
        )
        candidates, scene_status, ocr_status = build_candidates(
            prepared,
            region_focus_max=generic.region_focus_max,
            skip_ocr_stratum=arguments.skip_ocr_stratum,
        )
        include = tuple(value.strip() for value in arguments.include.split(",") if value.strip())
        selected, shortages = select_candidates(candidates, seed=arguments.seed, target=arguments.target, include=include)
        manifest = write_sample(
            selected,
            output=arguments.output,
            seed=arguments.seed,
            requested_target=arguments.target,
            scene_status=scene_status,
            ocr_status=ocr_status,
            shortages=shortages,
        )
    except (SamplingError, OSError, ValueError) as error:
        print(f"sampling failed: {error}", file=sys.stderr)
        return 1
    print(summarize(manifest))
    print(f"manifest={arguments.output.resolve() / 'sample_manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
