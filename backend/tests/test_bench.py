"""Offline benchmark tests driven by existing scrubbed GSI recordings."""

import json
import math
from pathlib import Path
import re
from typing import Any

import pytest

from pet.bench import (
    BenchResult,
    calculate_length_statistics,
    check_output,
    commentary_length_statistics,
    main,
    render_latency_report,
    render_report,
    run_bench,
    run_latency_experiment,
    write_report,
)
from pet.commentary_templates import COMMENTARY_TEMPLATES
from pet.llm import LlmError, LlmResult, LlmUsage
from pet.prompt import load_system_prompt

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_event_samples.json"


class FakeLlmClient:
    """Deterministic injectable client with an optional one-call failure."""

    def __init__(self, *, fail_call: int | None = None) -> None:
        self.fail_call = fail_call
        self.calls: list[tuple[str | None, str, str, int]] = []

    def complete(
        self,
        *,
        model: str,
        provider: str | None = None,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> LlmResult:
        del temperature
        self.calls.append((provider, system_prompt, user_prompt, max_tokens))
        call_number = len(self.calls)
        if call_number == self.fail_call:
            raise LlmError("injected failure", status_code=503, latency_seconds=0.25)
        return LlmResult(
            text=f"第{call_number}次模型输出，完整复述事实卡中的已知局面。",
            usage=LlmUsage(
                prompt_tokens=100 + call_number,
                completion_tokens=8 + call_number,
                cost_usd=0.001 * call_number,
            ),
            latency_seconds=0.1 * call_number,
            model=f"{model}-actual",
            provider="test-provider",
        )


@pytest.fixture()
def real_recording(tmp_path: Path) -> Path:
    loaded: object = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    samples: Any = loaded["ordinary_death_with_trade_kill"]["samples"]
    recording = tmp_path / "real-scrubbed-fragment.jsonl"
    recording.write_text(
        "\n".join(json.dumps(sample, ensure_ascii=False) for sample in samples),
        encoding="utf-8",
    )
    return recording


@pytest.fixture()
def prompts_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "reading.md").write_text("共享读卡指南", encoding="utf-8")
    (directory / "brother.md").write_text(
        "外置搭子提示词\n最多 {max_chars} 个汉字",
        encoding="utf-8",
    )
    (directory / "caster.md").write_text(
        "外置解说提示词\n最多 {max_chars} 个汉字",
        encoding="utf-8",
    )
    (directory / "inference.md").write_text(
        "只复述事实，不限制字数",
        encoding="utf-8",
    )
    return directory


def test_external_prompt_joins_reading_then_personality_and_replaces_limit() -> None:
    prompt = load_system_prompt("brother", max_chars=19)

    assert prompt.startswith("你在看一份 CS2 对局的实时事实卡")
    assert prompt.index("## 这张卡的五段") < prompt.index("你是观战朋友打 CS2")
    assert "最多包含 19 个汉字" in prompt
    assert "没有提供的信息不要推断，更不要编造" in prompt
    assert "{max_chars}" not in prompt


def test_inference_prompt_without_placeholder_loads_through_same_path(
    prompts_directory: Path,
) -> None:
    prompt = load_system_prompt(
        "inference",
        max_chars=19,
        prompts_directory=prompts_directory,
    )
    without_reading = load_system_prompt(
        "inference",
        max_chars=19,
        prompts_directory=prompts_directory,
        include_reading_guide=False,
    )

    assert prompt == "共享读卡指南\n\n只复述事实，不限制字数"
    assert without_reading == "只复述事实，不限制字数"


def test_missing_or_empty_prompt_raises_instead_of_falling_back(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError, match="reading.md"):
        load_system_prompt("brother", max_chars=19, prompts_directory=tmp_path)

    (tmp_path / "reading.md").write_text("guide", encoding="utf-8")
    (tmp_path / "brother.md").write_text("  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="系统提示词文件为空"):
        load_system_prompt("brother", max_chars=19, prompts_directory=tmp_path)


def test_nearest_rank_p90_and_current_pool_statistics_are_correct() -> None:
    known = ("一", "一二", "一二三", "一二三四", "一二三四五", "{detail}一二三四五六")
    statistics = calculate_length_statistics(known)

    assert statistics.minimum == 1
    assert statistics.median == 3.5
    assert statistics.p90 == 6
    assert statistics.maximum == 6

    pool = tuple(
        template.text
        for templates in COMMENTARY_TEMPLATES["brother"].values()
        for template in templates
    )
    independent_lengths = sorted(
        len(
            re.findall(
                r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]",
                re.sub(r"\{[^{}]+\}", "", text),
            )
        )
        for text in pool
    )
    current = commentary_length_statistics("brother")
    assert current.p90 == independent_lengths[math.ceil(len(independent_lengths) * 0.9) - 1]


def test_factual_checks_can_skip_only_the_length_limit() -> None:
    checked = check_output("去A点狠狠干草草草草草草", max_chars=4)
    inference = check_output(
        "去A点狠狠干草草草草草草",
        max_chars=4,
        enforce_length_limit=False,
    )

    assert checked.exceeds_max_chars is True
    assert inference.exceeds_max_chars is None
    assert inference.callout_terms == ("A点",)
    assert "草" in inference.raw_curses


def test_callout_check_does_not_confuse_m4a1_s_with_a_site() -> None:
    weapon = check_output("先用M4A1-S击杀", max_chars=100)
    actual_callout = check_output("守A点", max_chars=100)

    assert weapon.callout_terms == ()
    assert actual_callout.callout_terms == ("A点",)


def test_report_prints_exact_full_event_card_and_single_attempt(
    real_recording: Path,
    prompts_directory: Path,
    tmp_path: Path,
) -> None:
    client = FakeLlmClient()
    result = run_bench(
        real_recording,
        model="vendor/model-under-test",
        provider="provider-under-test",
        personality_style="caster",
        client=client,
        max_events=20,
        prompts_directory=prompts_directory,
    )
    report = render_report(result)
    output_path = tmp_path / "report.md"
    write_report(result, output_path)

    assert isinstance(result, BenchResult)
    assert result.selected_event_count == 1
    assert len(result.events) == 1
    assert len(client.calls) == 1
    provider, system_prompt, user_prompt, max_tokens = client.calls[0]
    assert provider == "provider-under-test"
    assert system_prompt.startswith("共享读卡指南\n\n外置解说提示词")
    assert max_tokens == 96
    assert user_prompt == result.events[0].event_card
    for card_line in user_prompt.splitlines():
        assert f"    {card_line}" in report
    assert "模板句：" in report
    assert "模型句：第1次模型输出" in report
    assert "实际返回的上游服务商去重列表：`test-provider`" in report
    assert report == output_path.read_text(encoding="utf-8")


def test_inference_uses_300_tokens_and_reports_no_length_limit(
    real_recording: Path,
    prompts_directory: Path,
) -> None:
    client = FakeLlmClient()

    result = run_bench(
        real_recording,
        model="vendor/model-under-test",
        provider="provider-under-test",
        personality_style="inference",
        client=client,
        max_events=20,
        prompts_directory=prompts_directory,
    )
    report = render_report(result)

    assert client.calls[0][3] == 300
    assert result.events[0].attempt is not None
    assert result.events[0].attempt.checks is not None
    assert result.events[0].attempt.checks.exceeds_max_chars is None
    assert "本轮不限字数" in report


def test_cards_only_renders_cards_without_loading_prompt_or_calling_client(
    real_recording: Path,
    tmp_path: Path,
) -> None:
    missing_prompts = tmp_path / "does-not-exist"
    client = FakeLlmClient()

    result = run_bench(
        real_recording,
        model=None,
        provider=None,
        personality_style="brother",
        client=client,
        max_events=20,
        prompts_directory=missing_prompts,
        cards_only=True,
    )
    report = render_report(result)

    assert client.calls == []
    assert result.events[0].attempt is None
    assert "模型调用次数：0（cards-only）" in report
    assert "模型句：" not in report
    for line in result.events[0].event_card.splitlines():
        assert f"    {line}" in report


def test_cards_only_cli_does_not_require_api_key(
    real_recording: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    output = tmp_path / "cards.md"

    exit_code = main(
        [
            "--replay",
            str(real_recording),
            "--cards-only",
            "--out",
            str(output),
        ]
    )

    assert exit_code == 0
    assert "模型调用次数：0（cards-only）" in output.read_text(encoding="utf-8")


def test_latency_experiment_runs_a_then_b_then_c_on_one_client(
    real_recording: Path,
    prompts_directory: Path,
) -> None:
    client = FakeLlmClient()

    experiment = run_latency_experiment(
        real_recording,
        model="vendor/model-under-test",
        provider="provider-under-test",
        client=client,
        max_events=20,
        prompts_directory=prompts_directory,
    )
    report = render_latency_report(experiment)

    assert len(client.calls) == 3
    assert client.calls[0][1].startswith("外置搭子提示词")
    assert client.calls[1][1].startswith("共享读卡指南\n\n外置搭子提示词")
    assert client.calls[2][1].startswith("共享读卡指南\n\n只复述事实")
    assert tuple(call[3] for call in client.calls) == (96, 96, 300)
    assert "执行顺序：A → B → C" in report
    assert "| A |" in report and "| B |" in report and "| C |" in report
    assert "P50 延迟 A→B" in report


def test_failed_call_is_recorded_without_a_second_attempt(
    real_recording: Path,
    prompts_directory: Path,
) -> None:
    client = FakeLlmClient(fail_call=1)

    result = run_bench(
        real_recording,
        model="vendor/model-under-test",
        provider=None,
        personality_style="brother",
        client=client,
        max_events=20,
        prompts_directory=prompts_directory,
    )

    assert len(client.calls) == 1
    assert result.events[0].attempt is not None
    assert result.events[0].attempt.result is None
    assert "injected failure" in (result.events[0].attempt.error or "")
    assert "未锁定（延迟数字不可比）" in render_report(result)
