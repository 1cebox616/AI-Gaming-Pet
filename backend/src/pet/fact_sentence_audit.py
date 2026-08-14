"""Generate the no-model M3-T8.13 fact-sentence coverage report."""

from __future__ import annotations

import argparse
from pathlib import Path
from collections.abc import Sequence

from pet.bench import (
    FactSentenceAuditCase,
    load_event_answer_keys,
    render_fact_sentence_audit_report,
)
from pet.config import load_config
from pet.event_card import render_fact_sentence, render_model_event_card
from pet.events import EventType
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
    )


def write_fact_sentence_audit_report(path: Path) -> None:
    """Write the report through the deterministic, zero-LLM replay path."""
    path.write_text(
        render_fact_sentence_audit_report(collect_fact_sentence_audit_cases()),
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local-only fact sentence audit."""
    parser = argparse.ArgumentParser(description="离线核验 M3-T8.13 自然语言事实句")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    write_fact_sentence_audit_report(args.out)
    print(f"事实句核验报告已写入：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
