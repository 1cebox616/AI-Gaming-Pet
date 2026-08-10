"""Idle utterances shared by the local pet backend."""

from typing import Literal

from pydantic import BaseModel, Field

from pet.config import PersonalityStyle

Emotion = Literal["neutral", "happy", "angry", "surprised", "speechless"]


class Utterance(BaseModel):
    """A line of pet dialogue sent to connected desktop clients."""

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    emotion: Emotion


IDLE_UTTERANCES_BY_PERSONALITY: dict[
    PersonalityStyle,
    tuple[Utterance, ...],
] = {
    "brother": (
        Utterance(id="idle-brother-001", text="你还在啊。", emotion="happy"),
        Utterance(id="idle-brother-002", text="兄弟，我陪你。", emotion="neutral"),
        Utterance(id="idle-brother-003", text="你这会儿又在忙啥呢？", emotion="happy"),
        Utterance(
            id="idle-brother-004",
            text="你盯屏幕的样子，像在跟进度条打残局。",
            emotion="neutral",
        ),
        Utterance(
            id="idle-brother-005",
            text="你敲键盘这阵仗，不知道的以为在打 Major。",
            emotion="surprised",
        ),
        Utterance(
            id="idle-brother-006",
            text="兄弟，你忙半天了，我这边观战席都坐热了。",
            emotion="happy",
        ),
        Utterance(
            id="idle-brother-007",
            text="你今天这专注度，五个人架你都拉不走。",
            emotion="surprised",
        ),
        Utterance(
            id="idle-brother-008",
            text="你忙你的，我就在旁边挂着；等你回头一看，兄弟还在，桌面也没被我偷偷改成旅游照。",
            emotion="happy",
        ),
        Utterance(
            id="idle-brother-009",
            text="你这鼠标点得，像在给待办清单逐个爆头。",
            emotion="happy",
        ),
        Utterance(
            id="idle-brother-010",
            text="你不说话，我也能陪你挂机，主打一个不掉线。",
            emotion="neutral",
        ),
    ),
    "caster": (
        Utterance(id="idle-caster-001", text="你已上线。", emotion="happy"),
        Utterance(id="idle-caster-002", text="镜头给你。", emotion="neutral"),
        Utterance(
            id="idle-caster-003",
            text="这位选手，你今天状态如何？",
            emotion="happy",
        ),
        Utterance(
            id="idle-caster-004",
            text="镜头里的你，正在和桌面展开漫长对峙。",
            emotion="neutral",
        ),
        Utterance(
            id="idle-caster-005",
            text="观众朋友们，你的键盘攻势已经进入第二阶段。",
            emotion="surprised",
        ),
        Utterance(
            id="idle-caster-006",
            text="画面给到你，今天的主舞台就是这张桌面。",
            emotion="happy",
        ),
        Utterance(
            id="idle-caster-007",
            text="你和进度条还在拉扯，双方暂时谁也没占上风。",
            emotion="neutral",
        ),
        Utterance(
            id="idle-caster-008",
            text="导播把长镜头留给你：鼠标走位，键盘补枪，窗口轮转，这套桌面联动已经把解说席看入迷了。",
            emotion="surprised",
        ),
        Utterance(
            id="idle-caster-009",
            text="这位选手，你的沉默也很有比赛气质。",
            emotion="neutral",
        ),
        Utterance(
            id="idle-caster-010",
            text="现场没有枪声，你依然牢牢占据画面中心。",
            emotion="happy",
        ),
    ),
}

_next_idle_utterance_indexes: dict[PersonalityStyle, int] = {
    style: 0 for style in IDLE_UTTERANCES_BY_PERSONALITY
}


def next_idle_utterance(style: PersonalityStyle) -> Utterance:
    """Return the next idle utterance for one startup-selected personality."""
    utterances = IDLE_UTTERANCES_BY_PERSONALITY[style]
    index = _next_idle_utterance_indexes[style]
    utterance = utterances[index]
    _next_idle_utterance_indexes[style] = (index + 1) % len(utterances)
    return utterance
