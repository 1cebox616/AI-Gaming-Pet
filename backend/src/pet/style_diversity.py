"""Run the M3-T9.6 repeated-sampling style diversity review."""

from __future__ import annotations

from pathlib import Path

from pet.fact_sentence_audit import collect_fact_sentence_audit_cases
from pet.llm import OpenRouterClient
from pet.prompt import load_system_prompt
from pet.style_review import MAX_TOKENS, MODEL, PROVIDER, REASONING_EFFORT, SEED

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPORT_PATH = BACKEND_ROOT / "bench-reports" / "m3-t9.6-diversity.md"
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
    """Call the authorized model exactly sixty times and write raw outputs."""
    cases = {case.case_id: case for case in collect_fact_sentence_audit_cases()}
    prompt = load_system_prompt("inference", max_chars=30)
    client = OpenRouterClient.from_env(timeout_seconds=10.0)
    lines = ["# M3-T9.6 温度多样性复核", "", "- 每张卡：温度 0.9 连续 5 次；温度 0 单次对照。", "- 全部为原样单次结果，不筛选、不重试。", ""]
    for case_id in _CASE_IDS:
        case = cases[case_id]
        lines.extend((f"## `{case_id}`", "", "事实句：", *(f"    {line}" for line in case.fact_sentence.splitlines()), ""))
        for index in range(1, 6):
            result = client.complete(model=MODEL, provider=PROVIDER, system_prompt=prompt, user_prompt=case.fact_sentence, max_tokens=MAX_TOKENS, temperature=0.9, seed=SEED, reasoning_effort=REASONING_EFFORT)
            lines.append(f"- 0.9 / {index}：{result.text}")
        result = client.complete(model=MODEL, provider=PROVIDER, system_prompt=prompt, user_prompt=case.fact_sentence, max_tokens=MAX_TOKENS, temperature=0.0, seed=SEED, reasoning_effort=REASONING_EFFORT)
        lines.extend((f"- 0 / 对照：{result.text}", ""))
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
