"""M5-B-T2 typed game-card promotion and persistence regression tests."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

import pet.core.gamecard as gamecard_module
from pet.core.gamecard import (
    GameCardRepository,
    GameCardSession,
    render_gamecard_markdown,
    slugify_game_id,
)
from pet.core.scene_fingerprint import SceneClusterer

STARTED = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _clusterer(
    *,
    run_durations: tuple[float, ...],
    stable_min_seconds: float = 5.0,
    card=(),
) -> SceneClusterer:
    clusterer = SceneClusterer(1, stable_min_seconds, card)
    observed_at = 0.0
    evidence_number = 1
    for visit_index, duration in enumerate(run_durations):
        for timestamp in (observed_at, observed_at + duration):
            match = clusterer.observe("0000000000000000", timestamp)
            clusterer.record_evidence(
                match.cluster_id,
                f"f{evidence_number}:scene:1",
            )
            evidence_number += 1
        observed_at += duration
        if visit_index < len(run_durations) - 1:
            observed_at += 1.0
            clusterer.observe("ffffffffffffffff", observed_at)
            observed_at += 1.0
    return clusterer


def _session(
    repository: GameCardRepository,
    game_id: str,
    clusterer: SceneClusterer,
) -> GameCardSession:
    card = repository.load_or_create(game_id, "Fixture Game", STARTED)
    return GameCardSession(
        repository,
        card,
        STARTED,
        card_min_dwell_seconds=30.0,
    )


def test_slug_rule_is_ascii_lowercase_collapsed_and_hashes_empty_results() -> None:
    assert slugify_game_id("  Grey Zone: Warfare.exe  ") == "grey-zone-warfare-exe"
    assert slugify_game_id("燕云 十六声") == "g-5ec2b67f"
    assert slugify_game_id("!!!") == "g-9a7b006d"
    assert slugify_game_id("---", "!!!") == "g-9a7b006d"


def test_stable_cluster_with_insufficient_dwell_does_not_promote(
    tmp_path: Path,
) -> None:
    repository = GameCardRepository(tmp_path)
    clusterer = _clusterer(run_durations=(5.0,))
    session = _session(repository, "fixture-game", clusterer)
    session.flush(clusterer.clusters)
    assert session.card.scenes == []


def test_repeated_short_runs_never_enter_the_scene_index_or_promote(
    tmp_path: Path,
) -> None:
    repository = GameCardRepository(tmp_path)
    clusterer = _clusterer(run_durations=(4.0,) * 8)
    assert clusterer.clusters == ()
    assert clusterer.candidate_count == 15
    session = _session(repository, "fixture-game", clusterer)
    session.flush(clusterer.clusters)
    assert session.card.scenes == []


def test_qualifying_cluster_promotes_and_periodic_flush_does_not_double_count(
    tmp_path: Path,
) -> None:
    repository = GameCardRepository(tmp_path)
    clusterer = _clusterer(run_durations=(15.0, 15.0))
    session = _session(repository, "fixture-game", clusterer)
    session.flush(clusterer.clusters)
    session.flush(clusterer.clusters)

    scene = session.card.scenes[0]
    assert scene.cluster_id == "scene:s1"
    assert scene.seen_count == 4
    assert scene.visit_count == 2
    assert scene.sessions_seen == 1
    assert len(scene.evidence_ids) == 4
    assert (tmp_path / "fixture-game" / "gamecard.json").is_file()
    assert (tmp_path / "fixture-game" / "gamecard.md").read_text(
        encoding="utf-8"
    ) == render_gamecard_markdown(session.card)


def test_second_session_keeps_session_cluster_one_and_merges_candidate(
    tmp_path: Path,
) -> None:
    repository = GameCardRepository(tmp_path)
    first_clusterer = _clusterer(run_durations=(15.0, 15.0))
    first_session = _session(repository, "fixture-game", first_clusterer)
    first_session.flush(first_clusterer.clusters)

    loaded = repository.load_or_create("fixture-game", "Fixture Game", STARTED)
    second_clusterer = _clusterer(
        run_durations=(15.0, 15.0),
        card=repository.card_references(loaded),
    )
    second_session = GameCardSession(
        repository,
        loaded,
        STARTED,
        card_min_dwell_seconds=30.0,
    )
    second_session.flush(second_clusterer.clusters)

    assert second_clusterer.clusters[0].cluster_id == 1
    assert second_clusterer.clusters[0].card_candidate is not None
    scene = second_session.card.scenes[0]
    assert scene.cluster_id == "scene:s1"
    assert scene.seen_count == 8
    assert scene.visit_count == 4
    assert scene.sessions_seen == 2


def test_unmatched_qualifying_cluster_creates_new_scene_and_keeps_old_last_seen(
    tmp_path: Path,
) -> None:
    repository = GameCardRepository(tmp_path)
    first_clusterer = _clusterer(run_durations=(30.0,))
    first_session = _session(repository, "fixture-game", first_clusterer)
    first_session.flush(first_clusterer.clusters)
    old_last_seen = first_session.card.scenes[0].last_seen

    loaded = repository.load_or_create("fixture-game", "Fixture Game", STARTED)
    new_clusterer = SceneClusterer(1, 5.0, repository.card_references(loaded))
    for index, observed_at in enumerate((0.0, 31.0), start=1):
        match = new_clusterer.observe("ffffffffffffffff", observed_at)
        new_clusterer.record_evidence(match.cluster_id, f"f{index}:scene:1")
    session = GameCardSession(
        repository,
        loaded,
        STARTED,
        card_min_dwell_seconds=30.0,
    )
    session.flush(new_clusterer.clusters)

    assert [scene.cluster_id for scene in session.card.scenes] == ["scene:s1", "scene:s2"]
    assert session.card.scenes[0].last_seen == old_last_seen


def test_atomic_write_failure_leaves_no_partial_target_or_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = GameCardRepository(tmp_path)
    card = repository.load_or_create("fixture-game", "Fixture Game", STARTED)

    def fail_replace(_source: os.PathLike[str], _target: os.PathLike[str]) -> None:
        raise OSError("injected atomic replace failure")

    monkeypatch.setattr(gamecard_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        repository.write(card)
    directory = tmp_path / "fixture-game"
    assert not (directory / "gamecard.json").exists()
    assert not tuple(directory.glob("*.tmp"))
