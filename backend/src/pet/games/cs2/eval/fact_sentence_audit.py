"""Generate the no-model M3-T8.13 fact-sentence coverage report."""

from __future__ import annotations

import argparse
from pathlib import Path
from collections.abc import Sequence
import re
import subprocess

from pet.games.cs2.eval.bench import (
    FactSentenceAuditCase,
    load_event_answer_keys,
    render_fact_sentence_audit_report,
)
from pet.core.config import load_config
from pet.games.cs2.fact_sentences import (
    fact_sentence_scene_tag_selection,
    render_fact_sentence,
    render_model_event_card,
)
from pet.games.cs2.events import EventType
from pet.core.prompt import load_system_prompt
from pet.games.cs2.eval.replay import CommentaryDisposition, load_recording, replay_commentary
from pet.games.cs2.eval.scenario_synth import SCENARIO_SPECS, SCENARIOS_DIRECTORY

BACKEND_ROOT = Path(__file__).resolve().parents[5]
REPORTS_DIRECTORY = BACKEND_ROOT / "eval-reports"
OLD_RECORDING = BACKEND_ROOT / "recordings" / "gsi-20260811-223119-169538.jsonl"
OLD_ANSWER_KEY = BACKEND_ROOT / "data" / "cs2" / "eval-assets" / "m3-t8.10-aligned-old23-answer-keys.json"
NEW_ANSWER_KEY = BACKEND_ROOT / "data" / "cs2" / "eval-assets" / "m3-t8.10-aligned-new32-answer-keys.json"
_MULTI_KILL_CASE_IDS = frozenset(
    {
        "triple_kill_same_stage",
        "triple_kill_cross_stage",
        "triple_kill_headshot_finish",
        "weapon_switch_double_kill",
        "last_bullet_triple",
        "empty_mag_after_triple",
        "low_health_triple",
        "four_kill",
        "ace",
        "postplant_triple_loss",
        "late_defuse",
    }
)


def collect_fact_sentence_audit_cases() -> tuple[FactSentenceAuditCase, ...]:
    """Replay all frozen cases and render their deterministic fact sentences."""
    configuration = load_config(strict=True)
    old_keys = load_event_answer_keys(OLD_ANSWER_KEY).cases
    old_replay = replay_commentary(
        load_recording(OLD_RECORDING),
        configuration.events,
        configuration.policy,
        personality_style="brother",
    )
    selected = tuple(
        disposition for disposition in old_replay.dispositions if disposition.decision.selected
    )[: len(old_keys)]
    if len(selected) != len(old_keys):
        raise ValueError(f"旧集只找到 {len(selected)} 个入选事件，期望 {len(old_keys)}")

    cases = [
        _audit_case(
            key.case_id,
            disposition,
            key.required_facts,
            key.forbidden_claims,
            configuration.events.death_after_kill_max_seconds,
        )
        for key, disposition in zip(old_keys, selected, strict=True)
    ]
    specs_by_id = {spec.scenario_id: spec for spec in SCENARIO_SPECS}
    for key in load_event_answer_keys(NEW_ANSWER_KEY).cases:
        spec = specs_by_id.get(key.case_id)
        if spec is None:
            raise ValueError(f"新集场景缺少答案题 {key.case_id}")
        replay = replay_commentary(
            load_recording(SCENARIOS_DIRECTORY / f"{spec.scenario_id}.jsonl"),
            configuration.events,
            configuration.policy,
            personality_style="brother",
        )
        expected_type = _answer_key_event_type(key.case_id, key.required_facts)
        candidates = tuple(
            disposition
            for disposition in replay.dispositions
            if disposition.decision.selected
            and disposition.decision.event.type == expected_type
        )
        if not candidates:
            raise ValueError(f"场景 {spec.scenario_id} 未选中 {expected_type}")
        cases.append(
            _audit_case(
                spec.scenario_id,
                candidates[-1],
                key.required_facts,
                key.forbidden_claims,
                configuration.events.death_after_kill_max_seconds,
            )
        )
    if len(cases) != 55:
        raise AssertionError(f"事实句核验题数应为55，实际为 {len(cases)}")
    return tuple(cases)


def _answer_key_event_type(case_id: str, required_facts: tuple[str, ...]) -> EventType:
    """Recover the frozen case's focus type without changing its answer text."""
    facts = set(required_facts)
    if "普通死亡" in facts:
        return "death"
    if "爆头击杀" in facts:
        return "kill_headshot"
    if case_id in _MULTI_KILL_CASE_IDS:
        return "multi_kill"
    return "kill"


def _audit_case(
    case_id: str,
    disposition: CommentaryDisposition,
    required: tuple[str, ...],
    forbidden: tuple[str, ...],
    death_after_kill_max_seconds: float,
) -> FactSentenceAuditCase:
    """Render one replay disposition without entering any model path."""
    snapshot = disposition.snapshot
    game = disposition.game
    round_situation = disposition.round_situation
    event = disposition.decision.event
    tag_selection = fact_sentence_scene_tag_selection(
        snapshot,
        game,
        round_situation,
        event,
    )
    return FactSentenceAuditCase(
        case_id=case_id,
        fact_sentence=render_fact_sentence(
            snapshot,
            game,
            round_situation,
            event,
            death_after_kill_max_seconds=death_after_kill_max_seconds,
        ),
        model_card=render_model_event_card(
            snapshot,
            game,
            round_situation,
            event,
            death_after_kill_max_seconds=death_after_kill_max_seconds,
        ),
        required_facts=required,
        forbidden_claims=forbidden,
        discarded_scene_tags=tag_selection.discarded,
    )


def write_fact_sentence_audit_report(path: Path) -> None:
    """Write the report through the deterministic, zero-LLM replay path."""
    cases = collect_fact_sentence_audit_cases()
    report = render_fact_sentence_audit_report(cases)
    path.write_text(
        report,
        encoding="utf-8",
    )


def write_assembled_prompt_report(path: Path) -> None:
    """Write the exact current prompt and a local-only token estimate.

    The runtime has no tokenizer dependency and this task must not call a
    model.  Counts therefore use the documented deterministic approximation
    below; the report deliberately does not present them as provider usage.
    """
    cases = collect_fact_sentence_audit_cases()
    current_prompt = load_system_prompt("cs2", max_chars=20)
    old_prompt = _prompt_before_t814()
    representative_sentence = cases[0].fact_sentence
    current_tokens = _estimated_token_count(current_prompt)
    old_tokens = _estimated_token_count(old_prompt)
    total_tokens = _estimated_token_count(
        current_prompt + "\n\n" + representative_sentence
    )
    lines = (
        "# M3-T8.15 组装后的提示词审计",
        "",
        "- 模式：仅本地文件读取与确定性词元估算；未调用模型、未读取密钥。",
        "- 估算方法：每个汉字、连续拉丁/数字串、或非空白标点各记一个词元。"
        "当前环境未安装模型专用 tokenizer，因此此处不是服务商 usage 的精确值。",
        "- 当前系统提示词估算 token：" + str(current_tokens),
        "- 加入下列典型事实句后的单次总输入估算 token：" + str(total_tokens),
        "- M3-T8.14 之前（旧 inference.md + reading.md）系统提示词估算 token："
        + str(old_tokens),
        "- 当前相对旧版系统提示词变化："
        + f"{current_tokens - old_tokens:+d} token。",
        "",
        "## 组装分支",
        "",
        "- 运行时不再读取 `reading.md`；`include_reading_guide` 参数已无效，"
        "不存在按该参数分支的不同提示词。",
        "- `vocabulary.md` 存在时，替换 `inference.md` 中的 `{{VOCABULARY}}`；"
        f"生产分支（文件存在）为 {current_tokens} token。",
        "- 文件不存在时，词库替换为空字符串；该降级分支只保留 inference.md 的规则，"
        "不用于生产。",
        "- 只有包含 `{{VOCABULARY}}` 的人格提示词会注入词库；当前评测人格"
        " `inference` 包含该占位符。",
        "",
        "## 典型事实句",
        "",
        "```text",
        representative_sentence,
        "```",
        "",
        "## 当前实际发送的系统提示词全文",
        "",
        "```text",
        current_prompt,
        "```",
        "",
        "## M3-T8.14 之前的对照系统提示词全文",
        "",
        "```text",
        old_prompt,
        "```",
        "",
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _prompt_before_t814() -> str:
    """Read the immediately preceding committed prompt without restoring it."""
    repository = BACKEND_ROOT.parent
    parts = []
    for path in ("backend/prompts/reading.md", "backend/prompts/inference.md"):
        result = subprocess.run(
            ["git", "show", f"HEAD^:{path}"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        parts.append(result.stdout.strip())
    return "\n\n".join(parts).replace("{max_chars}", "20")


def _estimated_token_count(text: str) -> int:
    """Return a reproducible local estimate when no model tokenizer is present."""
    return len(re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\s]", text))


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local-only fact sentence audit."""
    parser = argparse.ArgumentParser(description="离线核验自然语言事实句")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--assembled-prompt",
        action="store_true",
        help="输出当前组装提示词与本地词元估算",
    )
    args = parser.parse_args(argv)
    if args.assembled_prompt:
        write_assembled_prompt_report(args.out)
        print(f"assembled prompt report written: {args.out}")
    else:
        write_fact_sentence_audit_report(args.out)
        print(f"fact sentence report written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
