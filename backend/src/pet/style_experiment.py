"""Run the M3-T9.8 diversity-versus-hard-checks comparison without tuning."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import statistics

from pet.bench import FactSentenceAuditCase
from pet.fact_sentence_audit import collect_fact_sentence_audit_cases
from pet.hard_gate import check_hard_violations
from pet.llm import LlmError, LlmResult, OpenRouterClient
from pet.prompt import load_system_prompt
from pet.style_diversity import _CASE_IDS, _percentile, _vocabulary_entries
from pet.style_review import (
    MAX_TOKENS,
    MODEL,
    PROVIDER,
    REASONING_EFFORT,
    SEED,
    TIMEOUT_SECONDS,
    _ROUND_RESULT_STYLE_EXCLUSIONS,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = BACKEND_ROOT / "eval-reports" / "m3-t9.8-diversity-experiment.md"
VOCABULARY_PATH = BACKEND_ROOT / "prompts" / "vocabulary.md"
DIVERSITY_SUFFIX = "同一种场合有很多种说法，词库里给了不止一个选择。\n不要每次都用最顺手的那句，换着说。"


@dataclass(frozen=True, slots=True)
class ExperimentGroup:
    """One deliberately isolated diversity intervention."""

    name: str
    temperature: float
    append_diversity_instruction: bool


GROUPS: tuple[ExperimentGroup, ...] = (
    ExperimentGroup("基线", 0.9, False),
    ExperimentGroup("A", 1.15, False),
    ExperimentGroup("B", 0.9, True),
    ExperimentGroup("C", 1.15, True),
)


@dataclass(frozen=True, slots=True)
class CallRecord:
    """One raw, unfiltered model response or its only upstream error."""

    result: LlmResult | None
    error: str | None


@dataclass(frozen=True, slots=True)
class DiversityCard:
    """Five unseeded responses for one focused fact sentence."""

    case: FactSentenceAuditCase
    calls: tuple[CallRecord, ...]


@dataclass(frozen=True, slots=True)
class FormalCase:
    """One seeded, reproducible hard-check evaluation response."""

    case: FactSentenceAuditCase
    call: CallRecord


@dataclass(frozen=True, slots=True)
class GroupResult:
    """The paired unseeded sampling and seeded formal evaluation for one group."""

    group: ExperimentGroup
    prompt: str
    diversity_cards: tuple[DiversityCard, ...]
    formal_cases: tuple[FormalCase, ...]


def _runtime_prompt(group: ExperimentGroup) -> str:
    """Append only the specified experiment instruction, without editing prompt files."""
    prompt = load_system_prompt("inference", max_chars=30)
    return f"{prompt}\n{DIVERSITY_SUFFIX}" if group.append_diversity_instruction else prompt


def _call(
    client: OpenRouterClient,
    *,
    prompt: str,
    case: FactSentenceAuditCase,
    temperature: float,
    seed: int | None,
) -> CallRecord:
    """Make exactly one call; experiments deliberately do not retry failures."""
    arguments: dict[str, object] = {
        "model": MODEL,
        "provider": PROVIDER,
        "system_prompt": prompt,
        "user_prompt": case.fact_sentence,
        "max_tokens": MAX_TOKENS,
        "temperature": temperature,
        "reasoning_effort": REASONING_EFFORT,
    }
    if seed is not None:
        arguments["seed"] = seed
    try:
        return CallRecord(result=client.complete(**arguments), error=None)
    except LlmError as error:
        return CallRecord(result=None, error=str(error))


def run_group(
    client: OpenRouterClient,
    group: ExperimentGroup,
    *,
    diversity_cases: Sequence[FactSentenceAuditCase],
    formal_cases: Sequence[FactSentenceAuditCase],
) -> GroupResult:
    """Run unseeded five-way samples, then the independent seeded formal set."""
    prompt = _runtime_prompt(group)
    cards = tuple(
        DiversityCard(
            case=case,
            calls=tuple(
                _call(client, prompt=prompt, case=case, temperature=group.temperature, seed=None)
                for _ in range(5)
            ),
        )
        for case in diversity_cases
    )
    formal = tuple(
        FormalCase(
            case=case,
            call=_call(client, prompt=prompt, case=case, temperature=group.temperature, seed=SEED),
        )
        for case in formal_cases
    )
    return GroupResult(group=group, prompt=prompt, diversity_cards=cards, formal_cases=formal)


def _texts(records: Iterable[CallRecord]) -> tuple[str, ...]:
    return tuple(record.result.text for record in records if record.result is not None)


def _metrics(records: Iterable[CallRecord]) -> tuple[float | None, float | None, float]:
    """Return prompt-token median, P95 latency and total cost for successful calls."""
    results = tuple(record.result for record in records if record.result is not None)
    prompt_tokens = tuple(result.usage.prompt_tokens for result in results if result.usage.prompt_tokens is not None)
    latencies = tuple(result.latency_seconds for result in results)
    costs = tuple(result.usage.cost_usd for result in results if result.usage.cost_usd is not None)
    return (
        statistics.median(prompt_tokens) if prompt_tokens else None,
        _percentile(list(latencies), 0.95) if latencies else None,
        sum(costs),
    )


def _hard_counts(cases: Sequence[FormalCase]) -> tuple[int, int, int, int]:
    """Count the four declared hard violations, never scoring style."""
    checks = tuple(
        check_hard_violations(item.call.result.text, fact_sentence=item.case.fact_sentence)
        for item in cases
        if item.call.result is not None
    )
    return (
        sum(bool(check.unsupported_terms) for check in checks),
        sum(bool(check.binding_violations) for check in checks),
        sum(check.economy_tier_rewrite for check in checks),
        sum(check.eco_called_pistol_round for check in checks),
    )


def _vocabulary_usage(texts: Sequence[str]) -> tuple[int, int, tuple[str, ...]]:
    """Measure literal vocabulary coverage from the experiment's 50 raw samples."""
    vocabulary = _vocabulary_entries(VOCABULARY_PATH.read_text(encoding="utf-8"))
    combined = "\n".join(texts).lower()
    used = tuple(entry for entry in vocabulary if entry.lower() in combined)
    unused = tuple(entry for entry in vocabulary if entry not in used)
    return len(used), len(vocabulary), unused


def render_report(results: Sequence[GroupResult]) -> str:
    """Render raw outputs and mechanical measurements, without choosing a winner."""
    lines = [
        "# M3-T9.8 表达多样性四组对照实验",
        "",
        "- 每组：10 张相同的卡，各 5 次不传 seed 的采样（50 次）+ 49 题固定 seed=42 的正式评测（49 次）。",
        "- 总计划调用数：396；不重试、不筛选。B/C 的两句约束仅在运行时追加，未修改 `inference.md`。",
        "- 型号：`qwen/qwen3.5-122b-a10b`；上游：`Alibaba`；reasoning_effort：`none`；max_tokens：256；超时：10 秒。",
        "",
        "## 硬性违规对照（正式 49 题）",
        "",
        "| 检查 | 基线 | A | B | C |",
        "|---|---:|---:|---:|---:|",
    ]
    result_by_name = {result.group.name: result for result in results}
    for label, index in (("无依据词", 0), ("用词绑定", 1), ("经济档位改写", 2), ("eco 说成手枪局", 3)):
        values = [str(_hard_counts(result_by_name[name].formal_cases)[index]) for name in ("基线", "A", "B", "C")]
        lines.append(f"| {label} | " + " | ".join(values) + " |")
    lines.extend(("", "## 词库利用率（每组 50 次无种子采样）", "", "| 组 | 利用词条 | 利用率 | 未用词条数 |", "|---|---:|---:|---:|"))
    for group in GROUPS:
        result = result_by_name[group.name]
        texts = _texts(call for card in result.diversity_cards for call in card.calls)
        used, total, unused = _vocabulary_usage(texts)
        lines.append(f"| {group.name} | {used} / {total} | {used / total:.1%} | {len(unused)} |")
    lines.extend(("", "## 原样输出与逐卡判读", ""))
    for result in results:
        prompt_sha = sha256(result.prompt.encode("utf-8")).hexdigest()
        sample_records = tuple(call for card in result.diversity_cards for call in card.calls)
        formal_records = tuple(case.call for case in result.formal_cases)
        sample_tokens, sample_p95, sample_cost = _metrics(sample_records)
        formal_tokens, formal_p95, formal_cost = _metrics(formal_records)
        lines.extend(
            (
                f"## {result.group.name}（温度 {result.group.temperature}；" + ("运行时追加多样性约束" if result.group.append_diversity_instruction else "现状提示词") + "）",
                "",
                f"- 提示词 SHA-256：`{prompt_sha}`。",
                f"- 采样：输入 token 中位数 {sample_tokens if sample_tokens is not None else '上游未返回'}；P95 {sample_p95:.3f} 秒；花费 ${sample_cost:.6f}。" if sample_p95 is not None else "- 采样无成功调用。",
                f"- 正式：输入 token 中位数 {formal_tokens if formal_tokens is not None else '上游未返回'}；P95 {formal_p95:.3f} 秒；花费 ${formal_cost:.6f}。" if formal_p95 is not None else "- 正式评测无成功调用。",
                f"- 该组总花费：${sample_cost + formal_cost:.6f}。",
                "",
            )
        )
        for card in result.diversity_cards:
            texts = _texts(card.calls)
            lines.extend((f"### `{card.case.case_id}`", "", "事实句：", *(f"    {line}" for line in card.case.fact_sentence.splitlines()), ""))
            for index, call in enumerate(card.calls, 1):
                lines.append(f"- {index}：{call.result.text if call.result is not None else '调用失败：' + (call.error or '')}")
            lines.extend((f"- 初始差异数据：唯一文本 {len(set(texts))}/5。", "- 人工判读：待根据上述原样输出填写“有/无实质差异”及理由。", ""))
        texts = _texts(call for card in result.diversity_cards for call in card.calls)
        used, total, unused = _vocabulary_usage(texts)
        lines.extend(("- 未使用词条：" + ("｜".join(unused) or "无"), ""))
    return "\n".join(lines)


def main() -> None:
    """Run the authorized experiment only when called explicitly from the CLI."""
    all_cases = collect_fact_sentence_audit_cases()
    cases_by_id = {case.case_id: case for case in all_cases}
    diversity_cases = tuple(cases_by_id[case_id] for case_id in _CASE_IDS)
    formal_cases = tuple(case for case in all_cases if case.case_id not in _ROUND_RESULT_STYLE_EXCLUSIONS)
    client = OpenRouterClient.from_env(timeout_seconds=TIMEOUT_SECONDS)
    try:
        results = tuple(
            run_group(client, group, diversity_cases=diversity_cases, formal_cases=formal_cases)
            for group in GROUPS
        )
    finally:
        client.close()
    REPORT_PATH.write_text(render_report(results) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
