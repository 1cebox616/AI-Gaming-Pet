"""Idle utterances shared by the local pet backend."""

from typing import Literal

from pydantic import BaseModel

Emotion = Literal["neutral", "happy", "angry", "surprised", "speechless"]


class Utterance(BaseModel):
    """A line of pet dialogue sent to connected desktop clients."""

    id: str
    text: str
    emotion: Emotion


IDLE_UTTERANCES: tuple[Utterance, ...] = (
    Utterance(id="idle-001", text="今天也一起加油。", emotion="happy"),
    Utterance(id="idle-002", text="我在这里陪着你。", emotion="neutral"),
    Utterance(id="idle-003", text="要不要活动一下肩膀？", emotion="surprised"),
    Utterance(id="idle-004", text="这一局慢慢来，稳住节奏就好。", emotion="neutral"),
    Utterance(id="idle-005", text="喝口水再继续，眼睛也该休息一下啦。", emotion="neutral"),
    Utterance(id="idle-006", text="刚才的操作很有想法，我已经记在小本本上了。", emotion="happy"),
    Utterance(
        id="idle-007",
        text="如果觉得有点累，就把注意力放回下一步，不必急着赢下所有事情。",
        emotion="neutral",
    ),
    Utterance(
        id="idle-008",
        text="屏幕前的你已经很努力了，先深呼吸一下，再带着一点点从容把接下来的挑战完成吧。",
        emotion="happy",
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
