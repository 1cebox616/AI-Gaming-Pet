"""Run one custom-endpoint geography comparison after a live profile probe."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import csv
from dataclasses import asdict
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import sys
from urllib.parse import urlparse

from pet.core.config import (
    DEFAULT_CONFIG_PATH,
    LOCAL_CONFIG_PATH,
    LlmConfig,
    load_config,
    resolve_llm_profile,
)
from pet.core.llm import OpenRouterClient, probe_llm_profile
from pet.games.generic.eval import vision_exam


GROUP_LABEL = "D-custom-region"
REPETITIONS = 2
SEND_WIDTH = 896
MAX_TOKENS = 80


def build_variant() -> vision_exam.ExamVariant:
    """Return the exact M5-T2.12 fast request shape."""
    return vision_exam.ExamVariant(
        SEND_WIDTH,
        "sparse",
        vision_exam.DEFAULT_REGION_SPARSITY_MAX,
        "fast",
        max_tokens_override=MAX_TOKENS,
    )


def endpoint_host(base_url: str) -> str:
    """Return only the hostname safe for result records."""
    host = urlparse(base_url).hostname
    if host is None or not host.strip():
        raise vision_exam.VisionExamError("档位 base_url 缺少有效主机名")
    return host


def validate_baseline(
    path: Path,
    *,
    baseline_model: str,
    endpoint_model: str,
) -> tuple[dict[str, str], ...]:
    """Load the prior A rows and verify every controlled request dimension."""
    try:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = tuple(
                row
                for row in csv.DictReader(handle)
                if row.get("目标档位") == "A-baseline"
                and row.get("是否跳过") != "true"
            )
    except OSError as error:
        raise vision_exam.VisionExamError(f"无法读取基线 results.csv：{error}") from error
    if len(rows) != 11 * REPETITIONS:
        raise vision_exam.VisionExamError(
            f"A 组基线应有 22 行，实际 {len(rows)} 行"
        )
    checks = {
        "请求模型": {row.get("请求模型", "") for row in rows},
        "输出模式": {row.get("输出模式", "") for row in rows},
        "max_tokens": {row.get("max_tokens", "") for row in rows},
        "上传宽度": {row.get("上传宽度", "") for row in rows},
        "区域提示模式": {row.get("区域提示模式", "") for row in rows},
    }
    expected = {
        "请求模型": {baseline_model},
        "输出模式": {"fast"},
        "max_tokens": {str(MAX_TOKENS)},
        "上传宽度": {str(SEND_WIDTH)},
        "区域提示模式": {"sparse"},
    }
    mismatches = [
        f"{name}={sorted(values)}，期望 {sorted(expected[name])}"
        for name, values in checks.items()
        if values != expected[name]
    ]
    if mismatches:
        raise vision_exam.VisionExamError(
            "D 组与 A 组控制变量不一致：" + "；".join(mismatches)
        )
    # OpenAI-compatible hosts may expose the same model without the catalog's
    # publisher prefix. Compare the final identifier segment and retain both
    # exact IDs in run.json instead of silently treating unrelated models alike.
    if baseline_model.rsplit("/", 1)[-1] != endpoint_model.rsplit("/", 1)[-1]:
        raise vision_exam.VisionExamError(
            "A/D 模型标识不属于同一模型："
            f"A={baseline_model}，D={endpoint_model}"
        )
    return rows


def baseline_summary(rows: Sequence[Mapping[str, str]]) -> dict[str, object]:
    """Compute the same objective fields from the prior CSV."""
    successes = [row for row in rows if not row.get("错误原文")]
    ttft = _float_values(successes, "TTFT毫秒")
    latency = _float_values(successes, "往返毫秒")
    input_tokens = _float_values(successes, "输入token")
    output_tokens = _float_values(successes, "可见输出token")
    costs = [
        _row_cost(row)
        for row in successes
        if _row_cost(row) is not None
    ]
    repetition_medians = {
        str(repetition): _median_or_none(
            _float_values(
                [row for row in successes if row.get("第几遍") == str(repetition)],
                "TTFT毫秒",
            )
        )
        for repetition in range(1, REPETITIONS + 1)
    }
    first = repetition_medians["1"]
    second = repetition_medians["2"]
    return {
        "attempts": len(rows),
        "successes": len(successes),
        "failures": len(rows) - len(successes),
        "failure_rate": (len(rows) - len(successes)) / len(rows),
        "truncated": sum(row.get("是否截断") == "true" for row in rows),
        "truncated_rate": sum(row.get("是否截断") == "true" for row in rows)
        / len(rows),
        "ttft_median_ms": _median_or_none(ttft),
        "ttft_p90_ms": vision_exam._percentile(ttft, 0.9) if ttft else None,
        "ttft_max_ms": max(ttft) if ttft else None,
        "latency_median_ms": _median_or_none(latency),
        "latency_p90_ms": vision_exam._percentile(latency, 0.9) if latency else None,
        "latency_max_ms": max(latency) if latency else None,
        "repetition_ttft_medians_ms": repetition_medians,
        "repetition_median_absolute_difference_ms": (
            abs(first - second)
            if isinstance(first, float) and isinstance(second, float)
            else None
        ),
        "average_input_tokens": (
            statistics.fmean(input_tokens) if input_tokens else None
        ),
        "average_visible_output_tokens": (
            statistics.fmean(output_tokens) if output_tokens else None
        ),
        "average_cost_per_frame_usd": statistics.fmean(costs) if costs else None,
    }


def custom_summary(
    records: Sequence[vision_exam.ExamRecord],
) -> dict[str, object]:
    """Compute D statistics with the same field names as the baseline."""
    attempted = [record for record in records if not record.skipped]
    successful = [record for record in attempted if record.succeeded]
    ttft = [record.ttft_ms for record in successful if record.ttft_ms is not None]
    latency = [
        record.latency_ms for record in successful if record.latency_ms is not None
    ]
    inputs = [
        record.input_tokens
        for record in successful
        if record.input_tokens is not None
    ]
    outputs = [
        record.visible_output_tokens
        for record in successful
        if record.visible_output_tokens is not None
    ]
    costs = [vision_exam._record_actual_cost(record) for record in successful]
    repetitions = {
        str(repetition): _median_or_none(
            [
                record.ttft_ms
                for record in successful
                if record.repetition == repetition and record.ttft_ms is not None
            ]
        )
        for repetition in range(1, REPETITIONS + 1)
    }
    first = repetitions["1"]
    second = repetitions["2"]
    return {
        "attempts": len(attempted),
        "successes": len(successful),
        "failures": len(attempted) - len(successful),
        "failure_rate": (
            (len(attempted) - len(successful)) / len(attempted)
            if attempted
            else None
        ),
        "truncated": sum(record.truncated for record in attempted),
        "truncated_rate": (
            sum(record.truncated for record in attempted) / len(attempted)
            if attempted
            else None
        ),
        "ttft_median_ms": _median_or_none(ttft),
        "ttft_p90_ms": vision_exam._percentile(ttft, 0.9) if ttft else None,
        "ttft_max_ms": max(ttft) if ttft else None,
        "latency_median_ms": _median_or_none(latency),
        "latency_p90_ms": vision_exam._percentile(latency, 0.9) if latency else None,
        "latency_max_ms": max(latency) if latency else None,
        "repetition_ttft_medians_ms": repetitions,
        "repetition_median_absolute_difference_ms": (
            abs(first - second)
            if isinstance(first, float) and isinstance(second, float)
            else None
        ),
        "average_input_tokens": statistics.fmean(inputs) if inputs else None,
        "average_visible_output_tokens": statistics.fmean(outputs) if outputs else None,
        "average_cost_per_frame_usd": statistics.fmean(costs) if costs else None,
    }


def render_summary(
    baseline: Mapping[str, object],
    custom: Mapping[str, object],
    *,
    host: str,
    actual_cost_usd: float,
) -> str:
    """Render a purely mechanical A/D comparison."""
    lines = [
        "# M5-T2.12 同模型地理对照机器统计",
        "",
        f"- D 组端点主机名：{host}",
        "- 请求控制变量：fast / max_tokens=80 / width=896 / sparse=0.25 / 流式 / 两遍",
        "",
        "| 组 | 调用数 | TTFT 中位/P90/最大(ms) | 总时延中位/P90/最大(ms) | 两遍TTFT中位差(ms) | 平均输入token | 平均可见输出token | 单帧花费(USD) | 失败率 | 截断率 |",
        "|---|---:|---|---|---:|---:|---:|---:|---:|---:|",
        _summary_markdown_row("A-historical", baseline),
        _summary_markdown_row(GROUP_LABEL, custom),
        "",
        "## 中位差",
        "",
        "- A TTFT 中位 − D TTFT 中位："
        f"{_difference(baseline, custom, 'ttft_median_ms')} ms",
        "- A 总时延中位 − D 总时延中位："
        f"{_difference(baseline, custom, 'latency_median_ms')} ms",
        "",
        "## 花费",
        "",
        f"- D 组实际总花费（USD）：{actual_cost_usd:.9f}",
    ]
    return "\n".join(lines) + "\n"


def render_grading(
    manifest: vision_exam.ExamManifest,
    records: Sequence[vision_exam.ExamRecord],
    answer_key_path: Path,
) -> str:
    """Render only D repetition one with blank human grading columns."""
    answers = vision_exam.load_answer_key(answer_key_path)
    lines = [
        "# M5-T2.12 D 组快线人工判卷表（第一遍）",
        "",
        "准确性判定、漏了什么、编造了什么由产品负责人填写；以下人工列均为空。",
    ]
    for question in manifest.questions:
        answer = answers.get(question.question_id)
        if answer is None:
            raise vision_exam.VisionExamError(f"答案键缺少题目 {question.question_id}")
        record = next(
            item
            for item in records
            if item.question_id == question.question_id and item.repetition == 1
        )
        lines.extend(
            (
                "",
                f"## `{question.question_id}`",
                "",
                "| 端点主机名 | TTFT(ms) | 总时延(ms) | 是否截断 | 回答原文 | 错误原文 | 准确性判定 | 漏了什么 | 编造了什么 |",
                "|---|---:|---:|---|---|---|---|---|---|",
                "| "
                + " | ".join(
                    (
                        record.endpoint_host or "",
                        _display(record.ttft_ms, 3),
                        _display(record.latency_ms, 3),
                        str(record.truncated).lower(),
                        vision_exam._markdown_cell(record.response_text),
                        vision_exam._markdown_cell(record.error or ""),
                        "",
                        "",
                        "",
                    )
                )
                + " |",
                "",
                "### 【核心】要点",
                "",
            )
        )
        lines.extend(f"- {point}" for point in answer.core)
        lines.extend(("", "### 不得出现的内容", ""))
        lines.extend(f"- {point}" for point in answer.forbidden)
    return "\n".join(lines) + "\n"


def write_outputs(
    output_directory: Path,
    *,
    manifest: vision_exam.ExamManifest,
    records: Sequence[vision_exam.ExamRecord],
    baseline_rows: Sequence[Mapping[str, str]],
    answer_key_path: Path,
    host: str,
    run_payload: Mapping[str, object],
) -> None:
    """Write ignored CSV, machine summary, grading, and safe run metadata."""
    output_directory.mkdir(parents=True, exist_ok=False)
    vision_exam._write_csv(output_directory / "results.csv", records)
    baseline = baseline_summary(baseline_rows)
    custom = custom_summary(records)
    actual_cost = sum(vision_exam._record_actual_cost(record) for record in records)
    (output_directory / "summary.md").write_text(
        render_summary(baseline, custom, host=host, actual_cost_usd=actual_cost),
        encoding="utf-8",
    )
    (output_directory / "grading-fast.md").write_text(
        render_grading(manifest, records, answer_key_path), encoding="utf-8"
    )
    payload = dict(run_payload)
    payload["baseline_summary"] = baseline
    payload["custom_summary"] = custom
    (output_directory / "run.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _summary_markdown_row(label: str, summary: Mapping[str, object]) -> str:
    return (
        f"| {label} | {summary.get('attempts', 0)} | "
        f"{_triple(summary, 'ttft')} | {_triple(summary, 'latency')} | "
        f"{_display(summary.get('repetition_median_absolute_difference_ms'), 3)} | "
        f"{_display(summary.get('average_input_tokens'), 2)} | "
        f"{_display(summary.get('average_visible_output_tokens'), 2)} | "
        f"{_display(summary.get('average_cost_per_frame_usd'), 9)} | "
        f"{_display(summary.get('failure_rate'), 6)} | "
        f"{_display(summary.get('truncated_rate'), 6)} |"
    )


def _triple(summary: Mapping[str, object], prefix: str) -> str:
    return " / ".join(
        _display(summary.get(f"{prefix}_{suffix}_ms"), 3)
        for suffix in ("median", "p90", "max")
    )


def _difference(
    baseline: Mapping[str, object],
    custom: Mapping[str, object],
    field: str,
) -> str:
    first = baseline.get(field)
    second = custom.get(field)
    if not isinstance(first, (int, float)) or not isinstance(second, (int, float)):
        return "未取得"
    return f"{float(first) - float(second):.3f}"


def _display(value: object, digits: int) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "未取得"
    return f"{float(value):.{digits}f}"


def _float_values(rows: Sequence[Mapping[str, str]], field: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        raw = row.get(field, "")
        if raw:
            values.append(float(raw))
    return values


def _median_or_none(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def _row_cost(row: Mapping[str, str]) -> float | None:
    upstream = row.get("上游报告花费美元", "")
    configured = row.get("配置折算花费美元", "")
    raw = upstream or configured
    return float(raw) if raw else None


def render_copy_command(arguments: argparse.Namespace) -> str:
    """Return a full rerun command without any credential value."""
    values = [
        "python",
        "-m",
        "pet.games.generic.eval.vision_endpoint_exam",
        str(arguments.manifest),
        "--profile",
        arguments.profile,
        "--input-price",
        str(arguments.input_price),
        "--output-price",
        str(arguments.output_price),
        "--price-source",
        arguments.price_source,
        "--region-label",
        arguments.region_label,
        "--baseline-results",
        str(arguments.baseline_results),
        "--baseline-model",
        arguments.baseline_model,
        "--cost-cap",
        str(arguments.cost_cap),
        "--yes",
    ]
    return " ".join(vision_exam._powershell_quote(value) for value in values)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M5-T2.12 自定义端点地理对照")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--input-price", type=float)
    parser.add_argument("--output-price", type=float)
    parser.add_argument("--price-source")
    parser.add_argument("--region-label", required=True)
    parser.add_argument("--baseline-results", type=Path, required=True)
    parser.add_argument("--baseline-model", required=True)
    parser.add_argument("--cost-cap", type=float, default=vision_exam.DEFAULT_COST_CAP_USD)
    parser.add_argument(
        "--estimated-input-tokens",
        type=int,
        default=vision_exam.DEFAULT_ESTIMATED_INPUT_TOKENS,
    )
    parser.add_argument("--answer-key", type=Path, default=vision_exam.DEFAULT_ANSWER_KEY_PATH)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--local-config", type=Path, default=LOCAL_CONFIG_PATH)
    parser.add_argument("--yes", action="store_true")
    return parser


def _validate_price(arguments: argparse.Namespace) -> vision_exam.ModelPrice:
    if (
        arguments.input_price is None
        or arguments.output_price is None
        or not arguments.price_source
    ):
        raise vision_exam.VisionExamError(
            "必须提供 --input-price、--output-price 与 --price-source；单价不得猜测"
        )
    values = (arguments.input_price, arguments.output_price)
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise vision_exam.VisionExamError("输入/输出单价必须是非负有限数")
    return vision_exam.ModelPrice(*values)


def _effective_profile(configuration: LlmConfig, name: str) -> LlmConfig:
    try:
        profile = resolve_llm_profile(configuration, name)
    except ValueError as error:
        raise vision_exam.VisionExamError(str(error)) from error
    if profile.base_url is None or not profile.base_url.strip():
        raise vision_exam.VisionExamError(
            f"档位 {name} 没有配置 base_url；D 组不得回退默认端点"
        )
    return profile


def main(argv: Sequence[str] | None = None) -> int:
    vision_exam._configure_console_encoding()
    arguments = build_parser().parse_args(argv)
    try:
        price = _validate_price(arguments)
        configuration = load_config(arguments.config, arguments.local_config)
        profile = _effective_profile(configuration.llm, arguments.profile)
        manifest = vision_exam.load_manifest(arguments.manifest)
        baseline_rows = validate_baseline(
            arguments.baseline_results,
            baseline_model=arguments.baseline_model,
            endpoint_model=profile.model,
        )
        probe = probe_llm_profile(
            configuration.llm,
            arguments.profile,
            image_path=Path(__file__).resolve().parents[5]
            / "tests"
            / "fixtures"
            / "vision-exam-frame-a.ppm",
        )
        if not probe.passed:
            print("探针未通过，未尝试跑卷。可复制命令：")
            print(render_copy_command(arguments))
            return 0
        host = endpoint_host(profile.base_url)
        variant = build_variant()
        target = vision_exam.ModelTarget(
            label=GROUP_LABEL,
            model=profile.model,
            provider=None,
            temperature=profile.temperature,
            timeout_seconds=profile.timeout_seconds,
            price=price,
            provider_lock_status="自定义端点",
            provider_region=arguments.region_label,
            endpoint_host=host,
            reasoning_parameter_mode=probe.selected_reasoning_mode,
        )
        estimate = vision_exam.estimate_formal_cost(
            question_count=len(manifest.questions),
            variants=(variant,),
            targets=(target,),
            estimated_input_tokens_per_attempt=arguments.estimated_input_tokens,
            repetitions=REPETITIONS,
            include_relaxed=False,
        )
        if estimate.estimated_cost_usd > arguments.cost_cap:
            raise vision_exam.VisionExamError(
                f"预计花费 ${estimate.estimated_cost_usd:.6f} 超过 "
                f"--cost-cap ${arguments.cost_cap:.6f}"
            )
        files = vision_exam.upload_files(manifest, (variant,))
        vision_exam.print_upload_plan(
            files,
            (target,),
            (variant,),
            estimate,
            cost_cap_usd=arguments.cost_cap,
            repetitions=REPETITIONS,
        )
        if not vision_exam.confirm_upload(assume_yes=arguments.yes):
            print("未确认上传，考卷未执行。")
            return 2

        started_at = datetime.now().astimezone()
        outcome = vision_exam.run_formal_exam(
            manifest=manifest,
            variants=(variant,),
            targets=(target,),
            client_factory=lambda _: OpenRouterClient.from_profile(
                profile_name=arguments.profile,
                base_url=profile.base_url,
                api_key_env=profile.api_key_env,
                timeout_seconds=profile.timeout_seconds,
            ),
            cost_cap_usd=arguments.cost_cap,
            enable_relaxed=False,
            repetitions=REPETITIONS,
            streaming=True,
        )
        ended_at = datetime.now().astimezone()
        output_directory = (
            vision_exam.DEFAULT_OUTPUT_ROOT
            / f"vision-endpoint-exam-{started_at.strftime('%Y%m%d-%H%M%S-%f')}"
        )
        write_outputs(
            output_directory,
            manifest=manifest,
            records=outcome.records,
            baseline_rows=baseline_rows,
            answer_key_path=arguments.answer_key,
            host=host,
            run_payload={
                "task_id": "M5-T2.12",
                "manifest": str(manifest.path),
                "profile_name": arguments.profile,
                "endpoint_host": host,
                "api_key_environment_variable": profile.api_key_env,
                "api_key_value_recorded": False,
                "baseline_model": arguments.baseline_model,
                "custom_endpoint_model": profile.model,
                "model_identifier_match_rule": "final path segment must match",
                "region_label": arguments.region_label,
                "reasoning_probe": asdict(probe),
                "input_price_per_million_usd": price.input_per_million_usd,
                "output_price_per_million_usd": price.output_per_million_usd,
                "price_source": arguments.price_source,
                "baseline_results": str(arguments.baseline_results),
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "cost_estimate": asdict(estimate),
                "actual_cost_usd": outcome.actual_cost_usd,
                "expected_rows": 11 * REPETITIONS,
                "actual_rows": len(outcome.records),
            },
        )
        print(f"跑卷完成：{output_directory}")
        print(f"结果 {len(outcome.records)} 行；实际花费 ${outcome.actual_cost_usd:.9f}")
        return 0
    except vision_exam.VisionExamError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
