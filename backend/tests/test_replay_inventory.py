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
                "provider": {
                    "name": "Counter-Strike: Global Offensive",
                    "steamid": "76561198000000001",
                },
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
                    "name": "真实玩家甲",
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
                "provider": {
                    "name": "Counter-Strike: Global Offensive",
                    "steamid": "76561198000000001",
                },
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
                    "name": "真实玩家乙",
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
                "previously": {
                    "player": {
                        "name": "旧玩家",
                        "steamid": "76561198000000002",
                    }
                },
                "added": {
                    "player": {
                        "name": "新玩家",
                        "steamid": "76561198000000003",
                    }
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
    assert "| `bomb` |" not in report
    assert "| `phase_countdowns` |" not in report
    for identity_path in (
        "player.name",
        "player.steamid",
        "provider.steamid",
        "previously.player.name",
        "previously.player.steamid",
        "added.player.name",
        "added.player.steamid",
    ):
        assert f"| `{identity_path}` |" in report
        assert f"| `{identity_path}` | 1 | <已脱敏> |" in report or (
            identity_path in {"player.name", "player.steamid", "provider.steamid"}
            and f"| `{identity_path}` | 2 | <已脱敏> |" in report
        )
    weapon_name_row = next(
        line for line in report.splitlines() if line.startswith("| `player.weapons.*.name` |")
    )
    assert '"weapon_deagle"' in weapon_name_row
    assert '"weapon_knife"' in weapon_name_row
    assert "<已脱敏>" not in weapon_name_row
    assert "| `map.name` | 2 | \"de_anubis\" | 是 |" in report
    assert (
        '| `provider.name` | 2 | "Counter-Strike: Global Offensive" | 否 |'
        in report
    )
    for private_value in (
        "76561198000000001",
        "76561198000000002",
        "76561198000000003",
        "真实玩家甲",
        "真实玩家乙",
        "旧玩家",
        "新玩家",
    ):
        assert private_value not in report
    for fact_name in (
        "flash_count",
        "flashed_seconds_total",
        "longest_flash_seconds",
        "smoked_seconds_total",
        "max_smoke_intensity",
        "burn_count",
        "total_damage_taken",
        "lowest_health_while_alive",
        "health_before_death",
        "primary_weapons_used",
        "bought_equipment",
        "bomb_planted_at_ts",
        "seconds_since_bomb_planted",
        "is_low_health",
        "is_eco_round",
        "is_low_ammo",
        "armor_status",
        "held_weapon",
        "is_currently_flashed",
        "is_currently_smoked",
        "is_carrying_bomb",
    ):
        assert report.count(f"| `{fact_name}` |") == 1


def test_inventory_keeps_up_to_ten_distinct_string_samples(tmp_path: Path) -> None:
    recording = tmp_path / "ten-string-values.jsonl"
    rows = tuple(
        {"ts": float(index), "payload": {"custom": {"label": f"value-{index}"}}}
        for index in range(10)
    )
    recording.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    report = generate_data_inventory(recording)
    row = next(
        line for line in report.splitlines() if line.startswith("| `custom.label` |")
    )

    for index in range(10):
        assert f'"value-{index}"' in row
