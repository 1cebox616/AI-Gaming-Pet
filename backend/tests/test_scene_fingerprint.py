"""M5-B-T2 full-frame hash and session-clustering regression tests."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageEnhance

from pet.core.scene_fingerprint import (
    CardSceneReference,
    SceneClusterer,
    hamming,
    perceptual_hash,
)


def _structured_image() -> Image.Image:
    y, x = np.indices((288, 512))
    array = np.stack(
        (
            ((x // 32 + y // 32) % 2) * 180 + 30,
            ((x // 64) % 2) * 120 + 50,
            ((y // 48) % 2) * 150 + 40,
        ),
        axis=2,
    ).astype(np.uint8)
    return Image.fromarray(array, mode="RGB")


def test_all_hash_variants_are_deterministic_brightness_tolerant_and_fixed_width() -> None:
    random = np.random.default_rng(20260830)
    image = Image.fromarray(
        random.integers(0, 256, size=(288, 512, 3), dtype=np.uint8),
        mode="RGB",
    )
    brighter = ImageEnhance.Brightness(image).enhance(1.01)
    for kind in ("ahash", "dhash", "phash"):
        for bits, hex_length in ((64, 16), (256, 64)):
            first = perceptual_hash(image, kind, bits)
            assert perceptual_hash(image, kind, bits) == first
            assert len(first) == hex_length
            assert hamming(first, perceptual_hash(brighter, kind, bits)) < bits * 0.20


def test_unrelated_and_rotated_images_do_not_collapse_to_the_same_hash() -> None:
    image = _structured_image()
    noise = Image.effect_noise(image.size, 80.0).convert("RGB")
    black = Image.new("RGB", image.size, "black")
    rotated = image.rotate(180)
    for kind in ("ahash", "dhash", "phash"):
        threshold_scale = 64
        assert hamming(
            perceptual_hash(black, kind, 64),
            perceptual_hash(noise, kind, 64),
        ) > threshold_scale * 0.20
        assert hamming(
            perceptual_hash(image, kind, 64),
            perceptual_hash(rotated, kind, 64),
        ) > 0


def test_phash_is_less_sensitive_than_dhash_to_one_corner_animation_fixture() -> None:
    values = np.asarray(
        [
            [191, 91, 161, 165, 224, 241, 185, 141, 92],
            [180, 64, 227, 225, 253, 25, 166, 41, 152],
            [15, 137, 184, 161, 122, 60, 47, 191, 2],
            [183, 148, 179, 138, 195, 69, 72, 222, 163],
            [150, 87, 207, 39, 171, 182, 3, 146, 118],
            [85, 23, 34, 103, 110, 60, 223, 67, 177],
            [2, 69, 229, 30, 80, 110, 224, 48, 87],
            [207, 73, 0, 65, 39, 164, 154, 187, 121],
        ],
        dtype=np.uint8,
    )
    changed = values.copy()
    changed[:2, :2] = 128
    original = Image.fromarray(values).resize((900, 800), Image.Resampling.NEAREST)
    animated = Image.fromarray(changed).resize((900, 800), Image.Resampling.NEAREST)
    dhash_distance = hamming(
        perceptual_hash(original, "dhash", 64),
        perceptual_hash(animated, "dhash", 64),
    )
    phash_distance = hamming(
        perceptual_hash(original, "phash", 64),
        perceptual_hash(animated, "phash", 64),
    )
    assert phash_distance < dhash_distance


def test_clusterer_uses_fixed_representatives_stability_and_switch_semantics() -> None:
    clusterer = SceneClusterer(hamming_threshold=1, stable_min_seconds=2.0)
    first = clusterer.observe("0000000000000000", 0.0)
    second = clusterer.observe("0000000000000001", 1.0)
    third = clusterer.observe("0000000000000000", 2.0)
    switched = clusterer.observe("ffffffffffffffff", 3.0)
    returned = clusterer.observe("0000000000000000", 4.0)

    assert first.is_new_cluster is True and first.stable is False
    assert second.cluster_id == 1 and second.distance == 1 and second.stable is False
    assert third.cluster_id == 1 and third.stable is True
    assert switched.cluster_id == 2 and switched.switched_from == 1
    assert returned.cluster_id == 1 and returned.switched_from == 2
    assert returned.stable is False
    assert clusterer.clusters[0].representative_hash == "0000000000000000"
    assert clusterer.clusters[0].visit_spans[0].frame_count == 3
    assert len(clusterer.clusters[0].visit_spans) == 2
    assert clusterer.clusters[0].dwell_seconds == 2.0
    assert clusterer.clusters[0].visit_count == 2
    assert clusterer.clusters[0].longest_run_seconds == 2.0


def test_stability_uses_content_seconds_not_polling_frame_count() -> None:
    one_hz = SceneClusterer(hamming_threshold=0, stable_min_seconds=5.0)
    two_tenths_hz = SceneClusterer(hamming_threshold=0, stable_min_seconds=5.0)

    one_hz_matches = [
        one_hz.observe("0000000000000000", float(second))
        for second in range(6)
    ]
    sparse_matches = [
        two_tenths_hz.observe("0000000000000000", second)
        for second in (0.0, 2.5, 5.0)
    ]

    assert one_hz_matches[-2].stable is False
    assert one_hz_matches[-1].stable is True
    assert sparse_matches[-2].stable is False
    assert sparse_matches[-1].stable is True
    assert one_hz.clusters[0].longest_run_seconds == 5.0
    assert two_tenths_hz.clusters[0].longest_run_seconds == 5.0


def test_unstable_candidates_never_enter_the_retained_scene_index() -> None:
    clusterer = SceneClusterer(hamming_threshold=0, stable_min_seconds=4.0)

    first = clusterer.observe("0000000000000000", 0.0)
    second = clusterer.observe("ffffffffffffffff", 1.0)
    third = clusterer.observe("0f0f0f0f0f0f0f0f", 2.0)

    assert [first.cluster_id, second.cluster_id, third.cluster_id] == [1, 2, 3]
    assert clusterer.clusters == ()
    assert clusterer.candidate_count == 3
    assert clusterer.discarded_candidate_count == 2


def test_loaded_card_only_supplies_candidate_and_session_id_starts_from_one() -> None:
    clusterer = SceneClusterer(
        hamming_threshold=2,
        stable_min_seconds=1.0,
        card_scenes=(
            CardSceneReference("scene:s7", "0000000000000000"),
            CardSceneReference("scene:s8", "ffffffffffffffff"),
        ),
    )
    match = clusterer.observe("0000000000000001", 0.0)
    unrelated = clusterer.observe("0f0f0f0f0f0f0f0f", 1.0)

    assert match.cluster_id == 1
    assert match.card_candidate is not None
    assert match.card_candidate.scene_id == "scene:s7"
    assert unrelated.cluster_id == 2
    assert unrelated.card_candidate is None

    changed_width = SceneClusterer(
        hamming_threshold=25,
        stable_min_seconds=1.0,
        card_scenes=(CardSceneReference("scene:s9", "0" * 64),),
    )
    assert changed_width.observe("0" * 16, 0.0).card_candidate is None
