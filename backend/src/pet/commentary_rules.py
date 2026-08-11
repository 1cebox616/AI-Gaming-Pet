"""Shared factual checks for static and model-generated commentary."""

CALLOUT_TERMS: tuple[str, ...] = (
    "A点",
    "B点",
    "B洞",
    "中路",
    "狗洞",
    "跳台",
    "电梯",
    "大坑",
    "超市",
    "包点",
    "水下",
)

FORBIDDEN_RAW_CURSES: tuple[str, ...] = ("草", "操", "妈", "傻逼", "废物")

# This is deliberately a whitelist: these ordinary words contain a blocked
# single character but are not profanity in Chinese commentary.
RAW_CURSE_BENIGN_WORDS: tuple[str, ...] = (
    "操作",
    "操控",
    "体操",
    "草丛",
    "草地",
    "稻草",
    "妈妈",
    "大妈",
)


def find_forbidden_raw_curses(text: str) -> tuple[str, ...]:
    """Return blocked raw curses after removing explicitly benign words."""
    checked = text
    for word in RAW_CURSE_BENIGN_WORDS:
        checked = checked.replace(word, "")
    return tuple(term for term in FORBIDDEN_RAW_CURSES if term in checked)
