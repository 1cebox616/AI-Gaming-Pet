"""Offline benchmark tests driven by existing scrubbed GSI recordings."""

import json
import math
from pathlib import Path
import random
import re
from typing import Any

import pytest

from pet.bench import (
    BLINDING_SEED,
    BenchResult,
    build_system_prompt,
    calculate_length_statistics,
    check_output,
    commentary_length_statistics,
    render_report,
    run_bench,
    write_report,
)
from pet.commentary_templates import COMMENTARY_TEMPLATES
from pet.llm import LlmError, LlmResult, LlmUsage

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_event_samples.json"


class FakeLlmClient:
    """Deterministic injectable client with an optional one-call failure."""

    def __init__(self, *, fail_call: int | None = None) -> None:
        self.fail_call = fail_call
        self.calls: list[tuple[str, str]] = []

    def complete(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> LlmResult:
        del max_tokens, temperature
        self.calls.append((system_prompt, user_prompt))
        call_number = len(self.calls)
        if call_number == self.fail_call:
            raise LlmError("injected failure", status_code=503, latency_seconds=0.25)
        return LlmResult(
            text=f"第{call_number}次模型输出",
            usage=LlmUsage(
                prompt_tokens=100 + call_number,
                completion_tokens=8,
                cost_usd=0.001,
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


def test_bare_and_styled_prompts_share_rules_but_only_styled_has_samples() -> None:
    sample = "这下不算白给了"
    bare = build_system_prompt(
        "bare",
        personality_style="brother",
        category="kill",
        max_chars=18,
        community_lore="测试梗库",
    )
    styled = build_system_prompt(
        "styled",
        personality_style="brother",
        category="kill",
        max_chars=18,
        community_lore="测试梗库",
    )

    for prompt in (bare, styled):
        assert "只输出一句话本身" in prompt
        assert "最多包含 18 个汉字" in prompt
        assert "不得出现任何地图点位称呼" in prompt
        assert "不得暗示是玩家本人执行" in prompt
        assert "不强制使用人称" in prompt
    assert sample not in bare
    assert "测试梗库" not in bare
    assert sample in styled
    assert "测试梗库" in styled
    assert all(
        re.sub(r"\{[^{}]+\}", "", template.text).strip() in styled
        for template in COMMENTARY_TEMPLATES["brother"]["kill"]
    )


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


def test_factual_output_checks_identify_all_three_violations() -> None:
    checks = check_output("去A点狠狠干草草草草草草", max_chars=4)

    assert checks.exceeds_max_chars is True
    assert checks.callout_terms == ("A点",)
    assert "草" in checks.raw_curses


def test_one_failed_call_does_not_interrupt_the_other_variant(
    real_recording: Path,
) -> None:
    client = FakeLlmClient(fail_call=1)

    result = run_bench(
        real_recording,
        model="vendor/model-under-test",
        personality_style="brother",
        client=client,
        max_events=20,
    )

    assert len(result.events) == 1
    assert len(client.calls) == 2
    first, second = result.events[0].attempts
    assert first.result is None
    assert first.error is not None and "injected failure" in first.error
    assert second.result is not None


def test_real_fixture_generates_report_with_seeded_blind_mapping(
    real_recording: Path,
    tmp_path: Path,
) -> None:
    result = run_bench(
        real_recording,
        model="vendor/model-under-test",
        personality_style="caster",
        client=FakeLlmClient(),
        max_events=20,
    )
    report = render_report(result)
    output_path = tmp_path / "report.md"
    write_report(result, output_path)

    assert isinstance(result, BenchResult)
    assert result.selected_event_count == 1
    assert len(result.events) == 1
    assert "- 模板：" in report
    assert "- 甲：" in report
    assert "- 乙：" in report
    assert "延迟：P50" in report
    assert "平均输入 token" in report
    assert "命中点位词" in report
    assert f"固定打乱种子：`{BLINDING_SEED}`" in report
    variants = ["bare", "styled"]
    random.Random(BLINDING_SEED).shuffle(variants)
    expected_mapping = f"甲 = `{variants[0]}`；乙 = `{variants[1]}`"
    assert report.rstrip().splitlines()[-1] == expected_mapping
    assert report.count("`bare`") == 1
    assert report.count("`styled`") == 1
    assert report == output_path.read_text(encoding="utf-8")
