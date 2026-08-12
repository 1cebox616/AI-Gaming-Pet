"""Shared factual checks for static and model-generated commentary."""

WIN_METHOD_LABELS: dict[str, str] = {
    "elimination": "灭队",
    "bomb": "炸弹引爆",
    "defuse": "炸弹拆除",
    "time": "时间耗尽",
    "ct_win_elimination": "灭队",
    "t_win_elimination": "灭队",
    "ct_win_bomb": "炸弹引爆",
    "t_win_bomb": "炸弹引爆",
    "ct_win_defuse": "炸弹拆除",
    "t_win_defuse": "炸弹拆除",
    "ct_win_time": "时间耗尽",
    "t_win_time": "时间耗尽",
}

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
