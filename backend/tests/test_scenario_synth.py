"""Permanent regression checks for the real-payload scenario corpus."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import pytest

from pet.bench import run_bench
from pet.config import load_config
from pet.replay import load_recording, replay_recording
from pet.scenario_synth import (
    INVENTORY_PATHS,
    SCENARIO_SPECS,
    SCENARIOS_DIRECTORY,
    SYNTHETIC_NAME,
    SYNTHETIC_STEAMID,
    Mutation,
    load_inventory_constraints,
    synthesize_scenario,
)


def test_unknown_inventory_path_aborts_synthesis() -> None:
    spec = replace(
        SCENARIO_SPECS[0],
        mutations=(Mutation(18, "player.state.not_a_real_field", "set", 1),),
    )

    with pytest.raises(ValueError, match="不在数据清单白名单"):
        synthesize_scenario(spec)


@pytest.mark.parametrize(
    ("path", "value", "message"),
    (
        ("player.state.flashed", 255, "超出观测范围"),
        ("player.state.burning", 1, "超出观测范围"),
    ),
)
def test_out_of_range_state_value_aborts_synthesis(
    path: str, value: int, message: str
) -> None:
    spec = replace(
        SCENARIO_SPECS[0], mutations=(Mutation(18, path, "set", value),)
    )

    with pytest.raises(ValueError, match=message):
        synthesize_scenario(spec)


def test_inventory_reports_provide_positive_path_constraints() -> None:
    constraints = load_inventory_constraints(INVENTORY_PATHS)

    assert "player.state.flashed" in constraints
    assert constraints["player.state.flashed"].maximum == 1
    assert constraints["player.state.burning"].minimum == 0
    assert constraints["player.state.burning"].maximum == 0


def test_synthetic_products_scrub_all_source_identities() -> None:
    source_identities: set[str] = set()
    for spec in SCENARIO_SPECS:
        filename = spec.template_source.split(" 第 ", 1)[0]
        source = SCENARIOS_DIRECTORY.parent / "recordings" / filename
        for line in source.read_text(encoding="utf-8").splitlines():
            wrapper = json.loads(line)
            _collect_source_identities(wrapper, source_identities)

    combined = "".join(
        path.read_text(encoding="utf-8")
        for path in SCENARIOS_DIRECTORY.glob("*.jsonl")
    )
    assert "76561" not in combined
    assert SYNTHETIC_STEAMID in combined
    assert SYNTHETIC_NAME in combined
    assert all(identity not in combined for identity in source_identities)


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
        assert matching_cards, spec.scenario_id
        card = matching_cards[-1].event_card
        assert all(fact in card for fact in spec.required_facts), spec.scenario_id
        assert spec.forbidden_claims


def test_generated_values_respect_flash_and_burn_observations() -> None:
    for path in SCENARIOS_DIRECTORY.glob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            wrapper = json.loads(line)
            for field, value in _walk_fields(wrapper):
                if field == "flashed":
                    assert isinstance(value, int) and 0 <= value <= 1, path.name
                elif field == "burning":
                    assert value == 0, path.name


def _collect_source_identities(value: object, output: set[str]) -> None:
    if isinstance(value, dict):
        mapping = value
        for key, child in mapping.items():
            if key in {"steamid", "name"} and isinstance(child, str):
                if key == "steamid" or any(
                    marker in mapping for marker in ("observer_slot", "activity")
                ):
                    output.add(child)
            _collect_source_identities(child, output)
    elif isinstance(value, list):
        for child in value:
            _collect_source_identities(child, output)


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
