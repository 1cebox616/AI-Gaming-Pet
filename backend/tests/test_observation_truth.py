"""W-T0b truth schema rejects geometry, references, and manifest drift."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from pydantic import ValidationError
import pytest

from pet.games.generic.eval.observation_sampling import ManifestFrame, ManifestImage, SampleManifest
from pet.games.generic.eval.observation_truth import (
    TruthAction,
    TruthAttribute,
    TruthBox,
    TruthEntity,
    TruthError,
    TruthFile,
    TruthFrame,
    TruthRelation,
    load_truth,
    manifest_sha256,
    write_truth_atomic,
)


def _manifest(root: Path) -> Path:
    image = ManifestImage(path="frames/full/a.jpg", width=100, height=50, sha256="a" * 64)
    manifest = SampleManifest(
        seed=1, requested_target=1, actual_count=1, recording_count=1, single_recording=True,
        ocr_dense_threshold=None, ocr_dense_threshold_status=None, scene_stratum_status="available", ocr_stratum_status="skipped", shortages=[],
        frames=[ManifestFrame(frame_id="s001-f000001", root_capture_id="f1", session="fixture", observed_at=0, frame_seq=1, upload_branch="heartbeat", scope=None, strata=["upload:heartbeat"], scene_cluster_id=None, ocr_box_count=None, upload=image.model_copy(update={"path":"frames/upload/a.jpg"}), full=image)],
    )
    path = root / "sample_manifest.json"
    path.write_text(json.dumps(manifest.model_dump(mode="json"), sort_keys=True), encoding="utf-8")
    return path


def _truth(manifest: Path) -> TruthFile:
    return TruthFile(
        manifest_sha256=manifest_sha256(manifest), created_at=datetime.now(timezone.utc),
        frames=[TruthFrame(
            frame_id="s001-f000001", root_capture_id="f1", session="fixture", enumeration_complete=True,
            entities=[TruthEntity(id="o1", label="farm animal", bbox=TruthBox(x0=.1,y0=.2,x1=.4,y1=.8))],
            attributes=[TruthAttribute(entity="o1", facet="pose", value="standing")],
            relations=[TruthRelation(subject="player", relation="near", object="o1")],
            actions=[TruthAction(subject="o1", action="looking_at", target="player")],
        )],
    )


def test_truth_round_trip_and_atomic_replace(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    output = tmp_path / "truth.json"
    write_truth_atomic(output, _truth(manifest))
    assert load_truth(output).frames[0].entities[0].label == "farm animal"
    assert not (tmp_path / ".truth.json.tmp").exists()


def test_truth_rejects_bad_box_snake_reference_and_manifest(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    with pytest.raises(ValidationError, match="x0 < x1"):
        TruthBox(x0=.7, y0=.1, x1=.2, y1=.3)
    with pytest.raises(ValidationError, match="snake_case"):
        TruthFrame(frame_id="x", root_capture_id="f1", session="fixture", entities=[TruthEntity(id="o1",label="item",bbox=TruthBox(x0=0,y0=0,x1=1,y1=1))], attributes=[TruthAttribute(entity="o1",facet="Bad-Facet",value="x")])
    with pytest.raises(ValidationError, match="current entity"):
        TruthFrame(frame_id="x", root_capture_id="f1", session="fixture", relations=[TruthRelation(subject="o9",relation="near",object="player")])
    output = tmp_path / "truth.json"
    bad = _truth(manifest).model_copy(update={"manifest_sha256":"b" * 64})
    write_truth_atomic(output, bad)
    with pytest.raises(TruthError, match="does not match"):
        load_truth(output)
