"""M5-T2 tests use only synthetic images and injected clients."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import re
import tomllib

import pytest

from pet.core.config import LlmConfig, LlmProfileConfig
from pet.core.llm import LlmError, LlmResult, LlmUsage
from pet.games.generic.eval.vision_exam import (
    DEFAULT_PROMPT_PATH,
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
ANSWER_KEY = Path(__file__).parents[1] / "data" / "generic" / "vision-exam" / "answer-key.md"
FIELD_RESULTS = Path(__file__).parents[1] / "audit" / "m5-t1-field-test-results.md"
REAL_MANIFEST = ANSWER_KEY.with_name("manifest.toml")
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
    assert manifest.questions[0].game_context == "Synthetic Test Game"
    assert manifest.questions[1].game_context is None
    assert manifest.questions[1].relative_seconds == (0.0, 3.0)
    assert manifest.questions[1].region_grid == ("r12c7", "r12c8")
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


def test_manifest_rejects_removed_or_descriptive_fields(tmp_path: Path) -> None:
    frame = tmp_path / "frame.ppm"
    frame.write_text("P3\n1 1\n255\n0 0 0\n", encoding="ascii")
    manifest_path = tmp_path / "leaky.toml"
    removed_field = "prompt" + "_override"
    manifest_path.write_text(
        'version = 1\n[[questions]]\nid = "q1"\ntype = "single"\n'
        'frames = ["frame.ppm"]\nseconds = [0.0]\n'
        f'{removed_field} = "answer"\n',
        encoding="utf-8",
    )

    with pytest.raises(VisionExamError, match="不允许字段"):
        load_manifest(manifest_path)


def test_variant_request_includes_neutral_region_grid_crops_and_sparse_timeline() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    question = manifest.questions[1]
    variant = ExamVariant(with_region_grid=True, with_crops=True, send_width=1280)

    prompt = build_user_prompt(question, variant)
    images = build_images(question, variant)

    assert (
        "画面被划分为 16 行 9 列的网格。与上一采样帧相比，"
        "以下格子发生了变化：r12c7、r12c8。"
    ) in prompt
    assert "这是稀疏采样截图，不是连续视频" in prompt
    assert "第0.0秒：帧1；第0.1至3.0秒未采样；第3.0秒：帧2" in prompt
    assert len(images) == 3
    assert images[0].target_width == 1280
    assert images[-1].label == "机器裁剪图1"
    assert images[-1].max_edge is None
    assert images[-1].target_width is None

    client = _FakeVisionClient()
    run_exam(
        manifest=manifest,
        variants=(variant,),
        targets=(_target(),),
        client_factory=lambda _: client,
    )
    sequence_call = client.calls[1]
    assert "r12c7、r12c8" in str(sequence_call["user_prompt"])
    assert sequence_call["system_prompt"] == DEFAULT_PROMPT_PATH.read_text(
        encoding="utf-8"
    )
    assert len(sequence_call["images"]) == 3


def test_without_axes_omits_region_grid_and_crops() -> None:
    question = load_manifest(EXAMPLE_MANIFEST).questions[1]
    variant = ExamVariant(with_region_grid=False, with_crops=False, send_width=640)

    assert "region" not in build_user_prompt(question, variant).lower()
    assert "r12c7" not in build_user_prompt(question, variant)
    assert len(build_images(question, variant)) == 2


def test_game_context_is_injected_and_absence_keeps_prompt_context_free() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    variant = ExamVariant(False, False, 1280)

    with_context = build_user_prompt(manifest.questions[0], variant)
    without_context = build_user_prompt(manifest.questions[1], variant)

    assert "游戏上下文（由窗口标题与进程名确定）：Synthetic Test Game" in with_context
    assert "请在 game_guess 中填写这个名称" in with_context
    assert "游戏上下文（由窗口标题与进程名确定）" not in without_context

    client = _FakeVisionClient()
    run_exam(
        manifest=manifest,
        variants=(variant,),
        targets=(_target(),),
        client_factory=lambda _: client,
    )
    assert "Synthetic Test Game" in str(client.calls[0]["user_prompt"])
    assert "游戏上下文（由窗口标题与进程名确定）" not in str(
        client.calls[1]["user_prompt"]
    )


def test_model_messages_have_no_question_id_or_semantic_leakage() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    client = _FakeVisionClient()
    run_exam(
        manifest=manifest,
        variants=(ExamVariant(True, True, 1280),),
        targets=(_target(),),
        client_factory=lambda _: client,
    )
    forbidden_words = ("注意", "远处", "小目标", "对照题", "必须为空")
    expected_system_prompt = DEFAULT_PROMPT_PATH.read_text(encoding="utf-8")

    assert len(client.calls) == len(manifest.questions)
    for question, call in zip(manifest.questions, client.calls):
        user_prompt = str(call["user_prompt"])
        image_labels = "\n".join(image.label for image in call["images"])
        model_content = f"{user_prompt}\n{image_labels}"
        assert question.question_id not in model_content
        assert all(word not in model_content for word in forbidden_words)
        assert call["system_prompt"] == expected_system_prompt


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


def test_real_exam_answer_key_has_every_required_section_and_sourced_point() -> None:
    text = ANSWER_KEY.read_text(encoding="utf-8")
    question_sections = re.split(r"(?=^## \d+\. `)", text, flags=re.MULTILINE)[1:]
    required_headings = (
        "### 机械区域与裁剪",
        "### 现场记录",
        "### 离线复核",
        "### 参考答案要点",
        "### 不得出现的内容",
        "### 不确定项",
    )

    assert len(question_sections) == 13
    for section in question_sections:
        question_id = section.split("`", 2)[1]
        assert all(heading in section for heading in required_headings), question_id
        sourced_content = section.split("### 现场记录", 1)[1]
        assert "> 【现场记录】" in sourced_content, question_id
        for line in sourced_content.splitlines():
            if line.startswith("- "):
                assert line.startswith(("- 【现场记录】", "- 【离线复核】")), (
                    question_id,
                    line,
                )


def test_real_exam_field_notes_are_verbatim_after_markdown_reflow() -> None:
    answer_text = ANSWER_KEY.read_text(encoding="utf-8")
    field_text = FIELD_RESULTS.read_text(encoding="utf-8")

    normalized_field_text = re.sub(r"\s+", "", field_text)
    quoted_notes = re.findall(r"^> 【现场记录】(.+)$", answer_text, flags=re.MULTILINE)

    assert len(quoted_notes) == 13
    for note in quoted_notes:
        assert re.sub(r"\s+", "", note) in normalized_field_text, note


def test_real_exam_nocontext_pairs_duplicate_frames_without_context() -> None:
    with REAL_MANIFEST.open("rb") as handle:
        questions = {item["id"]: item for item in tomllib.load(handle)["questions"]}

    pairs = (
        ("spire-combat-ui", "spire-combat-ui-nocontext"),
        ("subnautica-night-underwater", "subnautica-night-underwater-nocontext"),
    )
    for contextual_id, no_context_id in pairs:
        contextual = questions[contextual_id]
        no_context = questions[no_context_id]
        assert contextual["frames"] == no_context["frames"]
        assert "game_context" in contextual
        assert "game_context" not in no_context


def test_real_exam_uses_only_allowed_fields_and_plain_game_names() -> None:
    with REAL_MANIFEST.open("rb") as handle:
        questions = tomllib.load(handle)["questions"]
    allowed_fields = {
        "id",
        "type",
        "game_context",
        "frames",
        "seconds",
        "region_grid",
        "crops",
    }
    allowed_contexts = {
        "Grey Zone Warfare",
        "Disco Elysium",
        "Slay the Spire 2",
        "Subnautica 2",
    }

    assert len(questions) == 13
    for question in questions:
        assert set(question) <= allowed_fields
        if "game_context" in question:
            assert question["game_context"] in allowed_contexts


def test_real_exam_text_artifacts_contain_no_player_identity_markers() -> None:
    text = "\n".join(
        (
            REAL_MANIFEST.read_text(encoding="utf-8"),
            ANSWER_KEY.read_text(encoding="utf-8"),
        )
    )
    identity_patterns = (
        r"765611\d{10}",
        r"STEAM_\d",
        r"steamid",
        r"好友列表",
        r"昵称[:：]",
    )

    for pattern in identity_patterns:
        assert re.search(pattern, text, flags=re.IGNORECASE) is None, pattern
