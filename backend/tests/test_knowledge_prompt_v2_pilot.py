"""Tests for the detailed game-context prompt pilot."""

from __future__ import annotations

import json

from pet.core.llm import LlmDispatchStats, LlmResult, LlmUsage
from pet.games.generic.eval import knowledge_prompt_v2_pilot as pilot


def _answer() -> dict[str, object]:
    return {
        "genre": ["测试类型", "测试子类型"],
        "perspective": "测试视角",
        "game_overview": "这是完整的测试游戏介绍，包含定位、目标和游玩形态。",
        "gameplay": {
            "player_goal": "完成测试目标。",
            "core_loop": "开始测试，执行动作，读取反馈，然后进入下一轮。",
            "major_systems": [
                {"name": f"系统 {index}", "description": "影响测试决策。"}
                for index in range(1, 5)
            ],
            "modes_and_structure": "按测试回合组织。",
        },
        "background": {
            "setting_and_premise": "不剧透的测试背景。",
            "release_and_service_status": "测试状态。",
        },
        "default_pc_keybinds": {
            "前进": "W",
            "后退": "S",
            "左移": "A",
            "右移": "D",
            "跳跃": "Space",
            "快捷物品栏1": "1",
        },
    }


class _FakeClient:
    def __init__(self, mode_id: str) -> None:
        self.mode_id = mode_id
        self.calls: list[dict[str, object]] = []

    def complete_knowledge(self, **arguments: object) -> LlmResult:
        self.calls.append(arguments)
        return LlmResult(
            text=json.dumps(_answer(), ensure_ascii=False),
            usage=LlmUsage(200, 500, 0.001, reasoning_tokens=0),
            latency_seconds=2.0,
            model=pilot.MODEL,
            provider="Synthetic Provider",
            finish_reason="stop",
        )

    def dispatch_stats(self) -> LlmDispatchStats:
        return LlmDispatchStats(
            profile_name=f"m5-b-t3a-v2:{self.mode_id}",
            rate_limit_count=0,
            cooldown_seconds=0.0,
            cooldown_drop_count=0,
            cooling_down=False,
            cooldown_remaining_seconds=0.0,
        )

    def close(self) -> None:
        return None


def test_v2_prompt_has_requested_scope_and_no_game_specific_content() -> None:
    prompt = pilot.SYSTEM_PROMPT_V2
    assert "game_overview" in prompt
    assert "gameplay" in prompt
    assert "background" in prompt
    assert "default_pc_keybinds" in prompt
    assert "不要描述 HUD" in prompt
    assert "不要输出社区术语" in prompt
    assert '"platform"' not in prompt
    assert "控制器键位" in prompt
    assert '"前进": "W"' in prompt
    assert "禁止写成 WASD" in prompt
    for games in pilot.GAME_SUITES.values():
        for game in games:
            assert game.game_name not in prompt


def test_followup_suite_is_diverse_and_does_not_overlap_initial_suite() -> None:
    initial_ids = {game.game_id for game in pilot.INITIAL_GAMES}
    followup_ids = {game.game_id for game in pilot.FOLLOWUP_GAMES}
    assert len(followup_ids) == 5
    assert initial_ids.isdisjoint(followup_ids)
    assert {game.category for game in pilot.FOLLOWUP_GAMES} == {
        "第三人称英雄射击",
        "动作角色扮演",
        "回合制策略",
        "体育模拟",
        "动作肉鸽",
    }
    assert [game.game_id for game in pilot.HEALING_GAMES] == [
        "marvel-rivals",
        "ea-sports-fc-26",
        "hades-ii",
    ]


def test_v2_strict_parser_accepts_contract_and_rejects_old_fields() -> None:
    text = json.dumps(_answer(), ensure_ascii=False)
    parsed, error = pilot.parse_answer(text)
    assert parsed == _answer()
    assert error is None

    old = dict(_answer())
    old["community_terms"] = []
    parsed, error = pilot.parse_answer(json.dumps(old, ensure_ascii=False))
    assert parsed is None
    assert error is not None and "多出" in error

    grouped = _answer()
    grouped["default_pc_keybinds"] = {"移动": "WASD"}
    parsed, error = pilot.parse_answer(json.dumps(grouped, ensure_ascii=False))
    assert parsed is None
    assert error is not None and "不是单一规范化 PC 输入" in error


def test_parser_extracts_one_complete_contract_object_but_keeps_warning() -> None:
    wrapped = "模型前言\n```json\n" + json.dumps(_answer(), ensure_ascii=False) + "\n```"
    parsed, error = pilot.parse_answer(wrapped)
    assert parsed == _answer()
    assert error is not None and "剥离 JSON 外文本" in error

    truncated = wrapped[:-20]
    parsed, error = pilot.parse_answer(truncated)
    assert parsed is None
    assert error is not None and "不是合法 JSON" in error

    alternatives = _answer()
    alternatives["default_pc_keybinds"] = {"切换武器": "1/2"}
    parsed, error = pilot.parse_answer(json.dumps(alternatives, ensure_ascii=False))
    assert parsed is None
    assert error is not None and "不是单一规范化 PC 输入" in error


def test_parser_deterministically_normalizes_safe_json_and_key_aliases() -> None:
    answer = _answer()
    answer["default_pc_keybinds"] = {"打开通信": "`", "前进": "W"}
    raw = json.dumps(answer, ensure_ascii=False)
    raw = raw[:-1] + ",}"
    parsed, warning, actions = pilot.parse_answer_detailed(
        "前言\n```json\n" + raw + "\n```"
    )
    assert parsed is not None
    assert parsed["default_pc_keybinds"] == {
        "打开通信": "Backquote",
        "前进": "W",
    }
    assert warning is not None and "尾随逗号" in warning
    assert actions[0] == "剥离 JSON 外文本／代码围栏"
    assert any("Backquote" in action for action in actions)


def test_parser_rejects_duplicate_keys_at_any_depth_and_nonstandard_constants() -> None:
    text = json.dumps(_answer(), ensure_ascii=False)
    duplicate_top = text[:-1] + ',"genre":["重复"]}'
    parsed, error = pilot.parse_answer(duplicate_top)
    assert parsed is None
    assert error == "JSON 含重复键：'genre'"

    duplicate_nested = text.replace(
        '"player_goal": "完成测试目标。",',
        '"player_goal": "完成测试目标。", "player_goal": "重复",',
    )
    parsed, error = pilot.parse_answer(duplicate_nested)
    assert parsed is None
    assert error == "JSON 含重复键：'player_goal'"

    nonstandard = text.replace('"genre": [', '"extra_number": NaN, "genre": [')
    parsed, error = pilot.parse_answer(nonstandard)
    assert parsed is None
    assert error == "JSON 含非标准常量：NaN"

    wrapped_duplicate = "前言\n```json\n" + duplicate_nested + "\n```"
    parsed, error = pilot.parse_answer(wrapped_duplicate)
    assert parsed is None
    assert error is not None and "找到完整 JSON 对象" in error
    assert "重复键" in error


def test_normalized_context_round_trips_through_strict_json() -> None:
    parsed, error, _actions = pilot.parse_answer_detailed(
        "```json\n" + json.dumps(_answer(), ensure_ascii=False) + "\n```"
    )
    assert error is not None
    assert parsed is not None
    canonical = json.dumps(parsed, ensure_ascii=False, allow_nan=False)
    assert pilot._strict_json_loads(canonical) == parsed


def test_fake_pilot_runs_five_games_online_only_and_writes_outputs(tmp_path) -> None:
    clients: dict[str, _FakeClient] = {}

    def factory(mode: pilot.ProbeMode) -> _FakeClient:
        client = _FakeClient(mode.mode_id)
        clients[mode.mode_id] = client
        return client

    run = pilot.run_pilot(client_factory=factory)
    assert len(run.attempts) == 5
    assert all(item.web_enabled and item.mode_id == "online" for item in run.attempts)
    assert set(clients) == {"online"}
    assert all(item.parsed_answer == _answer() for item in run.attempts)
    assert all(
        call["system_prompt"] == pilot.SYSTEM_PROMPT_V2
        and call["temperature"] == 0.0
        and call["max_tokens"] == 8000
        and call["web_enabled"] is True
        and call["provider"] is None
        and call["reasoning_effort"] is None
        and call["web_search_parameters"] == pilot.WEB_SEARCH_PARAMETERS
        and call["provider_options"] == pilot.PROVIDER_OPTIONS
        and call["response_format"] == pilot.RESPONSE_FORMAT
        and call["plugins"] == pilot.RESPONSE_PLUGINS
        for client in clients.values()
        for call in client.calls
    )

    pilot.write_outputs(tmp_path, run)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    answers = (tmp_path / "answers.md").read_text(encoding="utf-8")
    prompt = (tmp_path / "prompt-v3.md").read_text(encoding="utf-8")
    raw = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    parsed_contexts = json.loads(
        (tmp_path / "parsed-contexts.json").read_text(encoding="utf-8")
    )
    assert "不作选型推荐" in report
    assert "5/5" in report
    assert "每个游戏只跑联网模式" in report
    assert "知识模式" not in report
    assert "完整的测试游戏介绍" in answers
    assert pilot.SYSTEM_PROMPT_V2 in prompt
    assert len(raw["attempts"]) == 5
    assert raw["web_search_parameters"] == pilot.WEB_SEARCH_PARAMETERS
    assert raw["provider_options"] == pilot.PROVIDER_OPTIONS
    assert raw["plugins"] == list(pilot.RESPONSE_PLUGINS)
    assert len(parsed_contexts) == 5
    assert all(item["context"] == _answer() for item in parsed_contexts)
    assert pilot._strict_json_loads(
        (tmp_path / "results.json").read_text(encoding="utf-8")
    ) == raw
    assert pilot._strict_json_loads(
        (tmp_path / "parsed-contexts.json").read_text(encoding="utf-8")
    ) == parsed_contexts
