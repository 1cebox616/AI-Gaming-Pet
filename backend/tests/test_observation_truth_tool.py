"""The local truth tool serves only manifest frames and keeps boxes normalized."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from pet.games.generic.eval.observation_sampling import ManifestFrame, ManifestImage, SampleManifest
from pet.games.generic.eval.observation_truth_tool import create_app, display_point_to_normalized


def _fixture(root: Path) -> Path:
    for variant, size in (("full", (1920,1080)), ("upload", (896,504))):
        directory=root/"frames"/variant; directory.mkdir(parents=True)
        Image.new("RGB",size,"navy").save(directory/"frame.jpg")
    full=ManifestImage(path="frames/full/frame.jpg",width=1920,height=1080,sha256="a"*64)
    upload=ManifestImage(path="frames/upload/frame.jpg",width=896,height=504,sha256="b"*64)
    manifest=SampleManifest(seed=1,requested_target=1,actual_count=1,recording_count=1,single_recording=True,ocr_dense_threshold=None,ocr_dense_threshold_status=None,scene_stratum_status="available",ocr_stratum_status="skipped",shortages=[],frames=[ManifestFrame(frame_id="s001-f000001",root_capture_id="f1",session="fixture",observed_at=0,frame_seq=1,upload_branch="heartbeat",scope=None,strata=["upload:heartbeat"],scene_cluster_id=None,ocr_box_count=None,upload=upload,full=full)])
    path=root/"sample_manifest.json"; path.write_text(json.dumps(manifest.model_dump(mode="json")),encoding="utf-8"); return path


def test_coordinate_mapping_is_resolution_independent() -> None:
    assert display_point_to_normalized(960,540,left=0,top=0,width=1920,height=1080) == (.5,.5)
    assert display_point_to_normalized(448,252,left=0,top=0,width=896,height=504) == (.5,.5)


def test_truth_tool_serves_manifest_images_and_restores_saved_truth(tmp_path: Path) -> None:
    manifest=_fixture(tmp_path); output=tmp_path/"truth.json"; client=TestClient(create_app(manifest,output))
    assert client.get("/").status_code == 200
    assert client.get("/api/image/s001-f000001/full").status_code == 200
    assert client.get("/api/image/s001-f000001/upload").status_code == 200
    assert client.get("/api/image/missing/full").status_code == 404
    truth=client.get("/api/truth").json()
    truth["frames"]=[{"frame_id":"s001-f000001","root_capture_id":"f1","session":"fixture","enumeration_complete":True,"notes":"saved","entities":[],"attributes":[],"relations":[],"actions":[]}]
    assert client.post("/api/truth",json=truth).json() == {"saved":True,"frames":1}
    assert client.get("/api/truth").json()["frames"][0]["notes"] == "saved"
