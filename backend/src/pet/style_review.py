"""Run the M3-T9 two-temperature style review without scoring taste."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import statistics

from pet.bench import FactSentenceAuditCase
from pet.fact_sentence_audit import collect_fact_sentence_audit_cases
from pet.llm import LlmClientProtocol, LlmError, LlmResult, OpenRouterClient
from pet.prompt import load_system_prompt

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT_PATH = BACKEND_ROOT / "bench-reports" / "m3-t9.6-style-review.md"
MODEL = "qwen/qwen3.5-122b-a10b"
PROVIDER = "Alibaba"
TEMPERATURES = (0.9, 0.0)
SEED = 42
TIMEOUT_SECONDS = 10.0
# M3-T9-FIX B group: with explicit reasoning=none and 256 output tokens, all
# five diagnostic calls reached a final 8–13-token Chinese sentence. 48 tokens
# had instead been consumed by an English Thinking Process and truncated.
MAX_TOKENS = 256
REASONING_EFFORT = "none"
MAX_CHINESE_CHARS = 30
_HAN_PATTERN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")

# These terms cover the prohibited entity categories for the hard, literal check.
# They intentionally do not infer style or causal exaggeration; that remains manual review.
UNSUPPORTED_TERMS: tuple[str, ...] = (
    "队友",
    "队友们",
    "队伍",
    "对面",
    "对手",
    "敌人",
    "敌方",
    "土匪",
    "警察",
    "A点",
    "B点",
    "中路",
    "香蕉道",
    "二楼",
    "连接",
    "包点",
    "伤害来源",
)

# These scenarios exercise round-result/history facts.  Round outcomes remain
# deliberately non-speech events, so keeping them in a style-only set would
# test a chain that production never calls.
_ROUND_RESULT_STYLE_EXCLUSIONS = frozenset(
    {
        "rare_mvp_round_win",
        "rare_assist_round_win",
        "rare_grenade_pickup",
        "rare_primary_switch",
        "postplant_defuse_win",
        "postplant_counterattack_loss",
        "postplant_triple_loss",
        "late_defuse",
        "bomb_explosion_win",
    }
)


@dataclass(frozen=True, slots=True)
class HardChecks:
    """Mechanical checks that deliberately avoid judging prose quality."""

    chinese_char_count: int
    exceeds_30_chars: bool
    unsupported_terms: tuple[str, ...]
    binding_violations: tuple[str, ...]
    economy_tier_rewrite: bool
    eco_called_pistol_round: bool


@dataclass(frozen=True, slots=True)
class StyleAttempt:
    """One unfiltered call result or its single upstream failure."""

    result: LlmResult | None
    error: str | None
    checks: HardChecks | None


@dataclass(frozen=True, slots=True)
class StyleCaseReview:
    """One frozen case and its two otherwise identical calls."""

    case: FactSentenceAuditCase
    hot: StyleAttempt
    cold: StyleAttempt


@dataclass(frozen=True, slots=True)
class StyleReview:
    """The complete comparison, with no automatic taste score."""

    prompt: str
    prompt_sha256: str
    cases: tuple[StyleCaseReview, ...]


def chinese_character_count(text: str) -> int:
    """Count Han characters for the task's visible output limit."""
    return len(_HAN_PATTERN.findall(text))


def scene_tags(fact_sentence: str) -> str:
    """Extract the rendered tags without treating them as the model's facts."""
    for line in fact_sentence.splitlines():
        if line.startswith("【场景标签】"):
            value = line.removeprefix("【场景标签】").strip()
            return value or "无"
    return "无"


def check_hard_violations(text: str, *, fact_sentence: str) -> HardChecks:
    """Mark only explicit policy violations; do not grade style or inventiveness."""
    return HardChecks(
        chinese_char_count=chinese_character_count(text),
        exceeds_30_chars=chinese_character_count(text) > MAX_CHINESE_CHARS,
        unsupported_terms=tuple(term for term in UNSUPPORTED_TERMS if term in text),
        binding_violations=_binding_violations(text, fact_sentence=fact_sentence),
        economy_tier_rewrite=_economy_tier_rewrite(text, fact_sentence),
        eco_called_pistol_round="eco局" in fact_sentence and "手枪局" in text,
    )


def _economy_tier_rewrite(text: str, fact_sentence: str) -> bool:
    """Catch only explicit economy-tier substitutions, never infer economy."""
    tiers = {
        "eco局": ("eco",),
        "强起局": ("强起",),
        "全装局": ("全装",),
    }
    expected = next((tier for tier in tiers if tier in fact_sentence), None)
    return expected is not None and any(
        alias in text
        for tier, aliases in tiers.items()
        if tier != expected
        for alias in aliases
    )


def _binding_violations(text: str, *, fact_sentence: str) -> tuple[str, ...]:
    """Apply every literal binding row from vocabulary.md's final table."""
    lower = text.lower()
    facts = fact_sentence.lower()
    violations: list[str] = []
    if _contains_any(text, ("黄色闪光", "架住了")) and "awp" not in facts:
        violations.append("黄色闪光/架住了（非 AWP 打得好）")
    if _contains_any(text, ("烧火棍", "放炮", "刘农")) and "大狙空枪" not in fact_sentence:
        violations.append("烧火棍/放炮/刘农（非 AWP 空枪）")
    if _contains_any(text, ("扫射转移", "钢铁般的左键", "压住了")) and not _contains_any(
        facts, ("ak", "m4", "galil", "famas", "aug", "sg", "mp", "ump", "p90", "bizon")
    ):
        violations.append("自动武器说法（非可见自动武器）")
    if "沙鹰王" in text and "deagle" not in facts:
        violations.append("沙鹰王（非沙鹰）")
    if _contains_any(text, ("跑打仙人", "糊脸")) and not _contains_any(
        facts, ("mp", "ump", "p90", "bizon", "mac-10")
    ):
        violations.append("冲锋枪说法（非冲锋枪）")
    if _contains_any(text, ("颗秒", "一枪头", "爆头线")) and not _contains_any(
        fact_sentence, ("爆头", "秒了")
    ):
        violations.append("爆头/极少用弹说法（事实不支持）")
    if "一换一" in text and "被补" not in fact_sentence:
        violations.append("一换一（非刚杀完即被补）")
    if _contains_any(text, ("白给", "欢乐送", "全装秒躺")) and "白给" not in fact_sentence:
        violations.append("白给说法（事实非白给）")
    if _contains_any(text, ("马枪", "人体描边", "马完了")) and "马枪死" not in fact_sentence:
        violations.append("马枪说法（事实非大量开火未中）")
    if "武器大师" in text and "换枪后立刻杀" not in fact_sentence:
        violations.append("武器大师（非换枪后击杀）")
    if _contains_any(text, ("烤猪", "火中做自己")) and not _contains_any(
        fact_sentence, ("踩火杀", "烧惨了")
    ):
        violations.append("着火说法（事实非着火）")
    if _contains_any(text, ("白屏战神", "被白到死")) and not _contains_any(
        fact_sentence, ("白着打", "白着被打死", "白惨了")
    ):
        violations.append("闪光说法（事实非被闪）")
    if "doom来了" in lower and "踩火杀" not in fact_sentence:
        violations.append("doom 来了（非着火击杀）")
    if _contains_any(text, ("僵尸猪人", "大表猪play")) and "出烟就没了" not in fact_sentence:
        violations.append("僵尸猪人/大表猪play（非出烟死亡）")
    if _contains_any(text, ("大表哥play", "karrigan play")) and "摸烟击杀" not in fact_sentence:
        violations.append("大表哥play（非摸烟击杀）")
    # The final “僵尸” row needs movement/position data, which GSI fact sentences
    # deliberately lack. It is recorded as manually reviewable rather than guessed.
    return tuple(violations)


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(term.lower() in text.lower() for term in terms)


def run_style_review(client: LlmClientProtocol) -> StyleReview:
    """Call each frozen fact sentence once at each requested temperature."""
    prompt = load_system_prompt("inference", max_chars=30)
    cases = tuple(
        case
        for case in collect_fact_sentence_audit_cases()
        if case.case_id not in _ROUND_RESULT_STYLE_EXCLUSIONS
    )
    reviews: list[StyleCaseReview] = []
    for case in cases:
        hot = _attempt(client, prompt, case, temperature=TEMPERATURES[0])
        cold = _attempt(client, prompt, case, temperature=TEMPERATURES[1])
        reviews.append(StyleCaseReview(case=case, hot=hot, cold=cold))
    return StyleReview(
        prompt=prompt,
        prompt_sha256=sha256(prompt.encode("utf-8")).hexdigest(),
        cases=tuple(reviews),
    )


def _attempt(
    client: LlmClientProtocol,
    prompt: str,
    case: FactSentenceAuditCase,
    *,
    temperature: float,
) -> StyleAttempt:
    try:
        result = client.complete(
            model=MODEL,
            provider=PROVIDER,
            system_prompt=prompt,
            user_prompt=case.fact_sentence,
            max_tokens=MAX_TOKENS,
            temperature=temperature,
            seed=SEED,
            reasoning_effort=REASONING_EFFORT,
        )
    except LlmError as error:
        return StyleAttempt(result=None, error=str(error), checks=None)
    return StyleAttempt(
        result=result,
        error=None,
        checks=check_hard_violations(result.text, fact_sentence=case.fact_sentence),
    )


def render_style_review(review: StyleReview) -> str:
    """Render unfiltered outputs and hard flags, intentionally without scoring taste."""
    attempts = [attempt for case in review.cases for attempt in (case.hot, case.cold)]
    prompt_tokens = [
        attempt.result.usage.prompt_tokens
        for attempt in attempts
        if attempt.result is not None and attempt.result.usage.prompt_tokens is not None
    ]
    latencies = [
        attempt.result.latency_seconds
        for attempt in attempts
        if attempt.result is not None
    ]
    costs = [
        attempt.result.usage.cost_usd
        for attempt in attempts
        if attempt.result is not None and attempt.result.usage.cost_usd is not None
    ]
    length_hits = sum(
        bool(attempt.checks and attempt.checks.exceeds_30_chars) for attempt in attempts
    )
    unsupported_hits = sum(
        bool(attempt.checks and attempt.checks.unsupported_terms) for attempt in attempts
    )
    binding_hits = sum(
        bool(attempt.checks and attempt.checks.binding_violations) for attempt in attempts
    )
    economy_rewrite_hits = sum(
        bool(attempt.checks and attempt.checks.economy_tier_rewrite)
        for attempt in attempts
    )
    eco_pistol_hits = sum(
        bool(attempt.checks and attempt.checks.eco_called_pistol_round)
        for attempt in attempts
    )
    lines = [
        "# M3-T9.6 文风层双温度评测",
        "",
        "- 模型：`qwen/qwen3.5-122b-a10b`；上游锁定：`Alibaba`。",
        "- 温度：0.9 / 0；种子：42；单次超时：10 秒；"
        f"reasoning_effort：`{REASONING_EFFORT}`；输出上限：{MAX_TOKENS} tokens。",
        f"- 题数：{len(review.cases)}；调用数：{len(attempts)}（每题两个温度，各一次且不重试）。",
        f"- 提示词 SHA-256（两组相同）：`{review.prompt_sha256}`。",
        "- 实际输入 token 中位数："
        + (_format_number(statistics.median(prompt_tokens)) if prompt_tokens else "上游未返回")
        + "（M3-T8.15 的无 tokenizer 估算为 2634）。",
        "- 事件 P95 延迟："
        + (_format_seconds(_percentile(latencies, 0.95)) if latencies else "无成功调用")
        + "。",
        "- 总花费："
        + (f"${sum(costs):.6f}" if costs else "上游未返回")
        + "。",
        "- 硬性检查命中（输出条数）："
        f"超 30 汉字 {length_hits}；无依据词 {unsupported_hits}；用词绑定 {binding_hits}；"
        f"经济档位改写 {economy_rewrite_hits}；eco 说成手枪局 {eco_pistol_hits}。",
        "- 用词绑定覆盖：逐条实现表中的 15 个可由事实句判断的绑定；"
        "“僵尸”依赖走位/位置，而事实句刻意不含该数据，保留为人工复核项。",
        "- 本报告不含自动打分、审美排名或筛选；下列为原样单次输出。",
        "",
    ]
    for index, case_review in enumerate(review.cases, 1):
        lines.extend(
            (
                f"### {index}. `{case_review.case.case_id}`",
                "",
                "事实句：",
                *_indent(case_review.case.fact_sentence),
                f"场景标签：{scene_tags(case_review.case.fact_sentence)}",
                "舍弃标签：" + ("、".join(case_review.case.discarded_scene_tags) or "无"),
                "",
                _render_attempt("宠物说（温度0.9）", case_review.hot),
                _render_attempt("宠物说（温度0）", case_review.cold),
                "",
            )
        )
    lines.extend(
        (
            "## 人工编造复核",
            "",
            "以下统计由逐条阅读原样输出后补充；不把风格好坏计入编造。",
            "- 凭空新增实体：待人工复核。",
            "- 凭空新增因果或意图：待人工复核。",
            "- 夸大或改变事实：待人工复核。",
            "",
        )
    )
    return "\n".join(lines)


def _render_attempt(label: str, attempt: StyleAttempt) -> str:
    if attempt.result is None or attempt.checks is None:
        return f"{label}：调用失败：{attempt.error}"
    checks = attempt.checks
    flags = [
        f"字数 {checks.chinese_char_count}" + ("（超30）" if checks.exceeds_30_chars else ""),
        "无依据词：" + ("、".join(checks.unsupported_terms) or "无"),
        "用词绑定：" + ("；".join(checks.binding_violations) or "无"),
        "经济档位改写：" + ("是" if checks.economy_tier_rewrite else "无"),
        "eco 说成手枪局：" + ("是" if checks.eco_called_pistol_round else "无"),
    ]
    return f"{label}：{attempt.result.text}\n检查：{'；'.join(flags)}"


def _indent(value: str) -> tuple[str, ...]:
    return tuple(f"    {line}" for line in value.splitlines())


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile for no values")
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]


def _format_number(value: float) -> str:
    return f"{value:g}"


def _format_seconds(value: float) -> str:
    return f"{value:.3f} 秒"


def main() -> None:
    """Run the authorized paid review and write its Markdown report."""
    parser = argparse.ArgumentParser(description="M3-T9 风格对照评测")
    parser.add_argument("--out", type=Path, default=DEFAULT_REPORT_PATH)
    arguments = parser.parse_args()
    client = OpenRouterClient.from_env(timeout_seconds=TIMEOUT_SECONDS)
    try:
        review = run_style_review(client)
    finally:
        client.close()
    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    arguments.out.write_text(render_style_review(review), encoding="utf-8")


if __name__ == "__main__":
    main()
