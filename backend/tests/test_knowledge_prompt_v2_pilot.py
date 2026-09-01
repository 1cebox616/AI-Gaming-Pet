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
        "default_pc_keybinds": [
            {"action": f"动作 {index}", "input": f"按键 {index}"}
            for index in range(1, 7)
        ],
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
    for game in pilot.pilot_games():
        assert game.game_name not in prompt


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


def test_fake_pilot_runs_three_games_online_only_and_writes_outputs(tmp_path) -> None:
    clients: dict[str, _FakeClient] = {}

    def factory(mode: pilot.ProbeMode) -> _FakeClient:
        client = _FakeClient(mode.mode_id)
        clients[mode.mode_id] = client
        return client

    run = pilot.run_pilot(client_factory=factory)
    assert len(run.attempts) == 3
    assert all(item.web_enabled and item.mode_id == "online" for item in run.attempts)
    assert set(clients) == {"online"}
    assert all(item.parsed_answer == _answer() for item in run.attempts)
    assert all(
        call["system_prompt"] == pilot.SYSTEM_PROMPT_V2
        and call["temperature"] == 0.0
        and call["max_tokens"] == 2400
        and call["web_enabled"] is True
        and call["provider"] is None
        and call["reasoning_effort"] is None
        for client in clients.values()
        for call in client.calls
    )

    pilot.write_outputs(tmp_path, run)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    answers = (tmp_path / "answers.md").read_text(encoding="utf-8")
    prompt = (tmp_path / "prompt-v2.md").read_text(encoding="utf-8")
    raw = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert "不作选型推荐" in report
    assert "3/3" in report
    assert "每个游戏只跑联网模式" in report
    assert "知识模式" not in report
    assert "完整的测试游戏介绍" in answers
    assert pilot.SYSTEM_PROMPT_V2 in prompt
    assert len(raw["attempts"]) == 3
