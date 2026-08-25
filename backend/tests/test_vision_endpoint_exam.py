"""M5-T2.12 exam tests use only synthetic images and an injected client."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from pet.core.llm import LlmResult, LlmUsage
from pet.games.generic.eval import vision_exam
from pet.games.generic.eval.vision_endpoint_exam import (
    GROUP_LABEL,
    baseline_summary,
    build_variant,
    custom_summary,
    endpoint_host,
    render_summary,
    validate_baseline,
    write_outputs,
)


FIXTURES = Path(__file__).parent / "fixtures"
MANIFEST_PATH = FIXTURES / "vision-exam-example.toml"


class _Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def complete_with_images_stream(self, **arguments: object) -> LlmResult:
        self.calls.append(arguments)
        return LlmResult(
            text="合成图像中出现亮色方块。",
            usage=LlmUsage(100, 12, None, reasoning_tokens=0),
            latency_seconds=0.3,
            model="same/model",
            provider=None,
            finish_reason="stop",
            ttft_seconds=0.1,
            streamed=True,
        )

    def complete_with_images(self, **arguments: object) -> LlmResult:
        raise AssertionError("必须使用流式路径")

    def close(self) -> None:
        return None


def _baseline_rows(model: str = "same/model") -> list[dict[str, str]]:
    return [
        {
            "目标档位": "A-baseline",
            "是否跳过": "false",
            "请求模型": model,
            "输出模式": "fast",
            "max_tokens": "80",
            "上传宽度": "896",
            "区域提示模式": "sparse",
            "错误原文": "",
            "TTFT毫秒": str(200 + index),
            "往返毫秒": str(300 + index),
            "输入token": "100",
            "可见输出token": "12",
            "是否截断": "false",
            "第几遍": "1" if index < 11 else "2",
            "上游报告花费美元": "",
            "配置折算花费美元": "0.0001",
        }
        for index in range(22)
    ]


def _write_baseline(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _answer_key(path: Path) -> Path:
    path.write_text(
        "# synthetic\n\n"
        "## 1. `synthetic-single`\n\n"
        "### 产品负责人判定\n\n- 【核心】核心一。\n\n"
        "### 离线复核\n\n- 无。\n\n"
        "### 不得出现的内容\n\n- 编造一。\n\n"
        "### 不确定项\n\n- 无。\n\n"
        "## 2. `synthetic-sequence`\n\n"
        "### 产品负责人判定\n\n- 【核心】核心二。\n\n"
        "### 离线复核\n\n- 无。\n\n"
        "### 不得出现的内容\n\n- 编造二。\n\n"
        "### 不确定项\n\n- 无。\n",
        encoding="utf-8",
    )
    return path


def test_baseline_requires_same_model_and_fixed_request_shape(tmp_path: Path) -> None:
    path = _write_baseline(tmp_path / "baseline.csv", _baseline_rows())
    rows = validate_baseline(
        path,
        baseline_model="same/model",
        endpoint_model="model",
    )
    assert len(rows) == 22

    changed = _baseline_rows()
    changed[0]["上传宽度"] = "1280"
    path = _write_baseline(tmp_path / "changed.csv", changed)
    with pytest.raises(vision_exam.VisionExamError, match="上传宽度"):
        validate_baseline(
            path,
            baseline_model="same/model",
            endpoint_model="model",
        )

    with pytest.raises(vision_exam.VisionExamError, match="不属于同一模型"):
        validate_baseline(
            _write_baseline(tmp_path / "model.csv", _baseline_rows()),
            baseline_model="same/model",
            endpoint_model="different-model",
        )


def test_endpoint_host_keeps_only_hostname() -> None:
    assert endpoint_host("https://endpoint.invalid/openai/v1") == "endpoint.invalid"
    with pytest.raises(vision_exam.VisionExamError, match="主机名"):
        endpoint_host("not-a-url")


def test_fake_d_group_writes_endpoint_summary_and_blank_grading_columns(
    tmp_path: Path,
) -> None:
    manifest = vision_exam.load_manifest(MANIFEST_PATH)
    client = _Client()
    variant = build_variant()
    target = vision_exam.ModelTarget(
        label=GROUP_LABEL,
        model="same/model",
        provider=None,
        temperature=0.0,
        timeout_seconds=3.0,
        price=vision_exam.ModelPrice(1.0, 2.0),
        endpoint_host="endpoint.invalid",
        provider_region="region-one",
        reasoning_parameter_mode="effort_none",
    )
    outcome = vision_exam.run_formal_exam(
        manifest=manifest,
        variants=(variant,),
        targets=(target,),
        client_factory=lambda _: client,
        cost_cap_usd=5.0,
        enable_relaxed=False,
        repetitions=2,
        streaming=True,
    )
    output = tmp_path / "output"
    baseline_rows = _baseline_rows()
    write_outputs(
        output,
        manifest=manifest,
        records=outcome.records,
        baseline_rows=baseline_rows,
        answer_key_path=_answer_key(tmp_path / "answers.md"),
        host="endpoint.invalid",
        run_payload={"price_source": "https://pricing.invalid"},
    )
    with (output / "results.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    summary = (output / "summary.md").read_text(encoding="utf-8")
    grading = (output / "grading-fast.md").read_text(encoding="utf-8")
    assert len(rows) == 4
    assert {row["端点主机名"] for row in rows} == {"endpoint.invalid"}
    assert all(row["max_tokens"] == "80" for row in rows)
    assert all(row["上传宽度"] == "896" for row in rows)
    assert "A-historical" in summary
    assert GROUP_LABEL in summary
    assert "A TTFT 中位 − D TTFT 中位" in summary
    assert "第一遍" in grading
    assert all(call["reasoning_effort"] == "none" for call in client.calls)
    assert all(call["reasoning_enabled"] is None for call in client.calls)
    assert all(call["max_tokens"] == 80 for call in client.calls)


def test_machine_summary_contains_no_quality_judgment() -> None:
    baseline = baseline_summary(_baseline_rows())
    text = render_summary(
        baseline,
        custom_summary([]),
        host="endpoint.invalid",
        actual_cost_usd=0.0,
    )
    assert all(word not in text for word in ("推荐", "最好", "质量可接受", "能力不足"))
