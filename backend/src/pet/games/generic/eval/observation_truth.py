"""Validated human truth schema for the W-T0 observation sample."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pet.core.belief.protocol import _SNAKE_PATTERN
from pet.games.generic.eval.observation_sampling import SampleManifest

TRUTH_SCHEMA_VERSION = "1.0"
_ENGLISH_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]*$")


class TruthError(RuntimeError):
    """Raised when truth does not match its immutable sample manifest."""


class TruthModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TruthBox(TruthModel):
    x0: float = Field(ge=0.0, le=1.0)
    y0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def ordered(self) -> TruthBox:
        if self.x0 >= self.x1 or self.y0 >= self.y1:
            raise ValueError("bbox must satisfy x0 < x1 and y0 < y1")
        return self


class TruthEntity(TruthModel):
    id: str = Field(pattern=r"^o[1-9]\d*$")
    label: str = Field(min_length=1, max_length=80)
    bbox: TruthBox
    unclear: bool = False
    only_visible_at_full_res: bool = False

    @model_validator(mode="after")
    def english_label(self) -> TruthEntity:
        if not _ENGLISH_LABEL.fullmatch(self.label):
            raise ValueError("entity label must be a short English phrase")
        return self


class TruthAttribute(TruthModel):
    entity: str
    facet: str
    value: str = Field(min_length=1)


class TruthRelation(TruthModel):
    subject: str
    relation: str
    object: str


class TruthAction(TruthModel):
    subject: str
    action: str
    target: str | None = None


class TruthFrame(TruthModel):
    frame_id: str
    root_capture_id: str
    session: str
    enumeration_complete: bool = False
    notes: str = ""
    entities: list[TruthEntity] = Field(default_factory=list)
    attributes: list[TruthAttribute] = Field(default_factory=list)
    relations: list[TruthRelation] = Field(default_factory=list)
    actions: list[TruthAction] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_local_references(self) -> TruthFrame:
        expected_ids = [f"o{index}" for index in range(1, len(self.entities) + 1)]
        actual_ids = [entity.id for entity in self.entities]
        if actual_ids != expected_ids:
            raise ValueError("entity IDs must be unique and ordered o1, o2, ...")
        references = set(actual_ids) | {"player"}
        for attribute in self.attributes:
            _check_ref(attribute.entity, references, "attribute entity")
            _check_snake(attribute.facet, "attribute facet")
        for relation in self.relations:
            _check_ref(relation.subject, references, "relation subject")
            _check_ref(relation.object, references, "relation object")
            _check_snake(relation.relation, "relation")
        for action in self.actions:
            _check_ref(action.subject, references, "action subject")
            if action.target is not None:
                _check_ref(action.target, references, "action target")
            _check_snake(action.action, "action")
        return self


class TruthFile(TruthModel):
    schema_version: Literal["1.0"] = TRUTH_SCHEMA_VERSION
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    frames: list[TruthFrame]

    @model_validator(mode="after")
    def unique_frames(self) -> TruthFile:
        identifiers = [frame.frame_id for frame in self.frames]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("truth frame_id values must be unique")
        return self


def _check_ref(value: str, references: set[str], field_name: str) -> None:
    if value not in references:
        raise ValueError(f"{field_name} must reference a current entity or player")


def _check_snake(value: str, field_name: str) -> None:
    if not _SNAKE_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a snake_case token")


def manifest_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(path: Path) -> SampleManifest:
    try:
        return SampleManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TruthError(f"cannot load sample manifest {path}: {error}") from error


def validate_truth_against_manifest(
    truth: TruthFile,
    manifest: SampleManifest,
    manifest_path: Path,
) -> TruthFile:
    digest = manifest_sha256(manifest_path)
    if truth.manifest_sha256 != digest:
        raise TruthError("truth manifest_sha256 does not match sample_manifest.json")
    by_id = {frame.frame_id: frame for frame in manifest.frames}
    for frame in truth.frames:
        sample = by_id.get(frame.frame_id)
        if sample is None:
            raise TruthError(f"truth frame_id is absent from manifest: {frame.frame_id}")
        if frame.root_capture_id != sample.root_capture_id or frame.session != sample.session:
            raise TruthError(f"truth frame provenance differs from manifest: {frame.frame_id}")
    return truth


def load_truth(path: Path) -> TruthFile:
    """Load truth and validate it against a sibling sample_manifest.json."""
    manifest_path = path.resolve().parent / "sample_manifest.json"
    manifest = load_manifest(manifest_path)
    try:
        truth = TruthFile.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TruthError(f"cannot load truth {path}: {error}") from error
    return validate_truth_against_manifest(truth, manifest, manifest_path)


def write_truth_atomic(path: Path, truth: TruthFile) -> None:
    """Write a same-directory temporary file, flush it, then atomically replace."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    data = json.dumps(
        truth.model_dump(mode="json"),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        if temporary.exists():
            temporary.unlink()
        raise
