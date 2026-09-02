from __future__ import annotations

from dataclasses import replace
import re

import pytest
from pydantic import ValidationError

import pet.core.belief.protocol as protocol
from pet.core.belief.models import (
    EvidenceEvent,
    FrameObservationPayload,
    ObservationEnvelope,
)
from pet.core.belief.protocol import (
    PROTOCOL_VERSION,
    ParsedObservation,
    example_observation,
    parse_observation,
    system_format_section,
    to_bbox,
    user_format_directive,
)


def _parsed(*, pos_mode: str = "grid144") -> ParsedObservation:
    return parse_observation(
        example_observation(pos_mode),
        pos_mode=pos_mode,  # type: ignore[arg-type]
    )


def _envelope(**updates: object) -> ObservationEnvelope:
    values: dict[str, object] = {
        "frame_seq": 7,
        "frame_gap": 1,
        "coverage_mode": "enumerative_roi",
        "covered_regions": ["r8c5", "r9c6"],
        "enumeration_complete": True,
        "camera_change": "none",
    }
    values.update(updates)
    return ObservationEnvelope.model_validate(values)


def _payload(**updates: object) -> FrameObservationPayload:
    parsed = _parsed()
    values: dict[str, object] = {
        "raw_text": example_observation("grid144"),
        "parsed": parsed,
        "protocol_version": PROTOCOL_VERSION,
        "pos_mode": "grid144",
        "prompt_language": "en",
        "prompt_hash": "sha256:fixture",
        "envelope": _envelope(),
        "latency_ms": 123.0,
        "ttft_ms": 75.0,
        "visible_output_tokens": 30,
        "input_tokens": 200,
        "output_tokens": 30,
        "cost_usd": 0.0001,
        "actual_model": "fixture-model",
        "actual_provider": "fixture-provider",
        "user_prompt": "fixture prompt",
        "provider_truncated": False,
        "drop_reason": None,
    }
    values.update(updates)
    return FrameObservationPayload.model_validate(values)


def _event(
    *,
    payload: FrameObservationPayload | None = None,
    outcome: str = "ok",
    **updates: object,
) -> EvidenceEvent:
    values: dict[str, object] = {
        "evidence_id": "f7:fast:1",
        "source": "fast",
        "kind": "frame_observation",
        "root_capture_id": "f7",
        "observed_at": 7.0,
        "learned_at": 7.123,
        "scope": None,
        "payload": payload or _payload(),
        "derived_from": [],
        "context_version": None,
        "outcome": outcome,
    }
    values.update(updates)
    return EvidenceEvent.model_validate(values)


def test_parser_accepts_all_opcodes_and_preserves_physical_line_numbers() -> None:
    text = """\

S mode gameplay
S place open area
S lighting dim  with  gaps
E o1 r8c5-r9c6 generic object
E o2 r1c1 interface element
A o1 condition stationary
R o1 near o2
V o1 moving player
U o1 identity unclear
U r16c9 category uncertain
.
"""
    parsed = parse_observation(text, pos_mode="grid144")

    assert parsed.parse_errors == 0
    assert parsed.entities_declared == ["o1", "o2"]
    assert parsed.missing_required_s == []
    assert parsed.accepted.S[2].value == "dim  with  gaps"
    assert parsed.accepted.E[0].line_no == 5
    assert parsed.accepted.E[0].position.bbox == pytest.approx(
        (4 / 9, 7 / 16, 6 / 9, 9 / 16)
    )
    assert parsed.accepted.R[0].object == "o2"
    assert parsed.accepted.V[0].target == "player"
    assert parsed.accepted.U[1].position is not None
    assert parsed.accepted.U[1].position.bbox == pytest.approx(
        (8 / 9, 15 / 16, 1, 1)
    )
    assert not parsed.truncated


def test_bbox_parser_accepts_entity_and_position_uncertainty_subjects() -> None:
    text = """\
S mode gameplay
S place open area
E o1 0.100 0.200 0.300 0.400 generic object
U o1 identity unclear
U 0.999 0 0.002 1 category unclear
.
"""
    parsed = parse_observation(text, pos_mode="bbox")

    assert parsed.parse_errors == 0
    assert parsed.accepted.E[0].position.bbox == pytest.approx((0.1, 0.2, 0.4, 0.6))
    assert parsed.accepted.U[0].position is None
    assert parsed.accepted.U[1].position is not None
    assert parsed.accepted.U[1].position.bbox == pytest.approx((0.999, 0, 1, 1))


def test_parser_strips_for_parsing_but_preserves_accepted_raw_text() -> None:
    parsed = parse_observation(
        "  S mode gameplay  \r\n.\r\n",
        pos_mode="grid144",
    )

    assert parsed.parse_errors == 0
    assert parsed.accepted.S[0].value == "gameplay"
    assert parsed.accepted.S[0].raw == "  S mode gameplay  "


def test_duplicate_rules_keep_expected_lines_and_do_not_deduplicate_u() -> None:
    text = """\
S mode gameplay
S mode menu
S place open area
E o1 r8c5 generic object
A o1 condition still
A o1 condition moving
R o1 near player
R o1 near player
V o1 moving
V o1 moving
U o1 identity unclear
U o1 identity unclear
.
"""
    parsed = parse_observation(text, pos_mode="grid144")

    assert [line.value for line in parsed.accepted.S if line.facet == "mode"] == ["menu"]
    assert [line.value for line in parsed.accepted.A] == ["moving"]
    assert len(parsed.accepted.R) == 1
    assert len(parsed.accepted.V) == 1
    assert len(parsed.accepted.U) == 2
    assert parsed.duplicates == 4
    assert {item.opcode for item in parsed.duplicates_detail} == {"S", "A", "R", "V"}
    assert parsed.parse_errors == 0


def test_duplicate_entity_is_rejected_and_first_declaration_remains_valid() -> None:
    text = """\
E o1 r1c1 generic object
E o1 r2c2 another object
A o1 condition visible
.
"""
    parsed = parse_observation(text, pos_mode="grid144")

    assert parsed.entities_declared == ["o1"]
    assert len(parsed.accepted.E) == 1
    assert len(parsed.accepted.A) == 1
    assert parsed.rejected[0].reason == "duplicate_entity"
    assert parsed.rejected[0].raw == "E o1 r2c2 another object"
    assert parsed.duplicates == 0


@pytest.mark.parametrize(
    ("line", "reason", "pos_mode"),
    [
        ("Q unknown", "unknown_opcode", "grid144"),
        ("S mode", "field_count", "grid144"),
        ("R o1 near player extra", "field_count", "grid144"),
        ("A o1 condition visible", "undeclared_ref", "grid144"),
        ("S bad-facet value", "bad_token", "grid144"),
        ("E o1 c5r8 generic object", "bad_pos", "grid144"),
        ("E o1 r17c1 generic object", "bad_pos", "grid144"),
        ("E o1 0 0 0 1 generic object", "bad_pos", "bbox"),
        ("S  mode gameplay", "bad_spacing", "grid144"),
        ("S\tmode gameplay", "bad_spacing", "grid144"),
        ("S mode game\vplay", "bad_spacing", "grid144"),
        ("S mode game\x00play", "bad_line", "grid144"),
    ],
)
def test_malformed_lines_never_raise_and_keep_raw_reason_and_count(
    line: str,
    reason: str,
    pos_mode: str,
) -> None:
    parsed = parse_observation(
        f"{line}\n.",
        pos_mode=pos_mode,  # type: ignore[arg-type]
    )

    assert parsed.parse_errors == 1
    assert parsed.rejected[0].raw == line
    assert parsed.rejected[0].reason == reason
    assert parsed.over_limit == 0


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("E player r1c1 generic object", "bad_token"),
        ("E o0 r1c1 generic object", "bad_token"),
        ("R o1 near o0", "bad_token"),
        ("V o1 moving o2", "undeclared_ref"),
        ("U player identity unclear", "bad_token"),
    ],
)
def test_invalid_or_undeclared_entity_references_are_rejected(
    line: str,
    reason: str,
) -> None:
    prefix = "E o1 r1c1 generic object\n" if not line.startswith("E ") else ""
    parsed = parse_observation(f"{prefix}{line}\n.", pos_mode="grid144")

    assert parsed.parse_errors == 1
    assert parsed.rejected[0].raw == line
    assert parsed.rejected[0].reason == reason


def test_rejected_entity_declaration_does_not_declare_its_identifier() -> None:
    parsed = parse_observation(
        "E o2 c5r8 generic object\nA o2 condition visible\n.",
        pos_mode="grid144",
    )

    assert parsed.entities_declared == []
    assert [item.reason for item in parsed.rejected] == ["bad_pos", "undeclared_ref"]


def test_missing_required_statements_are_metadata_not_parse_errors() -> None:
    parsed = parse_observation("S weather clear\n.", pos_mode="grid144")

    assert parsed.parse_errors == 0
    assert parsed.missing_required_s == ["mode", "place"]
    assert not parsed.truncated


def test_only_terminator_is_a_valid_empty_observation() -> None:
    parsed = parse_observation(".", pos_mode="grid144")

    assert parsed.accepted.model_dump() == {key: [] for key in "SEARVU"}
    assert parsed.rejected == []
    assert parsed.parse_errors == 0
    assert parsed.duplicates == 0
    assert not parsed.truncated


def test_missing_terminator_marks_truncated_without_adding_parse_error() -> None:
    parsed = parse_observation("S mode gameplay", pos_mode="grid144")

    assert parsed.parse_errors == 0
    assert parsed.truncated


@pytest.mark.parametrize("trailing", [".", "S place open area"])
def test_second_terminator_and_content_after_terminator_are_trailing(
    trailing: str,
) -> None:
    parsed = parse_observation(
        f"S mode gameplay\n.\n{trailing}",
        pos_mode="grid144",
    )

    assert parsed.truncated
    assert parsed.parse_errors == 1
    assert parsed.rejected[0].reason == "trailing"
    assert parsed.rejected[0].raw == trailing


def test_over_limit_counts_only_nonempty_lines_before_terminator() -> None:
    parsed = parse_observation(
        "S mode gameplay\n\nS place open area\nS weather clear\n.",
        pos_mode="grid144",
        max_lines=2,
    )

    assert parsed.over_limit == 1
    assert parsed.parse_errors == 1
    assert parsed.rejected[0].reason == "over_limit"
    assert parsed.rejected[0].raw == "S weather clear"
    assert parsed.truncated


def test_programmer_errors_raise_value_error() -> None:
    with pytest.raises(ValueError, match="unknown pos_mode"):
        parse_observation(".", pos_mode="polar")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_lines"):
        parse_observation(".", pos_mode="grid144", max_lines=0)


def test_grid_positions_convert_to_fixed_normalized_boxes() -> None:
    assert to_bbox("r1c1", pos_mode="grid144") == pytest.approx((0, 0, 1 / 9, 1 / 16))
    assert to_bbox("r16c9", pos_mode="grid144") == pytest.approx((8 / 9, 15 / 16, 1, 1))
    assert to_bbox("r8c5-r9c6", pos_mode="grid144") == pytest.approx(
        (4 / 9, 7 / 16, 6 / 9, 9 / 16)
    )


@pytest.mark.parametrize(
    "position",
    ["c5r8", "r0c1", "r17c1", "r1c10", "r9c6-r8c5", "r1c2-r2c1"],
)
def test_invalid_grid_positions_are_rejected(position: str) -> None:
    with pytest.raises(ValueError):
        to_bbox(position, pos_mode="grid144")


def test_bbox_positions_validate_precision_size_and_endpoint_tolerance() -> None:
    assert to_bbox("0.100 0.200 0.300 0.400", pos_mode="bbox") == pytest.approx(
        (0.1, 0.2, 0.4, 0.6)
    )
    assert to_bbox("0.999 0 0.002 1", pos_mode="bbox") == pytest.approx(
        (0.999, 0, 1, 1)
    )
    for invalid in (
        "-0.1 0 0.2 0.2",
        "0 0 0 0.2",
        "0 0 0.2 0",
        "0.0000 0 0.2 0.2",
        ".1 0 0.2 0.2",
        "1e-1 0 0.2 0.2",
        "0.999 0 0.003 1",
        "0 0 0.2",
    ):
        with pytest.raises(ValueError):
            to_bbox(invalid, pos_mode="bbox")


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize("pos_mode", ["grid144", "bbox"])
def test_generated_examples_round_trip_for_all_language_and_position_combinations(
    language: str,
    pos_mode: str,
) -> None:
    section = system_format_section(
        pos_mode,  # type: ignore[arg-type]
        language,  # type: ignore[arg-type]
    )
    examples = example_observation(pos_mode)  # type: ignore[arg-type]
    parsed = parse_observation(
        examples,
        pos_mode=pos_mode,  # type: ignore[arg-type]
    )

    assert parsed.parse_errors == 0
    assert not parsed.truncated
    assert parsed.missing_required_s == []
    assert "S mode gameplay" in section
    assert "S place open area" in section
    assert "S lighting dim" in section
    assert all(getattr(parsed.accepted, opcode) for opcode in "SEARVU")
    assert examples.isascii()


def test_generated_sections_define_every_opcode_and_all_required_rules() -> None:
    zh = system_format_section("grid144", "zh")
    en = system_format_section("bbox", "en")

    for opcode in "SEARVU":
        assert re.search(rf"(?m)^{opcode} ", zh)
        assert re.search(rf"(?m)^{opcode} ", en)
    assert "r<行>c<列>" in zh
    assert "x y w h" in en
    assert "只写正证据" in zh
    assert "拿不准写 U" in zh
    assert "不得转写画面里的文字或数字" in zh
    assert "协议坐标所需的数字" in zh
    assert "不比较、不回溯、不写游戏名" in zh
    assert "词汇使用英文" in zh
    assert "现在进行时" in zh
    assert "positive evidence only" in en
    assert "Do not transcribe text or numbers" in en
    assert "coordinate numbers" in en
    assert "present-progressive" in en
    assert "period terminator" in en


def test_generated_examples_contain_no_concrete_game_content() -> None:
    examples = example_observation("grid144")
    banned = {
        "sword",
        "potion",
        "quest",
        "boss",
        "enemy",
        "inventory",
        "health",
        "mana",
        "attack",
        "combat",
        "minecraft",
        "cs2",
    }

    assert examples.isascii()
    assert not banned.intersection(re.findall(r"[a-z0-9_]+", examples.lower()))


@pytest.mark.parametrize("language", ["zh", "en"])
@pytest.mark.parametrize(
    "coverage_mode",
    ["enumerative_roi", "salient_positive_only"],
)
def test_user_format_directive_has_no_mechanical_numbers(
    language: str,
    coverage_mode: str,
) -> None:
    directive = user_format_directive(
        coverage_mode,  # type: ignore[arg-type]
        language,  # type: ignore[arg-type]
    )

    assert not re.search(r"\d", directive)
    if coverage_mode == "enumerative_roi":
        expected = "全部显著对象" if language == "zh" else "every salient object"
    else:
        expected = "只写画面中的显著对象" if language == "zh" else "only salient objects"
    assert expected in directive


def test_opcode_field_change_without_example_change_breaks_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definitions = tuple(
        replace(item, fields=(*item.fields, "facet")) if item.code == "E" else item
        for item in protocol._OPCODE_DEFINITIONS
    )
    monkeypatch.setattr(protocol, "_OPCODE_DEFINITIONS", definitions)

    with pytest.raises(ValueError, match="example fields do not match opcode E"):
        system_format_section("grid144", "en")


def test_observation_envelope_validates_region_codes_and_completeness() -> None:
    assert _envelope().enumeration_complete
    assert not _envelope(
        coverage_mode="salient_positive_only",
        covered_regions=[],
        enumeration_complete=False,
    ).enumeration_complete

    for updates in (
        {"covered_regions": ["c5r8"]},
        {"covered_regions": ["r17c1"]},
        {"covered_regions": [], "enumeration_complete": True},
        {
            "coverage_mode": "salient_positive_only",
            "enumeration_complete": True,
        },
    ):
        with pytest.raises(ValidationError):
            _envelope(**updates)


def test_frame_observation_models_match_the_persisted_contract_fields() -> None:
    assert set(ObservationEnvelope.model_fields) == {
        "frame_seq",
        "frame_gap",
        "coverage_mode",
        "covered_regions",
        "enumeration_complete",
        "camera_change",
    }
    assert set(FrameObservationPayload.model_fields) == {
        "raw_text",
        "parsed",
        "protocol_version",
        "pos_mode",
        "prompt_language",
        "prompt_hash",
        "envelope",
        "latency_ms",
        "ttft_ms",
        "visible_output_tokens",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "actual_model",
        "actual_provider",
        "user_prompt",
        "provider_truncated",
        "drop_reason",
    }


def test_frame_payload_protocol_and_position_modes_must_match_parsed_output() -> None:
    mismatched_version = _parsed().model_copy(update={"protocol_version": "1.0"})
    with pytest.raises(ValidationError, match="protocol versions must match"):
        _payload(parsed=mismatched_version)

    with pytest.raises(ValidationError, match="position modes must match"):
        _payload(parsed=_parsed(pos_mode="bbox"))


def test_complete_enumeration_requires_complete_clean_model_output() -> None:
    clean = _payload()
    assert clean.envelope.enumeration_complete

    bad_parse = parse_observation("Q invalid\n.", pos_mode="grid144")
    truncated = parse_observation("S mode gameplay", pos_mode="grid144")
    for updates in (
        {"parsed": bad_parse},
        {"parsed": truncated},
        {"provider_truncated": True},
        {"parsed": None},
    ):
        with pytest.raises(ValidationError, match="complete enumeration"):
            _payload(**updates)


def test_frame_observation_ok_outcome_round_trips_and_allows_truncation() -> None:
    event = _event()
    assert EvidenceEvent.model_validate(event.model_dump()) == event

    truncated = parse_observation("S mode gameplay", pos_mode="grid144")
    payload = _payload(
        parsed=truncated,
        envelope=_envelope(enumeration_complete=False),
        provider_truncated=True,
    )
    assert _event(payload=payload).outcome == "ok"


@pytest.mark.parametrize("outcome", ["dropped", "superseded", "failed"])
def test_frame_observation_non_ok_outcomes_require_empty_failed_payload(
    outcome: str,
) -> None:
    payload = _payload(
        raw_text="",
        parsed=None,
        envelope=_envelope(enumeration_complete=False),
        drop_reason="fixture failure",
    )
    event = _event(payload=payload, outcome=outcome)

    assert event.outcome == outcome
    assert event.payload == payload


@pytest.mark.parametrize(
    "updates",
    [
        {"raw_text": ""},
        {"parsed": None, "envelope": _envelope(enumeration_complete=False)},
        {"drop_reason": "unexpected"},
    ],
)
def test_frame_observation_ok_outcome_rejects_inconsistent_payload(
    updates: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="successful frame observation"):
        _event(payload=_payload(**updates))


@pytest.mark.parametrize(
    "updates",
    [
        {"raw_text": "unexpected"},
        {"parsed": _parsed()},
        {"drop_reason": None},
    ],
)
def test_frame_observation_non_ok_outcome_rejects_inconsistent_payload(
    updates: dict[str, object],
) -> None:
    failure_values: dict[str, object] = {
        "raw_text": "",
        "parsed": None,
        "envelope": _envelope(enumeration_complete=False),
        "drop_reason": "fixture failure",
    }
    failure_values.update(updates)
    payload = _payload(**failure_values)

    with pytest.raises(ValidationError, match="non-success frame observation"):
        _event(payload=payload, outcome="failed")


def test_frame_observation_kind_requires_fast_source_and_matching_payload() -> None:
    with pytest.raises(ValidationError, match="source does not match"):
        _event(source="deep")
    with pytest.raises(ValidationError, match="payload does not match"):
        _event(kind="fast_observation")
