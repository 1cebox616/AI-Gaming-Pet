"""Run the M5-T2.11 provider-geography comparison with live catalog data."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
import json
import math
import os
from pathlib import Path
import statistics
import sys
from typing import cast

import httpx

from pet.core.llm import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_BASE_URL,
    LlmVisionClientProtocol,
    OpenRouterClient,
)
from pet.games.generic.eval import vision_exam


OPENROUTER_PROVIDERS_URL = f"{OPENROUTER_BASE_URL}/providers"
GROUP_LABELS = ("A-baseline", "B-us-open", "C-us-vision")
REPETITIONS = 2
SEND_WIDTH = 896
MAX_TOKENS = 80


@dataclass(frozen=True, slots=True)
class ProviderLocation:
    """Location facts returned by OpenRouter, without geographic inference."""

    name: str
    slug: str
    headquarters: str | None
    datacenters: tuple[str, ...]

    @property
    def region_label(self) -> str:
        return "、".join(self.datacenters) if self.datacenters else "地区未知"

    @property
    def confirmed_us(self) -> bool:
        return any(region.casefold() == "us" for region in self.datacenters)


@dataclass(frozen=True, slots=True)
class ProviderEndpoint:
    """One live model endpoint joined to the provider location catalog."""

    model_slug: str
    endpoint_name: str
    provider_name: str
    provider_tag: str
    price: vision_exam.ModelPrice
    location: ProviderLocation | None

    @property
    def region_label(self) -> str:
        return self.location.region_label if self.location is not None else "地区未知"

    @property
    def headquarters(self) -> str:
        if self.location is None or self.location.headquarters is None:
            return "未知"
        return self.location.headquarters

    @property
    def confirmed_us(self) -> bool:
        return self.location is not None and self.location.confirmed_us


@dataclass(frozen=True, slots=True)
class SurveyedModel:
    """One exact image-capable model plus all current OpenRouter endpoints."""

    requested_name: str
    resolved: vision_exam.ResolvedModel
    endpoints: tuple[ProviderEndpoint, ...]


@dataclass(frozen=True, slots=True)
class ProviderSurvey:
    """Timestamped provider and model endpoint survey."""

    fetched_at: str
    model_catalog_url: str
    provider_catalog_url: str
    models: tuple[SurveyedModel, ...]


@dataclass(frozen=True, slots=True)
class DirectSettings:
    """Environment-only direct endpoint settings, never including the API key."""

    provider_name: str
    region: str
    base_url_env: str
    api_key_env: str
    model_env: str
    input_price_env: str
    output_price_env: str
    selection_reason: str
    evidence_urls: tuple[str, ...]
    missing_environment: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.missing_environment


@dataclass(frozen=True, slots=True)
class GroupSelection:
    """One requested comparison group, runnable or explicitly skipped."""

    label: str
    target: vision_exam.ModelTarget
    skip_reason: str | None
    transport: str


def survey_openrouter(
    requested_names: Sequence[str],
    *,
    client: httpx.Client | None = None,
    fetched_at: datetime | None = None,
) -> ProviderSurvey:
    """Resolve exact models, endpoint prices, and provider datacenter facts live."""
    if len(requested_names) != len(GROUP_LABELS):
        raise vision_exam.VisionExamError("M5-T2.11 必须按 A/B/C 顺序传入恰好三个 --model")
    owns_client = client is None
    catalog_client = client or httpx.Client(timeout=30.0)
    try:
        model_payload = vision_exam._get_openrouter_json(
            catalog_client, vision_exam.OPENROUTER_MODELS_URL, "型号目录"
        )
        provider_payload = vision_exam._get_openrouter_json(
            catalog_client, OPENROUTER_PROVIDERS_URL, "provider 目录"
        )
        raw_models = _catalog_list(model_payload, "型号目录")
        locations = _parse_provider_locations(provider_payload)
        surveyed: list[SurveyedModel] = []
        errors: list[str] = []
        for requested_name in requested_names:
            match = vision_exam._match_catalog_model(requested_name, raw_models)
            if match is None:
                candidates = vision_exam._closest_catalog_slugs(requested_name, raw_models)
                errors.append(
                    f"{requested_name}：不存在；最接近："
                    + ("、".join(candidates) if candidates else "无")
                )
                continue
            try:
                resolved = vision_exam._parse_resolved_model(requested_name, match)
            except vision_exam.VisionExamError as error:
                errors.append(str(error))
                continue
            if "image" not in resolved.input_modalities:
                errors.append(f"{requested_name} -> {resolved.slug}：不支持图像输入")
                continue
            endpoint_payload = vision_exam._get_openrouter_json(
                catalog_client,
                vision_exam.OPENROUTER_MODEL_ENDPOINTS_URL.format(slug=resolved.slug),
                f"{resolved.slug} 端点目录",
            )
            endpoints = _parse_model_endpoints(
                resolved.slug, endpoint_payload, locations
            )
            if not endpoints:
                errors.append(f"{resolved.slug}：端点目录为空")
                continue
            surveyed.append(SurveyedModel(requested_name, resolved, endpoints))
        if errors:
            raise vision_exam.VisionExamError(
                "候选型号或端点解析失败：\n- " + "\n- ".join(errors)
            )
        timestamp = fetched_at or datetime.now().astimezone()
        return ProviderSurvey(
            fetched_at=timestamp.isoformat(),
            model_catalog_url=vision_exam.OPENROUTER_MODELS_URL,
            provider_catalog_url=OPENROUTER_PROVIDERS_URL,
            models=tuple(surveyed),
        )
    finally:
        if owns_client:
            catalog_client.close()


def _catalog_list(payload: object, label: str) -> list[Mapping[str, object]]:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise vision_exam.VisionExamError(f"OpenRouter {label}缺少 data 数组")
    return [item for item in payload["data"] if isinstance(item, Mapping)]


def _parse_provider_locations(payload: object) -> dict[str, ProviderLocation]:
    providers = _catalog_list(payload, "provider 目录")
    locations: dict[str, ProviderLocation] = {}
    for raw in providers:
        name = raw.get("name")
        slug = raw.get("slug")
        if not isinstance(name, str) or not isinstance(slug, str):
            continue
        headquarters_value = raw.get("headquarters")
        datacenters_value = raw.get("datacenters")
        datacenters = (
            tuple(
                item.strip()
                for item in datacenters_value
                if isinstance(item, str) and item.strip()
            )
            if isinstance(datacenters_value, list)
            else ()
        )
        location = ProviderLocation(
            name=name,
            slug=slug,
            headquarters=(
                headquarters_value
                if isinstance(headquarters_value, str) and headquarters_value.strip()
                else None
            ),
            datacenters=datacenters,
        )
        locations[slug.casefold()] = location
        locations[name.casefold()] = location
    return locations


def _parse_model_endpoints(
    model_slug: str,
    payload: object,
    locations: Mapping[str, ProviderLocation],
) -> tuple[ProviderEndpoint, ...]:
    if not isinstance(payload, Mapping):
        raise vision_exam.VisionExamError(f"{model_slug}：端点目录不是 JSON 对象")
    data_value = payload.get("data")
    data = data_value if isinstance(data_value, Mapping) else {}
    endpoints_value = data.get("endpoints")
    if not isinstance(endpoints_value, list):
        raise vision_exam.VisionExamError(f"{model_slug}：端点目录缺少 endpoints 数组")
    endpoints: list[ProviderEndpoint] = []
    for raw in endpoints_value:
        if not isinstance(raw, Mapping):
            continue
        provider_name = raw.get("provider_name")
        provider_tag = raw.get("tag")
        endpoint_name = raw.get("name")
        if not isinstance(provider_name, str) or not isinstance(provider_tag, str):
            continue
        price = vision_exam._parse_price_mapping(
            raw.get("pricing"), label=f"{model_slug} {provider_name} 端点"
        )
        location = locations.get(provider_tag.split("/", 1)[0].casefold())
        if location is None:
            location = locations.get(provider_name.casefold())
        endpoints.append(
            ProviderEndpoint(
                model_slug=model_slug,
                endpoint_name=(endpoint_name if isinstance(endpoint_name, str) else ""),
                provider_name=provider_name,
                provider_tag=provider_tag,
                price=price,
                location=location,
            )
        )
    return tuple(endpoints)


def select_named_endpoint(
    model: SurveyedModel, provider_query: str
) -> ProviderEndpoint:
    """Select an exact provider name/tag supplied on the command line."""
    query = provider_query.strip().casefold()
    matches = [
        endpoint
        for endpoint in model.endpoints
        if query
        in {
            endpoint.provider_name.casefold(),
            endpoint.provider_tag.casefold(),
            endpoint.provider_tag.split("/", 1)[0].casefold(),
        }
    ]
    if not matches:
        available = "、".join(endpoint.provider_tag for endpoint in model.endpoints)
        raise vision_exam.VisionExamError(
            f"{model.resolved.slug} 没有指定上游 {provider_query}；可用：{available}"
        )
    return min(matches, key=_endpoint_price_sort_key)


def select_us_endpoint(model: SurveyedModel) -> ProviderEndpoint | None:
    """Mechanically choose the cheapest endpoint whose datacenter says US."""
    confirmed = [endpoint for endpoint in model.endpoints if endpoint.confirmed_us]
    return min(confirmed, key=_endpoint_price_sort_key) if confirmed else None


def _endpoint_price_sort_key(endpoint: ProviderEndpoint) -> tuple[float, str]:
    return (
        endpoint.price.input_per_million_usd
        + endpoint.price.output_per_million_usd,
        endpoint.provider_tag,
    )


def direct_settings_from_environment(arguments: argparse.Namespace) -> DirectSettings:
    """Check only caller-named environment variables for the optional direct group."""
    names = (
        arguments.direct_base_url_env,
        arguments.direct_api_key_env,
        arguments.direct_model_env,
        arguments.direct_input_price_env,
        arguments.direct_output_price_env,
    )
    missing = tuple(name for name in names if not os.environ.get(name, "").strip())
    return DirectSettings(
        provider_name=arguments.direct_provider,
        region=arguments.direct_region,
        base_url_env=arguments.direct_base_url_env,
        api_key_env=arguments.direct_api_key_env,
        model_env=arguments.direct_model_env,
        input_price_env=arguments.direct_input_price_env,
        output_price_env=arguments.direct_output_price_env,
        selection_reason=arguments.direct_selection_reason,
        evidence_urls=tuple(arguments.direct_evidence_url),
        missing_environment=missing,
    )


def build_group_selections(
    survey: ProviderSurvey,
    *,
    baseline_provider: str,
    direct: DirectSettings,
    temperature: float,
    timeout_seconds: float,
) -> tuple[GroupSelection, ...]:
    """Build A/B/C without substituting an unavailable group."""
    if len(survey.models) != len(GROUP_LABELS):
        raise vision_exam.VisionExamError("地区调查没有返回完整的 A/B/C 三组")
    baseline = select_named_endpoint(survey.models[0], baseline_provider)
    open_us = select_us_endpoint(survey.models[1])
    if open_us is None:
        raise vision_exam.VisionExamError(
            f"B 组 {survey.models[1].resolved.slug} 没有 OpenRouter 明确标为 US 的端点"
        )
    selections = [
        GroupSelection(
            GROUP_LABELS[0],
            _target_from_endpoint(
                GROUP_LABELS[0], survey.models[0], baseline, temperature, timeout_seconds
            ),
            None,
            "OpenRouter",
        ),
        GroupSelection(
            GROUP_LABELS[1],
            _target_from_endpoint(
                GROUP_LABELS[1], survey.models[1], open_us, temperature, timeout_seconds
            ),
            None,
            "OpenRouter",
        ),
    ]
    vision_us = select_us_endpoint(survey.models[2])
    if vision_us is not None:
        selections.append(
            GroupSelection(
                GROUP_LABELS[2],
                _target_from_endpoint(
                    GROUP_LABELS[2], survey.models[2], vision_us, temperature, timeout_seconds
                ),
                None,
                "OpenRouter",
            )
        )
        return tuple(selections)

    if direct.ready:
        price = _direct_price(direct)
        model = os.environ[direct.model_env].strip()
        selections.append(
            GroupSelection(
                GROUP_LABELS[2],
                vision_exam.ModelTarget(
                    label=GROUP_LABELS[2],
                    model=model,
                    provider=None,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                    price=price,
                    reasoning_disabled=True,
                    provider_lock_status="直连",
                    provider_display_name=direct.provider_name,
                    provider_region=direct.region,
                ),
                None,
                "direct",
            )
        )
    else:
        missing = "、".join(direct.missing_environment)
        fallback_price = survey.models[2].resolved.price
        selections.append(
            GroupSelection(
                GROUP_LABELS[2],
                vision_exam.ModelTarget(
                    label=GROUP_LABELS[2],
                    model=survey.models[2].resolved.slug,
                    provider=None,
                    temperature=temperature,
                    timeout_seconds=timeout_seconds,
                    price=fallback_price,
                    reasoning_disabled=survey.models[2].resolved.reasoning_disabled,
                    provider_lock_status="已跳过",
                    provider_display_name=direct.provider_name,
                    provider_region=direct.region,
                ),
                f"OpenRouter 无已确认美国端点，直连环境变量缺失：{missing}",
                "direct",
            )
        )
    return tuple(selections)


def _target_from_endpoint(
    label: str,
    model: SurveyedModel,
    endpoint: ProviderEndpoint,
    temperature: float,
    timeout_seconds: float,
) -> vision_exam.ModelTarget:
    return vision_exam.ModelTarget(
        label=label,
        model=model.resolved.slug,
        provider=endpoint.provider_tag,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        price=endpoint.price,
        reasoning_disabled=model.resolved.reasoning_disabled,
        provider_lock_status="已锁定",
        provider_endpoint=endpoint.endpoint_name,
        provider_display_name=endpoint.provider_name,
        provider_region=endpoint.region_label,
    )


def _direct_price(settings: DirectSettings) -> vision_exam.ModelPrice:
    try:
        input_price = float(os.environ[settings.input_price_env])
        output_price = float(os.environ[settings.output_price_env])
    except ValueError as error:
        raise vision_exam.VisionExamError("直连环境变量中的单价不是数字") from error
    if any(
        not math.isfinite(value) or value < 0 for value in (input_price, output_price)
    ):
        raise vision_exam.VisionExamError("直连环境变量中的单价必须是非负有限数")
    return vision_exam.ModelPrice(input_price, output_price)


def geo_variant() -> vision_exam.ExamVariant:
    """Return the single fixed M5-T2.11 request shape."""
    return vision_exam.ExamVariant(
        SEND_WIDTH,
        "sparse",
        vision_exam.DEFAULT_REGION_SPARSITY_MAX,
        "fast",
        max_tokens_override=MAX_TOKENS,
    )


def make_client_factory(
    selections: Sequence[GroupSelection], direct: DirectSettings
) -> Callable[[vision_exam.ModelTarget], LlmVisionClientProtocol]:
    """Create locked OpenRouter clients or the explicitly configured direct client."""
    transport_by_label = {selection.label: selection.transport for selection in selections}

    def factory(target: vision_exam.ModelTarget) -> LlmVisionClientProtocol:
        if transport_by_label[target.label] == "direct":
            return OpenRouterClient(
                os.environ[direct.api_key_env],
                base_url=os.environ[direct.base_url_env],
                timeout_seconds=target.timeout_seconds,
            )
        return OpenRouterClient.from_env(timeout_seconds=target.timeout_seconds)

    return factory


def skipped_records(
    manifest: vision_exam.ExamManifest,
    selection: GroupSelection,
    variant: vision_exam.ExamVariant,
) -> tuple[vision_exam.ExamRecord, ...]:
    """Preserve one explicit placeholder per required but unavailable call."""
    if selection.skip_reason is None:
        return ()
    return tuple(
        replace(
            vision_exam._failed_record(
                question,
                variant,
                selection.target,
                selection.skip_reason,
                repetition=repetition,
            ),
            skipped=True,
            provider=None,
            provider_region=None,
        )
        for repetition in range(1, REPETITIONS + 1)
        for question in manifest.questions
    )


def summarize_geo(
    records: Sequence[vision_exam.ExamRecord],
) -> dict[str, object]:
    """Compute only objective group, repetition, cost, and A-delta statistics."""
    groups: dict[str, dict[str, object]] = {}
    for label in GROUP_LABELS:
        selected = [record for record in records if record.target_label == label]
        groups[label] = _geo_summary_row(selected)
    baseline_median = groups[GROUP_LABELS[0]]["ttft_median_ms"]
    deltas: dict[str, float | None] = {}
    for label in GROUP_LABELS[1:]:
        comparison = groups[label]["ttft_median_ms"]
        deltas[label] = (
            cast(float, baseline_median) - cast(float, comparison)
            if isinstance(baseline_median, (int, float))
            and isinstance(comparison, (int, float))
            else None
        )
    return {
        "groups": groups,
        "a_minus_comparison_ttft_median_ms": deltas,
        "actual_cost_usd": sum(vision_exam._record_actual_cost(record) for record in records),
        "rows": len(records),
        "executed_calls": sum(not record.skipped for record in records),
        "skipped_rows": sum(record.skipped for record in records),
    }


def _geo_summary_row(records: Sequence[vision_exam.ExamRecord]) -> dict[str, object]:
    attempted = [record for record in records if not record.skipped]
    successful = [record for record in attempted if record.succeeded]
    ttft = [record.ttft_ms for record in successful if record.ttft_ms is not None]
    latency = [record.latency_ms for record in successful if record.latency_ms is not None]
    input_tokens = [
        record.input_tokens for record in successful if record.input_tokens is not None
    ]
    visible_tokens = [
        record.visible_output_tokens
        for record in successful
        if record.visible_output_tokens is not None
    ]
    reasoning_tokens = [
        record.reasoning_tokens
        for record in successful
        if record.reasoning_tokens is not None
    ]
    repetition_medians: dict[str, float | None] = {}
    for repetition in range(1, REPETITIONS + 1):
        values = [
            record.ttft_ms
            for record in successful
            if record.repetition == repetition and record.ttft_ms is not None
        ]
        repetition_medians[str(repetition)] = statistics.median(values) if values else None
    first = repetition_medians["1"]
    second = repetition_medians["2"]
    return {
        "rows": len(records),
        "attempts": len(attempted),
        "skipped": len(records) - len(attempted),
        "successes": len(successful),
        "failures": len(attempted) - len(successful),
        "failure_rate": (
            (len(attempted) - len(successful)) / len(attempted) if attempted else None
        ),
        "truncated": sum(record.truncated for record in attempted),
        "truncated_rate": (
            sum(record.truncated for record in attempted) / len(attempted)
            if attempted
            else None
        ),
        "ttft_median_ms": statistics.median(ttft) if ttft else None,
        "ttft_p90_ms": vision_exam._percentile(ttft, 0.90) if ttft else None,
        "ttft_max_ms": max(ttft) if ttft else None,
        "latency_median_ms": statistics.median(latency) if latency else None,
        "latency_p90_ms": vision_exam._percentile(latency, 0.90) if latency else None,
        "latency_max_ms": max(latency) if latency else None,
        "repetition_ttft_medians_ms": repetition_medians,
        "repetition_median_absolute_difference_ms": (
            abs(cast(float, first) - cast(float, second))
            if isinstance(first, (int, float)) and isinstance(second, (int, float))
            else None
        ),
        "average_input_tokens": statistics.fmean(input_tokens) if input_tokens else None,
        "average_visible_output_tokens": (
            statistics.fmean(visible_tokens) if visible_tokens else None
        ),
        "average_reasoning_tokens": (
            statistics.fmean(reasoning_tokens) if reasoning_tokens else None
        ),
        "average_cost_per_frame_usd": (
            statistics.fmean(
                vision_exam._record_actual_cost(record) / max(len(record.image_dimensions), 1)
                for record in successful
            )
            if successful
            else None
        ),
        "providers": sorted(
            {record.provider for record in attempted if record.provider is not None}
        ),
        "regions": sorted(
            {record.provider_region for record in attempted if record.provider_region is not None}
        ),
    }


def render_survey_table(survey: ProviderSurvey) -> str:
    """Render all endpoint/location facts, including unknown regions."""
    lines = [
        "| 候选模型 | provider 路由 tag | provider 名 | 数据中心地区 | 总部 | 美国境内确认 | 输入单价(USD/M) | 输出单价(USD/M) |",
        "|---|---|---|---|---|---|---:|---:|",
    ]
    for model in survey.models:
        for endpoint in model.endpoints:
            lines.append(
                "| "
                + " | ".join(
                    (
                        vision_exam._markdown_cell(model.resolved.slug),
                        vision_exam._markdown_cell(endpoint.provider_tag),
                        vision_exam._markdown_cell(endpoint.provider_name),
                        vision_exam._markdown_cell(endpoint.region_label),
                        vision_exam._markdown_cell(endpoint.headquarters),
                        (
                            "是"
                            if endpoint.confirmed_us
                            else "地区未知"
                            if endpoint.region_label == "地区未知"
                            else "否"
                        ),
                        f"{endpoint.price.input_per_million_usd:g}",
                        f"{endpoint.price.output_per_million_usd:g}",
                    )
                )
                + " |"
            )
    return "\n".join(lines)


def render_summary(
    survey: ProviderSurvey,
    summary: Mapping[str, object],
    *,
    selections: Sequence[GroupSelection] = (),
    direct: DirectSettings | None = None,
) -> str:
    """Render a machine-only M5-T2.11 summary without quality conclusions."""
    lines = [
        "# M5-T2.11 上游地理位置对比机器统计",
        "",
        f"- provider/端点目录获取时间：{survey.fetched_at}",
        f"- 结果总行数：{summary.get('rows', 0)}",
        f"- 实际调用数：{summary.get('executed_calls', 0)}",
        f"- 跳过占位行数：{summary.get('skipped_rows', 0)}",
        "",
        "## Provider 与地区调查",
        "",
        render_survey_table(survey),
        "",
        "地区仅以 provider 接口的 datacenters 字段确认；该字段为空时记为“地区未知”，不从总部推断。",
        "",
        "## 三组调用统计",
        "",
        "| 组 | 行数 | 实际调用 | 跳过 | TTFT 中位/P90/最大(ms) | 总时延中位/P90/最大(ms) | 两遍TTFT中位差(ms) | 平均输入token | 平均可见输出token | 平均推理token | 单帧花费(USD) | 失败率 | 截断率 |",
        "|---|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    groups = summary.get("groups")
    if isinstance(groups, Mapping):
        for label in GROUP_LABELS:
            raw = groups.get(label)
            if not isinstance(raw, Mapping):
                continue
            lines.append(
                f"| {label} | {raw.get('rows', 0)} | {raw.get('attempts', 0)} | "
                f"{raw.get('skipped', 0)} | "
                f"{_triple(raw, 'ttft')} | {_triple(raw, 'latency')} | "
                f"{_display(raw.get('repetition_median_absolute_difference_ms'), 3)} | "
                f"{_display(raw.get('average_input_tokens'), 2)} | "
                f"{_display(raw.get('average_visible_output_tokens'), 2)} | "
                f"{_display(raw.get('average_reasoning_tokens'), 2)} | "
                f"{_display(raw.get('average_cost_per_frame_usd'), 9)} | "
                f"{_display(raw.get('failure_rate'), 6)} | "
                f"{_display(raw.get('truncated_rate'), 6)} |"
            )
    lines.extend(("", "## A 组与美国组 TTFT 中位差", ""))
    deltas = summary.get("a_minus_comparison_ttft_median_ms")
    if isinstance(deltas, Mapping):
        for label in GROUP_LABELS[1:]:
            lines.append(
                f"- A 中位 − {label} 中位：{_display(deltas.get(label), 3)} ms"
            )
    lines.extend(
        (
            "",
            "## 花费",
            "",
            f"- 实际总花费（USD）：{_display(summary.get('actual_cost_usd'), 9)}",
        )
    )
    direct_selection = next(
        (selection for selection in selections if selection.label == GROUP_LABELS[2]),
        None,
    )
    if direct is not None and direct_selection is not None:
        lines.extend(
            (
                "",
                "## C 组直连回退状态",
                "",
                f"- 指定厂商：{direct.provider_name}",
                f"- 指定地区：{direct.region}",
                f"- 选择依据：{direct.selection_reason}",
            )
        )
        lines.extend(f"- 官方依据：{url}" for url in direct.evidence_urls)
        lines.append(f"- 跳过原因：{direct_selection.skip_reason or '未跳过'}")
    return "\n".join(lines) + "\n"


def _triple(raw: Mapping[str, object], prefix: str) -> str:
    return " / ".join(
        _display(raw.get(f"{prefix}_{suffix}_ms"), 3)
        for suffix in ("median", "p90", "max")
    )


def _display(value: object, digits: int) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "未取得"
    return f"{float(value):.{digits}f}"


def render_grading(
    manifest: vision_exam.ExamManifest,
    records: Sequence[vision_exam.ExamRecord],
    answer_key_path: Path,
) -> str:
    """Render first-repetition A/B/C answers beside authoritative fast criteria."""
    answers = vision_exam.load_answer_key(answer_key_path)
    lines = [
        "# M5-T2.11 快线人工判卷表（第一遍）",
        "",
        "准确性判定、漏了什么、编造了什么由产品负责人填写；以下人工列均为空。",
    ]
    for question in manifest.questions:
        answer = answers.get(question.question_id)
        if answer is None:
            raise vision_exam.VisionExamError(f"答案键缺少题目 {question.question_id}")
        question_records = [
            record
            for record in records
            if record.question_id == question.question_id and record.repetition == 1
        ]
        lines.extend(
            (
                "",
                f"## `{question.question_id}`",
                "",
                "| 组 | 实际上游 | 地区 | TTFT(ms) | 总时延(ms) | 是否截断 | 回答原文 | 错误原文 | 准确性判定 | 漏了什么 | 编造了什么 |",
                "|---|---|---|---:|---:|---|---|---|---|---|---|",
            )
        )
        for record in question_records:
            lines.append(
                "| "
                + " | ".join(
                    (
                        record.target_label,
                        vision_exam._markdown_cell(record.provider or ""),
                        vision_exam._markdown_cell(record.provider_region or "地区未知"),
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
                + " |"
            )
        lines.extend(("", "### 【核心】要点", ""))
        lines.extend(f"- {point}" for point in answer.core)
        lines.extend(("", "### 不得出现的内容", ""))
        lines.extend(f"- {point}" for point in answer.forbidden)
    return "\n".join(lines) + "\n"


def write_geo_outputs(
    output_directory: Path,
    *,
    manifest: vision_exam.ExamManifest,
    records: Sequence[vision_exam.ExamRecord],
    survey: ProviderSurvey,
    selections: Sequence[GroupSelection],
    direct: DirectSettings,
    answer_key_path: Path,
    run_payload: Mapping[str, object],
) -> None:
    """Write ignored machine statistics, grading sheet, CSV, and run metadata."""
    output_directory.mkdir(parents=True, exist_ok=False)
    vision_exam._write_csv(output_directory / "results.csv", records)
    summary = summarize_geo(records)
    (output_directory / "summary.md").write_text(
        render_summary(survey, summary, selections=selections, direct=direct),
        encoding="utf-8",
    )
    (output_directory / "grading-fast.md").write_text(
        render_grading(manifest, records, answer_key_path), encoding="utf-8"
    )
    payload = dict(run_payload)
    payload["summary"] = summary
    (output_directory / "run.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def provider_lock_verification(
    records: Sequence[vision_exam.ExamRecord],
    selections: Sequence[GroupSelection],
) -> dict[str, object]:
    """Compare every returned provider name with the selected endpoint name."""
    verification: dict[str, object] = {}
    for selection in selections:
        attempted = [
            record
            for record in records
            if record.target_label == selection.label and not record.skipped
        ]
        actual = sorted(
            {record.provider for record in attempted if record.provider is not None}
        )
        expected = selection.target.provider_display_name
        verification[selection.label] = {
            "expected_provider_name": expected,
            "expected_provider_route_tag": selection.target.provider,
            "expected_region": selection.target.provider_region,
            "actual_providers": actual,
            "consistent": (
                bool(actual)
                and expected is not None
                and all(name.casefold() == expected.casefold() for name in actual)
                if attempted
                else None
            ),
            "skip_reason": selection.skip_reason,
        }
    return verification


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="M5-T2.11 上游地理位置对比")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--model", action="append", default=[], help="按 A/B/C 顺序重复三次")
    parser.add_argument("--baseline-provider", required=True, help="A 组 provider 名或路由 tag")
    parser.add_argument("--direct-provider", required=True, help="C 组直连厂商显示名")
    parser.add_argument("--direct-region", required=True, help="C 组直连部署的已确认地区")
    parser.add_argument("--direct-base-url-env", required=True)
    parser.add_argument("--direct-api-key-env", required=True)
    parser.add_argument("--direct-model-env", required=True)
    parser.add_argument("--direct-input-price-env", required=True)
    parser.add_argument("--direct-output-price-env", required=True)
    parser.add_argument("--direct-selection-reason", required=True)
    parser.add_argument("--direct-evidence-url", action="append", required=True)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--cost-cap", type=float, default=vision_exam.DEFAULT_COST_CAP_USD)
    parser.add_argument(
        "--estimated-input-tokens",
        type=int,
        default=vision_exam.DEFAULT_ESTIMATED_INPUT_TOKENS,
    )
    parser.add_argument("--answer-key", type=Path, default=vision_exam.DEFAULT_ANSWER_KEY_PATH)
    parser.add_argument("--yes", action="store_true")
    return parser


def _sanitize_arguments(arguments: argparse.Namespace) -> dict[str, object]:
    return {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(arguments).items()
    }


def main(argv: Sequence[str] | None = None) -> int:
    vision_exam._configure_console_encoding()
    arguments = build_parser().parse_args(argv)
    try:
        if not 0 <= arguments.temperature <= 2:
            raise vision_exam.VisionExamError("--temperature 必须在 0–2")
        if arguments.timeout <= 0 or arguments.cost_cap <= 0:
            raise vision_exam.VisionExamError("--timeout 与 --cost-cap 必须大于 0")
        manifest = vision_exam.load_manifest(arguments.manifest)
        survey = survey_openrouter(arguments.model)
        direct = direct_settings_from_environment(arguments)
        selections = build_group_selections(
            survey,
            baseline_provider=arguments.baseline_provider,
            direct=direct,
            temperature=arguments.temperature,
            timeout_seconds=arguments.timeout,
        )
        runnable = tuple(
            selection.target for selection in selections if selection.skip_reason is None
        )
        variant = geo_variant()
        estimate = vision_exam.estimate_formal_cost(
            question_count=len(manifest.questions),
            variants=(variant,),
            targets=runnable,
            estimated_input_tokens_per_attempt=arguments.estimated_input_tokens,
            repetitions=REPETITIONS,
            include_relaxed=False,
        )
        if estimate.estimated_cost_usd > arguments.cost_cap:
            raise vision_exam.VisionExamError(
                f"预计花费 ${estimate.estimated_cost_usd:.6f} 超过 "
                f"--cost-cap ${arguments.cost_cap:.6f}"
            )
        openrouter_needed = any(
            selection.skip_reason is None and selection.transport == "OpenRouter"
            for selection in selections
        )
        if openrouter_needed and not os.environ.get(OPENROUTER_API_KEY_ENV, "").strip():
            print(f"未设置 {OPENROUTER_API_KEY_ENV}；未尝试任何模型调用。")
            return 0
        files = vision_exam.upload_files(manifest, (variant,))
        print("\nProvider 与地区调查：")
        print(render_survey_table(survey))
        for selection in selections:
            if selection.skip_reason is not None:
                print(f"\n{selection.label} 将跳过：{selection.skip_reason}")
        vision_exam.print_upload_plan(
            files,
            runnable,
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
            targets=runnable,
            client_factory=make_client_factory(selections, direct),
            cost_cap_usd=arguments.cost_cap,
            enable_relaxed=False,
            repetitions=REPETITIONS,
            streaming=True,
        )
        records = list(outcome.records)
        for selection in selections:
            records.extend(skipped_records(manifest, selection, variant))
        records.sort(
            key=lambda record: (
                GROUP_LABELS.index(record.target_label),
                record.repetition,
                next(
                    index
                    for index, question in enumerate(manifest.questions)
                    if question.question_id == record.question_id
                ),
            )
        )
        ended_at = datetime.now().astimezone()
        output_directory = vision_exam._run_directory(
            vision_exam.DEFAULT_OUTPUT_ROOT, started_at
        )
        verification = provider_lock_verification(records, selections)
        write_geo_outputs(
            output_directory,
            manifest=manifest,
            records=records,
            survey=survey,
            selections=selections,
            direct=direct,
            answer_key_path=arguments.answer_key,
            run_payload={
                "task_id": "M5-T2.11",
                "manifest": str(manifest.path),
                "arguments": _sanitize_arguments(arguments),
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "provider_survey": asdict(survey),
                "group_selections": [
                    {
                        "label": selection.label,
                        "transport": selection.transport,
                        "target": asdict(selection.target),
                        "skip_reason": selection.skip_reason,
                    }
                    for selection in selections
                ],
                "direct_environment": {
                    **asdict(direct),
                    "api_key_value_recorded": False,
                    "base_url_value_recorded": False,
                },
                "variant": asdict(variant)
                | {"name": variant.name, "max_tokens": variant.max_tokens},
                "cost_estimate": asdict(estimate),
                "cost_cap_usd": arguments.cost_cap,
                "actual_cost_usd": outcome.actual_cost_usd,
                "cost_guard_stopped": outcome.cost_guard_stopped,
                "expected_rows": len(GROUP_LABELS)
                * len(manifest.questions)
                * REPETITIONS,
                "actual_rows": len(records),
                "actual_calls": sum(not record.skipped for record in records),
                "provider_lock_verification": verification,
                "uploaded_files": [str(path) for path in files],
            },
        )
        print(f"考卷完成：{output_directory}")
        print(
            f"结果 {len(records)} 行；实际调用 "
            f"{sum(not record.skipped for record in records)} 次；"
            f"跳过 {sum(record.skipped for record in records)} 行。"
        )
        print(f"实际总花费：${outcome.actual_cost_usd:.9f}")
        return 0
    except vision_exam.VisionExamError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
