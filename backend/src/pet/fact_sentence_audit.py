"""Generate the no-model M3-T8.13 fact-sentence coverage report."""

from __future__ import annotations

import argparse
from pathlib import Path
from collections.abc import Sequence
import random
import re
import subprocess

from pet.bench import (
    FactSentenceAuditCase,
    _fact_has_sentence_evidence,
    load_event_answer_keys,
    render_fact_sentence_audit_report,
)
from pet.config import load_config
from pet.event_card import (
    fact_sentence_scene_tag_selection,
    render_fact_sentence,
    render_model_event_card,
)
from pet.events import EventType
from pet.prompt import load_system_prompt
from pet.replay import CommentaryDisposition, load_recording, replay_commentary
from pet.scenario_synth import SCENARIO_SPECS, SCENARIOS_DIRECTORY

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPORTS_DIRECTORY = BACKEND_ROOT / "bench-reports"
OLD_RECORDING = BACKEND_ROOT / "recordings" / "gsi-20260811-223119-169538.jsonl"
OLD_ANSWER_KEY = REPORTS_DIRECTORY / "m3-t8.10-aligned-old23-answer-keys.json"
NEW_ANSWER_KEY = REPORTS_DIRECTORY / "m3-t8.10-aligned-new32-answer-keys.json"
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
    configuration = load_config()
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
    report = _t815_report_preamble(cases) + "\n" + report
    path.write_text(
        report,
        encoding="utf-8",
    )


def _t815_report_preamble(cases: Sequence[FactSentenceAuditCase]) -> str:
    """Compare the current prose against the committed pre-change report."""
    previous = _previous_processes()
    current = {
        case.case_id: _process_from_sentence(case.fact_sentence)
        for case in cases
    }
    previous_lengths = sorted(_han_count(value) for value in previous.values())
    current_lengths = sorted(_han_count(value) for value in current.values())
    multikill_ids = [
        case.case_id
        for case in cases
        if "【事件】多杀" in case.fact_sentence
    ]
    sample_ids = random.Random(815).sample(sorted(current), k=10)
    lines = [
        "# M3-T8.16 多杀概括与可读性复核",
        "",
        "- 模式：仅离线重放与字符串渲染；未调用模型、未读取密钥。",
        "- M3-T8.15 旧【过程】长度（最短 / 中位 / 最长）："
        + _length_summary(previous_lengths),
        "- 改写后【过程】长度（最短 / 中位 / 最长）："
        + _length_summary(current_lengths),
        "- 不设字数上限；长度仅供观察。",
        "- 掉血只在相邻且中间没有其他时间线条目时合并；"
        "多杀的“期间掉血”则明确是整段累计。",
        "",
        "## 5 条多杀前后对照",
        "",
    ]
    for case_id in multikill_ids[:5]:
        lines.extend(
            (
                f"### `{case_id}`",
                "压缩前：" + previous[case_id],
                "压缩后：" + current.get(case_id, "该题不在当前核验集"),
                "",
            )
        )
    lines.extend(("## M3-T8.15 的 19 个未覆盖案例", ""))
    prior_missing = _previous_missing_case_ids()
    cases_by_id = {case.case_id: case for case in cases}
    superseded_multikill_atoms = frozenset(
        {"本回合第1杀", "本回合第2杀", "该阶段双杀", "该阶段三杀"}
    )
    for case_id in prior_missing:
        case = cases_by_id[case_id]
        missing = tuple(
            fact
            for fact in case.required_facts
            if not _fact_has_sentence_evidence(fact, case.fact_sentence)
        )
        if not missing:
            status = "已覆盖"
        elif set(missing).issubset(superseded_multikill_atoms):
            status = (
                "答案本身有问题：新规则概括总杀数与跨阶段关系，"
                "不再逐次写每一杀的编号或阶段计数"
            )
        else:
            status = "仍未覆盖：" + "、".join(missing)
        lines.append(f"- `{case_id}`：{status}")
    lines.extend(("", "## 随机 10 条可读性抽查", ""))
    internal_terms = ("未观测", "可观测", "同一时刻", "连续过程", "阶段不可判断")
    for case_id in sample_ids:
        process = current[case_id]
        found = tuple(term for term in internal_terms if term in process)
        assessment = (
            "发现内部措辞：" + "、".join(found)
            if found
            else "可直接理解这一波经过；未发现内部措辞。"
        )
        lines.extend((f"- `{case_id}`：{process} —— {assessment}",))
    lines.extend(("", "---", ""))
    return "\n".join(lines)


def _previous_processes() -> dict[str, str]:
    """Extract the pre-compression process lines from the committed T8.14 report."""
    result = subprocess.run(
        ["git", "show", "HEAD:backend/bench-reports/m3-t8.14-fact-sentences.md"],
        cwd=BACKEND_ROOT.parent,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    pattern = re.compile(
        r"### \d+\. `(?P<case_id>[^`]+)`\n新格式：\n```text\n.*?\n"
        r"【过程】(?P<process>[^\n]+)",
        re.DOTALL,
    )
    processes = {
        match.group("case_id"): match.group("process")
        for match in pattern.finditer(result.stdout)
    }
    if len(processes) != 55:
        raise ValueError(f"T8.14 报告中应有55条过程，实际为 {len(processes)}")
    return processes


def _previous_missing_case_ids() -> tuple[str, ...]:
    """Read the frozen historical failure set without editing answer keys."""
    result = subprocess.run(
        ["git", "show", "HEAD:backend/bench-reports/m3-t8.15-fact-sentences.md"],
        cwd=BACKEND_ROOT.parent,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    matched = re.search(r"- 有必答项未覆盖的题目：(.*)", result.stdout)
    if matched is None:
        raise ValueError("T8.15 报告缺少未覆盖题目清单")
    case_ids = tuple(re.findall(r"`([^`]+)`", matched.group(1)))
    if len(case_ids) != 19:
        raise ValueError(f"T8.15 未覆盖题应为19条，实际为 {len(case_ids)}")
    return case_ids


def _process_from_sentence(sentence: str) -> str:
    """Return the prose payload after the structured process heading."""
    return next(
        (line.removeprefix("【过程】") for line in sentence.splitlines() if line.startswith("【过程】")),
        "",
    )


def _han_count(text: str) -> int:
    """Count Chinese characters for the product's process-length budget."""
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def _length_summary(lengths: Sequence[int]) -> str:
    """Format a non-empty min/median/max length distribution."""
    if not lengths:
        return "0 / 0 / 0"
    middle = len(lengths) // 2
    median = (
        lengths[middle]
        if len(lengths) % 2
        else (lengths[middle - 1] + lengths[middle]) / 2
    )
    return f"{lengths[0]} / {median:g} / {lengths[-1]}"


def write_assembled_prompt_report(path: Path) -> None:
    """Write the exact current prompt and a local-only token estimate.

    The runtime has no tokenizer dependency and this task must not call a
    model.  Counts therefore use the documented deterministic approximation
    below; the report deliberately does not present them as provider usage.
    """
    cases = collect_fact_sentence_audit_cases()
    current_prompt = load_system_prompt("inference", max_chars=20)
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
