"""Offline evaluation of GSI-event-card-driven CS2 commentary."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pet.commentary import commentary_category
from pet.commentary_rules import CALLOUT_TERMS, find_forbidden_raw_curses
from pet.commentary_templates import COMMENTARY_TEMPLATES, CommentaryCategory
from pet.config import PersonalityStyle, load_config
from pet.events import EventType, GameEvent
from pet.llm import (
    LlmAnalysisClientProtocol,
    LlmAnalysisResult,
    LlmClientProtocol,
    LlmError,
    LlmResult,
    OpenRouterClient,
)
from pet.prompt import PROMPTS_DIRECTORY, PromptPersonality, load_system_prompt
from pet.replay import load_recording, replay_commentary
from pet.event_card import (
    render_event_card,
    render_model_event_card,
)

TEMPERATURE = 0.9
ANALYSIS_TEMPERATURE = 0.0
ANALYSIS_MAX_COMPLETION_TOKENS = 128
ANALYSIS_SEED = 42
ANALYSIS_MAX_EVENT_CHARS = 30
ANALYSIS_WHOLE_ACCURACY_TARGET = 0.95
ANALYSIS_ATOM_ACCURACY_TARGET = 0.95
ANALYSIS_MIN_SCENE_SENTENCES = 2
ANALYSIS_MAX_SCENE_SENTENCES = 4
_NEGATIVE_SUMMARY_TERMS = (
    "未阵亡",
    "未拆除",
    "未受伤",
    "未换枪",
    "未被闪",
    "没有发生",
    "没有出现",
    "没有交火",
    "没有特殊",
    "没有值得",
    "无特殊",
    "无明显",
)
_UNSUPPORTED_INFERENCE_TERMS = (
    "换回",
    "另一枚",
    "被击中",
    "被击杀",
    "导致",
    "造成",
    "因此",
    "全员存活",
)
_SUBJECT_EXPANSION_PATTERN = re.compile(
    r"我方(?:(?:在|先后?|连续|使用|用)[^。！？!?]{0,12})?"
    r"(?:完成|击杀|掉血|阵亡|换枪|扔出|购买|捡到|被闪|进烟|出烟)"
)
MAX_COMPLETION_TOKENS_BY_PERSONALITY: dict[PromptPersonality, int] = {
    "brother": 96,
    "caster": 96,
    "inference": 300,
}
TEMPLATE_PERSONALITY_BY_PROMPT: dict[PromptPersonality, PersonalityStyle] = {
    "brother": "brother",
    "caster": "caster",
    "inference": "brother",
}

_PLACEHOLDER_PATTERN = re.compile(r"\{[^{}]+\}")
_HAN_CHARACTER_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ANALYSIS_REPORT_CASE_ID_PATTERN = re.compile(
    r"^### \d+\. `([^`]+)`$", re.MULTILINE
)
_ANALYSIS_REPORT_EVENT_LINE_PATTERN = re.compile(r"^事件：(.*)$", re.MULTILINE)

_EVENT_LABELS: dict[EventType, str] = {
    "kill": "普通击杀",
    "kill_headshot": "爆头击杀",
    "multi_kill": "多杀",
    "death": "死亡",
    "death_after_kill": "击杀后被补枪",
    "death_thrown_away": "死亡（白给）",
    "round_win": "回合胜利",
    "round_loss": "回合失败",
}
_CATEGORY_LABELS: dict[CommentaryCategory, str] = {
    "kill": "普通击杀",
    "kill_headshot": "爆头击杀",
    "multi_2": "双杀",
    "multi_3": "三杀",
    "multi_4": "四杀",
    "multi_5": "五杀",
    "multi_general": "多杀",
    "death": "普通死亡",
    "death_after_kill": "击杀后被补枪",
    "death_thrown_away": "白给",
    "round_win_elimination": "回合胜利·灭队",
    "round_win_bomb": "回合胜利·引爆",
    "round_win_defuse": "回合胜利·拆包",
    "round_win_time": "回合胜利·时间到",
    "round_win_general": "回合胜利·通用",
    "round_loss_elimination": "回合失败·灭队",
    "round_loss_bomb": "回合失败·引爆",
    "round_loss_defuse": "回合失败·拆包",
    "round_loss_time": "回合失败·时间到",
    "round_loss_general": "回合失败·通用",
}


@dataclass(frozen=True, slots=True)
class LengthStatistics:
    """Chinese-character distribution of one personality's template pool."""

    minimum: int
    median: float
    p90: int
    maximum: int


@dataclass(frozen=True, slots=True)
class OutputChecks:
    """Factual violations observed in one successful model output."""

    exceeds_max_chars: bool | None
    callout_terms: tuple[str, ...]
    raw_curses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BenchAttempt:
    """One model call's success or failure."""

    result: LlmResult | None
    error: str | None
    checks: OutputChecks | None


@dataclass(frozen=True, slots=True)
class BenchEvent:
    """One selected production event and the facts shown to the model."""

    event: GameEvent
    category: CommentaryCategory
    event_card: str
    model_card: str
    template_text: str
    attempt: BenchAttempt | None


@dataclass(frozen=True, slots=True)
class BenchResult:
    """All data needed to render one self-contained Markdown report."""

    recording_path: Path
    requested_model: str | None
    requested_provider: str | None
    personality_style: PromptPersonality
    system_prompt: str | None
    include_reading_guide: bool
    cards_only: bool
    run_timestamp: datetime
    snapshot_count: int
    detected_event_count: int
    selected_event_count: int
    truncated_event_count: int
    length_statistics: LengthStatistics
    events: tuple[BenchEvent, ...]


@dataclass(frozen=True, slots=True)
class FactSentenceAuditCase:
    """One deterministic fact sentence paired with its immutable rubric."""

    case_id: str
    fact_sentence: str
    model_card: str
    required_facts: tuple[str, ...]
    forbidden_claims: tuple[str, ...]
    omitted_facts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LatencyExperimentResult:
    """Sequential A/B/C runs that isolate prompt and output-length changes."""

    recording_path: Path
    requested_model: str
    requested_provider: str | None
    run_timestamp: datetime
    groups: tuple[BenchResult, BenchResult, BenchResult]


@dataclass(frozen=True, slots=True)
class AnalysisChecks:
    """Protocol and static checks for one streamed two-field response."""

    event_exceeds_max_chars: bool
    scene_sentence_count: int
    event_callout_terms: tuple[str, ...]
    scene_callout_terms: tuple[str, ...]
    event_raw_curses: tuple[str, ...]
    scene_raw_curses: tuple[str, ...]
    negative_summary_terms: tuple[str, ...]
    unsupported_inference_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AnalysisAttempt:
    """One streamed call, including a protocol or transport failure."""

    result: LlmAnalysisResult | None
    error: str | None
    checks: AnalysisChecks | None
    partial_event_text: str | None = None
    event_latency_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class AnalysisBenchEvent:
    """One stable real-recording case sent through streamed analysis."""

    case_id: str
    recording_path: Path
    event: GameEvent
    category: CommentaryCategory
    event_card: str
    model_card: str
    attempt: AnalysisAttempt


@dataclass(frozen=True, slots=True)
class AnalysisBenchResult:
    """A multi-recording streamed factual-analysis benchmark."""

    recording_paths: tuple[Path, ...]
    requested_model: str
    requested_provider: str | None
    system_prompt: str
    run_timestamp: datetime
    snapshot_count: int
    detected_event_count: int
    selected_event_count: int
    truncated_event_count: int
    event_timeout_seconds: float
    full_timeout_seconds: float
    max_completion_tokens: int
    reasoning_effort: str
    seed: int
    events: tuple[AnalysisBenchEvent, ...]


class FieldScore(BaseModel):
    """Manual strict score for one output field."""

    model_config = ConfigDict(extra="forbid")

    whole_correct: bool
    correct_atoms: int = Field(ge=0)
    total_atoms: int = Field(ge=1)
    errors: tuple[str, ...] = ()
    notes: str = ""

    @model_validator(mode="after")
    def correct_atoms_cannot_exceed_total(self) -> Self:
        if self.correct_atoms > self.total_atoms:
            raise ValueError("correct_atoms 不能超过 total_atoms")
        return self


class CaseScore(BaseModel):
    """Manual event and scene scores keyed to a generated case ID."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    event: FieldScore
    scene: FieldScore | None = None


class AnalysisScoreFile(BaseModel):
    """Complete human judgment file for one immutable analysis report."""

    model_config = ConfigDict(extra="forbid")

    scope: Literal["event_and_scene", "event_only"] = "event_and_scene"
    answer_key_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    cases: tuple[CaseScore, ...]

    @model_validator(mode="after")
    def fields_match_declared_scope(self) -> Self:
        if self.scope == "event_only":
            if self.answer_key_sha256 is None:
                raise ValueError("event_only 评分必须绑定 answer key SHA-256")
            if any(case.scene is not None for case in self.cases):
                raise ValueError("event_only 评分不得包含场面分数")
        elif any(case.scene is None for case in self.cases):
            raise ValueError("event_and_scene 评分必须包含场面分数")
        return self


class EventAnswerKeyCase(BaseModel):
    """One product-approved semantic target for a stable real-recording case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    expected_summary: str = Field(min_length=1)
    required_facts: tuple[str, ...] = Field(min_length=1)
    forbidden_claims: tuple[str, ...] = ()
    notes: str = ""


class EventAnswerKeyFile(BaseModel):
    """The immutable semantic rubric for one event-only benchmark set."""

    model_config = ConfigDict(extra="forbid")

    cases: tuple[EventAnswerKeyCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def case_ids_are_unique(self) -> Self:
        case_ids = tuple(case.case_id for case in self.cases)
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("answer key 包含重复 case_id")
        return self


class UniversalForbiddenFile(BaseModel):
    """Project-wide unsupported claims applied to every event answer key."""

    model_config = ConfigDict(extra="forbid")

    a_gsi_unavailable: tuple[str, ...] = Field(min_length=1)
    b_inference_or_causality: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def terms_are_nonempty_and_unique(self) -> Self:
        terms = self.terms
        if any(not term.strip() for term in terms):
            raise ValueError("通用禁项不得包含空词条")
        if len(terms) != len(set(terms)):
            raise ValueError("通用禁项不得包含重复词条")
        return self

    @property
    def terms(self) -> tuple[str, ...]:
        """Return both factual categories in stable matching order."""
        return self.a_gsi_unavailable + self.b_inference_or_causality


@dataclass(frozen=True, slots=True)
class ForbiddenViolation:
    """One generated event line that contains project-wide unsupported claims."""

    case_id: str
    event_text: str
    matched_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ForbiddenRescoreResult:
    """Old and adjusted human scores for one immutable model report."""

    report_path: Path
    scores_path: Path
    original_scores: AnalysisScoreFile
    adjusted_scores: AnalysisScoreFile
    violations: tuple[ForbiddenViolation, ...]
    event_texts: tuple[str, ...]


def calculate_length_statistics(texts: Sequence[str]) -> LengthStatistics:
    """Calculate nearest-rank P90 after removing format placeholders."""
    lengths = sorted(_chinese_character_count(text) for text in texts)
    if not lengths:
        raise ValueError("cannot calculate template lengths from an empty sequence")
    p90_index = math.ceil(len(lengths) * 0.9) - 1
    return LengthStatistics(
        minimum=lengths[0],
        median=float(statistics.median(lengths)),
        p90=lengths[p90_index],
        maximum=lengths[-1],
    )


def commentary_length_statistics(
    personality_style: PersonalityStyle,
) -> LengthStatistics:
    """Measure all template lines for the selected production personality."""
    texts = tuple(
        template.text
        for templates in COMMENTARY_TEMPLATES[personality_style].values()
        for template in templates
    )
    return calculate_length_statistics(texts)


def check_output(
    text: str,
    *,
    max_chars: int,
    enforce_length_limit: bool = True,
) -> OutputChecks:
    """Record factual violations without filtering or regenerating the output."""
    return OutputChecks(
        exceeds_max_chars=(
            _chinese_character_count(text) > max_chars
            if enforce_length_limit
            else None
        ),
        callout_terms=tuple(term for term in CALLOUT_TERMS if term in text),
        raw_curses=find_forbidden_raw_curses(text),
    )


def run_bench(
    recording_path: Path,
    *,
    model: str | None,
    provider: str | None,
    personality_style: PromptPersonality,
    client: LlmClientProtocol | None,
    max_events: int,
    prompts_directory: Path = PROMPTS_DIRECTORY,
    include_reading_guide: bool = True,
    cards_only: bool = False,
) -> BenchResult:
    """Replay one recording and optionally call the model for selected events."""
    if max_events < 1:
        raise ValueError("max_events must be at least 1")
    if not cards_only and (model is None or client is None):
        raise ValueError("非 cards-only 模式必须提供型号与模型客户端")

    template_personality = TEMPLATE_PERSONALITY_BY_PROMPT[personality_style]
    snapshots = load_recording(recording_path)
    configuration = load_config()
    replay = replay_commentary(
        snapshots,
        configuration.events,
        configuration.policy,
        personality_style=template_personality,
    )
    selected = tuple(
        disposition
        for disposition in replay.dispositions
        if disposition.decision.selected
    )
    chosen = selected[:max_events]
    length_statistics = commentary_length_statistics(template_personality)
    system_prompt = (
        None
        if cards_only
        else load_system_prompt(
            personality_style,
            max_chars=length_statistics.p90,
            prompts_directory=prompts_directory,
            include_reading_guide=include_reading_guide,
        )
    )
    events: list[BenchEvent] = []

    for disposition in chosen:
        event = disposition.decision.event
        if disposition.utterance is None:
            raise ValueError(f"selected event {event.id} has no template utterance")
        card = render_event_card(
            disposition.snapshot,
            disposition.game,
            disposition.round_situation,
            event,
            death_after_kill_max_seconds=(
                configuration.events.death_after_kill_max_seconds
            ),
        )
        model_card = render_model_event_card(
            disposition.snapshot,
            disposition.game,
            disposition.round_situation,
            event,
            death_after_kill_max_seconds=(
                configuration.events.death_after_kill_max_seconds
            ),
        )
        attempt = None
        if not cards_only:
            assert model is not None
            assert client is not None
            assert system_prompt is not None
            attempt = _attempt_completion(
                model=model,
                provider=provider,
                system_prompt=system_prompt,
                event_card=model_card,
                max_chars=length_statistics.p90,
                max_tokens=MAX_COMPLETION_TOKENS_BY_PERSONALITY[personality_style],
                enforce_length_limit=personality_style != "inference",
                client=client,
            )
        events.append(
            BenchEvent(
                event=event,
                category=commentary_category(event),
                event_card=card,
                model_card=model_card,
                template_text=disposition.utterance.text,
                attempt=attempt,
            )
        )

    return BenchResult(
        recording_path=recording_path,
        requested_model=model,
        requested_provider=provider,
        personality_style=personality_style,
        system_prompt=system_prompt,
        include_reading_guide=include_reading_guide,
        cards_only=cards_only,
        run_timestamp=datetime.now(timezone.utc),
        snapshot_count=len(snapshots),
        detected_event_count=len(replay.dispositions),
        selected_event_count=len(selected),
        truncated_event_count=max(0, len(selected) - len(chosen)),
        length_statistics=length_statistics,
        events=tuple(events),
    )


def render_fact_sentence_audit_report(
    cases: Sequence[FactSentenceAuditCase],
) -> str:
    """Render deterministic answer-key coverage without a model invocation."""
    lengths = sorted(_fact_process_character_count(case.fact_sentence) for case in cases)
    missing_cases = tuple(
        case.case_id
        for case in cases
        if any(
            not _fact_has_sentence_evidence(fact, case.fact_sentence)
            for fact in case.required_facts
        )
    )
    median = statistics.median(lengths) if lengths else 0
    lines = [
        "# M3-T8.16 分区事实句离线核验",
        "",
        "- 模式：只渲染代码事实句；未调用模型、未读取密钥。",
        f"- 题数：{len(cases)}",
        (
            "- 汉字长度（最短 / 中位 / 最长）："
            f"{lengths[0] if lengths else 0} / {median:g} / {lengths[-1] if lengths else 0}"
        ),
        "- 有必答项未覆盖的题目："
        + ("无" if not missing_cases else "、".join(f"`{case_id}`" for case_id in missing_cases)),
        "",
    ]
    for index, case in enumerate(cases, 1):
        covered = tuple(
            fact for fact in case.required_facts if _fact_has_sentence_evidence(fact, case.fact_sentence)
        )
        missing = tuple(fact for fact in case.required_facts if fact not in covered)
        forbidden = tuple(
            claim for claim in case.forbidden_claims if claim and claim in case.fact_sentence
        )
        lines.extend(
            (
                f"### {index}. `{case.case_id}`",
            "新格式：\n```text\n" + case.fact_sentence + "\n```",
                "必答覆盖：" + "  ".join(
                    f"{'✅' if fact in covered else '❌'} {fact}" for fact in case.required_facts
                ),
                "禁项检查：" + ("✅ 无" if not forbidden else "❌ " + "、".join(forbidden)),
                "【过程】汉字数：" + str(_fact_process_character_count(case.fact_sentence)),
                "因取舍舍弃：" + ("无" if not case.omitted_facts else "、".join(case.omitted_facts)),
                "对照——M3-T8.13 旧事实句：",
                "```text",
                case.model_card,
                "```",
                "",
            )
        )
    return "\n".join(lines)


def _fact_process_character_count(text: str) -> int:
    """Count only the prose payload following the structured process heading."""
    process = next((line.removeprefix("【过程】") for line in text.splitlines() if line.startswith("【过程】")), "")
    return _chinese_character_count(process)


def _fact_has_sentence_evidence(required_fact: str, sentence: str) -> bool:
    """Match frozen semantic atoms to their neutral prose evidence.

    Answer keys deliberately retain their historical labels.  This map keeps
    those labels immutable while checking the equivalent wording emitted by the
    deterministic fact renderer.
    """
    if required_fact in sentence:
        return True
    if required_fact == "残血":
        return re.search(r"还剩(?:[0-2]?\d|30)血", sentence) is not None
    evidence = {
        "普通击杀": ("完成击杀",),
        "本回合第2杀": ("双杀",),
        "本回合累计3杀": ("三杀",),
        "本回合第4杀": ("四杀",),
        "本回合第5杀": ("五杀",),
        "该阶段三杀": ("三杀",),
        "爆头击杀": ("爆头", "完成击杀"),
        "普通死亡": ("阵亡",),
        "对枪胜利": ("受伤后仍完成击杀",),
        "击杀后被补枪": ("完成击杀后不久阵亡",),
        "对枪输了": ("受伤的交火后阵亡",),
        "白着打": ("受闪光影响时完成击杀",),
        "白着被打死": ("受闪光影响时阵亡",),
        "受闪光影响未结束": ("仍被闪",),
        "闪光影响结束": ("闪光结束",),
        "进烟后出烟": ("进烟", "出烟"),
        "摸烟击杀": ("出烟后不久完成击杀",),
        "出烟就没了": ("出烟后不久阵亡",),
        "残血击杀": ("残血时完成击杀",),
        "残血": ("降到30血或以下",),
        "换枪后立刻杀": ("换主武器后不久完成击杀",),
        "一枪秒": ("可观测用弹为1到3发",),
        "一梭子秒": ("可观测用弹为4到7发",),
        "打了多发": ("可观测用弹至少10发",),
        "一枪没开就没了": ("未观测到开火就阵亡",),
        "马枪死": ("交火的可观测用弹至少10发",),
        "白给": ("本回合零杀", "时阵亡"),
        "弹匣打空": ("弹匣已空",),
    }
    terms = evidence.get(required_fact, (required_fact,))
    return all(term in sentence for term in terms)


def run_latency_experiment(
    recording_path: Path,
    *,
    model: str,
    provider: str | None,
    client: LlmClientProtocol,
    max_events: int,
    prompts_directory: Path = PROMPTS_DIRECTORY,
) -> LatencyExperimentResult:
    """Run A, then B, then C through one reusable client connection."""
    group_a = run_bench(
        recording_path,
        model=model,
        provider=provider,
        personality_style="brother",
        client=client,
        max_events=max_events,
        prompts_directory=prompts_directory,
        include_reading_guide=False,
    )
    group_b = run_bench(
        recording_path,
        model=model,
        provider=provider,
        personality_style="brother",
        client=client,
        max_events=max_events,
        prompts_directory=prompts_directory,
        include_reading_guide=True,
    )
    group_c = run_bench(
        recording_path,
        model=model,
        provider=provider,
        personality_style="inference",
        client=client,
        max_events=max_events,
        prompts_directory=prompts_directory,
        include_reading_guide=True,
    )
    return LatencyExperimentResult(
        recording_path=recording_path,
        requested_model=model,
        requested_provider=provider,
        run_timestamp=datetime.now(timezone.utc),
        groups=(group_a, group_b, group_c),
    )


def run_stream_analysis(
    recording_paths: Sequence[Path],
    *,
    model: str,
    provider: str | None,
    client: LlmAnalysisClientProtocol,
    max_events: int,
    event_timeout_seconds: float,
    full_timeout_seconds: float,
    max_completion_tokens: int = ANALYSIS_MAX_COMPLETION_TOKENS,
    reasoning_effort: str = "none",
    seed: int = ANALYSIS_SEED,
    prompts_directory: Path = PROMPTS_DIRECTORY,
    expected_event_types: Sequence[EventType] | None = None,
) -> AnalysisBenchResult:
    """Replay recordings and stream one strict audited response per event."""
    if not recording_paths:
        raise ValueError("stream analysis requires at least one recording")
    if not model.strip():
        raise ValueError("stream analysis requires a non-empty model ID")
    if provider is None or not provider.strip():
        raise ValueError("stream analysis requires a locked provider")
    if max_events < 1:
        raise ValueError("max_events must be at least 1")
    if event_timeout_seconds <= 0:
        raise ValueError("event timeout must be positive")
    if full_timeout_seconds < event_timeout_seconds:
        raise ValueError("full timeout must be at least the event timeout")
    if max_completion_tokens < 1:
        raise ValueError("max completion tokens must be positive")
    if expected_event_types is not None and len(expected_event_types) != len(recording_paths):
        raise ValueError("目标事件类型数量必须与录制文件数量一致")

    system_prompt = load_system_prompt(
        "inference",
        max_chars=ANALYSIS_MAX_EVENT_CHARS,
        prompts_directory=prompts_directory,
    )
    configuration = load_config()
    events: list[AnalysisBenchEvent] = []
    snapshot_count = 0
    detected_event_count = 0
    selected_event_count = 0

    for recording_index, recording_path in enumerate(recording_paths):
        snapshots = load_recording(recording_path)
        replay = replay_commentary(
            snapshots,
            configuration.events,
            configuration.policy,
            personality_style="brother",
        )
        selected = tuple(
            disposition
            for disposition in replay.dispositions
            if disposition.decision.selected
        )
        snapshot_count += len(snapshots)
        detected_event_count += len(replay.dispositions)
        expected_type = (
            expected_event_types[recording_index]
            if expected_event_types is not None
            else None
        )
        if expected_type is None:
            chosen_for_recording = selected
        else:
            matches = tuple(
                disposition
                for disposition in selected
                if disposition.decision.event.type == expected_type
            )
            if not matches:
                observed = ", ".join(
                    disposition.decision.event.type for disposition in selected
                ) or "无"
                raise ValueError(
                    f"{recording_path.name} 未选中目标事件 {expected_type}；"
                    f"实际为：{observed}"
                )
            # A synthetic scenario may contain setup events of the same kind.
            # Its declared target is the final matching event, identical to
            # scenario_synth's cards-only selection rule.
            chosen_for_recording = (matches[-1],)
        selected_event_count += len(chosen_for_recording)
        for selected_index, disposition in enumerate(chosen_for_recording, 1):
            if len(events) >= max_events:
                continue
            event = disposition.decision.event
            card = render_event_card(
                disposition.snapshot,
                disposition.game,
                disposition.round_situation,
                event,
                death_after_kill_max_seconds=(
                    configuration.events.death_after_kill_max_seconds
                ),
            )
            model_card = render_model_event_card(
                disposition.snapshot,
                disposition.game,
                disposition.round_situation,
                event,
                death_after_kill_max_seconds=(
                    configuration.events.death_after_kill_max_seconds
                ),
            )
            case_id = (
                recording_path.stem
                if expected_type is not None
                else (
                    f"{recording_path.stem}:{selected_index:03d}:{event.type}:"
                    f"r{event.round_number if event.round_number is not None else 'na'}"
                )
            )
            events.append(
                AnalysisBenchEvent(
                    case_id=case_id,
                    recording_path=recording_path,
                    event=event,
                    category=commentary_category(event),
                    event_card=card,
                    model_card=model_card,
                    attempt=_attempt_stream_analysis(
                        model=model,
                        provider=provider,
                        system_prompt=system_prompt,
                        event_card=model_card,
                        event_timeout_seconds=event_timeout_seconds,
                        full_timeout_seconds=full_timeout_seconds,
                        max_completion_tokens=max_completion_tokens,
                        reasoning_effort=reasoning_effort,
                        seed=seed,
                        client=client,
                    ),
                )
            )

    return AnalysisBenchResult(
        recording_paths=tuple(recording_paths),
        requested_model=model,
        requested_provider=provider,
        system_prompt=system_prompt,
        run_timestamp=datetime.now(timezone.utc),
        snapshot_count=snapshot_count,
        detected_event_count=detected_event_count,
        selected_event_count=selected_event_count,
        truncated_event_count=max(0, selected_event_count - len(events)),
        event_timeout_seconds=event_timeout_seconds,
        full_timeout_seconds=full_timeout_seconds,
        max_completion_tokens=max_completion_tokens,
        reasoning_effort=reasoning_effort,
        seed=seed,
        events=tuple(events),
    )


def _attempt_stream_analysis(
    *,
    model: str,
    provider: str | None,
    system_prompt: str,
    event_card: str,
    event_timeout_seconds: float,
    full_timeout_seconds: float,
    max_completion_tokens: int,
    reasoning_effort: str,
    seed: int,
    client: LlmAnalysisClientProtocol,
) -> AnalysisAttempt:
    try:
        result = client.analyze_stream(
            model=model,
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=event_card,
            max_tokens=max_completion_tokens,
            temperature=ANALYSIS_TEMPERATURE,
            event_timeout_seconds=event_timeout_seconds,
            full_timeout_seconds=full_timeout_seconds,
            seed=seed,
            reasoning_effort=reasoning_effort,
        )
    except LlmError as error:
        context = (
            f"（{error.latency_seconds:.3f}s）"
            if error.latency_seconds is not None
            else ""
        )
        return AnalysisAttempt(
            result=None,
            error=f"{error}{context}",
            checks=None,
            partial_event_text=error.partial_event_text,
            event_latency_seconds=error.event_latency_seconds,
        )
    event_checks = check_output(
        result.event_text,
        max_chars=ANALYSIS_MAX_EVENT_CHARS,
    )
    scene_checks = check_output(
        result.scene_text,
        max_chars=10_000,
        enforce_length_limit=False,
    )
    return AnalysisAttempt(
        result=result,
        error=None,
        checks=AnalysisChecks(
            event_exceeds_max_chars=event_checks.exceeds_max_chars is True,
            scene_sentence_count=_scene_sentence_count(result.scene_text),
            event_callout_terms=event_checks.callout_terms,
            scene_callout_terms=scene_checks.callout_terms,
            event_raw_curses=event_checks.raw_curses,
            scene_raw_curses=scene_checks.raw_curses,
            negative_summary_terms=tuple(
                term for term in _NEGATIVE_SUMMARY_TERMS if term in result.scene_text
            ),
            unsupported_inference_terms=tuple(
                (
                    *(
                        term
                        for term in _UNSUPPORTED_INFERENCE_TERMS
                        if term in result.event_text or term in result.scene_text
                    ),
                    *(
                        match.group(0)
                        for match in _SUBJECT_EXPANSION_PATTERN.finditer(
                            result.scene_text
                        )
                    ),
                )
            ),
        ),
        partial_event_text=None,
        event_latency_seconds=None,
    )


def _scene_sentence_count(text: str) -> int:
    return len(
        tuple(
            part
            for part in re.split(r"[。！？!?]+", text.strip())
            if part.strip()
        )
    )


def render_report(result: BenchResult) -> str:
    """Render full cards plus model outputs, or a zero-call cards-only report."""
    attempts = tuple(
        item.attempt for item in result.events if item.attempt is not None
    )
    successful_results = tuple(
        attempt.result for attempt in attempts if attempt.result is not None
    )
    actual_models = sorted({item.model for item in successful_results})
    actual_providers = sorted(
        {item.provider for item in successful_results if item.provider is not None}
    )
    stats = result.length_statistics
    provider_label = (
        f"`{result.requested_provider}`（已锁定且关闭自动回退）"
        if result.requested_provider is not None
        else "未锁定（延迟数字不可比）"
    )
    lines = [
        "# GSI 事件卡推断评测报告"
        if not result.cards_only
        else "# GSI 事件卡审阅报告（cards-only）",
        "",
        "## 本次运行",
        "",
        f"- 录制文件：`{result.recording_path}`",
        f"- 处理快照：{result.snapshot_count}",
        f"- 检测到事件：{result.detected_event_count}",
        f"- 策略入选事件：{result.selected_event_count}",
        f"- 本次实际评测：{len(result.events)}",
        f"- 因 `max_events` 截断：{result.truncated_event_count}",
        f"- 性格：`{result.personality_style}`",
        f"- 模式：{'只出卡，不调用模型' if result.cards_only else '模型推断'}",
        f"- 请求型号 ID：{f'`{result.requested_model}`' if result.requested_model else '未调用模型'}",
        f"- 锁定的上游服务商：{provider_label if not result.cards_only else '未调用模型'}",
        f"- 上游实际返回型号：{_join_or_unavailable(actual_models)}",
        f"- 运行时间：{result.run_timestamp.isoformat()}",
    ]
    if result.system_prompt is not None:
        prompt_summary = tuple(
            line for line in result.system_prompt.splitlines() if line.strip()
        )[:2]
        lines.extend(
            (
                f"- 读卡指南：{'已拼接' if result.include_reading_guide else '已跳过'}",
                "- 系统提示词摘要：",
                *(f"  - {line}" for line in prompt_summary),
            )
        )
    lines.extend(
        (
            "- 模板汉字数（去除占位符）："
            f"最小 {stats.minimum} / 中位 {_format_number(stats.median)} / "
            f"P90 {stats.p90} / 最大 {stats.maximum}",
            "",
            "## 逐条结果",
            "",
        )
    )
    if not result.events:
        lines.extend(("（没有策略入选事件。）", ""))
    for index, item in enumerate(result.events, 1):
        event = item.event
        round_label = (
            f"第 {event.round_number} 回合"
            if event.round_number is not None
            else "回合数未提供"
        )
        lines.extend(
            (
                f"### 事件 {index} —— {_EVENT_LABELS[event.type]}"
                f"（{_CATEGORY_LABELS[item.category]}），{round_label}",
                "",
                "发给模型的精简事件卡：",
                "",
                *(f"    {line}" for line in item.model_card.splitlines()),
                "",
                "人工核对用完整 GSI 事件卡：",
                "",
                *(f"    {line}" for line in item.event_card.splitlines()),
                "",
            )
        )
        if item.attempt is not None:
            lines.extend(
                (
                    f"模板句：{item.template_text}",
                    f"模型句：{_attempt_text(item.attempt)}",
                    "",
                    _attempt_checks(
                        item.attempt,
                        max_chars=stats.p90,
                        enforce_length_limit=result.personality_style != "inference",
                    ),
                    _attempt_metrics(item.attempt),
                    "",
                )
            )

    lines.extend(("## 数字汇总", ""))
    if result.cards_only:
        lines.extend(
            (
                "- 模型调用次数：0（cards-only）",
                f"- 原样输出 GSI 事件卡：{len(result.events)}",
            )
        )
    else:
        lines.extend(
            _summary_lines(
                result.events,
                actual_providers,
                enforce_length_limit=result.personality_style != "inference",
            )
        )
    return "\n".join(lines) + "\n"


def render_stream_analysis_report(
    result: AnalysisBenchResult,
    scores: AnalysisScoreFile | None = None,
) -> str:
    """Render streamed event/scene outputs, split latency, and optional human scores."""
    successful = tuple(
        item.attempt.result
        for item in result.events
        if item.attempt.result is not None
    )
    actual_models = sorted({item.model for item in successful})
    actual_providers = sorted(
        {item.provider for item in successful if item.provider is not None}
    )
    full_card_digest = _sha256_text(
        "\n\n".join(f"{item.case_id}\n{item.event_card}" for item in result.events)
    )
    model_card_digest = _sha256_text(
        "\n\n".join(f"{item.case_id}\n{item.model_card}" for item in result.events)
    )
    lines = [
        "# GSI 事件卡流式事实评测报告",
        "",
        "## 本次运行",
        "",
        "- 录制文件：" + "、".join(f"`{path}`" for path in result.recording_paths),
        f"- 处理快照：{result.snapshot_count}",
        f"- 检测到事件：{result.detected_event_count}",
        f"- 策略入选事件：{result.selected_event_count}",
        f"- 本次实际评测：{len(result.events)}",
        f"- 因 `max_events` 截断：{result.truncated_event_count}",
        f"- 请求型号 ID：`{result.requested_model}`",
        "- 锁定的上游服务商："
        + (
            f"`{result.requested_provider}`（关闭自动回退）"
            if result.requested_provider is not None
            else "未锁定（延迟数字不可比）"
        ),
        f"- 上游实际返回型号：{_join_or_unavailable(actual_models)}",
        f"- 实际上游：{_join_or_unavailable(actual_providers)}",
        "- 提示词：只响应【刚刚】事件或连续事件",
        f"- 事件/完整截止时间：{result.event_timeout_seconds:g}s / "
        f"{result.full_timeout_seconds:g}s",
        f"- 温度 / 完成上限：{ANALYSIS_TEMPERATURE:g} / "
        f"{result.max_completion_tokens} token",
        f"- 推理强度：{result.reasoning_effort}",
        f"- 固定随机种子：{result.seed}",
        f"- 提示词 SHA-256：`{_sha256_text(result.system_prompt)}`",
        f"- 模型输入卡集合 SHA-256：`{model_card_digest}`",
        f"- 完整核对卡集合 SHA-256：`{full_card_digest}`",
        f"- 运行时间：{result.run_timestamp.isoformat()}",
        "",
        "## 逐条结果",
        "",
    ]
    if not result.events:
        lines.extend(("（没有策略入选事件。）", ""))
    for index, item in enumerate(result.events, 1):
        lines.extend(
            (
                f"### {index}. `{item.case_id}`",
                "",
                f"- 类型：{_EVENT_LABELS[item.event.type]}（{_CATEGORY_LABELS[item.category]}）",
                f"- 来源：`{item.recording_path}`",
                "",
                "发给模型的精简事件卡：",
                "",
                *(f"    {line}" for line in item.model_card.splitlines()),
                "",
                "人工核对用完整 GSI 事件卡：",
                "",
                *(f"    {line}" for line in item.event_card.splitlines()),
                "",
            )
        )
        attempt = item.attempt
        if attempt.result is None:
            lines.append(f"调用失败：{attempt.error}")
            if attempt.partial_event_text is not None:
                lines.append(f"已收到事件：{attempt.partial_event_text}")
            if attempt.event_latency_seconds is not None:
                lines.append(f"事件延迟：{attempt.event_latency_seconds:.3f}s")
            lines.append("")
            continue
        streamed = attempt.result
        checks = attempt.checks
        assert checks is not None
        sentence_ok = (
            ANALYSIS_MIN_SCENE_SENTENCES
            <= checks.scene_sentence_count
            <= ANALYSIS_MAX_SCENE_SENTENCES
        )
        lines.extend(
            (
                f"核对：{streamed.audit_text}",
                f"事件：{streamed.event_text}",
                f"场面：{streamed.scene_text}",
                "",
                "自动检查："
                f"事件字数 {'✓' if not checks.event_exceeds_max_chars else '✗'} ｜ "
                f"场面句数 {checks.scene_sentence_count} "
                f"{'✓' if sentence_ok else '✗'} ｜ "
                "点位 "
                f"{'✓' if not checks.event_callout_terms and not checks.scene_callout_terms else '✗'} ｜ "
                "脏字 "
                f"{'✓' if not checks.event_raw_curses and not checks.scene_raw_curses else '✗'} ｜ "
                "否定总结 "
                f"{'✓' if not checks.negative_summary_terms else '✗'} ｜ "
                "越界措辞 "
                f"{'✓' if not checks.unsupported_inference_terms else '✗'}",
                f"延迟：事件 {streamed.event_latency_seconds:.3f}s ｜ "
                f"完整 {streamed.latency_seconds:.3f}s",
                f"Token：输入 {_optional_number(streamed.usage.prompt_tokens)} ｜ "
                f"输出 {_optional_number(streamed.usage.completion_tokens)} ｜ "
                f"花费 {_cost_label(streamed.usage.cost_usd)} ｜ "
                f"上游 {streamed.provider or '上游未提供'}",
                "",
            )
        )

    lines.extend(("## 数字汇总", ""))
    event_latencies = tuple(
        latency
        for item in result.events
        for latency in (
            item.attempt.result.event_latency_seconds
            if item.attempt.result is not None
            else item.attempt.event_latency_seconds,
        )
        if latency is not None
    )
    full_latencies = tuple(item.latency_seconds for item in successful)
    prompt_tokens = tuple(
        item.usage.prompt_tokens
        for item in successful
        if item.usage.prompt_tokens is not None
    )
    completion_tokens = tuple(
        item.usage.completion_tokens
        for item in successful
        if item.usage.completion_tokens is not None
    )
    costs = tuple(
        item.usage.cost_usd for item in successful if item.usage.cost_usd is not None
    )
    lines.extend(
        (
            f"- 调用次数：{len(result.events)}",
            f"- 成功：{len(successful)}；失败：{len(result.events) - len(successful)}",
            f"- 按时收到事件行：{len(event_latencies)}",
            f"- 事件延迟：{_latency_values(event_latencies)}",
            f"- 完整延迟：{_latency_values(full_latencies)}",
            f"- 输入 token：{_token_summary(prompt_tokens, len(successful))}",
            f"- 输出 token：{_token_summary(completion_tokens, len(successful))}",
            "- 总花费："
            + (
                f"${sum(costs):.6f}"
                if len(costs) == len(successful) and successful
                else "上游未完整提供"
            ),
        )
    )
    if scores is not None:
        lines.extend(("", "## 人工事实评分", ""))
        lines.extend(_score_summary_lines(result, scores))
    return "\n".join(lines) + "\n"


def load_analysis_scores(path: Path) -> AnalysisScoreFile:
    """Load one strict human score file without accepting unknown fields."""
    return AnalysisScoreFile.model_validate_json(path.read_text(encoding="utf-8"))


def load_event_answer_keys(path: Path) -> EventAnswerKeyFile:
    """Load the product-approved event-only rubric from a checked-in artifact."""
    return EventAnswerKeyFile.model_validate_json(path.read_text(encoding="utf-8"))


def load_universal_forbidden(path: Path) -> UniversalForbiddenFile:
    """Load project-wide unsupported claims without accepting unknown fields."""
    return UniversalForbiddenFile.model_validate_json(path.read_text(encoding="utf-8"))


def answer_key_sha256(path: Path) -> str:
    """Return the digest used to bind manual scores to an immutable rubric."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_analysis_event_lines(report_text: str) -> dict[str, str]:
    """Extract successful event outputs from a completed Markdown report."""
    headings = tuple(_ANALYSIS_REPORT_CASE_ID_PATTERN.finditer(report_text))
    event_lines: dict[str, str] = {}
    for index, heading in enumerate(headings):
        case_id = heading.group(1)
        block_end = (
            headings[index + 1].start()
            if index + 1 < len(headings)
            else len(report_text)
        )
        block = report_text[heading.end() : block_end]
        event_match = _ANALYSIS_REPORT_EVENT_LINE_PATTERN.search(block)
        if event_match is not None:
            event_lines[case_id] = event_match.group(1).strip()
    return event_lines


def apply_universal_forbidden(
    report_text: str,
    scores: AnalysisScoreFile,
    forbidden: UniversalForbiddenFile,
) -> tuple[AnalysisScoreFile, tuple[ForbiddenViolation, ...]]:
    """Mark literal forbidden-term hits wrong while preserving required-fact atoms."""
    event_lines = extract_analysis_event_lines(report_text)
    expected_ids = tuple(case.case_id for case in scores.cases)
    if set(event_lines) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(event_lines))
        extra = sorted(set(event_lines) - set(expected_ids))
        raise ValueError(
            f"报告事件行与评分 case_id 不一致：缺少 {missing}；多出 {extra}"
        )

    violations: list[ForbiddenViolation] = []
    adjusted_cases: list[CaseScore] = []
    for case in scores.cases:
        event_text = event_lines[case.case_id]
        matched_terms = tuple(term for term in forbidden.terms if term in event_text)
        if not matched_terms:
            adjusted_cases.append(case)
            continue
        violations.append(
            ForbiddenViolation(
                case_id=case.case_id,
                event_text=event_text,
                matched_terms=matched_terms,
            )
        )
        error = "通用禁项：" + "、".join(matched_terms)
        adjusted_event = case.event.model_copy(
            update={
                "whole_correct": False,
                "errors": (*case.event.errors, error),
            }
        )
        adjusted_cases.append(case.model_copy(update={"event": adjusted_event}))

    return (
        scores.model_copy(update={"cases": tuple(adjusted_cases)}),
        tuple(violations),
    )


def rescore_existing_analysis_report(
    report_path: Path,
    scores_path: Path,
    forbidden: UniversalForbiddenFile,
    *,
    expected_answer_key_sha256: str,
) -> ForbiddenRescoreResult:
    """Apply universal forbidden terms to an existing report without model access."""
    report_text = report_path.read_text(encoding="utf-8")
    scores = load_analysis_scores(scores_path)
    if scores.scope != "event_only":
        raise ValueError("通用禁项重评分只接受 event_only 评分")
    if scores.answer_key_sha256 != expected_answer_key_sha256:
        raise ValueError("评分文件绑定的 answer key SHA-256 与当前基准不一致")
    adjusted, violations = apply_universal_forbidden(report_text, scores, forbidden)
    return ForbiddenRescoreResult(
        report_path=report_path,
        scores_path=scores_path,
        original_scores=scores,
        adjusted_scores=adjusted,
        violations=violations,
        event_texts=tuple(extract_analysis_event_lines(report_text).values()),
    )


def render_forbidden_rescore_report(
    results: Sequence[ForbiddenRescoreResult],
    forbidden: UniversalForbiddenFile,
    *,
    forbidden_path: Path,
    answer_key_path: Path,
) -> str:
    """Render a self-contained comparison of old and forbidden-aware scores."""
    if not results:
        raise ValueError("通用禁项重评分至少需要一份报告")
    digest = answer_key_sha256(answer_key_path)
    event_texts = tuple(text for result in results for text in result.event_texts)
    lengths = calculate_length_statistics(event_texts)
    lines = [
        "# M3-T5.9 通用禁项离线重评分",
        "",
        "## 输入与方法",
        "",
        f"- 通用禁项：`{forbidden_path}`",
        f"- Answer key：`{answer_key_path}`",
        f"- Answer key SHA-256：`{digest}`",
        "- 执行方式：只读取既有 Markdown 事件行与人工评分 JSON，"
        "按字面命中通用禁项后调整整句判定；未构造 OpenRouter 客户端，未调用模型。",
        "- 原子事实分数保持原人工评分；命中任一通用禁项时，整句强制判错。",
        "",
        "## 通用禁项",
        "",
        "- A 类（GSI 完全不提供）：" + "、".join(forbidden.a_gsi_unavailable),
        "- B 类（推测语气与因果）："
        + "、".join(forbidden.b_inference_or_causality),
        "",
        "## 新旧结果",
        "",
        "| 报告 | 旧标准 | 新标准 | 新增错题 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for result in results:
        total = len(result.original_scores.cases)
        old_correct = sum(
            case.event.whole_correct for case in result.original_scores.cases
        )
        new_correct = sum(
            case.event.whole_correct for case in result.adjusted_scores.cases
        )
        newly_wrong = sum(
            old.event.whole_correct and not new.event.whole_correct
            for old, new in zip(
                result.original_scores.cases,
                result.adjusted_scores.cases,
                strict=True,
            )
        )
        lines.append(
            f"| `{result.report_path.name}` | {old_correct}/{total} "
            f"({old_correct / total:.1%}) | {new_correct}/{total} "
            f"({new_correct / total:.1%}) | {newly_wrong} |"
        )

    lines.extend(("", "## 禁项命中明细", ""))
    all_violations = tuple(
        (result.report_path.name, violation)
        for result in results
        for violation in result.violations
    )
    if not all_violations:
        lines.append("- 无命中；三份报告准确率不变。")
    else:
        for report_name, violation in all_violations:
            lines.append(
                f"- `{report_name}` / `{violation.case_id}`："
                f"命中「{'、'.join(violation.matched_terms)}」；"
                f"事件行「{violation.event_text}」"
            )

    if all_violations:
        lines.extend(
            (
                "",
                "## 与架构师预期的差异",
                "",
                "- 架构师预期三份准确率不变，但字面重评分得到下降。",
                "- 原因是规格 A 类明确列出「包点」，而既有事件行多次包含"
                "「反攻包点」；原扫描列举的搜索词中没有「包点」，因此漏掉了这些命中。",
                "- 本报告未为迎合预期而删除词条、增加豁免或改写既有输出。",
            )
        )

    lines.extend(
        (
            "",
            "## 事件行汉字数分布",
            "",
            f"- 样本：{len(event_texts)} 条",
            f"- 最小：{lengths.minimum}",
            f"- 中位：{lengths.median:g}",
            f"- P90：{lengths.p90}",
            f"- 最大：{lengths.maximum}",
        )
    )
    return "\n".join(lines) + "\n"


def write_forbidden_rescore_report(
    results: Sequence[ForbiddenRescoreResult],
    forbidden: UniversalForbiddenFile,
    *,
    forbidden_path: Path,
    answer_key_path: Path,
    output_path: Path,
) -> None:
    """Write a forbidden-aware comparison report without any model client."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_forbidden_rescore_report(
            results,
            forbidden,
            forbidden_path=forbidden_path,
            answer_key_path=answer_key_path,
        ),
        encoding="utf-8",
    )


def write_analysis_score_template(
    result: AnalysisBenchResult,
    path: Path,
    *,
    scope: Literal["event_and_scene", "event_only"] = "event_and_scene",
    answer_key_digest: str | None = None,
) -> None:
    """Write a complete, deliberately failing score sheet for human review."""
    if scope == "event_only" and answer_key_digest is None:
        raise ValueError("event_only 评分模板必须绑定 answer key SHA-256")
    payload = {
        "scope": scope,
        **(
            {"answer_key_sha256": answer_key_digest}
            if answer_key_digest is not None
            else {}
        ),
        "cases": [
            {
                "case_id": item.case_id,
                "event": {
                    "whole_correct": False,
                    "correct_atoms": 0,
                    "total_atoms": 1,
                    "errors": ["未评分"],
                    "notes": "",
                },
                **(
                    {
                        "scene": {
                            "whole_correct": False,
                            "correct_atoms": 0,
                            "total_atoms": 1,
                            "errors": ["未评分"],
                            "notes": "",
                        }
                    }
                    if scope == "event_and_scene"
                    else {}
                ),
            }
            for item in result.events
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _score_summary_lines(
    result: AnalysisBenchResult, scores: AnalysisScoreFile
) -> list[str]:
    return _score_summary_lines_for_ids(
        tuple(item.case_id for item in result.events), scores
    )


def _score_summary_lines_for_ids(
    expected_ids: tuple[str, ...], scores: AnalysisScoreFile
) -> list[str]:
    actual_ids = tuple(item.case_id for item in scores.cases)
    if len(set(actual_ids)) != len(actual_ids):
        raise ValueError("评分文件包含重复 case_id")
    if set(actual_ids) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(actual_ids))
        extra = sorted(set(actual_ids) - set(expected_ids))
        raise ValueError(f"评分 case_id 与报告不一致：缺少 {missing}；多出 {extra}")
    by_id = {item.case_id: item for item in scores.cases}
    ordered = tuple(by_id[case_id] for case_id in expected_ids)
    if not ordered:
        return ["- 没有可评分事件"]
    lines = [
        "- 评分范围："
        + ("仅事件短句" if scores.scope == "event_only" else "事件短句与场面描述")
    ]
    if scores.answer_key_sha256 is not None:
        lines.append(f"- Answer key SHA-256：`{scores.answer_key_sha256}`")
    scored_fields: list[tuple[str, tuple[FieldScore, ...]]] = [
        ("事件", tuple(item.event for item in ordered))
    ]
    if scores.scope == "event_and_scene":
        scenes = tuple(item.scene for item in ordered)
        assert all(scene is not None for scene in scenes)
        scored_fields.append(
            ("场面", tuple(scene for scene in scenes if scene is not None))
        )
    for label, fields in scored_fields:
        whole_rate = sum(item.whole_correct for item in fields) / len(fields)
        correct_atoms = sum(item.correct_atoms for item in fields)
        total_atoms = sum(item.total_atoms for item in fields)
        atom_rate = correct_atoms / total_atoms
        lines.append(
            f"- {label}：整句 {whole_rate:.1%} "
            f"{'✓' if whole_rate >= ANALYSIS_WHOLE_ACCURACY_TARGET else '✗'}；"
            f"原子事实 {atom_rate:.1%}（{correct_atoms}/{total_atoms}）"
            f" {'✓' if atom_rate >= ANALYSIS_ATOM_ACCURACY_TARGET else '✗'}"
        )
    return lines


def render_scored_analysis_report(
    report_text: str, scores: AnalysisScoreFile
) -> str:
    """Append validated human scores to an existing report without rerunning a model."""
    if "\n## 人工事实评分\n" in report_text:
        raise ValueError("报告已经包含人工事实评分")
    case_ids = tuple(_ANALYSIS_REPORT_CASE_ID_PATTERN.findall(report_text))
    if not case_ids:
        raise ValueError("报告中没有找到流式评测 case_id")
    summary = _score_summary_lines_for_ids(case_ids, scores)
    return (
        report_text.rstrip("\n")
        + "\n\n## 人工事实评分\n\n"
        + "\n".join(summary)
        + "\n"
    )


def write_scored_analysis_report(
    report_path: Path, scores_path: Path, output_path: Path
) -> None:
    """Validate and append a score file to a completed report, entirely offline."""
    report_text = report_path.read_text(encoding="utf-8")
    scores = load_analysis_scores(scores_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_scored_analysis_report(report_text, scores), encoding="utf-8"
    )


def write_stream_analysis_report(
    result: AnalysisBenchResult,
    output_path: Path,
    scores: AnalysisScoreFile | None = None,
) -> None:
    """Write one immutable streamed-analysis report."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_stream_analysis_report(result, scores), encoding="utf-8"
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_latency_report(result: LatencyExperimentResult) -> str:
    """Render the sequential A/B/C latency attribution experiment."""
    labels = (
        ("A", "仅 brother 性格文件；96 token 上限"),
        ("B", "reading.md + brother；96 token 上限"),
        ("C", "reading.md + inference；300 token 上限"),
    )
    provider_label = (
        f"`{result.requested_provider}`（已锁定且关闭自动回退）"
        if result.requested_provider is not None
        else "未锁定（延迟数字不可比）"
    )
    lines = [
        "# M3-T4 延迟归因实验",
        "",
        "## 实验设置",
        "",
        f"- 录制文件：`{result.recording_path}`",
        f"- 请求型号 ID：`{result.requested_model}`",
        f"- 上游服务商：{provider_label}",
        "- 执行顺序：A → B → C，串行、不并发，共用同一个 HTTP 客户端连接",
        "- 首次调用位于 A 组，会承担可能存在的一次性连接建立开销",
        f"- 运行时间：{result.run_timestamp.isoformat()}",
        "",
        "## 数字汇总",
        "",
        "| 组 | 配置 | 调用/成功/失败 | 输入 token 中位数 | 输出 token 中位数 | 延迟 P50 / P95 / 最大 | 单次花费中位数 |",
        "|---|---|---:|---:|---:|---|---:|",
    ]
    metrics = []
    for group, (label, description) in zip(result.groups, labels, strict=True):
        values = _latency_metrics(group.events)
        metrics.append(values)
        lines.append(
            f"| {label} | {description} | "
            f"{values.call_count}/{values.success_count}/{values.failure_count} | "
            f"{_optional_median(values.prompt_tokens)} | "
            f"{_optional_median(values.completion_tokens)} | "
            f"{_latency_values(values.latencies)} | "
            f"{_optional_cost_median(values.costs)} |"
        )
    lines.extend(("", "## 结论", "", _latency_conclusion(metrics)))
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class _LatencyMetrics:
    call_count: int
    success_count: int
    failure_count: int
    prompt_tokens: tuple[int, ...]
    completion_tokens: tuple[int, ...]
    latencies: tuple[float, ...]
    costs: tuple[float, ...]


def write_report(result: BenchResult, output_path: Path) -> None:
    """Write one benchmark report to the explicitly requested location."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_report(result), encoding="utf-8")


def write_latency_report(
    result: LatencyExperimentResult, output_path: Path
) -> None:
    """Write one A/B/C latency report to the explicitly requested location."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_latency_report(result), encoding="utf-8")


def _attempt_completion(
    *,
    model: str,
    provider: str | None,
    system_prompt: str,
    event_card: str,
    max_chars: int,
    max_tokens: int,
    enforce_length_limit: bool,
    client: LlmClientProtocol,
) -> BenchAttempt:
    try:
        result = client.complete(
            model=model,
            provider=provider,
            system_prompt=system_prompt,
            user_prompt=event_card,
            max_tokens=max_tokens,
            temperature=TEMPERATURE,
        )
    except LlmError as error:
        context = []
        if error.status_code is not None:
            context.append(f"HTTP {error.status_code}")
        if error.latency_seconds is not None:
            context.append(f"{error.latency_seconds:.3f}s")
        suffix = f"（{'，'.join(context)}）" if context else ""
        return BenchAttempt(result=None, error=f"{error}{suffix}", checks=None)
    return BenchAttempt(
        result=result,
        error=None,
        checks=check_output(
            result.text,
            max_chars=max_chars,
            enforce_length_limit=enforce_length_limit,
        ),
    )


def _chinese_character_count(text: str) -> int:
    return len(_HAN_CHARACTER_PATTERN.findall(_remove_placeholders(text)))


def _remove_placeholders(text: str) -> str:
    return _PLACEHOLDER_PATTERN.sub("", text).strip()


def _attempt_text(attempt: BenchAttempt) -> str:
    if attempt.result is not None:
        return attempt.result.text
    return f"调用失败：{attempt.error}"


def _attempt_checks(
    attempt: BenchAttempt,
    *,
    max_chars: int,
    enforce_length_limit: bool,
) -> str:
    if attempt.result is None or attempt.checks is None:
        return "自动检查：调用失败，未检查"
    checks = attempt.checks
    count = _chinese_character_count(attempt.result.text)
    length_label = (
        f"字数 {count}/{max_chars} "
        f"{'✓' if checks.exceeds_max_chars is False else '✗'}"
        if enforce_length_limit
        else f"字数 {count}（本轮不限字数）"
    )
    return (
        f"自动检查：{length_label} ｜ "
        f"点位 {'✓' if not checks.callout_terms else '✗ ' + '、'.join(checks.callout_terms)} ｜ "
        f"脏字 {'✓' if not checks.raw_curses else '✗ ' + '、'.join(checks.raw_curses)}"
    )


def _attempt_metrics(attempt: BenchAttempt) -> str:
    if attempt.result is None:
        return "延迟/Token/上游：调用失败"
    result = attempt.result
    return (
        f"延迟 {result.latency_seconds:.3f}s ｜ "
        f"输入 {_optional_number(result.usage.prompt_tokens)} token ｜ "
        f"输出 {_optional_number(result.usage.completion_tokens)} token ｜ "
        f"花费 {_cost_label(result.usage.cost_usd)} ｜ "
        f"上游 {result.provider or '上游未提供'}"
    )


def _summary_lines(
    events: Sequence[BenchEvent],
    actual_providers: Sequence[str],
    *,
    enforce_length_limit: bool,
) -> list[str]:
    attempts = tuple(
        event.attempt for event in events if event.attempt is not None
    )
    successes = tuple(attempt for attempt in attempts if attempt.result is not None)
    latencies = tuple(
        attempt.result.latency_seconds
        for attempt in successes
        if attempt.result is not None
    )
    prompt_tokens = tuple(
        attempt.result.usage.prompt_tokens
        for attempt in successes
        if attempt.result is not None and attempt.result.usage.prompt_tokens is not None
    )
    completion_tokens = tuple(
        attempt.result.usage.completion_tokens
        for attempt in successes
        if attempt.result is not None
        and attempt.result.usage.completion_tokens is not None
    )
    costs = tuple(
        attempt.result.usage.cost_usd
        for attempt in successes
        if attempt.result is not None and attempt.result.usage.cost_usd is not None
    )
    checks = tuple(attempt.checks for attempt in successes if attempt.checks is not None)
    latency_label = _latency_values(latencies)
    cost_label = (
        f"${sum(costs):.6f}"
        if successes and len(costs) == len(successes)
        else "上游未完整提供"
    )
    length_summary = (
        f"{sum(item.exceeds_max_chars is True for item in checks)}"
        if enforce_length_limit
        else "本轮不限字数"
    )
    return [
        f"- 调用次数：{len(attempts)}",
        f"- 成功：{len(successes)}；失败：{len(attempts) - len(successes)}",
        f"- 延迟：{latency_label}",
        f"- 输入 token：{_token_summary(prompt_tokens, len(successes))}",
        f"- 输出 token：{_token_summary(completion_tokens, len(successes))}",
        f"- 总花费：{cost_label}",
        f"- 超出字数上限：{length_summary}",
        f"- 命中点位词：{sum(bool(item.callout_terms) for item in checks)}",
        f"- 命中未替代脏字：{sum(bool(item.raw_curses) for item in checks)}",
        f"- 实际返回的上游服务商去重列表：{_join_or_unavailable(actual_providers)}",
    ]


def _latency_metrics(events: Sequence[BenchEvent]) -> _LatencyMetrics:
    attempts = tuple(
        event.attempt for event in events if event.attempt is not None
    )
    results = tuple(
        attempt.result for attempt in attempts if attempt.result is not None
    )
    return _LatencyMetrics(
        call_count=len(attempts),
        success_count=len(results),
        failure_count=len(attempts) - len(results),
        prompt_tokens=tuple(
            item.usage.prompt_tokens
            for item in results
            if item.usage.prompt_tokens is not None
        ),
        completion_tokens=tuple(
            item.usage.completion_tokens
            for item in results
            if item.usage.completion_tokens is not None
        ),
        latencies=tuple(item.latency_seconds for item in results),
        costs=tuple(
            item.usage.cost_usd for item in results if item.usage.cost_usd is not None
        ),
    )


def _latency_conclusion(metrics: Sequence[_LatencyMetrics]) -> str:
    if len(metrics) != 3 or any(not item.latencies for item in metrics):
        return "有分组缺少成功调用，无法归因延迟增量。"
    p50_a, p50_b, p50_c = (
        _percentile(item.latencies, 0.50) for item in metrics
    )
    prompt_delta = p50_b - p50_a
    output_delta = p50_c - p50_b
    if abs(prompt_delta) > abs(output_delta):
        source = "输入提示词变长"
    elif abs(output_delta) > abs(prompt_delta):
        source = "输出变长"
    else:
        source = "两者相当"
    return (
        f"P50 延迟 A→B {_signed_seconds(prompt_delta)}，"
        f"B→C {_signed_seconds(output_delta)}；"
        f"本次观察到的主要增量来自{source}。"
    )


def _latency_values(values: Sequence[float]) -> str:
    if not values:
        return "无成功调用"
    return (
        f"P50 {_percentile(values, 0.50):.3f}s / "
        f"P95 {_percentile(values, 0.95):.3f}s / 最大 {max(values):.3f}s"
    )


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * quantile) - 1]


def _token_summary(values: Sequence[int], success_count: int) -> str:
    if not values or len(values) != success_count:
        return "上游未完整提供"
    return f"总计 {sum(values)} / 平均 {statistics.fmean(values):.1f}"


def _optional_median(values: Sequence[int]) -> str:
    return f"{statistics.median(values):g}" if values else "上游未提供"


def _optional_cost_median(values: Sequence[float]) -> str:
    return f"${statistics.median(values):.6f}" if values else "上游未提供"


def _signed_seconds(value: float) -> str:
    return f"增加 {value:.3f}s" if value >= 0 else f"减少 {abs(value):.3f}s"


def _format_number(value: float) -> str:
    return f"{value:g}"


def _optional_number(value: int | None) -> str:
    return str(value) if value is not None else "上游未提供"


def _cost_label(value: float | None) -> str:
    return f"${value:.6f}" if value is not None else "上游未提供"


def _join_or_unavailable(values: Sequence[str]) -> str:
    return "、".join(f"`{value}`" for value in values) if values else "上游未提供"


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须至少为 1")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须至少为 0")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线评测 CS2 GSI 事件卡驱动的模型推断")
    parser.add_argument(
        "--replay",
        type=Path,
        action="append",
        help="GSI JSONL 录制文件；stream-analysis 模式可重复传入",
    )
    parser.add_argument(
        "--expected-event-type",
        action="append",
        choices=(
            "kill",
            "kill_headshot",
            "multi_kill",
            "death",
            "death_after_kill",
            "death_thrown_away",
            "round_win",
            "round_loss",
        ),
        help="与每个 --replay 对齐的目标事件类型；用于每场景只评测声明的目标事件",
    )
    parser.add_argument("--model", help="OpenRouter 型号 ID；cards-only 时可省略")
    parser.add_argument("--provider", help="锁定的 OpenRouter 上游服务商 slug")
    parser.add_argument("--out", type=Path, required=True, help="Markdown 报告输出路径")
    parser.add_argument(
        "--personality",
        choices=("brother", "caster", "inference"),
        default="brother",
        help="要评测的提示词性格",
    )
    parser.add_argument(
        "--max-events",
        type=_positive_int,
        default=40,
        help="最多评测多少个策略入选事件",
    )
    parser.add_argument(
        "--cards-only",
        action="store_true",
        help="只渲染 GSI 事件卡，不读取密钥或调用模型",
    )
    parser.add_argument(
        "--no-reading-guide",
        action="store_true",
        help="评测时跳过共享 reading.md；只用于对照实验",
    )
    parser.add_argument(
        "--latency-experiment",
        action="store_true",
        help="按 A→B→C 串行运行延迟归因实验",
    )
    parser.add_argument(
        "--stream-analysis",
        action="store_true",
        help="流式输出事件短句与场面描述，并分别测量延迟",
    )
    parser.add_argument(
        "--event-timeout-seconds",
        type=_positive_float,
        default=10.0,
        help="stream-analysis 等待完整事件行的秒数上限",
    )
    parser.add_argument(
        "--full-timeout-seconds",
        type=_positive_float,
        default=10.0,
        help="stream-analysis 等待完整场面描述的秒数上限",
    )
    parser.add_argument(
        "--seed",
        type=_nonnegative_int,
        default=ANALYSIS_SEED,
        help="stream-analysis 使用的固定随机种子",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high"),
        default="none",
        help="stream-analysis 的 OpenRouter 推理强度",
    )
    parser.add_argument(
        "--analysis-max-tokens",
        type=_positive_int,
        default=ANALYSIS_MAX_COMPLETION_TOKENS,
        help="stream-analysis 的最大完成 token 数",
    )
    parser.add_argument(
        "--scores",
        type=Path,
        help="人工评分 JSON；与 --score-report 一起离线追加准确率汇总",
    )
    parser.add_argument(
        "--score-report",
        type=Path,
        help="对已完成的流式报告离线追加人工评分，不读取密钥或调用模型",
    )
    parser.add_argument(
        "--score-template-out",
        type=Path,
        help="stream-analysis 同时写出待人工填写的评分模板",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run offline evaluation without exposing credentials in arguments."""
    args = _build_parser().parse_args(argv)
    score_report_mode = args.score_report is not None
    modes = sum(
        (
            args.cards_only,
            args.latency_experiment,
            args.stream_analysis,
            score_report_mode,
        )
    )
    if modes > 1:
        print(
            "错误：cards-only、latency-experiment、stream-analysis、score-report 互斥",
            file=sys.stderr,
        )
        return 2
    if args.no_reading_guide and (
        args.cards_only or args.latency_experiment or args.stream_analysis
    ):
        print("错误：该模式不能额外指定 --no-reading-guide", file=sys.stderr)
        return 2
    if args.scores is not None and not score_report_mode:
        print("错误：--scores 必须与 --score-report 一起使用", file=sys.stderr)
        return 2
    if score_report_mode and args.scores is None:
        print("错误：--score-report 必须同时提供 --scores", file=sys.stderr)
        return 2
    if args.score_template_out is not None and not args.stream_analysis:
        print("错误：--score-template-out 仅适用于 --stream-analysis", file=sys.stderr)
        return 2
    if not args.cards_only and not score_report_mode and args.model is None:
        print("错误：模型评测必须传入 --model", file=sys.stderr)
        return 2
    recordings = tuple(args.replay or ())
    if score_report_mode and recordings:
        print("错误：--score-report 不接受 --replay，避免误触发模型调用", file=sys.stderr)
        return 2
    if score_report_mode and (args.model is not None or args.provider is not None):
        print("错误：--score-report 不接受模型或服务商参数", file=sys.stderr)
        return 2
    if not score_report_mode and not recordings:
        print("错误：该模式必须至少传入一个 --replay", file=sys.stderr)
        return 2
    if not args.stream_analysis and not score_report_mode and len(recordings) != 1:
        print("错误：只有 --stream-analysis 可重复传入 --replay", file=sys.stderr)
        return 2
    expected_event_types = tuple(args.expected_event_type or ())
    if expected_event_types and not args.stream_analysis:
        print("错误：--expected-event-type 仅适用于 --stream-analysis", file=sys.stderr)
        return 2
    if expected_event_types and len(expected_event_types) != len(recordings):
        print("错误：--expected-event-type 数量必须与 --replay 数量一致", file=sys.stderr)
        return 2

    if score_report_mode:
        assert args.score_report is not None
        assert args.scores is not None
        try:
            write_scored_analysis_report(args.score_report, args.scores, args.out)
        except (FileNotFoundError, ValueError) as error:
            print(f"错误：{error}", file=sys.stderr)
            return 2
        print(f"人工评分报告已写入：{args.out}")
        return 0

    recording = recordings[0]

    if args.cards_only:
        result = run_bench(
            recording,
            model=None,
            provider=None,
            personality_style=args.personality,
            client=None,
            max_events=args.max_events,
            cards_only=True,
        )
        write_report(result, args.out)
        print(f"GSI 事件卡报告已写入：{args.out}")
        return 0

    try:
        client = OpenRouterClient.from_env()
    except LlmError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    try:
        assert args.model is not None
        if args.stream_analysis:
            analysis = run_stream_analysis(
                recordings,
                model=args.model,
                provider=args.provider,
                client=client,
                max_events=args.max_events,
                event_timeout_seconds=args.event_timeout_seconds,
                full_timeout_seconds=args.full_timeout_seconds,
                max_completion_tokens=args.analysis_max_tokens,
                reasoning_effort=args.reasoning_effort,
                seed=args.seed,
                expected_event_types=expected_event_types or None,
            )
            write_stream_analysis_report(analysis, args.out)
            if args.score_template_out is not None:
                write_analysis_score_template(analysis, args.score_template_out)
        elif args.latency_experiment:
            experiment = run_latency_experiment(
                recording,
                model=args.model,
                provider=args.provider,
                client=client,
                max_events=args.max_events,
            )
            write_latency_report(experiment, args.out)
        else:
            result = run_bench(
                recording,
                model=args.model,
                provider=args.provider,
                personality_style=args.personality,
                client=client,
                max_events=args.max_events,
                include_reading_guide=not args.no_reading_guide,
            )
            write_report(result, args.out)
    except (FileNotFoundError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    finally:
        client.close()
    print(f"评测报告已写入：{args.out}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
