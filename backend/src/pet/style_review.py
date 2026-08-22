"""Run the M3-T9 two-temperature style review without scoring taste."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import statistics

from pet.bench import FactSentenceAuditCase
from pet.fact_sentence_audit import collect_fact_sentence_audit_cases
from pet.hard_gate import (
    HardChecks,
    VOCABULARY_BINDINGS,
    check_hard_violations,
    scene_tags,
)
from pet.llm import LlmClientProtocol, LlmError, LlmResult, OpenRouterClient
from pet.prompt import load_system_prompt

BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORT_PATH = BACKEND_ROOT / "eval-reports" / "m3-t9.7-style-review.md"
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

# These synthetic files remain valuable detector fixtures, but their named
# centerpiece is earlier than the selected speech focus.  Label the report so
# a reviewer does not mistake the filename for the current fact sentence.
_OUT_OF_FOCUS_SCENARIO_IDS = frozenset(
    {
        "rare_reload_then_kill",
        "rare_ammo_low_death",
        "awp_miss_then_death",
        "triple_kill_headshot_finish",
        "flash_double_kill",
        "long_smoke_then_kill",
        "four_grenades_then_kill",
        "double_flash_then_kill",
        "smoke_flash_kill",
        "bomb_pickup_then_death",
        "bomb_planted_then_death",
    }
)


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
        "# M3-T9.7 文风层双温度评测",
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
        "- 用词绑定覆盖：启动时解析 `vocabulary.md` 的三列表格并缓存；"
        f"当前解析 {len(VOCABULARY_BINDINGS or ())} 行，标为“无法映射”的行保留人工复核。",
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
                *(
                    ("（注：该场景核心事实不在焦点范围内，场景名仅为文件名，不代表卡的内容）",)
                    if case_review.case.case_id in _OUT_OF_FOCUS_SCENARIO_IDS
                    else ()
                ),
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
