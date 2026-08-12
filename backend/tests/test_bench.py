"""Offline benchmark tests driven by existing scrubbed GSI recordings."""

import json
import math
from pathlib import Path
import re
from typing import Any

import pytest

from pet.bench import (
    ANALYSIS_MAX_EVENT_CHARS,
    AnalysisBenchResult,
    AnalysisScoreFile,
    BenchResult,
    CaseScore,
    FieldScore,
    UniversalForbiddenFile,
    apply_universal_forbidden,
    answer_key_sha256,
    build_analysis_system_prompt,
    calculate_length_statistics,
    check_output,
    commentary_length_statistics,
    load_analysis_scores,
    load_event_answer_keys,
    load_universal_forbidden,
    main,
    render_latency_report,
    render_report,
    render_scored_analysis_report,
    render_stream_analysis_report,
    rescore_existing_analysis_report,
    run_bench,
    run_latency_experiment,
    run_stream_analysis,
    write_analysis_score_template,
    write_report,
    _score_summary_lines_for_ids,
)
from pet.commentary_templates import COMMENTARY_TEMPLATES
from pet.llm import LlmAnalysisResult, LlmError, LlmResult, LlmUsage
from pet.prompt import load_system_prompt

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "gsi_event_samples.json"
ANSWER_KEY_PATH = (
    Path(__file__).parents[1]
    / "bench-reports"
    / "m3-t5.6-event-answer-keys.json"
)
UNIVERSAL_FORBIDDEN_PATH = (
    Path(__file__).parents[1]
    / "bench-reports"
    / "m3-t5.9-universal-forbidden.json"
)


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


class FakeAnalysisClient:
    """Deterministic streamed-analysis client used without a network."""

    def __init__(
        self,
        *,
        fail: bool = False,
        audit_text: str = "类型=爆头击杀；方式=无；武器=AK47",
        event_text: str = "爆头击杀",
        scene_text: str = "掉血后紧接着完成击杀。当前比分仍然落后。",
    ) -> None:
        self.fail = fail
        self.audit_text = audit_text
        self.event_text = event_text
        self.scene_text = scene_text
        self.calls: list[
            tuple[
                str | None, str, str, int, float, float, float, int | None, str
            ]
        ] = []

    def analyze_stream(
        self,
        *,
        model: str,
        provider: str | None = None,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        event_timeout_seconds: float,
        full_timeout_seconds: float,
        seed: int | None = None,
        reasoning_effort: str = "none",
    ) -> LlmAnalysisResult:
        self.calls.append(
            (
                provider,
                system_prompt,
                user_prompt,
                max_tokens,
                temperature,
                event_timeout_seconds,
                full_timeout_seconds,
                seed,
                reasoning_effort,
            )
        )
        if self.fail:
            raise LlmError(
                "injected stream failure",
                latency_seconds=0.25,
                partial_event_text="回合胜利",
                event_latency_seconds=0.15,
            )
        return LlmAnalysisResult(
            audit_text=self.audit_text,
            event_text=self.event_text,
            scene_text=self.scene_text,
            usage=LlmUsage(prompt_tokens=200, completion_tokens=30, cost_usd=0.002),
            event_latency_seconds=0.4,
            latency_seconds=0.8,
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

    assert prompt.startswith("你会收到一份 CS2 的 GSI 事件卡")
    assert prompt.index("## 卡片结构") < prompt.index("你是观战朋友打 CS2")
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


def test_stream_analysis_uses_strict_protocol_settings_and_split_metrics(
    real_recording: Path,
    prompts_directory: Path,
) -> None:
    client = FakeAnalysisClient()

    result = run_stream_analysis(
        (real_recording,),
        model="vendor/model-under-test",
        provider="provider-under-test",
        client=client,
        max_events=20,
        event_timeout_seconds=10.0,
        full_timeout_seconds=10.0,
        seed=43,
        prompts_directory=prompts_directory,
    )
    report = render_stream_analysis_report(result)

    assert isinstance(result, AnalysisBenchResult)
    assert result.selected_event_count == 1
    assert len(result.events) == 1
    assert len(client.calls) == 1
    (
        provider,
        prompt,
        card,
        max_tokens,
        temperature,
        event_timeout,
        full_timeout,
        seed,
        reasoning_effort,
    ) = client.calls[0]
    assert provider == "provider-under-test"
    assert prompt == "共享读卡指南\n\n只复述事实，不限制字数"
    assert card == result.events[0].event_card
    assert max_tokens == 128
    assert temperature == pytest.approx(0.0)
    assert ANALYSIS_MAX_EVENT_CHARS == 30
    assert event_timeout == pytest.approx(10.0)
    assert full_timeout == pytest.approx(10.0)
    assert seed == 43
    assert reasoning_effort == "none"
    assert "事件：爆头击杀" in report
    assert "核对：类型=爆头击杀；方式=无；武器=AK47" in report
    assert "场面：掉血后紧接着完成击杀" in report
    assert "事件延迟：P50 0.400s" in report
    assert "完整延迟：P50 0.800s" in report
    assert "提示词 SHA-256" in report
    assert "事件卡集合 SHA-256" in report
    assert "固定随机种子：43" in report
    assert "提示词变体：A：骨架逐字复制" in report
    assert "否定总结 ✓" in report
    assert "越界措辞 ✓" in report


def test_stream_analysis_flags_known_unsupported_inference_phrases(
    real_recording: Path,
    prompts_directory: Path,
) -> None:
    result = run_stream_analysis(
        (real_recording,),
        model="vendor/model-under-test",
        provider="provider-under-test",
        client=FakeAnalysisClient(
            event_text="灭队导致回合失败",
            scene_text="我方连续击杀三人。玩家换回AK47后被击中。",
        ),
        max_events=20,
        event_timeout_seconds=3.0,
        full_timeout_seconds=6.0,
        prompts_directory=prompts_directory,
    )

    checks = result.events[0].attempt.checks
    assert checks is not None
    assert "导致" in checks.unsupported_inference_terms
    assert "换回" in checks.unsupported_inference_terms
    assert "被击中" in checks.unsupported_inference_terms
    assert any(term.startswith("我方连续击杀") for term in checks.unsupported_inference_terms)
    assert "越界措辞 ✗" in render_stream_analysis_report(result)


def test_stream_analysis_failure_is_recorded_once(
    real_recording: Path,
    prompts_directory: Path,
) -> None:
    client = FakeAnalysisClient(fail=True)

    result = run_stream_analysis(
        (real_recording,),
        model="vendor/model-under-test",
        provider="provider-under-test",
        client=client,
        max_events=20,
        event_timeout_seconds=3.0,
        full_timeout_seconds=6.0,
        prompts_directory=prompts_directory,
    )

    assert len(client.calls) == 1
    assert result.events[0].attempt.result is None
    assert "injected stream failure" in (result.events[0].attempt.error or "")
    assert result.events[0].attempt.partial_event_text == "回合胜利"
    report = render_stream_analysis_report(result)
    assert "已收到事件：回合胜利" in report
    assert "按时收到事件行：1" in report
    assert "事件延迟：P50 0.150s" in report


def test_stream_analysis_requires_a_locked_provider(
    real_recording: Path,
    prompts_directory: Path,
) -> None:
    client = FakeAnalysisClient()

    with pytest.raises(ValueError, match="requires a locked provider"):
        run_stream_analysis(
            (real_recording,),
            model="vendor/model-under-test",
            provider=None,
            client=client,
            max_events=20,
            event_timeout_seconds=3.0,
            full_timeout_seconds=6.0,
            prompts_directory=prompts_directory,
        )

    assert client.calls == []


def test_human_score_template_round_trips_and_reports_both_thresholds(
    real_recording: Path,
    prompts_directory: Path,
    tmp_path: Path,
) -> None:
    result = run_stream_analysis(
        (real_recording,),
        model="vendor/model-under-test",
        provider="provider-under-test",
        client=FakeAnalysisClient(),
        max_events=20,
        event_timeout_seconds=3.0,
        full_timeout_seconds=6.0,
        prompts_directory=prompts_directory,
    )
    template_path = tmp_path / "scores.json"
    write_analysis_score_template(result, template_path)
    template = load_analysis_scores(template_path)
    assert template.cases[0].event.errors == ("未评分",)

    case_id = result.events[0].case_id
    passing = AnalysisScoreFile(
        cases=(
            CaseScore(
                case_id=case_id,
                event=FieldScore(
                    whole_correct=True,
                    correct_atoms=2,
                    total_atoms=2,
                ),
                scene=FieldScore(
                    whole_correct=True,
                    correct_atoms=4,
                    total_atoms=4,
                ),
            ),
        )
    )
    report = render_stream_analysis_report(result, passing)

    assert "事件：整句 100.0% ✓；原子事实 100.0%（2/2） ✓" in report
    assert "场面：整句 100.0% ✓；原子事实 100.0%（4/4） ✓" in report

    with pytest.raises(ValueError, match="correct_atoms 不能超过 total_atoms"):
        FieldScore(
            whole_correct=False,
            correct_atoms=2,
            total_atoms=1,
        )


def test_event_whole_accuracy_target_is_ninety_five_percent() -> None:
    case_ids = tuple(f"case-{index}" for index in range(20))

    def score_file(correct_cases: int) -> AnalysisScoreFile:
        return AnalysisScoreFile(
            cases=tuple(
                CaseScore(
                    case_id=case_id,
                    event=FieldScore(
                        whole_correct=index < correct_cases,
                        correct_atoms=1,
                        total_atoms=1,
                    ),
                    scene=FieldScore(
                        whole_correct=True,
                        correct_atoms=1,
                        total_atoms=1,
                    ),
                )
                for index, case_id in enumerate(case_ids)
            )
        )

    passing = _score_summary_lines_for_ids(case_ids, score_file(19))
    failing = _score_summary_lines_for_ids(case_ids, score_file(18))

    assert "事件：整句 95.0% ✓" in "\n".join(passing)
    assert "事件：整句 90.0% ✗" in "\n".join(failing)


def test_product_event_answer_key_contains_exactly_the_28_stable_cases() -> None:
    answer_keys = load_event_answer_keys(ANSWER_KEY_PATH)
    expected_ids = (
        "gsi-20260809-112213:001:kill:r6",
        "gsi-20260809-112213:002:multi_kill:r6",
        "gsi-20260809-112213:003:multi_kill:r6",
        "gsi-20260809-112213:004:round_win:r6",
        "gsi-20260809-121524-745957:001:kill_headshot:r1",
        "gsi-20260809-121524-745957:002:death_after_kill:r2",
        "gsi-20260809-121524-745957:003:round_win:r2",
        "gsi-20260810-114649-321103:001:kill_headshot:r2",
        "gsi-20260810-114649-321103:002:multi_kill:r2",
        "gsi-20260810-114649-321103:003:round_loss:r2",
        "gsi-20260810-114649-321103:004:kill:r3",
        "gsi-20260810-114649-321103:005:death:r3",
        "gsi-20260810-114649-321103:006:kill:r4",
        "gsi-20260810-114649-321103:007:round_loss:r4",
        "gsi-20260810-114649-321103:008:death:r5",
        "gsi-20260810-154052-044137:001:round_win:r1",
        "gsi-20260810-154052-044137:002:kill:r2",
        "gsi-20260810-154052-044137:003:multi_kill:r2",
        "gsi-20260810-154052-044137:004:round_loss:r2",
        "gsi-20260810-154052-044137:005:round_win:r3",
        "gsi-20260810-154052-044137:006:kill_headshot:r4",
        "gsi-20260810-154052-044137:007:round_win:r4",
        "gsi-20260810-154052-044137:008:kill:r5",
        "gsi-20260810-175001-250246:001:death:r2",
        "gsi-20260810-175001-250246:002:round_loss:r2",
        "gsi-20260810-175001-250246:003:kill:r3",
        "gsi-20260810-175001-250246:004:multi_kill:r3",
        "gsi-20260810-175001-250246:005:death:r3",
    )

    assert tuple(case.case_id for case in answer_keys.cases) == expected_ids
    assert all(case.expected_summary.strip() for case in answer_keys.cases)
    assert all(case.required_facts for case in answer_keys.cases)


def test_event_only_scores_bind_answer_key_and_omit_scene_metric(
    real_recording: Path,
    prompts_directory: Path,
    tmp_path: Path,
) -> None:
    result = run_stream_analysis(
        (real_recording,),
        model="vendor/model-under-test",
        provider="provider-under-test",
        client=FakeAnalysisClient(),
        max_events=20,
        event_timeout_seconds=3.0,
        full_timeout_seconds=6.0,
        prompts_directory=prompts_directory,
    )
    digest = answer_key_sha256(ANSWER_KEY_PATH)
    template_path = tmp_path / "event-only-scores.json"
    write_analysis_score_template(
        result,
        template_path,
        scope="event_only",
        answer_key_digest=digest,
    )
    template = load_analysis_scores(template_path)

    assert template.scope == "event_only"
    assert template.answer_key_sha256 == digest
    assert template.cases[0].scene is None

    passing = AnalysisScoreFile(
        scope="event_only",
        answer_key_sha256=digest,
        cases=(
            CaseScore(
                case_id=result.events[0].case_id,
                event=FieldScore(
                    whole_correct=True,
                    correct_atoms=3,
                    total_atoms=3,
                ),
            ),
        ),
    )
    report = render_stream_analysis_report(result, passing)

    assert "评分范围：仅事件短句" in report
    assert f"Answer key SHA-256：`{digest}`" in report
    assert "事件：整句 100.0% ✓；原子事实 100.0%（3/3） ✓" in report
    assert "场面：整句" not in report

    with pytest.raises(ValueError, match="必须绑定 answer key"):
        AnalysisScoreFile(
            scope="event_only",
            cases=passing.cases,
        )


def test_existing_analysis_report_can_be_scored_without_rerunning_model(
    real_recording: Path,
    prompts_directory: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = run_stream_analysis(
        (real_recording,),
        model="vendor/model-under-test",
        provider="provider-under-test",
        client=FakeAnalysisClient(),
        max_events=20,
        event_timeout_seconds=3.0,
        full_timeout_seconds=6.0,
        prompts_directory=prompts_directory,
    )
    original_report = render_stream_analysis_report(result)
    report_path = tmp_path / "analysis.md"
    report_path.write_text(original_report, encoding="utf-8")
    case_id = result.events[0].case_id
    score_path = tmp_path / "scores.json"
    score_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": case_id,
                        "event": {
                            "whole_correct": True,
                            "correct_atoms": 2,
                            "total_atoms": 2,
                        },
                        "scene": {
                            "whole_correct": False,
                            "correct_atoms": 3,
                            "total_atoms": 4,
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "scored.md"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    exit_code = main(
        [
            "--score-report",
            str(report_path),
            "--scores",
            str(score_path),
            "--out",
            str(output_path),
        ]
    )

    assert exit_code == 0
    scored = output_path.read_text(encoding="utf-8")
    assert scored.startswith(original_report.rstrip("\n"))
    assert "事件：整句 100.0% ✓；原子事实 100.0%（2/2） ✓" in scored
    assert "场面：整句 0.0% ✗；原子事实 75.0%（3/4） ✗" in scored

    loaded = load_analysis_scores(score_path)
    with pytest.raises(ValueError, match="已经包含人工事实评分"):
        render_scored_analysis_report(scored, loaded)


def test_universal_forbidden_file_has_both_factual_categories() -> None:
    forbidden = load_universal_forbidden(UNIVERSAL_FORBIDDEN_PATH)

    assert "队友" in forbidden.a_gsi_unavailable
    assert "对手的钱" in forbidden.a_gsi_unavailable
    assert "A点" in forbidden.a_gsi_unavailable
    assert "B点" in forbidden.a_gsi_unavailable
    assert "中路" in forbidden.a_gsi_unavailable
    assert "包点" not in forbidden.a_gsi_unavailable
    assert "可能" in forbidden.b_inference_or_causality
    assert "导致" in forbidden.b_inference_or_causality
    assert len(forbidden.terms) == len(set(forbidden.terms))


def test_analysis_prompt_variants_change_only_the_skeleton_contract() -> None:
    baseline = build_analysis_system_prompt(variant="baseline")
    checklist = build_analysis_system_prompt(variant="checklist")
    personality = build_analysis_system_prompt(variant="checklist_personality")

    assert "事件行逐字复制 X" in baseline
    assert "推断评测必须原样复制" in baseline
    assert "事件行逐字复制 X" not in checklist
    assert "推断评测必须原样复制" not in checklist
    assert "下列事实必须全部出现，措辞由你决定" in checklist
    assert "事件必答覆盖规则" not in baseline
    assert "事件必答覆盖规则" in checklist
    assert "被闪/烟雾/燃烧" in checklist
    assert "不要把未列入【事件必答】的旁支事件自行升级成必答项" in checklist
    assert "已按保留优先级从左到右排列" in checklist
    assert "不得删除罕见关系、武器、最终多杀数、“有显著贡献”或回合结果" in checklist
    assert "事件必答覆盖规则" in personality
    assert personality.startswith(
        "你是陪朋友打 CS2 的中文游戏搭子，说话短、随口、像个懂行的老玩家。\n"
        "可以损但不刻薄。不要用书面语，不要像解说员报幕。\n"
        "但下面的事实要求高于一切：宁可说得平淡，也不能说错或说出卡上没有的事。\n\n"
    )
    assert personality.endswith(checklist)
    assert len({baseline, checklist, personality}) == 3


def test_universal_forbidden_hit_forces_whole_sentence_wrong() -> None:
    scores = AnalysisScoreFile(
        scope="event_only",
        answer_key_sha256="0" * 64,
        cases=(
            CaseScore(
                case_id="case-a",
                event=FieldScore(
                    whole_correct=True,
                    correct_atoms=3,
                    total_atoms=3,
                ),
            ),
        ),
    )
    report = """# report

### 1. `case-a`

事件：可能有队友掩护，完成击杀
场面：省略
"""
    forbidden = UniversalForbiddenFile(
        a_gsi_unavailable=("队友", "掩护"),
        b_inference_or_causality=("可能",),
    )

    adjusted, violations = apply_universal_forbidden(report, scores, forbidden)

    assert adjusted.cases[0].event.whole_correct is False
    assert adjusted.cases[0].event.correct_atoms == 3
    assert adjusted.cases[0].event.errors == ("通用禁项：队友、掩护、可能",)
    assert violations[0].matched_terms == ("队友", "掩护", "可能")


def test_universal_forbidden_rescore_is_offline_and_preserves_clean_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = answer_key_sha256(ANSWER_KEY_PATH)
    report_path = tmp_path / "existing.md"
    report_path.write_text(
        "### 1. `case-clean`\n\n事件：前期用AK47完成爆头击杀\n场面：省略\n",
        encoding="utf-8",
    )
    scores_path = tmp_path / "scores.json"
    scores_path.write_text(
        json.dumps(
            {
                "scope": "event_only",
                "answer_key_sha256": digest,
                "cases": [
                    {
                        "case_id": "case-clean",
                        "event": {
                            "whole_correct": True,
                            "correct_atoms": 3,
                            "total_atoms": 3,
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    result = rescore_existing_analysis_report(
        report_path,
        scores_path,
        load_universal_forbidden(UNIVERSAL_FORBIDDEN_PATH),
        expected_answer_key_sha256=digest,
    )

    assert result.violations == ()
    assert result.adjusted_scores.cases[0].event.whole_correct is True
