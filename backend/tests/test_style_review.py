import pytest

from pet.core import gate as hard_gate_module
from pet.games.cs2.eval.bench import FactSentenceAuditCase
from pet.core.gate import MAX_CHINESE_CHARS, check_hard_violations, scene_tags
from pet.core.llm import LlmResult, LlmUsage
from pet.games.cs2.eval.style_review import (
    MAX_TOKENS,
    REASONING_EFFORT,
    StyleReview,
    render_style_review,
)


def test_scene_tags_reads_the_dedicated_line() -> None:
    fact_sentence = "de_nuke CT 2:2 追平\n【事件】击杀\n【过程】中期，用AK完成击杀\n【场景标签】对枪胜利、吃力击杀"

    assert scene_tags(fact_sentence) == "对枪胜利、吃力击杀"


def test_hard_checks_mark_length_unsupported_entities_and_bound_words() -> None:
    fact_sentence = "【事件】击杀\n【过程】用AK完成击杀\n【场景标签】无"
    text = "队友说黄色闪光，" + "啊" * (MAX_CHINESE_CHARS + 1)

    checks = check_hard_violations(text, fact_sentence=fact_sentence)

    assert checks.exceeds_30_chars is True
    assert checks.unsupported_terms == ("队友",)
    assert checks.binding_violations == (
        "黄色闪光（需要武器：AWP）",
        "黄色闪光（需要标签：狙击击杀）",
    )


def test_hard_checks_allow_bound_awp_phrase_when_fact_supports_it() -> None:
    fact_sentence = "【事件】击杀\n【过程】用AWP完成击杀\n【场景标签】狙击击杀"

    checks = check_hard_violations("这把黄色闪光真架住了", fact_sentence=fact_sentence)

    assert checks.binding_violations == ()


def test_hard_checks_bind_heavy_fire_phrase_to_misfire_death() -> None:
    text = "开了这么多枪没打死一个"
    ordinary_duel = "【事件】阵亡\n【过程】玩家阵亡，开火后没打过\n【场景标签】对枪输了"
    misfire_death = "【事件】阵亡\n【过程】玩家阵亡，开了这么多枪没打死\n【场景标签】马枪死、对枪输了"

    rejected = check_hard_violations(text, fact_sentence=ordinary_duel)
    accepted = check_hard_violations(text, fact_sentence=misfire_death)

    assert rejected.binding_violations == ("开了这么多枪没打死一个（需要标签：马枪死）",)
    assert accepted.binding_violations == ()


def test_product_vocabulary_binding_table_is_parsed_and_cached() -> None:
    bindings = hard_gate_module.VOCABULARY_BINDINGS

    assert bindings is not None
    assert len(bindings) == 28
    assert sum(binding.requirement_kind == "unmapped" for binding in bindings) == 1


@pytest.mark.parametrize("phrase", ("一枪头", "爆头线"))
def test_binding_table_can_require_an_event_type(phrase: str) -> None:
    unsupported = check_hard_violations(
        phrase,
        fact_sentence="【事件】击杀\n【场景标签】普通击杀",
    )
    headshot = check_hard_violations(
        phrase,
        fact_sentence="【事件】爆头击杀\n【场景标签】普通击杀",
    )
    assert unsupported.binding_violations
    assert headshot.binding_violations == ()


def test_specific_pellet_kill_row_remains_stricter_than_general_headshot_row() -> None:
    headshot_without_pellet_tag = check_hard_violations(
        "颗秒",
        fact_sentence="【事件】爆头击杀\n【场景标签】普通击杀",
    )
    pellet_kill = check_hard_violations(
        "颗秒",
        fact_sentence="【事件】击杀\n【场景标签】颗秒",
    )

    assert headshot_without_pellet_tag.binding_violations == (
        "颗秒（需要标签：颗秒）",
    )
    assert pellet_kill.binding_violations == ()


def test_revised_binding_rows_keep_positive_and_negative_flash_scenes_separate() -> None:
    positive = "【事件】击杀\n【场景标签】白着打"
    negative = "【事件】阵亡\n【场景标签】白着被打死"
    flash_bad_luck = "【事件】阵亡\n【场景标签】白惨了"

    assert check_hard_violations(
        "被白到死", fact_sentence=positive
    ).binding_violations
    assert check_hard_violations(
        "被白到死", fact_sentence=negative
    ).binding_violations == ()
    assert check_hard_violations(
        "白屏战神", fact_sentence=positive
    ).binding_violations
    assert check_hard_violations(
        "白屏战神", fact_sentence=flash_bad_luck
    ).binding_violations == ()


def test_revised_binding_rows_do_not_allow_misfire_or_zombie_terms() -> None:
    hard_kill = "【事件】击杀\n【场景标签】马完了"
    misfire_death = "【事件】阵亡\n【场景标签】马枪死"

    assert check_hard_violations("马枪", fact_sentence=hard_kill).binding_violations
    assert check_hard_violations(
        "马枪", fact_sentence=misfire_death
    ).binding_violations == ()
    assert check_hard_violations(
        "这走位像僵尸", fact_sentence="【事件】阵亡\n【场景标签】对枪输了"
    ).binding_violations == ("僵尸（不得使用）",)


def test_new_vocabulary_binding_row_takes_effect_without_code_change(
    tmp_path, monkeypatch
) -> None:
    vocabulary = tmp_path / "vocabulary.md"
    vocabulary.write_text(
        "# 用词绑定（说错了就是事实错误）\n\n"
        "| 说法 | 只能用在 | 需要的标签 |\n"
        "|---|---|---|\n"
        "| 测试暗号 | 只用于对枪胜利 | 对枪胜利 |\n",
        encoding="utf-8",
    )
    bindings = hard_gate_module._load_vocabulary_bindings(vocabulary)
    assert bindings is not None
    monkeypatch.setattr(hard_gate_module, "VOCABULARY_BINDINGS", bindings)

    rejected = check_hard_violations(
        "测试暗号", fact_sentence="【事件】击杀\n【场景标签】普通击杀"
    )
    accepted = check_hard_violations(
        "测试暗号", fact_sentence="【事件】击杀\n【场景标签】对枪胜利"
    )

    assert rejected.binding_violations == ("测试暗号（需要标签：对枪胜利）",)
    assert accepted.binding_violations == ()


def test_invalid_vocabulary_table_logs_error_and_uses_legacy_fallback(
    tmp_path, monkeypatch, caplog
) -> None:
    vocabulary = tmp_path / "vocabulary.md"
    vocabulary.write_text("# 用词绑定（说错了就是事实错误）\n表坏了\n", encoding="utf-8")
    bindings = hard_gate_module._load_vocabulary_bindings(vocabulary)
    assert bindings is None
    monkeypatch.setattr(hard_gate_module, "VOCABULARY_BINDINGS", bindings)

    checks = check_hard_violations(
        "这波白给", fact_sentence="【事件】阵亡\n【场景标签】对枪输了"
    )

    assert "missing three-column vocabulary binding header" in caplog.text
    assert checks.binding_violations == ("白给说法（事实非白给）",)


@pytest.mark.parametrize("requirement", ("不存在的标签", "事件:不存在的事件"))
def test_unknown_binding_requirement_rejects_the_entire_table(
    requirement: str, tmp_path, caplog
) -> None:
    vocabulary = tmp_path / "vocabulary.md"
    vocabulary.write_text(
        "# 用词绑定（说错了就是事实错误）\n\n"
        "| 说法 | 只能用在 | 需要的标签 |\n"
        "|---|---|---|\n"
        f"| 测试暗号 | 不供机器读取 | {requirement} |\n",
        encoding="utf-8",
    )

    assert hard_gate_module._load_vocabulary_bindings(vocabulary) is None
    assert "unknown requirements" in caplog.text


def test_binding_parser_supports_escaped_pipes_and_prose_continuations() -> None:
    bindings = hard_gate_module._parse_vocabulary_bindings(
        "# 用词绑定（说错了就是事实错误）\n\n"
        "| 说法 | 只能用在 | 需要的标签 |\n"
        "|---|---|---|\n"
        "| 测试暗号 | 这列自然语言续行；\\\n"
        "机器不得解释 | 马枪死\\|白给 |\n"
    )

    assert len(bindings) == 1
    assert bindings[0].terms == ("测试暗号",)
    assert bindings[0].requirement_values == ("马枪死", "白给")
    assert not hasattr(bindings[0], "human_condition")


def test_noncontiguous_binding_rows_reject_the_entire_table() -> None:
    with pytest.raises(ValueError, match="appears after the table ended"):
        hard_gate_module._parse_vocabulary_bindings(
            "# 用词绑定（说错了就是事实错误）\n\n"
            "| 说法 | 只能用在 | 需要的标签 |\n"
            "|---|---|---|\n"
            "| 第一条 | 不供机器读取 | 白给 |\n\n"
            "| 第二条 | 不供机器读取 | 马枪死 |\n"
        )


def test_hard_checks_mark_shortened_economy_tier_rewrite() -> None:
    fact_sentence = "de_nuke T 2:5 落后 强起局\n【事件】阵亡\n【过程】玩家阵亡\n【场景标签】白给"

    checks = check_hard_violations("全装白给，寄！", fact_sentence=fact_sentence)

    assert checks.economy_tier_rewrite is True


def test_render_style_review_contains_raw_outputs_without_a_score() -> None:
    case = FactSentenceAuditCase(
        case_id="case",
        fact_sentence="【事件】击杀\n【过程】用AK完成击杀\n【场景标签】无",
        model_card="unused",
        required_facts=(),
        forbidden_claims=(),
    )
    result = LlmResult(
        text="好枪",
        usage=LlmUsage(prompt_tokens=100, completion_tokens=2, cost_usd=0.001),
        latency_seconds=0.2,
        model="model",
        provider="Alibaba",
    )
    from pet.games.cs2.eval.style_review import StyleAttempt, StyleCaseReview

    attempt = StyleAttempt(
        result=result,
        error=None,
        checks=check_hard_violations(result.text, fact_sentence=case.fact_sentence),
    )
    report = render_style_review(
        StyleReview(
            prompt="prompt",
            prompt_sha256="abc",
            cases=(StyleCaseReview(case=case, hot=attempt, cold=attempt),),
        )
    )

    assert "宠物说（温度0.9）：好枪" in report
    assert "自动打分" in report
    assert "评分：" not in report


def test_style_review_uses_diagnostic_reasoning_and_token_limits() -> None:
    assert REASONING_EFFORT == "none"
    assert MAX_TOKENS == 256
