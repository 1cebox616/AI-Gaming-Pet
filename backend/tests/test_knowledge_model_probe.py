"""M5-B-T3a probe tests use only fake responses and a mock transport."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx

from pet.core.llm import LlmDispatchStats, LlmResult, LlmUsage
from pet.games.generic.eval import knowledge_model_probe as probe


def _answer() -> dict[str, object]:
    return {
        "genre": "合成类型",
        "perspective": "合成视角",
        "core_gameplay": "进行合成核心循环。",
        "hud_conventions": [{"element": "元素", "usual_position": "位置"}],
        "default_keybinds": [
            {"action": "动作", "input": "按键", "platform": "平台"}
        ],
        "community_terms": [{"term": "术语", "meaning": "含义"}],
    }


class _FakeClient:
    def __init__(self, candidate: probe.Candidate, mode: probe.ProbeMode) -> None:
        self.candidate = candidate
        self.mode = mode
        self.calls: list[dict[str, object]] = []

    def complete_knowledge(self, **arguments: object) -> LlmResult:
        self.calls.append(arguments)
        return LlmResult(
            text=json.dumps(_answer(), ensure_ascii=False),
            usage=LlmUsage(100, 50, 0.001, reasoning_tokens=0),
            latency_seconds=1.0,
            model=self.candidate.model,
            provider=self.candidate.provider,
            finish_reason="stop",
        )

    def dispatch_stats(self) -> LlmDispatchStats:
        return LlmDispatchStats(
            profile_name=(
                f"m5-b-t3a:{self.candidate.candidate_id}:{self.mode.mode_id}"
            ),
            rate_limit_count=0,
            cooldown_seconds=0.0,
            cooldown_drop_count=0,
            cooling_down=False,
            cooldown_remaining_seconds=0.0,
        )

    def close(self) -> None:
        return None


def test_manifest_has_required_games_categories_and_four_post_2025_cases() -> None:
    names = {game.game_name for game in probe.GAMES}
    assert len(probe.GAMES) == 15
    assert {
        "Overwatch 2",
        "Don't Starve Together",
        "Gray Zone Warfare",
        "Slay the Spire 2",
    } <= names
    assert sum(game.era.startswith("2026-") for game in probe.GAMES) == 4
    assert len({game.category for game in probe.GAMES}) >= 14


def test_one_generic_prompt_contains_no_game_names() -> None:
    assert "{game_name}" in probe.USER_PROMPT_TEMPLATE
    combined = probe.SYSTEM_PROMPT + probe.USER_PROMPT_TEMPLATE
    for game in probe.GAMES:
        assert game.game_name not in combined
        assert game.display_name not in probe.SYSTEM_PROMPT


def test_strict_parser_records_format_drift_without_repair() -> None:
    text = json.dumps(_answer(), ensure_ascii=False)
    parsed, error = probe.parse_answer(text)
    assert parsed == _answer()
    assert error is None

    parsed, error = probe.parse_answer(f"```json\n{text}\n```")
    assert parsed is None
    assert error is not None and "合法 JSON" in error
    assert probe.parse_display_answer(f"```json\n{text}\n```") == _answer()
    quoted = _answer()
    quoted["core_gameplay"] = '执行"合成动作"并继续。'
    invalid_inner = json.dumps(quoted, ensure_ascii=False).replace(
        '\\"合成动作\\"', '"合成动作"'
    )
    displayed = probe.parse_display_answer(f"```json\n{invalid_inner}\n```")
    assert displayed is not None
    assert displayed["core_gameplay"] == '执行"合成动作"并继续。'

    too_many = _answer()
    too_many["community_terms"] = [
        {"term": str(index), "meaning": "含义"} for index in range(6)
    ]
    parsed, error = probe.parse_answer(json.dumps(too_many, ensure_ascii=False))
    assert parsed is None
    assert error == "community_terms 超过 5 项"


def test_online_mode_adds_only_gateway_server_tool_and_locks_provider() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(
            200,
            json={
                "model": "vendor/model",
                "provider": "Provider",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(_answer(), ensure_ascii=False)
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "cost": 0.001,
                },
            },
        )

    client = probe.ProbeOpenRouterClient(
        "key",
        profile_name="test",
        transport=httpx.MockTransport(handler),
    )
    try:
        client.complete_knowledge(
            model="vendor/model",
            provider="Provider",
            system_prompt=probe.SYSTEM_PROMPT,
            user_prompt=probe.render_user_prompt("Synthetic Game"),
            max_tokens=probe.MAX_TOKENS,
            temperature=probe.TEMPERATURE,
            web_enabled=True,
        )
    finally:
        client.close()
    assert bodies[0]["tools"] == [
        {
            "type": "openrouter:web_search",
            "parameters": {"max_results": 3},
        }
    ]
    assert bodies[0]["provider"] == {
        "only": ["Provider"],
        "allow_fallbacks": False,
    }
    assert bodies[0]["temperature"] == 0.0


def test_online_mode_can_leave_provider_routing_to_gateway() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "vendor/model",
                "provider": "Compliant Provider",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(_answer(), ensure_ascii=False)
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    client = probe.ProbeOpenRouterClient(
        "key",
        profile_name="test-auto-provider",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.complete_knowledge(
            model="vendor/model",
            provider=None,
            system_prompt=probe.SYSTEM_PROMPT,
            user_prompt=probe.render_user_prompt("Synthetic Game"),
            max_tokens=probe.MAX_TOKENS,
            temperature=probe.TEMPERATURE,
            web_enabled=True,
        )
    finally:
        client.close()
    assert "provider" not in bodies[0]
    assert result.provider == "Compliant Provider"


def test_fake_full_run_writes_90_aligned_attempts_and_blank_scores(tmp_path) -> None:
    clients: dict[tuple[str, str], _FakeClient] = {}

    def factory(candidate: probe.Candidate, mode: probe.ProbeMode) -> _FakeClient:
        client = _FakeClient(candidate, mode)
        clients[(candidate.candidate_id, mode.mode_id)] = client
        return client

    run = probe.run_probe(client_factory=factory)
    assert len(run.attempts) == 15 * 3 * 2
    assert all(attempt.parsed_answer == _answer() for attempt in run.attempts)
    assert all(
        call["system_prompt"] == probe.SYSTEM_PROMPT
        and call["temperature"] == 0.0
        and call["max_tokens"] == 900
        for client in clients.values()
        for call in client.calls
    )

    probe.write_outputs(tmp_path, run)
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    judging = (tmp_path / "judging.md").read_text(encoding="utf-8")
    prompt = (tmp_path / "prompt.md").read_text(encoding="utf-8")
    raw = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))

    assert "P50 / P90 / 最大" in report
    assert "累计冷却时长" in report
    assert "每次调用的耗时与花费" in report
    assert "与规格的偏差\n\n- 无" in report
    assert report.count("| 15/15（100.0%） |") == 6
    assert judging.count("### 知识模式") == 15
    assert judging.count("### 联网模式") == 15
    assert judging.count("评分（对／错／不确定）") == 1 + 15 * 2 * 3
    assert "| 类型 | 合成类型 |  |" in judging
    assert "Overwatch 2" not in probe.SYSTEM_PROMPT
    assert probe.SYSTEM_PROMPT in prompt
    assert len(raw["attempts"]) == 90

    malformed = replace(
        run.attempts[0],
        parsed_answer=None,
        format_error="合成格式错误",
    )
    mixed_run = replace(run, attempts=(malformed, *run.attempts[1:]))
    mixed_report = probe.render_report(mixed_run)
    mixed_judging = probe.render_judging(mixed_run)
    assert "| 0 | 0 | 1 | 0 | 14 |" in mixed_report
    assert "格式不合原文" in mixed_judging

    fenced = replace(
        run.attempts[0],
        response_text=f"```json\n{run.attempts[0].response_text}\n```",
        parsed_answer=None,
        format_error="不是合法 JSON",
    )
    fenced_judging = probe.render_judging(
        replace(run, attempts=(fenced, *run.attempts[1:]))
    )
    assert "格式不合：机械容错拆栏；原文保留" in fenced_judging
    assert "合成类型" in fenced_judging

    pipe_answer = dict(_answer())
    pipe_answer["default_keybinds"] = [
        {"action": "动作", "input": "按键", "platform": "Xbox Series X|S"}
    ]
    pipe_attempt = replace(run.attempts[0], parsed_answer=pipe_answer)
    pipe_judging = probe.render_judging(
        replace(run, attempts=(pipe_attempt, *run.attempts[1:]))
    )
    assert "Xbox Series X&#124;S" in pipe_judging
    field_rows = [line for line in pipe_judging.splitlines() if line.startswith("| 类型 |")]
    assert all(len(line.strip("|").split("|")) == 7 for line in field_rows)


def test_percentile_uses_linear_interpolation() -> None:
    assert probe._percentile((1.0, 2.0, 3.0, 4.0), 0.5) == 2.5
    assert probe._percentile((1.0, 2.0, 3.0, 4.0), 0.9) == 3.7


def test_seeded_attempt_is_not_dispatched_and_stats_are_combined(tmp_path) -> None:
    clients: dict[tuple[str, str], _FakeClient] = {}

    def factory(candidate: probe.Candidate, mode: probe.ProbeMode) -> _FakeClient:
        client = _FakeClient(candidate, mode)
        clients[(candidate.candidate_id, mode.mode_id)] = client
        return client

    first = probe.run_probe(client_factory=factory)
    seeded_attempt = replace(
        first.attempts[0],
        response_text="",
        parsed_answer=None,
        error="seeded 429",
        error_metadata={"status_code": 429, "cooldown_drop": False},
    )
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(
        json.dumps(
            {
                "attempts": [probe.asdict(seeded_attempt)],
                "dispatch_stats": [
                    {
                        "profile_name": "m5-b-t3a:qwen38-flash:knowledge",
                        "rate_limit_count": 1,
                        "cooldown_seconds": 2.0,
                        "cooldown_drop_count": 0,
                        "cooling_down": False,
                        "cooldown_remaining_seconds": 0.0,
                    }
                ],
                "deviations": ["synthetic recovery"],
                "prior_unpersisted_call_count": 90,
            }
        ),
        encoding="utf-8",
    )
    clients.clear()
    checkpoints: list[probe.ProbeRun] = []
    recovered = probe.run_probe(
        client_factory=factory,
        seed=probe.load_seed(seed_path),
        checkpoint=checkpoints.append,
    )
    assert len(recovered.attempts) == 90
    assert sum(len(client.calls) for client in clients.values()) == 89
    assert recovered.attempts[0].error == "seeded 429"
    qwen_knowledge = next(
        item
        for item in recovered.dispatch_stats
        if item.profile_name == "m5-b-t3a:qwen38-flash:knowledge"
    )
    assert qwen_knowledge.rate_limit_count == 1
    assert qwen_knowledge.cooldown_seconds == 2.0
    assert recovered.deviations == ("synthetic recovery",)
    assert recovered.prior_unpersisted_call_count == 90
    assert len(checkpoints) == 89

    output = tmp_path / "saved"
    probe.write_raw_results(output, recovered)
    loaded = probe.load_run(output / "results.json")
    assert loaded == recovered
