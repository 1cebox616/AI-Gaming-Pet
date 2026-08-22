"""Run the M3-T9.7 unseeded repeated-sampling style diversity review."""

from __future__ import annotations

from pathlib import Path
import re
import statistics

from pet.games.cs2.eval.fact_sentence_audit import collect_fact_sentence_audit_cases
from pet.core.llm import OpenRouterClient
from pet.core.prompt import load_system_prompt
from pet.games.cs2.eval.style_review import MAX_TOKENS, MODEL, PROVIDER, REASONING_EFFORT

BACKEND_ROOT = Path(__file__).resolve().parents[5]
REPORT_PATH = BACKEND_ROOT / "eval-reports" / "m3-t9.7-diversity.md"
VOCABULARY_PATH = BACKEND_ROOT / "prompts" / "cs2" / "vocabulary.md"
_CASE_IDS = (
    "gsi-20260811-223119-169538:002:kill:r5",
    "gsi-20260811-223119-169538:014:kill_headshot:r3",
    "triple_kill_same_stage",
    "gsi-20260811-223119-169538:001:death:r4",
    "gsi-20260811-223119-169538:006:death_thrown_away:r8",
    "flash_kill",
    "smoke_exit_death",
    "weapon_switch_double_kill",
    "burning_kill",
    "awp_miss_then_death",
)


def main() -> None:
    """Call the authorized model fifty times without a seed and write raw outputs."""
    cases = {case.case_id: case for case in collect_fact_sentence_audit_cases()}
    prompt = load_system_prompt("cs2", max_chars=30)
    client = OpenRouterClient.from_env(timeout_seconds=10.0)
    outputs: list[str] = []
    prompt_tokens: list[int] = []
    latencies: list[float] = []
    costs: list[float] = []
    lines = [
        "# M3-T9.7 温度多样性复核",
        "",
        "- 每张卡：温度 0.9 连续 5 次；**不传 seed**。",
        "- 全部为原样单次结果，不筛选、不重试；正式双温度评测仍使用固定种子。",
        "",
    ]
    for case_id in _CASE_IDS:
        case = cases[case_id]
        case_outputs: list[str] = []
        lines.extend((f"## `{case_id}`", "", "事实句：", *(f"    {line}" for line in case.fact_sentence.splitlines()), ""))
        for index in range(1, 6):
            result = client.complete(
                model=MODEL,
                provider=PROVIDER,
                system_prompt=prompt,
                user_prompt=case.fact_sentence,
                max_tokens=MAX_TOKENS,
                temperature=0.9,
                reasoning_effort=REASONING_EFFORT,
            )
            lines.append(f"- 0.9 / {index}：{result.text}")
            outputs.append(result.text)
            case_outputs.append(result.text)
            if result.usage.prompt_tokens is not None:
                prompt_tokens.append(result.usage.prompt_tokens)
            latencies.append(result.latency_seconds)
            if result.usage.cost_usd is not None:
                costs.append(result.usage.cost_usd)
        distinct = len(set(case_outputs))
        judgment = "有文本差异；是否属于实质差异见该卡五条原样输出" if distinct >= 2 else "无，五条逐字相同"
        lines.extend((f"- 逐张判断：唯一文本 {distinct}/5；{judgment}。", ""))
    vocabulary = _vocabulary_entries(VOCABULARY_PATH.read_text(encoding="utf-8"))
    used = tuple(entry for entry in vocabulary if entry.lower() in "\n".join(outputs).lower())
    unused = tuple(entry for entry in vocabulary if entry not in used)
    lines.extend(
        (
            "## 统计",
            "",
            f"- 调用数：{len(outputs)}；提示词 token 中位数：{statistics.median(prompt_tokens) if prompt_tokens else '上游未返回'}。",
            f"- 事件 P95 延迟：{_percentile(latencies, 0.95):.3f} 秒；总花费：${sum(costs):.6f}。",
            f"- 词库字面利用率：{len(used)} / {len(vocabulary)}（{len(used) / len(vocabulary):.1%}）。",
            "- 未使用词条：" + ("｜".join(unused) or "无"),
            "- 结论：" + (
                "温度 0.9 至少产生了逐字不同的表达，但是否足够多样仍需产品负责人按逐卡原样输出判断。"
                if len(set(outputs)) > len(_CASE_IDS)
                else "温度 0.9 未产生可观测的同卡多样性。"
            ),
            "",
        )
    )
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _vocabulary_entries(text: str) -> tuple[str, ...]:
    """Extract literal word and sentence examples for the report-only count."""
    entries: list[str] = []
    for line in text.splitlines():
        if not line.startswith(("词：", "句：")):
            continue
        for entry in re.split(r"[、／/；;]", line[2:]):
            normalized = entry.strip().strip("（）()。！!")
            if len(normalized) > 1 and normalized not in entries:
                entries.append(normalized)
    return tuple(entries)


def _percentile(values: list[float], percentile: float) -> float:
    """Return the nearest observed percentile for a finite sample."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * percentile)]


if __name__ == "__main__":
    main()
