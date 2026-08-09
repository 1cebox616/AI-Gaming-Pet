"""Idle utterances shared by the local pet backend."""

from typing import Literal

from pydantic import BaseModel, Field

Emotion = Literal["neutral", "happy", "angry", "surprised", "speechless"]


class Utterance(BaseModel):
    """A line of pet dialogue sent to connected desktop clients."""

    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    emotion: Emotion


IDLE_UTTERANCES: tuple[Utterance, ...] = (
    Utterance(id="idle-001", text="我在呢。", emotion="happy"),
    Utterance(id="idle-002", text="慢慢来。", emotion="neutral"),
    Utterance(id="idle-003", text="今天过得怎么样？", emotion="happy"),
    Utterance(
        id="idle-004",
        text="坐久了就站起来伸个懒腰吧。",
        emotion="neutral",
    ),
    Utterance(
        id="idle-005",
        text="喝口水，顺便让眼睛离开屏幕一小会儿。",
        emotion="neutral",
    ),
    Utterance(
        id="idle-006",
        text="手头的事情一件件来，我负责在旁边给你打气。",
        emotion="happy",
    ),
    Utterance(
        id="idle-007",
        text="要是脑子有点卡住，就先挑最小的一步做，动起来以后往往会轻松很多。",
        emotion="neutral",
    ),
    Utterance(
        id="idle-008",
        text="屏幕前的你已经忙了好一阵，先深呼吸一下，活动活动肩膀，再带着一点从容继续处理手头的事情；我会好好待在这里，不催你，也不偷偷给你的待办清单加项目。",
        emotion="happy",
    ),
    Utterance(
        id="idle-009",
        text="今天也辛苦啦，别忘了给自己留一点喘气的空隙。",
        emotion="happy",
    ),
    Utterance(
        id="idle-010",
        text="你忙你的，我安静陪着，需要我时再叫我。",
        emotion="neutral",
    ),
    Utterance(
        id="idle-011",
        text="如果现在没什么安排，发会儿呆也完全合理。",
        emotion="neutral",
    ),
    Utterance(
        id="idle-012",
        text="肩膀放松，眉头也松一松，事情不会因为你休息半分钟就跑掉。",
        emotion="surprised",
    ),
)

_next_idle_utterance_index = 0


def next_idle_utterance() -> Utterance:
    """Return the next idle utterance, cycling through the table in order."""
    global _next_idle_utterance_index

    utterance = IDLE_UTTERANCES[_next_idle_utterance_index]
    _next_idle_utterance_index = (
        _next_idle_utterance_index + 1
    ) % len(IDLE_UTTERANCES)
    return utterance
