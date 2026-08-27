"""Notebook probe mechanics use injected clients and never call the network."""

from __future__ import annotations

import json
from pathlib import Path

from pet.core.config import LlmConfig, LlmProfileConfig
from pet.core.llm import LlmResult, LlmUsage
from pet.games.generic.eval.notebook_probe import (
    CostBudget,
    EMPTY_NOTEBOOK,
    ObservationEntry,
    PreparedLog,
    _file_sha256,
    build_parser,
    parse_notebook,
    run_combination,
    split_windows,
)


def _notebook(label: str, *, confirmed: bool = False) -> str:
    status = "已印证" if confirmed else "初步"
    return f"""【稳定认知】
1. {label}｜状态：{status}｜支撑：2026-08-26T00:00:00Z

【本轮变动】
新增：
- {label}
驳回：暂无
翻案：暂无

【未决问题】
- 该对象是否持续存在

【信息缺口】
- 缺少中央对象的连续位置记录"""


class FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def complete(self, **kwargs: object) -> LlmResult:
        self.calls.append(kwargs)
        text = self.responses[len(self.calls) - 1]
        return LlmResult(
            text=text,
            usage=LlmUsage(
                prompt_tokens=100,
                completion_tokens=50,
                cost_usd=None,
                reasoning_tokens=0,
            ),
            latency_seconds=0.25,
            model="configured-by-test",
            provider=None,
            finish_reason="stop",
        )


def _configuration() -> LlmConfig:
    return LlmConfig(
        enabled=True,
        model="fallback",
        profiles={
            "notebook": LlmProfileConfig(
                model="configured-by-test",
                max_tokens=200,
                temperature=0.0,
                input_price_per_million_usd=1.0,
                output_price_per_million_usd=2.0,
            )
        },
    )


def _prepared(tmp_path: Path, count: int = 5) -> PreparedLog:
    log = tmp_path / "fixture" / "observations.jsonl"
    log.parent.mkdir(parents=True)
    raw_lines: list[str] = []
    entries: list[ObservationEntry] = []
    for seq in range(1, count + 1):
        payload = {
            "seq": seq,
            "wall": f"2026-08-26T00:00:{seq:02d}Z",
            "game": "Fixture Game",
            "text": "" if seq == 2 else f"观察 {seq}",
            "dropped": "timeout" if seq == 2 else None,
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        raw_lines.append(raw)
        entries.append(ObservationEntry(seq, raw))
    log.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
    return PreparedLog(
        path=log,
        name="fixture",
        game="Fixture Game",
        entries=tuple(entries),
        sha256=_file_sha256(log),
    )


def test_parser_accepts_multiple_logs_profiles_and_default_window() -> None:
    parsed = build_parser().parse_args(
        [
            "--log",
            "one.jsonl",
            "--log",
            "two.jsonl",
            "--profile",
            "strong",
            "--profile",
            "cheap",
        ]
    )
    assert len(parsed.log) == 2
    assert parsed.profile == ["strong", "cheap"]
    assert parsed.window == 60
    assert parsed.cost_cap == 1.0


def test_window_split_keeps_short_tail() -> None:
    entries = tuple(ObservationEntry(seq, str(seq)) for seq in range(1, 126))
    windows = split_windows(entries, 60)
    assert [len(window) for window in windows] == [60, 60, 5]
    assert windows[-1][0].seq == 121
    assert windows[-1][-1].seq == 125


def test_dropped_raw_line_notebook_carry_and_parse_failure_continue(
    tmp_path: Path,
) -> None:
    prepared = _prepared(tmp_path)
    first = _notebook("首窗认知")
    third = _notebook("尾窗认知", confirmed=True)
    client = FakeClient([first, "不符合四节格式的模型原文", third])
    output = tmp_path / "output"
    summary = run_combination(
        prepared,
        "notebook",
        _configuration(),
        "system prompt",
        output,
        2,
        CostBudget(1.0),
        client=client,
    )

    assert len(client.calls) == 3
    assert client.calls[0]["reasoning_enabled"] is False
    first_message = str(client.calls[0]["user_prompt"])
    assert first_message.index("游戏名：") < first_message.index("当前笔记本全文：")
    assert first_message.index("当前笔记本全文：") < first_message.index("本窗观察条目原文：")
    assert EMPTY_NOTEBOOK in first_message
    assert prepared.entries[1].raw in first_message
    assert '"dropped":"timeout"' in first_message
    assert "2026-08-26T00:00:02Z" in first_message
    assert parse_notebook(first).text in str(client.calls[1]["user_prompt"])
    assert parse_notebook(first).text in str(client.calls[2]["user_prompt"])
    assert "不符合四节格式的模型原文" in (
        output / "notebook-w02.md"
    ).read_text(encoding="utf-8")
    assert "尾窗认知" in (output / "notebook.md").read_text(encoding="utf-8")
    stats = json.loads((output / "stats.json").read_text(encoding="utf-8"))
    assert [item["entry_count"] for item in stats["windows"]] == [2, 2, 1]
    assert stats["totals"]["parse_failures"] == 1
    assert summary.parse_failure_count == 1
    assert summary.confirmed_count == 1
    gaps = (output / "gaps.md").read_text(encoding="utf-8")
    assert gaps.count("缺少中央对象的连续位置记录") == 1


def test_cost_guard_stops_before_call_and_records_snapshot(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path, count=2)
    client = FakeClient([_notebook("不应调用")])
    output = tmp_path / "guarded"
    summary = run_combination(
        prepared,
        "notebook",
        _configuration(),
        "x" * 300,
        output,
        2,
        CostBudget(0.000001),
        client=client,
    )

    assert client.calls == []
    assert summary.stopped_by_cost_guard
    stats = json.loads((output / "stats.json").read_text(encoding="utf-8"))
    assert stats["windows"][0]["called"] is False
    assert stats["windows"][0]["error"] == "cost_cap"
    assert "费用护栏停止" in (output / "notebook-w01.md").read_text(
        encoding="utf-8"
    )


def test_prompt_contains_contract_without_concrete_game_terms() -> None:
    prompt = (
        Path(__file__).parents[1] / "prompts" / "generic" / "notebook-probe.md"
    ).read_text(encoding="utf-8")
    for heading in ("【稳定认知】", "【本轮变动】", "【未决问题】", "【信息缺口】"):
        assert heading in prompt
    assert "完整的新版笔记本" in prompt
    assert "低信息条目" in prompt
    assert "不得用日志外的游戏知识" in prompt
    for concrete_term in ("杀戮尖塔", "灰区", "手电筒", "闪电", "直升机", "卡牌", "枪械"):
        assert concrete_term not in prompt
