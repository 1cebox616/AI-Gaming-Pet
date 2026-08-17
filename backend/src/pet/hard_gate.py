"""Runtime-only validation for model commentary output."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import logging
from pathlib import Path
import re

from pet.event_card import FACT_EVENT_NAMES
from pet.situation import SCENE_TAGS

BACKEND_ROOT = Path(__file__).resolve().parents[2]
VOCABULARY_PATH = BACKEND_ROOT / "prompts" / "vocabulary.md"
MAX_CHINESE_CHARS = 30

_HAN_PATTERN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_BINDING_HEADING = "# 用词绑定（说错了就是事实错误）"
_BINDING_HEADER = ("说法", "只能用在", "需要的标签")
_TERM_SEPARATOR_PATTERN = re.compile(r"[、，,/／]")

logger = logging.getLogger(__name__)

_WEAPON_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "AWP": ("awp",),
    "DEAGLE": ("deagle", "沙鹰"),
    "自动武器": (
        "ak47",
        "m4a4",
        "m4a1-s",
        "galil ar",
        "famas",
        "sg 553",
        "aug",
        "mp9",
        "mac-10",
        "mp7",
        "mp5-sd",
        "ump-45",
        "p90",
        "pp-bizon",
        "negev",
        "m249",
    ),
    "冲锋枪": ("mp9", "mac-10", "mp7", "mp5-sd", "ump-45", "p90", "pp-bizon"),
}

# Literal entity categories that a focused GSI fact sentence cannot support.
UNSUPPORTED_TERMS: tuple[str, ...] = (
    "队友",
    "队友们",
    "队伍",
    "对面",
    "对手",
    "敌人",
    "敌方",
    "土匪",
    "警察",
    "A点",
    "B点",
    "中路",
    "香蕉道",
    "二楼",
    "连接",
    "包点",
    "伤害来源",
)


@dataclass(frozen=True, slots=True)
class HardChecks:
    """Mechanical checks that deliberately avoid judging prose quality."""

    chinese_char_count: int
    exceeds_30_chars: bool
    unsupported_terms: tuple[str, ...]
    binding_violations: tuple[str, ...]
    economy_tier_rewrite: bool
    eco_called_pistol_round: bool


@dataclass(frozen=True, slots=True)
class VocabularyBinding:
    """Machine-readable first and third columns from one vocabulary row."""

    terms: tuple[str, ...]
    requirement_kind: str
    requirement_values: tuple[str, ...]


def chinese_character_count(text: str) -> int:
    """Count Han characters for the runtime output limit."""
    return len(_HAN_PATTERN.findall(text))


def scene_tags(fact_sentence: str) -> str:
    """Extract the rendered tags without treating them as the model's facts."""
    for line in fact_sentence.splitlines():
        if line.startswith("【场景标签】"):
            value = line.removeprefix("【场景标签】").strip()
            return value or "无"
    return "无"


def check_hard_violations(text: str, *, fact_sentence: str) -> HardChecks:
    """Mark only explicit policy violations; do not grade prose quality."""
    chinese_char_count = chinese_character_count(text)
    return HardChecks(
        chinese_char_count=chinese_char_count,
        exceeds_30_chars=chinese_char_count > MAX_CHINESE_CHARS,
        unsupported_terms=tuple(term for term in UNSUPPORTED_TERMS if term in text),
        binding_violations=_binding_violations(text, fact_sentence=fact_sentence),
        economy_tier_rewrite=_economy_tier_rewrite(text, fact_sentence),
        eco_called_pistol_round="eco局" in fact_sentence and "手枪局" in text,
    )


def _economy_tier_rewrite(text: str, fact_sentence: str) -> bool:
    """Catch only explicit economy-tier substitutions, never infer economy."""
    tiers = {
        "eco局": ("eco",),
        "强起局": ("强起",),
        "全装局": ("全装",),
    }
    expected = next((tier for tier in tiers if tier in fact_sentence), None)
    return expected is not None and any(
        alias in text
        for tier, aliases in tiers.items()
        if tier != expected
        for alias in aliases
    )


def _binding_violations(text: str, *, fact_sentence: str) -> tuple[str, ...]:
    """Apply the cached vocabulary table, with legacy checks only as fallback."""
    if VOCABULARY_BINDINGS is None:
        return _fallback_binding_violations(text, fact_sentence=fact_sentence)
    return _table_binding_violations(
        text,
        fact_sentence=fact_sentence,
        bindings=VOCABULARY_BINDINGS,
    )


def _table_binding_violations(
    text: str,
    *,
    fact_sentence: str,
    bindings: Iterable[VocabularyBinding],
) -> tuple[str, ...]:
    lower = text.lower()
    tags = _fact_scene_tags(fact_sentence)
    event_name = _fact_event_name(fact_sentence)
    fact_lower = fact_sentence.lower()
    violations: list[str] = []
    for binding in bindings:
        matched = tuple(term for term in binding.terms if term.lower() in lower)
        if not matched or binding.requirement_kind == "unmapped":
            continue
        requirement_met = False
        if binding.requirement_kind == "forbidden":
            requirement_met = False
        elif binding.requirement_kind == "labels":
            requirement_met = bool(tags.intersection(binding.requirement_values))
        elif binding.requirement_kind == "conditions":
            requirement_met = any(
                value.removeprefix("事件:") == event_name
                if value.startswith("事件:")
                else value in tags
                for value in binding.requirement_values
            )
        elif binding.requirement_kind == "weapon":
            requirement_met = any(
                any(alias in fact_lower for alias in _WEAPON_REQUIREMENTS[value])
                for value in binding.requirement_values
            )
        if not requirement_met:
            phrase = "、".join(matched)
            requirement = _binding_requirement_description(binding)
            if binding.requirement_kind == "forbidden":
                violations.append(f"{phrase}（{requirement}）")
            else:
                violations.append(f"{phrase}（需要{requirement}）")
    return tuple(dict.fromkeys(violations))


def _binding_requirement_description(binding: VocabularyBinding) -> str:
    if binding.requirement_kind == "forbidden":
        return "不得使用"
    if binding.requirement_kind == "weapon":
        return "武器：" + "或".join(binding.requirement_values)
    if binding.requirement_kind == "conditions":
        return "或".join(
            value.replace("事件:", "事件：", 1)
            if value.startswith("事件:")
            else f"标签：{value}"
            for value in binding.requirement_values
        )
    return "标签：" + "或".join(binding.requirement_values)


def _fact_scene_tags(fact_sentence: str) -> frozenset[str]:
    rendered = scene_tags(fact_sentence)
    if rendered == "无":
        return frozenset()
    return frozenset(tag.strip() for tag in rendered.split("、") if tag.strip())


def _fact_event_name(fact_sentence: str) -> str | None:
    for line in fact_sentence.splitlines():
        if line.startswith("【事件】"):
            return line.removeprefix("【事件】").strip() or None
    return None


def _parse_vocabulary_bindings(text: str) -> tuple[VocabularyBinding, ...]:
    """Parse only the final three-column table; never interpret its prose column."""
    if _BINDING_HEADING not in text:
        raise ValueError(f"missing binding heading: {_BINDING_HEADING}")
    section = text.split(_BINDING_HEADING, 1)[1]
    # Join Markdown prose continuations before recognizing table rows.
    section = section.replace("\\\r\n", "").replace("\\\n", "")
    lines = section.splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if _split_markdown_row(line) == _BINDING_HEADER
        ),
        None,
    )
    if header_index is None:
        raise ValueError("missing three-column vocabulary binding header")
    if header_index + 1 >= len(lines) or not _is_markdown_separator(
        lines[header_index + 1]
    ):
        raise ValueError("missing vocabulary binding table separator")

    bindings: list[VocabularyBinding] = []
    table_ended = False
    for row_number, line in enumerate(lines[header_index + 2 :], start=header_index + 3):
        if not line.strip():
            table_ended = True
            continue
        if table_ended:
            if line.lstrip().startswith("|"):
                raise ValueError(f"binding row {row_number} appears after the table ended")
            continue
        if not line.lstrip().startswith("|"):
            raise ValueError(f"binding row {row_number} is not a Markdown table row")
        cells = _split_markdown_row(line)
        if len(cells) != 3:
            raise ValueError(f"binding row {row_number} has {len(cells)} columns, expected 3")
        phrases, _, requirement = cells
        terms = tuple(
            term.strip()
            for term in _TERM_SEPARATOR_PATTERN.split(phrases)
            if term.strip()
        )
        if not terms:
            raise ValueError(f"binding row {row_number} has no phrases")
        kind, values = _parse_binding_requirement(requirement, row_number=row_number)
        bindings.append(
            VocabularyBinding(
                terms=terms,
                requirement_kind=kind,
                requirement_values=values,
            )
        )
    if not bindings:
        raise ValueError("vocabulary binding table has no data rows")
    return tuple(bindings)


def _split_markdown_row(line: str) -> tuple[str, ...]:
    sentinel = "\x00PIPE\x00"
    protected = line.strip().replace("\\|", sentinel)
    if not protected.startswith("|") or not protected.endswith("|"):
        return ()
    return tuple(
        cell.strip().replace(sentinel, "|")
        for cell in protected[1:-1].split("|")
    )


def _is_markdown_separator(line: str) -> bool:
    cells = _split_markdown_row(line)
    return len(cells) == 3 and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _parse_binding_requirement(
    requirement: str, *, row_number: int
) -> tuple[str, tuple[str, ...]]:
    if requirement == "不得使用":
        return "forbidden", ()
    if requirement == "无法映射":
        return "unmapped", ()
    if requirement.startswith("武器:"):
        values = tuple(
            value.strip()
            for value in requirement.removeprefix("武器:").split("|")
            if value.strip()
        )
        unknown = tuple(value for value in values if value not in _WEAPON_REQUIREMENTS)
        if not values or unknown:
            raise ValueError(
                f"binding row {row_number} has unknown weapon requirement: "
                f"{unknown or requirement}"
            )
        return "weapon", values

    conditions = tuple(value.strip() for value in requirement.split("|") if value.strip())
    event_conditions = tuple(
        value.removeprefix("事件:")
        for value in conditions
        if value.startswith("事件:")
    )
    unknown_events = tuple(
        value for value in event_conditions if value not in FACT_EVENT_NAMES
    )
    unknown_labels = tuple(
        value
        for value in conditions
        if not value.startswith("事件:") and value not in SCENE_TAGS
    )
    if not conditions or unknown_events or unknown_labels:
        raise ValueError(
            f"binding row {row_number} has unknown requirements: "
            f"{unknown_labels + tuple('事件:' + value for value in unknown_events) or requirement}"
        )
    return ("conditions" if event_conditions else "labels"), conditions


def _load_vocabulary_bindings(path: Path) -> tuple[VocabularyBinding, ...] | None:
    try:
        return _parse_vocabulary_bindings(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        logger.error(
            "Failed to parse vocabulary binding table from %s; "
            "using legacy hard-coded fallback: %s",
            path,
            error,
        )
        return None


def _fallback_binding_violations(text: str, *, fact_sentence: str) -> tuple[str, ...]:
    """Keep the pre-table checks solely for startup parse failures."""
    lower = text.lower()
    facts = fact_sentence.lower()
    violations: list[str] = []
    if _contains_any(text, ("黄色闪光", "架住了")) and "awp" not in facts:
        violations.append("黄色闪光/架住了（非 AWP 打得好）")
    if _contains_any(text, ("烧火棍", "放炮", "刘农")) and "大狙空枪" not in fact_sentence:
        violations.append("烧火棍/放炮/刘农（非 AWP 空枪）")
    if _contains_any(text, ("扫射转移", "钢铁般的左键", "压住了")) and not _contains_any(
        facts, ("ak", "m4", "galil", "famas", "aug", "sg", "mp", "ump", "p90", "bizon")
    ):
        violations.append("自动武器说法（非可见自动武器）")
    if "沙鹰王" in text and "deagle" not in facts:
        violations.append("沙鹰王（非沙鹰）")
    if _contains_any(text, ("跑打仙人", "糊脸")) and not _contains_any(
        facts, ("mp", "ump", "p90", "bizon", "mac-10")
    ):
        violations.append("冲锋枪说法（非冲锋枪）")
    if _contains_any(text, ("颗秒", "一枪头", "爆头线")) and not _contains_any(
        fact_sentence, ("爆头", "颗秒", "秒杀")
    ):
        violations.append("爆头/极少用弹说法（事实不支持）")
    if "一换一" in text and "被补" not in fact_sentence:
        violations.append("一换一（非刚杀完即被补）")
    if _contains_any(text, ("白给", "欢乐送", "全装秒躺")) and "白给" not in fact_sentence:
        violations.append("白给说法（事实非白给）")
    if _contains_any(
        text,
        ("马枪", "人体描边", "马完了", "开了很多枪没打死", "没打死一个"),
    ) and "马枪死" not in fact_sentence:
        violations.append("马枪说法（事实非大量开火未中）")
    if "武器大师" in text and "换枪后立刻杀" not in fact_sentence:
        violations.append("武器大师（非换枪后击杀）")
    if _contains_any(text, ("烤猪", "火中做自己")) and not _contains_any(
        fact_sentence, ("踩火杀", "烧惨了")
    ):
        violations.append("着火说法（事实非着火）")
    if _contains_any(text, ("白屏战神", "被白到死")) and not _contains_any(
        fact_sentence, ("白着打", "白着被打死", "白惨了")
    ):
        violations.append("闪光说法（事实非被闪）")
    if "doom来了" in lower and "踩火杀" not in fact_sentence:
        violations.append("doom 来了（非着火击杀）")
    if _contains_any(text, ("僵尸猪人", "大表猪play")) and "出烟就没了" not in fact_sentence:
        violations.append("僵尸猪人/大表猪play（非出烟死亡）")
    if _contains_any(text, ("大表哥play", "karrigan play")) and "摸烟击杀" not in fact_sentence:
        violations.append("大表哥play（非摸烟击杀）")
    return tuple(violations)


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(term.lower() in text.lower() for term in terms)


VOCABULARY_BINDINGS = _load_vocabulary_bindings(VOCABULARY_PATH)
