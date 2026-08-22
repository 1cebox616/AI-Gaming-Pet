"""Permanent regression checks for the real-payload scenario corpus."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pet.games.cs2.eval.bench import run_bench
from pet.core.config import load_config
from pet.games.cs2.eval.replay import load_recording, replay_policy, replay_recording
from pet.games.cs2.eval.scenario_synth import (
    INVENTORY_PATHS,
    OBSERVED_CONSTRAINTS_PATH,
    RECORDINGS_DIRECTORY,
    SCENARIO_SPECS,
    SCENARIOS_DIRECTORY,
    SYNTHETIC_OTHER_NAME,
    SYNTHETIC_OTHER_STEAMID,
    SYNTHETIC_SELF_NAME,
    SYNTHETIC_SELF_STEAMID,
    Mutation,
    load_inventory_constraints,
    validate_mutation,
)

_requires_recordings = pytest.mark.skipif(
    not RECORDINGS_DIRECTORY.is_dir(),
    reason="需要 gitignore 的 backend/recordings/ 真实录制",
)


def test_unknown_inventory_path_aborts_synthesis() -> None:
    constraints = load_inventory_constraints(INVENTORY_PATHS)

    with pytest.raises(ValueError, match="不在数据清单白名单"):
        validate_mutation(
            Mutation(18, "player.state.not_a_real_field", "set", 1),
            constraints,
        )


@_requires_recordings
@pytest.mark.parametrize(
    ("path", "value", "message"),
    (
        ("player.state.flashed", 255, "超出观测范围"),
        ("player.state.burning", 256, "超出观测范围"),
    ),
)
def test_out_of_range_state_value_aborts_synthesis(
    path: str, value: int, message: str
) -> None:
    constraints = load_inventory_constraints(INVENTORY_PATHS)

    with pytest.raises(ValueError, match=message):
        validate_mutation(Mutation(18, path, "set", value), constraints)


@_requires_recordings
def test_inventory_reports_provide_positive_path_constraints() -> None:
    constraints = load_inventory_constraints(INVENTORY_PATHS)

    assert "player.state.flashed" in constraints
    assert constraints["player.state.flashed"].maximum == 1
    assert constraints["player.state.burning"].minimum == 0
    assert constraints["player.state.burning"].maximum == 255
    assert "gsi-20260811-223119-169538.jsonl" in constraints[
        "player.state.burning"
    ].source_files
    assert constraints["player.state.round_kills"].maximum == 6
    evidence = OBSERVED_CONSTRAINTS_PATH.read_text(encoding="utf-8")
    assert "76561" not in evidence


def test_observed_constraints_are_complete_real_recording_evidence() -> None:
    evidence = json.loads(OBSERVED_CONSTRAINTS_PATH.read_text(encoding="utf-8"))

    assert evidence["metadata"]["payload_count"] == 2400
    assert evidence["metadata"]["source_files"]
    for constraint in evidence["constraints"].values():
        assert constraint["source_files"]
        assert not (
            not constraint["values"]
            and constraint["minimum"] is not None
            and constraint["maximum"] not in (None, 0)
        )


def test_synthetic_products_scrub_all_source_identities() -> None:
    combined = "".join(
        path.read_text(encoding="utf-8")
        for path in SCENARIOS_DIRECTORY.glob("*.jsonl")
    )
    assert "76561" not in combined
    assert SYNTHETIC_SELF_STEAMID in combined
    assert SYNTHETIC_SELF_NAME in combined
    explosion = (SCENARIOS_DIRECTORY / "bomb_explosion_win.jsonl").read_text(
        encoding="utf-8"
    )
    assert SYNTHETIC_SELF_STEAMID in explosion
    assert SYNTHETIC_OTHER_STEAMID in explosion
    for path in SCENARIOS_DIRECTORY.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            _assert_placeholder_identities(json.loads(line))


def test_every_scenario_replays_and_renders_declared_facts() -> None:
    configuration = load_config()
    assert 28 <= len(SCENARIO_SPECS) <= 36

    for spec in SCENARIO_SPECS:
        path = SCENARIOS_DIRECTORY / f"{spec.scenario_id}.jsonl"
        snapshots = load_recording(path)
        replay = replay_recording(path, configuration.events)
        assert snapshots, spec.scenario_id
        assert spec.expected_event_type in {
            event.type for event in replay.events
        }, spec.scenario_id

        if spec.expected_event_type in {"round_win", "round_loss"}:
            # Round result facts remain detector regressions, but this
            # milestone no longer evaluates them as speech candidates.
            continue

        cards = run_bench(
            path,
            model=None,
            provider=None,
            personality_style="inference",
            client=None,
            max_events=40,
            cards_only=True,
        )
        matching_cards = tuple(
            item
            for item in cards.events
            if item.event.type == spec.expected_event_type
        )
        if spec.scenario_id == "weapon_switch_double_kill":
            # The synthetic fixture has no snapshot in the usable 2.5–5.0s
            # release window after its final multi-kill.  The policy must
            # therefore expire the deferred callout rather than speak a stale
            # double kill in the next round; direct policy tests cover release.
            assert not matching_cards
            policy_replay = replay_policy(
                snapshots,
                configuration.events,
                configuration.policy,
            )
            assert any(
                decision.event.type == "multi_kill"
                and decision.reason_code == "deferred"
                for decision in policy_replay.decisions
            )
            continue
        assert matching_cards, spec.scenario_id
        item = matching_cards[-1]
        card = item.event_card
        # M3-T11 removes the internal ``残血击杀`` label while retaining
        # the underlying fact in the online natural-language sentence.
        # Keep the frozen scenario requirement intact and verify its new
        # rendering contract instead of resurrecting the retired label.
        assert all(
            (
                "丝血" in item.fact_sentence
                if fact == "残血击杀"
                else fact in card
            )
            for fact in spec.required_facts
        ), spec.scenario_id
        assert spec.forbidden_claims


def test_generated_values_respect_flash_and_burn_observations() -> None:
    observed_burning = False
    for path in SCENARIOS_DIRECTORY.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            wrapper = json.loads(line)
            for field, value in _walk_fields(wrapper):
                if field == "flashed":
                    assert isinstance(value, int) and 0 <= value <= 1, path.name
                elif field == "burning":
                    assert isinstance(value, int) and 0 <= value <= 255, path.name
                    observed_burning = observed_burning or value > 0
    assert observed_burning


def test_rare_corrective_scenarios_are_permanent_regressions() -> None:
    scenario_ids = {spec.scenario_id for spec in SCENARIO_SPECS}

    assert {
        "four_kill",
        "ace",
        "burning_kill",
        "late_defuse",
        "bomb_explosion_win",
    } <= scenario_ids


def test_burning_scenario_reuses_the_observed_burning_shape() -> None:
    rows = [
        json.loads(line)
        for line in (SCENARIOS_DIRECTORY / "burning_kill.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    burning = [row["payload"]["player"]["state"]["burning"] for row in rows]

    assert [255, 227, 167, 108] == burning[:4]
    assert burning[-1] == 0


def _assert_placeholder_identities(value: object) -> None:
    if isinstance(value, dict):
        mapping = value
        steamid = mapping.get("steamid")
        if isinstance(steamid, str):
            assert steamid in {
                SYNTHETIC_SELF_STEAMID,
                SYNTHETIC_OTHER_STEAMID,
            }
        if "observer_slot" in mapping and isinstance(mapping.get("name"), str):
            assert mapping["name"] in {SYNTHETIC_SELF_NAME, SYNTHETIC_OTHER_NAME}
        for child in mapping.values():
            _assert_placeholder_identities(child)
    elif isinstance(value, list):
        for child in value:
            _assert_placeholder_identities(child)


def _walk_fields(value: object) -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            result.append((key, child))
            result.extend(_walk_fields(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_walk_fields(child))
    return result
