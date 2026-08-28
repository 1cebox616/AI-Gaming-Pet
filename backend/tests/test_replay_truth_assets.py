from __future__ import annotations

from pathlib import Path


BACKEND = Path(__file__).parents[1]
TRUTH = BACKEND / "data" / "generic" / "replay-truth"


def test_session_context_distinguishes_whole_session_from_clip() -> None:
    text = (TRUTH / "session-context.md").read_text(encoding="utf-8")
    assert "简述覆盖整段录像，片段只是其中一截" in text
    assert "backend/recordings/capture/20260827-171815" in text
    assert "backend/recordings/capture/20260827-203925" in text
    assert "backend/recordings/capture/20260827-215554" in text
    assert "backend/recordings/capture/20260827-220206" in text
    assert "切换到英雄西格玛进攻C点" in text
    assert "使用威尔逊开始第一天" in text
    assert "进入室内搜刮保险箱和物资箱" in text
    assert "大型透明鱼类敌人“灵魂异鱼”" in text


def test_answer_keys_keep_context_and_field_notes_separate() -> None:
    expected = {
        "fast-fps": "这段全程使用温斯顿",
        "survival-2d": "约 T+154 秒时，玩家抬高视角",
        "simulation-fps": "整段以夜间探索和搜查为主",
        "card-game": "T+100–140 秒之间录制流几乎没有新帧",
    }
    for role, field_phrase in expected.items():
        text = (TRUTH / f"{role}-answer-key.md").read_text(encoding="utf-8")
        assert "## 【会话背景】" in text
        assert "## 【现场记录】" in text
        assert "session-context.md" in text
        assert field_phrase in text


def test_card_game_exemption_and_numeric_truth_are_explicit() -> None:
    text = (TRUTH / "card-game-answer-key.md").read_text(encoding="utf-8")
    assert "T+100–140 内的任何漏报不计入模型失分" in text
    assert "39.857 秒" in text
    for value in ("52/75", "197/221", "45/75", "86/221", "`20`", "`3`"):
        assert value in text


def test_judging_glossary_is_not_referenced_by_model_input_sources() -> None:
    glossary = (TRUTH / "judging-glossary.md").read_text(encoding="utf-8")
    assert "只用于判卷，绝不注入任何模型" in glossary
    for root in (BACKEND / "src", BACKEND / "prompts"):
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md", ".toml", ".json"}:
                assert "judging-glossary" not in path.read_text(
                    encoding="utf-8", errors="replace"
                ), path
