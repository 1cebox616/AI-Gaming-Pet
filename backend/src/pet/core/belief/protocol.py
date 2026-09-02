"""Observation-language protocol definitions, parsing, and prompt rendering."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from typing import Callable, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pet.core.capture import DEFAULT_BLOCK_GRID

PROTOCOL_VERSION = "1.1"
# This value awaits measurement in W-T0c.
DEFAULT_MAX_LINES = 24

GRID_COLUMNS, GRID_ROWS = DEFAULT_BLOCK_GRID
assert (GRID_COLUMNS, GRID_ROWS) == (9, 16)

PosMode = Literal["grid144", "bbox"]
PromptLanguage = Literal["zh", "en"]
CoverageMode = Literal["salient_positive_only", "enumerative_roi"]
RejectReason = Literal[
    "unknown_opcode",
    "field_count",
    "undeclared_ref",
    "bad_token",
    "bad_pos",
    "bad_spacing",
    "bad_line",
    "trailing",
    "over_limit",
    "duplicate_entity",
]

_SNAKE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_ENTITY_PATTERN = re.compile(r"^o[1-9]\d*$")
_GRID_POSITION_PATTERN = re.compile(
    r"^r(?P<row0>[1-9]\d*)c(?P<column0>[1-9]\d*)"
    r"(?:-r(?P<row1>[1-9]\d*)c(?P<column1>[1-9]\d*))?$"
)
_BBOX_NUMBER_PATTERN = re.compile(r"^(?:0(?:\.\d{1,3})?|1(?:\.0{1,3})?)$")
_BBOX_END_TOLERANCE = Decimal("1.001")


class ProtocolModel(BaseModel):
    """Strict base for protocol values persisted inside evidence."""

    model_config = ConfigDict(extra="forbid")


class ParsedPosition(ProtocolModel):
    raw: str
    mode: PosMode
    bbox: tuple[float, float, float, float]


class StatementLine(ProtocolModel):
    opcode: Literal["S"] = "S"
    line_no: int = Field(ge=1)
    raw: str
    facet: str
    value: str


class EntityLine(ProtocolModel):
    opcode: Literal["E"] = "E"
    line_no: int = Field(ge=1)
    raw: str
    entity: str
    position: ParsedPosition
    label: str


class AttributeLine(ProtocolModel):
    opcode: Literal["A"] = "A"
    line_no: int = Field(ge=1)
    raw: str
    entity: str
    facet: str
    value: str


class RelationLine(ProtocolModel):
    opcode: Literal["R"] = "R"
    line_no: int = Field(ge=1)
    raw: str
    subject: str
    relation: str
    object: str


class ActionLine(ProtocolModel):
    opcode: Literal["V"] = "V"
    line_no: int = Field(ge=1)
    raw: str
    subject: str
    action: str
    target: str | None


class UncertaintyLine(ProtocolModel):
    opcode: Literal["U"] = "U"
    line_no: int = Field(ge=1)
    raw: str
    subject: str
    position: ParsedPosition | None
    facet: str
    value: str


class AcceptedLines(ProtocolModel):
    S: list[StatementLine] = Field(default_factory=list)
    E: list[EntityLine] = Field(default_factory=list)
    A: list[AttributeLine] = Field(default_factory=list)
    R: list[RelationLine] = Field(default_factory=list)
    V: list[ActionLine] = Field(default_factory=list)
    U: list[UncertaintyLine] = Field(default_factory=list)


class RejectedLine(ProtocolModel):
    line_no: int = Field(ge=1)
    raw: str
    reason: RejectReason
    message: str | None = None


class DuplicateDetail(ProtocolModel):
    opcode: Literal["S", "A", "R", "V"]
    key: list[str]
    line_no: int = Field(ge=1)
    raw: str
    kept_line_no: int = Field(ge=1)


class ParsedObservation(ProtocolModel):
    accepted: AcceptedLines
    rejected: list[RejectedLine]
    parse_errors: int = Field(ge=0)
    duplicates: int = Field(ge=0)
    duplicates_detail: list[DuplicateDetail]
    over_limit: int = Field(ge=0)
    truncated: bool
    entities_declared: list[str]
    missing_required_s: list[Literal["mode", "place"]]
    protocol_version: Literal["1.1"] = PROTOCOL_VERSION
    pos_mode: PosMode

    @model_validator(mode="after")
    def validate_counts(self) -> ParsedObservation:
        if self.parse_errors != len(self.rejected):
            raise ValueError("parse_errors must equal the rejected-line count")
        if self.duplicates != len(self.duplicates_detail):
            raise ValueError("duplicates must equal the duplicate-detail count")
        if self.over_limit != sum(
            item.reason == "over_limit" for item in self.rejected
        ):
            raise ValueError("over_limit must equal the over-limit rejection count")
        return self


@dataclass(frozen=True)
class _Example:
    values: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict[str, str]:
        return dict(self.values)


@dataclass(frozen=True)
class _OpcodeDefinition:
    code: Literal["S", "E", "A", "R", "V", "U"]
    fields: tuple[str, ...]
    meaning_zh: str
    meaning_en: str
    examples: tuple[_Example, ...]


_OPCODE_DEFINITIONS = (
    _OpcodeDefinition(
        "S",
        ("facet", "text"),
        "描述整幅画面；mode 和 place 每帧各写一次，其他 facet 开放",
        "Describe the whole frame; emit mode and place once each, with other facets open",
        (
            _Example((("facet", "mode"), ("text", "gameplay"))),
            _Example((("facet", "place"), ("text", "open area"))),
            _Example((("facet", "lighting"), ("text", "dim"))),
        ),
    ),
    _OpcodeDefinition(
        "E",
        ("entity_decl", "position", "text"),
        "声明本帧看见的局部对象、位置和开放标签",
        "Declare a frame-local observed object, its position, and an open label",
        (
            _Example(
                (("entity_decl", "o1"), ("position", "@center"), ("text", "generic object"))
            ),
        ),
    ),
    _OpcodeDefinition(
        "A",
        ("entity_ref", "facet", "text"),
        "描述已声明对象的一个属性",
        "Describe one attribute of an already declared object",
        (
            _Example(
                (("entity_ref", "o1"), ("facet", "condition"), ("text", "stationary"))
            ),
        ),
    ),
    _OpcodeDefinition(
        "R",
        ("entity_ref", "relation", "object_ref"),
        "描述已声明对象与另一对象或 player 的关系",
        "Describe a relation from a declared object to another object or player",
        (
            _Example(
                (("entity_ref", "o1"), ("relation", "near"), ("object_ref", "player"))
            ),
        ),
    ),
    _OpcodeDefinition(
        "V",
        ("entity_ref", "action", "optional_object_ref"),
        "描述已声明对象当前正在进行的动作，可带宾语",
        "Describe a declared object's current action, optionally with a target",
        (
            _Example((("entity_ref", "o1"), ("action", "moving"))),
        ),
    ),
    _OpcodeDefinition(
        "U",
        ("uncertain_subject", "facet", "text"),
        "记录对已声明对象或某位置拿不准的观察",
        "Record an uncertain observation about a declared object or position",
        (
            _Example(
                (("uncertain_subject", "o1"), ("facet", "identity"), ("text", "unclear"))
            ),
        ),
    ),
)
_OPCODES = {definition.code: definition for definition in _OPCODE_DEFINITIONS}


class _LineFailure(Exception):
    def __init__(self, reason: RejectReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _validate_pos_mode(pos_mode: str) -> PosMode:
    if pos_mode not in {"grid144", "bbox"}:
        raise ValueError(f"unknown pos_mode: {pos_mode!r}")
    return cast(PosMode, pos_mode)


def _validate_language(language: str) -> PromptLanguage:
    if language not in {"zh", "en"}:
        raise ValueError(f"unknown language: {language!r}")
    return cast(PromptLanguage, language)


def _validate_coverage_mode(coverage_mode: str) -> CoverageMode:
    if coverage_mode not in {"salient_positive_only", "enumerative_roi"}:
        raise ValueError(f"unknown coverage_mode: {coverage_mode!r}")
    return cast(CoverageMode, coverage_mode)


def to_bbox(position: str, *, pos_mode: PosMode) -> tuple[float, float, float, float]:
    """Validate one encoded position and return normalized x0,y0,x1,y1."""

    mode = _validate_pos_mode(pos_mode)
    if mode == "grid144":
        match = _GRID_POSITION_PATTERN.fullmatch(position)
        if match is None:
            raise ValueError("grid144 position must use r<row>c<column> order")
        row0 = int(match.group("row0"))
        column0 = int(match.group("column0"))
        row1 = int(match.group("row1") or row0)
        column1 = int(match.group("column1") or column0)
        if not (
            1 <= row0 <= row1 <= GRID_ROWS
            and 1 <= column0 <= column1 <= GRID_COLUMNS
        ):
            raise ValueError("grid144 position is outside the 16-row, 9-column grid")
        return (
            (column0 - 1) / GRID_COLUMNS,
            (row0 - 1) / GRID_ROWS,
            column1 / GRID_COLUMNS,
            row1 / GRID_ROWS,
        )

    parts = position.split(" ")
    if len(parts) != 4 or any(_BBOX_NUMBER_PATTERN.fullmatch(part) is None for part in parts):
        raise ValueError("bbox position requires four 0-1 decimals with at most three places")
    try:
        x, y, width, height = (Decimal(part) for part in parts)
    except InvalidOperation as error:
        raise ValueError("bbox position contains an invalid decimal") from error
    if width <= 0 or height <= 0:
        raise ValueError("bbox width and height must be positive")
    x1 = x + width
    y1 = y + height
    if x1 > _BBOX_END_TOLERANCE or y1 > _BBOX_END_TOLERANCE:
        raise ValueError("bbox endpoint exceeds the 1.001 tolerance")
    return float(x), float(y), float(min(x1, Decimal(1))), float(min(y1, Decimal(1)))


def _position(value: str, pos_mode: PosMode) -> ParsedPosition:
    try:
        bbox = to_bbox(value, pos_mode=pos_mode)
    except ValueError as error:
        raise _LineFailure("bad_pos", str(error)) from error
    return ParsedPosition(raw=value, mode=pos_mode, bbox=bbox)


def _take_token(remainder: str | None) -> tuple[str, str | None]:
    if remainder is None or remainder == "":
        raise _LineFailure("field_count", "a required field is missing")
    if remainder.startswith(" "):
        raise _LineFailure("bad_spacing", "fixed fields require one ASCII space")
    token, separator, rest = remainder.partition(" ")
    if not separator:
        return token, None
    if rest.startswith(" "):
        raise _LineFailure("bad_spacing", "fixed fields require one ASCII space")
    return token, rest


def _extract_fields(
    body: str,
    definition: _OpcodeDefinition,
    pos_mode: PosMode,
) -> dict[str, str | None]:
    values: dict[str, str | None] = {}
    remainder: str | None = body
    for field_name in definition.fields:
        if field_name == "text":
            if remainder is None or remainder == "":
                raise _LineFailure("field_count", "free text is required")
            if remainder.startswith(" "):
                raise _LineFailure("bad_spacing", "free text requires one separator")
            values[field_name] = remainder
            remainder = None
            continue
        if field_name == "optional_object_ref" and remainder is None:
            values[field_name] = None
            continue
        if field_name == "position":
            count = 1 if pos_mode == "grid144" else 4
            tokens: list[str] = []
            for _ in range(count):
                token, remainder = _take_token(remainder)
                tokens.append(token)
            values[field_name] = " ".join(tokens)
            continue
        if field_name == "uncertain_subject":
            token, remainder = _take_token(remainder)
            if pos_mode == "bbox" and not token.startswith("o"):
                if token == "player" or token[:1].isalpha():
                    values[field_name] = token
                    continue
                tokens = [token]
                for _ in range(3):
                    next_token, remainder = _take_token(remainder)
                    tokens.append(next_token)
                values[field_name] = " ".join(tokens)
            else:
                values[field_name] = token
            continue
        token, remainder = _take_token(remainder)
        values[field_name] = token
    if remainder is not None:
        raise _LineFailure("field_count", "the line has extra fixed fields")
    return values


def _snake(value: str, field_name: str) -> str:
    if _SNAKE_PATTERN.fullmatch(value) is None:
        raise _LineFailure("bad_token", f"{field_name} must be a snake_case token")
    return value


def _entity_token(value: str, field_name: str) -> str:
    if _ENTITY_PATTERN.fullmatch(value) is None:
        raise _LineFailure("bad_token", f"{field_name} must use o<positive integer>")
    return value


def _declared_entity(value: str, declared: set[str], field_name: str) -> str:
    entity = _entity_token(value, field_name)
    if entity not in declared:
        raise _LineFailure("undeclared_ref", f"{entity} has not been declared by E")
    return entity


def _object_reference(value: str, declared: set[str], field_name: str) -> str:
    if value == "player":
        return value
    return _declared_entity(value, declared, field_name)


def _build_line(
    definition: _OpcodeDefinition,
    values: dict[str, str | None],
    *,
    line_no: int,
    raw: str,
    pos_mode: PosMode,
    declared: set[str],
) -> StatementLine | EntityLine | AttributeLine | RelationLine | ActionLine | UncertaintyLine:
    code = definition.code
    if code == "S":
        return StatementLine(
            line_no=line_no,
            raw=raw,
            facet=_snake(cast(str, values["facet"]), "facet"),
            value=cast(str, values["text"]),
        )
    if code == "E":
        entity = _entity_token(cast(str, values["entity_decl"]), "entity")
        if entity in declared:
            raise _LineFailure("duplicate_entity", f"{entity} was already declared")
        position = _position(cast(str, values["position"]), pos_mode)
        return EntityLine(
            line_no=line_no,
            raw=raw,
            entity=entity,
            position=position,
            label=cast(str, values["text"]),
        )
    if code == "A":
        return AttributeLine(
            line_no=line_no,
            raw=raw,
            entity=_declared_entity(
                cast(str, values["entity_ref"]), declared, "attribute subject"
            ),
            facet=_snake(cast(str, values["facet"]), "facet"),
            value=cast(str, values["text"]),
        )
    if code == "R":
        return RelationLine(
            line_no=line_no,
            raw=raw,
            subject=_declared_entity(
                cast(str, values["entity_ref"]), declared, "relation subject"
            ),
            relation=_snake(cast(str, values["relation"]), "relation"),
            object=_object_reference(
                cast(str, values["object_ref"]), declared, "relation object"
            ),
        )
    if code == "V":
        target_value = values["optional_object_ref"]
        return ActionLine(
            line_no=line_no,
            raw=raw,
            subject=_declared_entity(
                cast(str, values["entity_ref"]), declared, "action subject"
            ),
            action=_snake(cast(str, values["action"]), "action"),
            target=(
                None
                if target_value is None
                else _object_reference(target_value, declared, "action target")
            ),
        )
    subject_value = cast(str, values["uncertain_subject"])
    if subject_value.startswith("o"):
        subject = _declared_entity(subject_value, declared, "uncertainty subject")
        position = None
    else:
        if subject_value == "player" or (
            subject_value[:1].isalpha() and pos_mode == "bbox"
        ):
            raise _LineFailure(
                "bad_token", "uncertainty subject must be a declared entity or position"
            )
        position = _position(subject_value, pos_mode)
        subject = subject_value
    return UncertaintyLine(
        line_no=line_no,
        raw=raw,
        subject=subject,
        position=position,
        facet=_snake(cast(str, values["facet"]), "facet"),
        value=cast(str, values["text"]),
    )


def _parse_protocol_line(
    line: str,
    *,
    line_no: int,
    raw: str,
    pos_mode: PosMode,
    declared: set[str],
) -> StatementLine | EntityLine | AttributeLine | RelationLine | ActionLine | UncertaintyLine:
    if any(ord(character) < 32 and not character.isspace() for character in line):
        raise _LineFailure("bad_line", "the line contains a control character")
    if any(character.isspace() and character != " " for character in line):
        raise _LineFailure("bad_spacing", "only ASCII spaces may separate fields")
    opcode, separator, body = line.partition(" ")
    definition = _OPCODES.get(opcode)
    if definition is None:
        raise _LineFailure("unknown_opcode", f"unknown opcode {opcode!r}")
    if not separator or not body:
        raise _LineFailure("field_count", "the opcode is missing required fields")
    if body.startswith(" "):
        raise _LineFailure("bad_spacing", "fixed fields require one ASCII space")
    values = _extract_fields(body, definition, pos_mode)
    return _build_line(
        definition,
        values,
        line_no=line_no,
        raw=raw,
        pos_mode=pos_mode,
        declared=declared,
    )


_LastWinsLine = TypeVar("_LastWinsLine", StatementLine, AttributeLine)
_FirstWinsLine = TypeVar("_FirstWinsLine", RelationLine, ActionLine)


def _deduplicate_last(
    lines: list[_LastWinsLine],
    key_parts: Callable[[_LastWinsLine], tuple[str, ...]],
) -> tuple[list[_LastWinsLine], list[DuplicateDetail]]:
    final_indexes: dict[tuple[str, ...], int] = {}
    for index, line in enumerate(lines):
        final_indexes[key_parts(line)] = index
    kept = [
        line
        for index, line in enumerate(lines)
        if final_indexes[key_parts(line)] == index
    ]
    details = [
        DuplicateDetail(
            opcode=line.opcode,
            key=list(key_parts(line)),
            line_no=line.line_no,
            raw=line.raw,
            kept_line_no=lines[final_indexes[key_parts(line)]].line_no,
        )
        for index, line in enumerate(lines)
        if final_indexes[key_parts(line)] != index
    ]
    return kept, details


def _deduplicate_first(
    lines: list[_FirstWinsLine],
    key_parts: Callable[[_FirstWinsLine], tuple[str, ...]],
) -> tuple[list[_FirstWinsLine], list[DuplicateDetail]]:
    first_indexes: dict[tuple[str, ...], int] = {}
    details: list[DuplicateDetail] = []
    kept: list[_FirstWinsLine] = []
    for index, line in enumerate(lines):
        key = key_parts(line)
        first_index = first_indexes.get(key)
        if first_index is None:
            first_indexes[key] = index
            kept.append(line)
            continue
        details.append(
            DuplicateDetail(
                opcode=line.opcode,
                key=list(key),
                line_no=line.line_no,
                raw=line.raw,
                kept_line_no=lines[first_index].line_no,
            )
        )
    return kept, details


def _deduplicate(accepted: AcceptedLines) -> tuple[AcceptedLines, list[DuplicateDetail]]:
    statements, statement_details = _deduplicate_last(
        accepted.S, lambda line: (line.facet,)
    )
    attributes, attribute_details = _deduplicate_last(
        accepted.A, lambda line: (line.entity, line.facet)
    )
    relations, relation_details = _deduplicate_first(
        accepted.R, lambda line: (line.subject, line.relation, line.object)
    )
    actions, action_details = _deduplicate_first(
        accepted.V,
        lambda line: (line.subject, line.action, line.target or ""),
    )
    details = sorted(
        statement_details + attribute_details + relation_details + action_details,
        key=lambda item: item.line_no,
    )
    return AcceptedLines(
        S=cast(list[StatementLine], statements),
        E=accepted.E,
        A=cast(list[AttributeLine], attributes),
        R=cast(list[RelationLine], relations),
        V=cast(list[ActionLine], actions),
        U=accepted.U,
    ), details


def parse_observation(
    text: str,
    *,
    pos_mode: PosMode,
    max_lines: int = DEFAULT_MAX_LINES,
) -> ParsedObservation:
    """Parse arbitrary model output without repairing malformed protocol lines."""

    mode = _validate_pos_mode(pos_mode)
    if max_lines < 1:
        raise ValueError("max_lines must be at least 1")
    accepted = AcceptedLines()
    rejected: list[RejectedLine] = []
    declared: set[str] = set()
    entities_declared: list[str] = []
    terminated = False
    trailing_content = False
    protocol_line_count = 0
    over_limit = 0

    for line_no, raw_with_carriage_return in enumerate(text.split("\n"), start=1):
        raw = (
            raw_with_carriage_return[:-1]
            if raw_with_carriage_return.endswith("\r")
            else raw_with_carriage_return
        )
        line = raw.strip()
        if not line:
            continue
        if terminated:
            rejected.append(
                RejectedLine(
                    line_no=line_no,
                    raw=raw,
                    reason="trailing",
                    message="non-empty content follows the first terminator",
                )
            )
            trailing_content = True
            continue
        if line == ".":
            terminated = True
            continue
        protocol_line_count += 1
        if protocol_line_count > max_lines:
            rejected.append(
                RejectedLine(
                    line_no=line_no,
                    raw=raw,
                    reason="over_limit",
                    message=f"protocol line exceeds max_lines={max_lines}",
                )
            )
            over_limit += 1
            continue
        try:
            parsed_line = _parse_protocol_line(
                line,
                line_no=line_no,
                raw=raw,
                pos_mode=mode,
                declared=declared,
            )
        except _LineFailure as error:
            rejected.append(
                RejectedLine(
                    line_no=line_no,
                    raw=raw,
                    reason=error.reason,
                    message=error.message,
                )
            )
            continue
        group = getattr(accepted, parsed_line.opcode)
        group.append(parsed_line)
        if isinstance(parsed_line, EntityLine):
            declared.add(parsed_line.entity)
            entities_declared.append(parsed_line.entity)

    accepted, duplicate_details = _deduplicate(accepted)
    present_facets = {line.facet for line in accepted.S}
    missing_required = [
        cast(Literal["mode", "place"], facet)
        for facet in ("mode", "place")
        if facet not in present_facets
    ]
    return ParsedObservation(
        accepted=accepted,
        rejected=rejected,
        parse_errors=len(rejected),
        duplicates=len(duplicate_details),
        duplicates_detail=duplicate_details,
        over_limit=over_limit,
        truncated=not terminated or trailing_content or over_limit > 0,
        entities_declared=entities_declared,
        missing_required_s=missing_required,
        pos_mode=mode,
    )


_FIELD_PLACEHOLDERS = {
    "facet": "<facet>",
    "text": "<free text>",
    "entity_decl": "oN",
    "entity_ref": "oN",
    "position": "<POS>",
    "relation": "<relation>",
    "object_ref": "<oM|player>",
    "action": "<action>",
    "optional_object_ref": "[<oM|player>]",
    "uncertain_subject": "<oN|POS>",
}


def _syntax(definition: _OpcodeDefinition) -> str:
    return " ".join(
        (definition.code, *(_FIELD_PLACEHOLDERS[field] for field in definition.fields))
    )


def _example_value(value: str, pos_mode: PosMode) -> str:
    if value != "@center":
        return value
    return "r8c5-r9c6" if pos_mode == "grid144" else "0.400 0.400 0.200 0.200"


def _render_example(definition: _OpcodeDefinition, example: _Example, pos_mode: PosMode) -> str:
    values = example.as_dict()
    required = {field for field in definition.fields if field != "optional_object_ref"}
    optional = {field for field in definition.fields if field == "optional_object_ref"}
    if not required.issubset(values) or not set(values).issubset(required | optional):
        raise ValueError(f"example fields do not match opcode {definition.code}")
    rendered = [definition.code]
    for field_name in definition.fields:
        value = values.get(field_name)
        if value is not None:
            rendered.append(_example_value(value, pos_mode))
    return " ".join(rendered)


def example_observation(pos_mode: PosMode) -> str:
    """Render the abstract examples embedded in the system format section."""

    mode = _validate_pos_mode(pos_mode)
    lines = [
        _render_example(definition, example, mode)
        for definition in _OPCODE_DEFINITIONS
        for example in definition.examples
    ]
    return "\n".join((*lines, "."))


def system_format_section(pos_mode: PosMode, language: PromptLanguage) -> str:
    """Render the system-prompt format section from the protocol table."""

    mode = _validate_pos_mode(pos_mode)
    selected_language = _validate_language(language)
    if selected_language == "zh":
        definition_lines = [
            f"{_syntax(definition)}：{definition.meaning_zh}。"
            for definition in _OPCODE_DEFINITIONS
        ]
        pos_line = (
            "POS 使用 grid144：r<行>c<列>，行 1–16、列 1–9；矩形范围写成左上格-右下格。"
            if mode == "grid144"
            else "POS 使用 bbox：x y w h 四个 0–1 小数，最多三位小数，宽高必须为正。"
        )
        rules = (
            "规则：只输出协议行，不输出 Markdown 围栏、列表、标题、解释或任何评论；只写正证据；"
            "拿不准写 U；不得转写画面里的文字或数字，但必须输出协议坐标所需的数字；不比较、不回溯、"
            "不写游戏名；词汇使用英文；动作使用现在进行时；最后独占一行输出句点终止符。"
        )
        example_intro = "以下是抽象格式示例；实际输出只写与当前画面相符的协议行："
        version_line = f"观察协议版本 {PROTOCOL_VERSION}。"
    else:
        definition_lines = [
            f"{_syntax(definition)}: {definition.meaning_en}."
            for definition in _OPCODE_DEFINITIONS
        ]
        pos_line = (
            "POS uses grid144: r<row>c<column>, rows 1-16 and columns 1-9; "
            "write a rectangle as top-left cell-bottom-right cell."
            if mode == "grid144"
            else (
                "POS uses bbox: four 0-1 decimals x y w h with at most three "
                "decimal places and positive width and height."
            )
        )
        rules = (
            "Rules: Output protocol lines only, with no Markdown fences, lists, "
            "headings, explanations, or comments. Write positive evidence only. "
            "Use U when uncertain. Do not transcribe text or numbers from the image, "
            "but do emit coordinate numbers required by the protocol. Do not compare, "
            "look back, or name the game. Use English vocabulary and "
            "present-progressive actions. End with the period terminator on its own line."
        )
        example_intro = (
            "These are abstract format examples; actual output must contain only "
            "lines supported by the current frame:"
        )
        version_line = f"Observation protocol version {PROTOCOL_VERSION}."
    return "\n".join(
        (
            version_line,
            *definition_lines,
            pos_line,
            rules,
            example_intro,
            example_observation(mode),
        )
    )


def user_format_directive(
    coverage_mode: CoverageMode,
    language: PromptLanguage,
) -> str:
    """Render the non-mechanical user-message format directive."""

    mode = _validate_coverage_mode(coverage_mode)
    selected_language = _validate_language(language)
    if selected_language == "zh":
        if mode == "enumerative_roi":
            return "列出指定区域内全部显著对象，并优先写这些对象的 A、R、V 行；按观察协议输出。"
        return "只写画面中的显著对象和其他正证据；按观察协议输出。"
    if mode == "enumerative_roi":
        return (
            "Enumerate every salient object in the specified region and prioritize "
            "their A, R, and V lines; use the observation protocol."
        )
    return (
        "Write only salient objects and other positive evidence in the frame; "
        "use the observation protocol."
    )


__all__ = [
    "AcceptedLines",
    "ActionLine",
    "AttributeLine",
    "CoverageMode",
    "DEFAULT_MAX_LINES",
    "DuplicateDetail",
    "EntityLine",
    "GRID_COLUMNS",
    "GRID_ROWS",
    "PROTOCOL_VERSION",
    "ParsedObservation",
    "ParsedPosition",
    "PosMode",
    "PromptLanguage",
    "RejectReason",
    "RejectedLine",
    "RelationLine",
    "StatementLine",
    "UncertaintyLine",
    "example_observation",
    "parse_observation",
    "system_format_section",
    "to_bbox",
    "user_format_directive",
]
