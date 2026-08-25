"""M5-T2.11 tests use fake catalogs, fake clients, and synthetic images only."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx
import pytest

from pet.core.llm import LlmResult, LlmUsage
from pet.games.generic.eval import vision_exam
from pet.games.generic.eval.vision_geo_exam import (
    DirectSettings,
    GROUP_LABELS,
    ProviderSurvey,
    build_group_selections,
    geo_variant,
    provider_lock_verification,
    render_summary,
    select_us_endpoint,
    skipped_records,
    summarize_geo,
    survey_openrouter,
    write_geo_outputs,
)


FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_MANIFEST = FIXTURES / "vision-exam-example.toml"


def _model(slug: str) -> dict[str, object]:
    return {
        "id": slug,
        "name": slug,
        "architecture": {"input_modalities": ["text", "image"]},
        "pricing": {"prompt": "0.0000001", "completion": "0.0000002"},
        "supported_parameters": ["reasoning"],
        "reasoning": {"mandatory": False},
    }


def _endpoint(
    model: str,
    provider: str,
    tag: str,
    input_price: str,
    output_price: str,
) -> dict[str, object]:
    return {
        "name": f"{model}-{provider}",
        "provider_name": provider,
        "tag": tag,
        "pricing": {"prompt": input_price, "completion": output_price},
    }


def _survey() -> ProviderSurvey:
    models = ("vendor/a", "vendor/b", "vendor/c")
    providers = [
        {
            "name": "Asia Provider",
            "slug": "asia",
            "headquarters": "SG",
            "datacenters": ["SG", "CN"],
        },
        {
            "name": "US Cheap",
            "slug": "us-cheap",
            "headquarters": "US",
            "datacenters": ["US"],
        },
        {
            "name": "US Expensive",
            "slug": "us-expensive",
            "headquarters": "US",
            "datacenters": ["US"],
        },
        {
            "name": "HQ Only",
            "slug": "hq-only",
            "headquarters": "US",
            "datacenters": None,
        },
    ]
    endpoint_payloads = {
        "/api/v1/models/vendor/a/endpoints": [
            _endpoint("a", "Asia Provider", "asia", "0.00000003", "0.00000013")
        ],
        "/api/v1/models/vendor/b/endpoints": [
            _endpoint("b", "HQ Only", "hq-only/fp8", "0.00000001", "0.00000001"),
            _endpoint(
                "b", "US Expensive", "us-expensive/fp8", "0.0000005", "0.0000035"
            ),
            _endpoint("b", "US Cheap", "us-cheap/fp8", "0.0000004", "0.000003"),
        ],
        "/api/v1/models/vendor/c/endpoints": [
            _endpoint("c", "Asia Provider", "asia", "0.0000001", "0.0000004")
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"data": [_model(slug) for slug in models]})
        if request.url.path == "/api/v1/providers":
            return httpx.Response(200, json={"data": providers})
        endpoints = endpoint_payloads.get(request.url.path)
        if endpoints is not None:
            slug = request.url.path.removeprefix("/api/v1/models/").removesuffix(
                "/endpoints"
            )
            return httpx.Response(200, json={"data": {"id": slug, "endpoints": endpoints}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        return survey_openrouter(
            models,
            client=client,
            fetched_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        )
    finally:
        client.close()


def _direct_missing() -> DirectSettings:
    return DirectSettings(
        provider_name="Direct Provider",
        region="US",
        base_url_env="MISSING_BASE",
        api_key_env="MISSING_KEY",
        model_env="MISSING_MODEL",
        input_price_env="MISSING_INPUT",
        output_price_env="MISSING_OUTPUT",
        selection_reason="官方模型页与部署地区文档共同确认。",
        evidence_urls=("https://example.test/model", "https://example.test/regions"),
        missing_environment=(
            "MISSING_BASE",
            "MISSING_KEY",
            "MISSING_MODEL",
            "MISSING_INPUT",
            "MISSING_OUTPUT",
        ),
    )


class _FakeClient:
    def __init__(self, provider: str, ttft_seconds: float) -> None:
        self.provider = provider
        self.ttft_seconds = ttft_seconds
        self.calls: list[dict[str, object]] = []

    def complete_with_images_stream(self, **arguments: object) -> LlmResult:
        self.calls.append(arguments)
        return LlmResult(
            text="画面中的角色处于当前场景。",
            usage=LlmUsage(100, 15, 0.0002, reasoning_tokens=0),
            latency_seconds=self.ttft_seconds + 0.2,
            model=str(arguments["model"]),
            provider=self.provider,
            finish_reason="stop",
            ttft_seconds=self.ttft_seconds,
            streamed=True,
        )

    def complete_with_images(self, **arguments: object) -> LlmResult:
        raise AssertionError("本测试必须使用流式路径")

    def close(self) -> None:
        return None


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


def test_survey_confirms_us_only_from_datacenters_and_selects_cheapest() -> None:
    survey = _survey()
    model_b = survey.models[1]
    hq_only = next(
        endpoint
        for endpoint in model_b.endpoints
        if endpoint.provider_name == "HQ Only"
    )
    assert hq_only.headquarters == "US"
    assert hq_only.region_label == "地区未知"
    assert not hq_only.confirmed_us
    selected = select_us_endpoint(model_b)
    assert selected is not None
    assert selected.provider_name == "US Cheap"
    assert selected.provider_tag == "us-cheap/fp8"


def test_groups_lock_a_and_b_and_skip_unconfigured_c() -> None:
    selections = build_group_selections(
        _survey(),
        baseline_provider="asia",
        direct=_direct_missing(),
        temperature=0.0,
        timeout_seconds=30.0,
    )
    assert [selection.label for selection in selections] == list(GROUP_LABELS)
    assert selections[0].target.provider == "asia"
    assert selections[0].target.provider_region == "SG、CN"
    assert selections[1].target.provider == "us-cheap/fp8"
    assert selections[1].target.provider_region == "US"
    assert selections[2].skip_reason is not None
    assert "OpenRouter 无已确认美国端点" in selections[2].skip_reason


def test_geo_variant_is_fixed_fast_896_sparse_and_80_tokens() -> None:
    variant = geo_variant()
    assert variant.output_mode == "fast"
    assert variant.send_width == 896
    assert variant.region_mode == "sparse"
    assert variant.region_sparsity_max == 0.25
    assert variant.max_tokens == 80


def test_fake_geo_run_writes_rows_summary_grading_and_lock_verification(
    tmp_path: Path,
) -> None:
    survey = _survey()
    selections = build_group_selections(
        survey,
        baseline_provider="asia",
        direct=_direct_missing(),
        temperature=0.0,
        timeout_seconds=30.0,
    )
    clients = {
        GROUP_LABELS[0]: _FakeClient("Asia Provider", 0.150),
        GROUP_LABELS[1]: _FakeClient("US Cheap", 0.080),
    }
    manifest = vision_exam.load_manifest(EXAMPLE_MANIFEST)
    variant = geo_variant()
    runnable = tuple(
        selection.target for selection in selections if selection.skip_reason is None
    )
    outcome = vision_exam.run_formal_exam(
        manifest=manifest,
        variants=(variant,),
        targets=runnable,
        client_factory=lambda target: clients[target.label],
        cost_cap_usd=5.0,
        enable_relaxed=False,
        repetitions=2,
        streaming=True,
    )
    records = list(outcome.records)
    for selection in selections:
        records.extend(skipped_records(manifest, selection, variant))
    output = tmp_path / "geo"
    write_geo_outputs(
        output,
        manifest=manifest,
        records=records,
        survey=survey,
        selections=selections,
        direct=_direct_missing(),
        answer_key_path=_answer_key(tmp_path / "answer-key.md"),
        run_payload={"provider_lock_verification": provider_lock_verification(records, selections)},
    )
    with (output / "results.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    summary = json.loads((output / "run.json").read_text(encoding="utf-8"))["summary"]
    summary_markdown = (output / "summary.md").read_text(encoding="utf-8")
    grading = (output / "grading-fast.md").read_text(encoding="utf-8")
    assert len(rows) == 3 * 2 * 2
    assert sum(row["是否跳过"] == "true" for row in rows) == 4
    assert all(
        not row["实际上游"]
        for row in rows
        if row["是否跳过"] == "true"
    )
    assert all(row["max_tokens"] == "80" for row in rows)
    assert all(row["上传宽度"] == "896" for row in rows)
    assert summary["executed_calls"] == 8
    assert summary["skipped_rows"] == 4
    assert summary["a_minus_comparison_ttft_median_ms"][GROUP_LABELS[1]] == pytest.approx(70)
    assert "Provider 与地区调查" in summary_markdown
    assert "| HQ Only | 地区未知 | US | 地区未知 |" in summary_markdown
    assert "## C 组直连回退状态" in summary_markdown
    assert "A 中位 − B-us-open 中位：70.000 ms" in summary_markdown
    assert "第一遍" in grading
    assert "第二遍" not in grading
    assert all(client.calls for client in clients.values())
    assert all(
        call["max_tokens"] == 80
        and call["provider"] in {"asia", "us-cheap/fp8"}
        for client in clients.values()
        for call in client.calls
    )
    verification = provider_lock_verification(records, selections)
    assert verification[GROUP_LABELS[0]]["consistent"] is True  # type: ignore[index]
    assert verification[GROUP_LABELS[1]]["consistent"] is True  # type: ignore[index]
    assert verification[GROUP_LABELS[2]]["consistent"] is None  # type: ignore[index]


def test_summary_reports_missing_comparison_without_inventing_ttft() -> None:
    survey = _survey()
    text = render_summary(
        survey,
        summarize_geo([]),
    )
    assert "A 中位 − B-us-open 中位：未取得 ms" in text
    assert "A 中位 − C-us-vision 中位：未取得 ms" in text
