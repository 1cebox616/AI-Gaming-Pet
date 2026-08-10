"""Automatic inventory coverage for structurally real GSI payloads."""

from datetime import datetime, timezone
import json
from pathlib import Path

from pet.replay import generate_data_inventory


def test_inventory_counts_raw_paths_ranges_and_all_derivations(tmp_path: Path) -> None:
    recording = tmp_path / "inventory-sample.jsonl"
    payloads = (
        {
            "ts": 10.0,
            "payload": {
                "provider": {"steamid": "76561198000000001"},
                "map": {
                    "mode": "casual",
                    "name": "de_anubis",
                    "phase": "live",
                    "round": 0,
                    "team_ct": {"score": 0},
                    "team_t": {"score": 0},
                },
                "round": {"phase": "live"},
                "player": {
                    "steamid": "76561198000000001",
                    "activity": "playing",
                    "team": "CT",
                    "state": {
                        "health": 100,
                        "armor": 100,
                        "helmet": True,
                        "money": 4000,
                        "equip_value": 2500,
                        "flashed": 0,
                        "smoked": 0,
                        "burning": 0,
                    },
                    "weapons": {
                        "weapon_1": {
                            "name": "weapon_deagle",
                            "paintkit": "default",
                            "type": "Pistol",
                            "ammo_clip": 7,
                            "ammo_clip_max": 7,
                            "ammo_reserve": 35,
                            "state": "active",
                        },
                        "weapon_0": {
                            "name": "weapon_knife",
                            "paintkit": "default",
                            "type": "Knife",
                            "state": "holstered",
                        },
                    },
                },
            },
        },
        {
            "ts": 11.0,
            "payload": {
                "provider": {"steamid": "76561198000000001"},
                "map": {
                    "mode": "casual",
                    "name": "de_anubis",
                    "phase": "live",
                    "round": 0,
                    "team_ct": {"score": 0},
                    "team_t": {"score": 0},
                },
                "round": {"phase": "live", "bomb": "planted"},
                "player": {
                    "steamid": "76561198000000001",
                    "activity": "playing",
                    "team": "CT",
                    "state": {
                        "health": 50,
                        "armor": 80,
                        "helmet": True,
                        "money": 4000,
                        "equip_value": 2500,
                        "flashed": 20,
                        "smoked": 0,
                        "burning": 0,
                    },
                    "weapons": {
                        "weapon_1": {
                            "name": "weapon_deagle",
                            "paintkit": "default",
                            "type": "Pistol",
                            "ammo_clip": 1,
                            "ammo_clip_max": 7,
                            "ammo_reserve": 35,
                            "state": "active",
                        }
                    },
                },
            },
        },
    )
    recording.write_text(
        "\n".join(json.dumps(payload) for payload in payloads) + "\n",
        encoding="utf-8",
    )

    report = generate_data_inventory(
        recording,
        generated_at=datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc),
    )

    assert "- payload 条数：2" in report
    assert "- 覆盖回合数：1" in report
    assert "| `player.state.health` | 2 | 最小 50 / 最大 100 | 是 |" in report
    assert "| `player.weapons.*.ammo_clip` | 2 | 最小 1 / 最大 7 | 是 |" in report
    assert "| `player.weapons.*.paintkit` | 2 | \"default\" | 否 |" in report
    assert "| `round.bomb` | 1 | \"planted\" | 是 |" in report
    assert "| `bomb` | 0 | — | 否 |" in report
    assert "| `phase_countdowns` | 0 | — | 否 |" in report
    for fact_name in (
        "flash_count",
        "burn_count",
        "total_damage_taken",
        "lowest_health",
        "health_before_death",
        "weapon_switch_count",
        "bought_equipment",
        "is_low_health",
        "is_eco_round",
        "is_low_ammo",
        "armor_status",
        "held_weapon",
        "is_currently_flashed",
        "is_currently_smoked",
    ):
        assert report.count(f"| `{fact_name}` |") == 1
