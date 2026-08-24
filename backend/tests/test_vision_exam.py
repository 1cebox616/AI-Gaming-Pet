"""M5-T2 tests use only synthetic images and injected clients."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from pet.core.config import LlmConfig, LlmProfileConfig
from pet.core.llm import LlmError, LlmResult, LlmUsage
from pet.games.generic.eval.vision_exam import (
    ExamVariant,
    ModelPrice,
    ModelTarget,
    VisionExamError,
    build_images,
    build_timeline,
    build_user_prompt,
    build_variants,
    confirm_upload,
    load_manifest,
    parse_prices,
    resolve_targets,
    run_exam,
    summarize,
    upload_files,
    write_outputs,
)

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_MANIFEST = FIXTURES / "vision-exam-example.toml"
VALID_RESPONSE = json.dumps(
    {
        "scene": "合成测试画面",
        "notable_events": ["亮区发生变化"],
        "game_guess": "不确定",
        "confidence": 0.2,
    },
    ensure_ascii=False,
)


class _FakeVisionClient:
    def __init__(self, outcomes: list[object] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self._outcomes = outcomes or []
        self.closed = False

    def complete_with_images(self, **arguments: object) -> LlmResult:
        self.calls.append(arguments)
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            assert isinstance(outcome, str)
            text = outcome
        else:
            text = VALID_RESPONSE
        return LlmResult(
            text=text,
            usage=LlmUsage(120, 30, 0.009),
            latency_seconds=0.125,
            model="fake/actual",
            provider="fake-provider",
        )

    def close(self) -> None:
        self.closed = True


def _target() -> ModelTarget:
    return ModelTarget(
        label="fake/model",
        model="fake/model",
        provider=None,
        temperature=0.0,
        timeout_seconds=5.0,
        max_tokens=128,
        price=ModelPrice(1.0, 2.0),
    )


def test_manifest_parses_single_sequence_and_resolves_synthetic_files() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)

    assert [question.question_id for question in manifest.questions] == [
        "synthetic-single",
        "synthetic-sequence",
    ]
    assert manifest.questions[0].question_type == "single"
    assert manifest.questions[1].relative_seconds == (0.0, 3.0)
    assert all(path.is_file() for path in manifest.questions[1].frames)


def test_manifest_rejects_missing_frame(tmp_path: Path) -> None:
    manifest_path = tmp_path / "missing.toml"
    manifest_path.write_text(
        'version = 1\n[[questions]]\nid = "q1"\ntype = "single"\n'
        'frames = ["missing.png"]\nseconds = [0.0]\n',
        encoding="utf-8",
    )

    with pytest.raises(VisionExamError, match="缺少文件"):
        load_manifest(manifest_path)


def test_manifest_rejects_out_of_order_seconds(tmp_path: Path) -> None:
    frame = tmp_path / "frame.ppm"
    frame.write_text("P3\n1 1\n255\n0 0 0\n", encoding="ascii")
    manifest_path = tmp_path / "unordered.toml"
    manifest_path.write_text(
        'version = 1\n[[questions]]\nid = "q1"\ntype = "sequence"\n'
        'frames = ["frame.ppm", "frame.ppm"]\nseconds = [2.0, 1.0]\n',
        encoding="utf-8",
    )

    with pytest.raises(VisionExamError, match="严格递增"):
        load_manifest(manifest_path)


def test_variant_request_includes_region_hint_crops_and_sparse_timeline() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    question = manifest.questions[1]
    variant = ExamVariant(with_region_hint=True, with_crops=True, send_width=1280)

    prompt = build_user_prompt(question, variant)
    images = build_images(question, variant)

    assert "第二帧右下区域出现高亮变化" in prompt
    assert "这是稀疏采样截图，不是连续视频" in prompt
    assert "第0.0秒：帧1；第0.1至3.0秒未采样；第3.0秒：帧2" in prompt
    assert len(images) == 3
    assert images[0].target_width == 1280
    assert images[-1].label == "原生裁剪图1"
    assert images[-1].max_edge is None
    assert images[-1].target_width is None

    client = _FakeVisionClient()
    run_exam(
        manifest=manifest,
        variants=(variant,),
        targets=(_target(),),
        system_prompt="system",
        client_factory=lambda _: client,
    )
    sequence_call = client.calls[1]
    assert question.region_hint in str(sequence_call["user_prompt"])
    assert len(sequence_call["images"]) == 3


def test_without_axes_omits_hint_and_crops() -> None:
    question = load_manifest(EXAMPLE_MANIFEST).questions[1]
    variant = ExamVariant(with_region_hint=False, with_crops=False, send_width=640)

    assert question.region_hint not in build_user_prompt(question, variant)
    assert len(build_images(question, variant)) == 2


def test_switches_can_request_full_cartesian_product() -> None:
    variants = build_variants(
        send_width=1280,
        region_choices=(True, False),
        crop_choices=(True, False),
    )

    assert len(variants) == 4
    assert len({variant.name for variant in variants}) == 4


def test_failure_and_invalid_json_are_recorded_while_exam_continues() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    client = _FakeVisionClient(
        [
            LlmError("timed out", latency_seconds=0.4),
            "not-json",
        ]
    )

    records = run_exam(
        manifest=manifest,
        variants=(ExamVariant(False, False, 1280),),
        targets=(_target(),),
        system_prompt="system",
        client_factory=lambda _: client,
    )

    assert len(records) == 2
    assert records[0].error == "timed out"
    assert records[0].latency_ms == pytest.approx(400)
    assert records[1].response_text == "not-json"
    assert records[1].error is not None and "非法 JSON" in records[1].error
    assert len(client.calls) == 2
    assert client.closed


def test_fake_client_full_flow_writes_csv_report_and_run_json(tmp_path: Path) -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    client = _FakeVisionClient()
    variants = (
        ExamVariant(False, False, 1280),
        ExamVariant(True, True, 1280),
    )
    records = run_exam(
        manifest=manifest,
        variants=variants,
        targets=(_target(),),
        system_prompt="system",
        client_factory=lambda _: client,
    )
    output = tmp_path / "vision-exam-test"

    write_outputs(
        output_directory=output,
        records=records,
        run_payload={"arguments": {"yes": True}},
    )

    with (output / "results.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    report = (output / "report.md").read_text(encoding="utf-8")
    run_payload = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert len(rows) == 4
    assert rows[0]["回答原文"] == VALID_RESPONSE
    assert "准确性判定 | 漏了什么 | 编造了什么" in report
    assert "## 题目汇总" in report
    assert "## 变体轴同类对比" in report
    assert run_payload["summary"]["models"]["fake/model"]["successes"] == 4
    assert len(client.calls) == 4


def test_summary_uses_configured_price_not_upstream_cost() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    records = run_exam(
        manifest=manifest,
        variants=(ExamVariant(False, False, 1280),),
        targets=(_target(),),
        system_prompt="system",
        client_factory=lambda _: _FakeVisionClient(),
    )

    row = summarize(records)["models"]["fake/model"]
    assert row["average_tokens_per_attempt"] == 150
    assert row["average_configured_cost_usd_per_attempt"] == pytest.approx(0.00018)


def test_profile_resolution_and_price_mapping() -> None:
    config = LlmConfig(
        model="fallback/model",
        provider="fallback-provider",
        temperature=0.4,
        timeout_seconds=4.0,
        max_tokens=200,
        profiles={
            "vision": LlmProfileConfig(
                model="profile/model",
                provider="profile-provider",
                temperature=0.1,
                timeout_seconds=8.0,
                max_tokens=300,
            )
        },
    )
    prices = parse_prices(("profile:vision=0.5,1.5",))

    target = resolve_targets(
        ("profile:vision",),
        llm_config=config,
        provider=None,
        temperature=0.0,
        timeout_seconds=30.0,
        max_tokens=512,
        prices=prices,
    )[0]

    assert target.model == "profile/model"
    assert target.provider == "profile-provider"
    assert target.price == ModelPrice(0.5, 1.5)


def test_target_resolution_refuses_to_guess_missing_price() -> None:
    with pytest.raises(VisionExamError, match="缺少 --price"):
        resolve_targets(
            ("fake/model",),
            llm_config=LlmConfig(),
            provider=None,
            temperature=0.0,
            timeout_seconds=30.0,
            max_tokens=512,
            prices={},
        )


def test_upload_plan_only_includes_crops_when_selected() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    without = upload_files(manifest, (ExamVariant(False, False, 1280),))
    with_crops = upload_files(manifest, (ExamVariant(False, True, 1280),))

    assert all("crop" not in path.name for path in without)
    assert any("crop" in path.name for path in with_crops)


def test_confirmation_requires_exact_yes_or_explicit_override() -> None:
    assert confirm_upload(assume_yes=True, input_function=lambda _: "")
    assert confirm_upload(assume_yes=False, input_function=lambda _: "YES")
    assert not confirm_upload(assume_yes=False, input_function=lambda _: "yes")
