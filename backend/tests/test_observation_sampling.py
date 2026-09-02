"""W-T0b mechanical sampling is deterministic and honors hard strata."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from pet.games.generic.eval.observation_replay import ReplayFrameSpec
from pet.games.generic.eval.observation_sampling import Candidate, select_candidates, write_sample


def _candidates(root: Path) -> list[Candidate]:
    candidates: list[Candidate] = []
    for sequence in range(1, 46):
        path = root / f"raw-{sequence:06d}-20260902T120000.000000Z.jpg"
        Image.new("RGB", (64, 36), (sequence * 5 % 255, sequence * 7 % 255, sequence * 11 % 255)).save(path, quality=70)
        branch = "focus" if sequence <= 15 else ("coarse" if sequence <= 30 else "heartbeat")
        strata = [f"upload:{branch}", "scene:stable_interface" if sequence % 2 else "scene:default_gameplay", "ocr:dense" if sequence % 3 else "ocr:sparse"]
        if sequence <= 10:
            strata.append("scene:first_upload_after_epoch_change")
        candidates.append(
            Candidate(
                session_path=root,
                session="fixture-session",
                spec=ReplayFrameSpec(path, None, float(sequence), (), (), 0.0, 0.0, 0.0, branch == "heartbeat", 0.0),  # type: ignore[arg-type]
                frame_seq=sequence,
                branch=branch,  # type: ignore[arg-type]
                scope=None,
                strata=tuple(strata),
                scene_cluster_id=f"fixture:c{sequence % 2 + 1}",
                ocr_box_count=20 if sequence % 3 else 2,
            )
        )
    return candidates


def test_sample_manifest_is_byte_deterministic_and_meets_strata(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    first, first_shortages = select_candidates(candidates, seed=77, target=40, include=("f45",))
    second, second_shortages = select_candidates(candidates, seed=77, target=40, include=("f45",))
    assert [(item.session, item.frame_seq) for item in first] == [(item.session, item.frame_seq) for item in second]
    assert first_shortages == second_shortages == []
    assert any(item.frame_seq == 45 and item.manually_included for item in first)
    for branch in ("focus", "coarse", "heartbeat"):
        assert sum(item.branch == branch for item in first) >= 6
    assert sum("scene:first_upload_after_epoch_change" in item.strata for item in first) >= 8

    out_a, out_b = tmp_path / "out-a", tmp_path / "out-b"
    manifest_a = write_sample(first, output=out_a, seed=77, requested_target=40, scene_status="available", ocr_status="available", shortages=())
    manifest_b = write_sample(second, output=out_b, seed=77, requested_target=40, scene_status="available", ocr_status="available", shortages=())
    assert manifest_a == manifest_b
    assert (out_a / "sample_manifest.json").read_bytes() == (out_b / "sample_manifest.json").read_bytes()
    for frame in manifest_a.frames:
        assert (out_a / frame.upload.path).read_bytes() == (out_b / frame.upload.path).read_bytes()
        assert (out_a / frame.full.path).read_bytes() == (out_b / frame.full.path).read_bytes()
