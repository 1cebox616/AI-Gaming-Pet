"""M5-T2 tests use only synthetic images and injected clients.

提交进仓库的测试不得依赖未提交的机器本地数据：产品负责人本机的录制数据缺失时，
依赖它的测试只能明确跳过，不能变红。
"""

from __future__ import annotations

import base64
import csv
from io import BytesIO
import json
from pathlib import Path
import re
import tomllib

import httpx
from PIL import Image
import pytest

from pet.core.config import LlmConfig, LlmProfileConfig
from pet.core.llm import LlmError, LlmResult, LlmUsage, OpenRouterClient
from pet.games.generic.eval.vision_exam import (
    DEEP_MAX_TOKENS,
    DEEP_PROMPT_PATH,
    FAST_MAX_TOKENS,
    FAST_PROMPT_PATH,
    ExamVariant,
    ModelPrice,
    ModelTarget,
    VisionExamError,
    build_images,
    build_parser,
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
VALID_RESPONSE = "合成测试画面的亮区发生了变化。"
OWNER_RECORDING_SKIP_REASON = "该测试依赖产品负责人本机的录制数据"


def _require_owner_recordings(manifest_path: Path) -> None:
    with manifest_path.open("rb") as handle:
        payload = tomllib.load(handle)
    base_directory = manifest_path.resolve().parent
    missing = [
        (base_directory / raw_path).resolve()
        for question in payload["questions"]
        for raw_path in question["frames"]
        if not (base_directory / raw_path).resolve().is_file()
    ]
    if missing:
        pytest.skip(f"{OWNER_RECORDING_SKIP_REASON}；缺少 {missing[0]}")


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
        price=ModelPrice(1.0, 2.0),
    )


def _variant(
    *,
    width: int = 1280,
    mode: str = "off",
    output: str = "fast",
    limit: float = 0.25,
) -> ExamVariant:
    return ExamVariant(width, mode, limit, output)  # type: ignore[arg-type]


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


@pytest.mark.parametrize("removed_field", ("prompt" + "_override", "crop" + "s"))
def test_manifest_rejects_removed_or_descriptive_fields(
    tmp_path: Path, removed_field: str
) -> None:
    frame = tmp_path / "frame.ppm"
    frame.write_text("P3\n1 1\n255\n0 0 0\n", encoding="ascii")
    manifest_path = tmp_path / "leaky.toml"
    manifest_path.write_text(
        'version = 1\n[[questions]]\nid = "q1"\ntype = "single"\n'
        'frames = ["frame.ppm"]\nseconds = [0.0]\n'
        f'{removed_field} = "answer"\n',
        encoding="utf-8",
    )

    with pytest.raises(VisionExamError, match="不允许字段"):
        load_manifest(manifest_path)


def test_variant_request_includes_neutral_region_grid_and_sparse_timeline() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    question = manifest.questions[1]
    variant = _variant(mode="always")

    prompt = build_user_prompt(question, variant)
    images = build_images(question, variant)

    assert (
        "画面被划分为 16 行 9 列的网格。与上一采样帧相比，"
        "以下格子发生了变化：r12c7、r12c8。"
    ) in prompt
    assert "这是稀疏采样截图，不是连续视频" in prompt
    assert "第0.0秒：帧1；第0.1至3.0秒未采样；第3.0秒：帧2" in prompt
    assert len(images) == 2
    assert images[0].target_width == 1280
    assert images[-1].label == "全图帧2（相对第3.0秒）"
    assert images[-1].target_width == 1280

    client = _FakeVisionClient()
    run_exam(
        manifest=manifest,
        variants=(variant,),
        targets=(_target(),),
        client_factory=lambda _: client,
    )
    sequence_call = client.calls[1]
    assert "r12c7、r12c8" in str(sequence_call["user_prompt"])
    assert sequence_call["system_prompt"] == FAST_PROMPT_PATH.read_text(
        encoding="utf-8"
    )
    assert sequence_call["max_tokens"] == FAST_MAX_TOKENS
    assert len(sequence_call["images"]) == 2


def test_off_mode_omits_region_grid() -> None:
    question = load_manifest(EXAMPLE_MANIFEST).questions[1]
    variant = _variant(width=640, mode="off")

    assert "region" not in build_user_prompt(question, variant).lower()
    assert "r12c7" not in build_user_prompt(question, variant)
    assert all(image.target_width == 640 for image in build_images(question, variant))


def test_game_context_is_injected_and_absence_keeps_prompt_context_free() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    variant = _variant()

    with_context = build_user_prompt(manifest.questions[0], variant)
    without_context = build_user_prompt(manifest.questions[1], variant)

    assert "已知上下文（由窗口标题与进程名确定）：游戏名为 Synthetic Test Game" in with_context
    assert "已知上下文（由窗口标题与进程名确定）" not in without_context

    client = _FakeVisionClient()
    run_exam(
        manifest=manifest,
        variants=(variant,),
        targets=(_target(),),
        client_factory=lambda _: client,
    )
    assert "Synthetic Test Game" in str(client.calls[0]["user_prompt"])
    assert "已知上下文（由窗口标题与进程名确定）" not in str(
        client.calls[1]["user_prompt"]
    )


def test_model_messages_have_no_question_id_or_semantic_leakage() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    client = _FakeVisionClient()
    run_exam(
        manifest=manifest,
        variants=(_variant(mode="always"),),
        targets=(_target(),),
        client_factory=lambda _: client,
    )
    forbidden_words = ("注意", "远处", "小目标", "对照题", "必须为空")
    expected_system_prompt = FAST_PROMPT_PATH.read_text(encoding="utf-8")

    assert len(client.calls) == len(manifest.questions)
    for question, call in zip(manifest.questions, client.calls):
        user_prompt = str(call["user_prompt"])
        image_labels = "\n".join(image.label for image in call["images"])
        model_content = f"{user_prompt}\n{image_labels}"
        assert question.question_id not in model_content
        assert all(word not in model_content for word in forbidden_words)
        assert call["system_prompt"] == expected_system_prompt


def test_fast_and_deep_modes_use_exact_prompts_and_fixed_token_budgets() -> None:
    assert FAST_PROMPT_PATH.is_file()
    assert DEEP_PROMPT_PATH.is_file()
    assert not FAST_PROMPT_PATH.with_name("observation.md").exists()
    manifest = load_manifest(EXAMPLE_MANIFEST)
    client = _FakeVisionClient()
    variants = (
        _variant(output="fast"),
        _variant(output="deep"),
    )

    run_exam(
        manifest=manifest,
        variants=variants,
        targets=(_target(),),
        client_factory=lambda _: client,
    )

    fast_call, deep_call = client.calls[:2]
    assert fast_call["system_prompt"] == FAST_PROMPT_PATH.read_text(encoding="utf-8")
    assert fast_call["max_tokens"] == FAST_MAX_TOKENS == 60
    assert deep_call["system_prompt"] == DEEP_PROMPT_PATH.read_text(encoding="utf-8")
    assert deep_call["max_tokens"] == DEEP_MAX_TOKENS == 1600
    forbidden_json_fields = (
        "sc" + "ene",
        "notable" + "_events",
        "game" + "_guess",
        "confi" + "dence",
    )
    for call in (fast_call, deep_call):
        message_text = f"{call['system_prompt']}\n{call['user_prompt']}"
        assert all(field not in message_text for field in forbidden_json_fields)


def test_region_modes_apply_sparse_suppression_without_changing_template() -> None:
    question = load_manifest(EXAMPLE_MANIFEST).questions[0]
    neutral_template = (
        "画面被划分为 16 行 9 列的网格。与上一采样帧相比，"
        "以下格子发生了变化：r3c5、r3c6、r4c5。"
    )

    assert neutral_template not in build_user_prompt(question, _variant(mode="off"))
    assert neutral_template not in build_user_prompt(
        question, _variant(mode="sparse", limit=0.01)
    )
    assert neutral_template in build_user_prompt(
        question, _variant(mode="sparse", limit=0.25)
    )
    assert neutral_template in build_user_prompt(
        question, _variant(mode="always", limit=0.0)
    )


def test_switches_build_two_by_three_by_two_cartesian_product() -> None:
    variants = build_variants(
        send_widths=(1280, 0),
        region_modes=("off", "sparse", "always"),
        output_modes=("fast", "deep"),
        region_sparsity_max=0.25,
    )

    assert len(variants) == 12
    assert len({variant.name for variant in variants}) == 12


def test_cli_collects_repeated_width_and_region_mode_switches() -> None:
    arguments = build_parser().parse_args(
        [
            "exam.toml",
            "--send-width",
            "1280",
            "--send-width",
            "0",
            "--region-mode",
            "off",
            "--region-mode",
            "sparse",
            "--region-mode",
            "always",
            "--output-mode",
            "fast",
            "--output-mode",
            "deep",
        ]
    )

    assert arguments.send_widths == [1280, 0]
    assert arguments.region_modes == ["off", "sparse", "always"]
    assert arguments.output_modes == ["fast", "deep"]


def test_native_and_fixed_width_match_actual_message_payload(tmp_path: Path) -> None:
    image_path = tmp_path / "frame.png"
    Image.new("RGB", (20, 10), (10, 20, 30)).save(image_path)
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text(
        'version = 1\n[[questions]]\nid = "size-test"\ntype = "single"\n'
        'frames = ["frame.png"]\nseconds = [0.0]\n',
        encoding="utf-8",
    )
    uploaded_sizes: list[tuple[int, int]] = []
    uploaded_bytes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        data_url = payload["messages"][1]["content"][2]["image_url"]["url"]
        image_bytes = base64.b64decode(data_url.partition(",")[2])
        with Image.open(BytesIO(image_bytes)) as uploaded:
            uploaded_sizes.append(uploaded.size)
        uploaded_bytes.append(len(image_bytes))
        return httpx.Response(
            200,
            json={
                "model": "fake/actual",
                "choices": [{"message": {"content": VALID_RESPONSE}}],
            },
        )

    client = OpenRouterClient("test-api-key", transport=httpx.MockTransport(handler))
    records = run_exam(
        manifest=load_manifest(manifest_path),
        variants=build_variants(
            send_widths=(0, 10, 40),
            region_modes=("off",),
            output_modes=("fast",),
            region_sparsity_max=0.25,
        ),
        targets=(_target(),),
        client_factory=lambda _: client,
    )

    assert uploaded_sizes == [(20, 10), (10, 5), (40, 20)]
    assert [record.image_dimensions for record in records] == [
        ("20x10",),
        ("10x5",),
        ("40x20",),
    ]
    assert [record.image_byte_sizes for record in records] == [
        (uploaded_bytes[0],),
        (uploaded_bytes[1],),
        (uploaded_bytes[2],),
    ]


def test_failure_is_recorded_and_arbitrary_plain_text_continues() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    client = _FakeVisionClient(
        [
            LlmError("timed out", latency_seconds=0.4),
            "not-json",
        ]
    )

    records = run_exam(
        manifest=manifest,
        variants=(_variant(),),
        targets=(_target(),),
        client_factory=lambda _: client,
    )

    assert len(records) == 2
    assert records[0].error == "timed out"
    assert records[0].latency_ms == pytest.approx(400)
    assert records[1].response_text == "not-json"
    assert records[1].error is None
    assert len(client.calls) == 2
    assert client.closed


def test_fake_client_runs_pruned_recommended_combinations_and_writes_outputs(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    client = _FakeVisionClient()
    variants = (
        _variant(output="fast", width=1280, mode="off"),
        _variant(output="fast", width=1280, mode="sparse"),
        _variant(output="deep", width=1280, mode="off"),
        _variant(output="deep", width=0, mode="off"),
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
    assert len(rows) == 8
    assert rows[0]["回答原文"] == VALID_RESPONSE
    assert rows[0]["输出模式"] == "fast"
    assert rows[0]["max_tokens"] == "60"
    assert rows[0]["实际输出token"] == "30"
    assert rows[0]["上传宽度"] == "1280"
    assert rows[0]["区域提示模式"] == "off"
    assert rows[0]["本次是否实际注入了提示"] == "false"
    assert rows[0]["本次实际上传的图像像素尺寸"]
    assert rows[0]["本次实际上传的图像字节数"]
    assert "准确性判定 | 漏了什么 | 编造了什么" in report
    assert "## 题目汇总" in report
    assert "## 变体轴同类对比" in report
    assert "## 上传宽度同类对比" in report
    assert "## 输出模式同类对比" in report
    assert run_payload["summary"]["models"]["fake/model"]["successes"] == 8
    assert run_payload["summary"]["output_modes"]["fast"]["attempts"] == 4
    assert run_payload["summary"]["output_modes"]["deep"]["attempts"] == 4
    assert len(client.calls) == 8


def test_summary_uses_configured_price_not_upstream_cost() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    records = run_exam(
        manifest=manifest,
        variants=(_variant(),),
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
            prices={},
        )


def test_upload_plan_contains_only_full_frames() -> None:
    manifest = load_manifest(EXAMPLE_MANIFEST)
    files = upload_files(manifest, (_variant(),))

    assert files == tuple(
        dict.fromkeys(path for question in manifest.questions for path in question.frames)
    )


def test_confirmation_requires_exact_yes_or_explicit_override() -> None:
    assert confirm_upload(assume_yes=True, input_function=lambda _: "")
    assert confirm_upload(assume_yes=False, input_function=lambda _: "YES")
    assert not confirm_upload(assume_yes=False, input_function=lambda _: "yes")


def test_real_exam_answer_key_has_every_required_section_and_sourced_point() -> None:
    text = ANSWER_KEY.read_text(encoding="utf-8")
    question_sections = re.split(r"(?=^## \d+\. `)", text, flags=re.MULTILINE)[1:]
    with REAL_MANIFEST.open("rb") as handle:
        manifest_ids = [item["id"] for item in tomllib.load(handle)["questions"]]
    answer_ids = [section.split("`", 2)[1] for section in question_sections]
    required_headings = (
        "### 机械区域",
        "### 现场记录",
        "### 产品负责人判定",
        "### 离线复核",
        "### 参考答案要点",
        "### 不得出现的内容",
        "### 不确定项",
    )

    assert len(question_sections) == 11
    assert answer_ids == manifest_ids
    for section in question_sections:
        question_id = section.split("`", 2)[1]
        assert all(heading in section for heading in required_headings), question_id
        assert "> 【现场记录】" in section, question_id
        owner_content = section.split("### 产品负责人判定", 1)[1].split(
            "### 离线复核", 1
        )[0]
        owner_points = [line for line in owner_content.splitlines() if line.startswith("- ")]
        assert owner_points, question_id
        assert all(
            line.startswith(("- 【核心】", "- 【细节】", "- 【存疑】"))
            for line in owner_points
        ), question_id
        sourced_content = section.split("### 离线复核", 1)[1]
        for line in sourced_content.splitlines():
            if line.startswith("- "):
                assert line.startswith(
                    (
                        "- 【现场记录】",
                        "- 【离线复核】",
                        "- 【离线复核→不确定】",
                        "- 【产品负责人判定内部差异】",
                        "- 【产品负责人判定与机械时间差】",
                    )
                ), (
                    question_id,
                    line,
                )


def test_real_exam_answer_key_fractions_match_manifest_grids() -> None:
    _require_owner_recordings(REAL_MANIFEST)
    manifest = load_manifest(REAL_MANIFEST)
    text = ANSWER_KEY.read_text(encoding="utf-8")
    sections = {
        section.split("`", 2)[1]: section
        for section in re.split(r"(?=^## \d+\. `)", text, flags=re.MULTILINE)[1:]
    }

    for question in manifest.questions:
        section = sections[question.question_id]
        if question.region_grid is None:
            assert "变化格子占比：未能计算" in section
            continue
        expected = (
            f"{len(question.region_grid)} / 144 = "
            f"{len(question.region_grid) / 144:.9f}"
        )
        assert expected in section


def test_missing_owner_recordings_mark_dependent_test_as_skipped(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "manifest.toml"
    manifest_path.write_text(
        'version = 1\n[[questions]]\nid = "missing-local-frame"\n'
        'type = "single"\nframes = ["missing.png"]\nseconds = [0.0]\n',
        encoding="utf-8",
    )

    with pytest.raises(pytest.skip.Exception, match=OWNER_RECORDING_SKIP_REASON):
        _require_owner_recordings(manifest_path)


def test_real_exam_field_notes_are_verbatim_after_markdown_reflow() -> None:
    answer_text = ANSWER_KEY.read_text(encoding="utf-8")
    field_text = FIELD_RESULTS.read_text(encoding="utf-8")

    normalized_field_text = re.sub(r"\s+", "", field_text)
    quoted_notes = re.findall(r"^> 【现场记录】(.+)$", answer_text, flags=re.MULTILINE)

    assert len(quoted_notes) == 11
    for note in quoted_notes:
        assert re.sub(r"\s+", "", note) in normalized_field_text, note


def test_real_exam_replaces_static_controls_with_single_and_real_time_sequence() -> None:
    with REAL_MANIFEST.open("rb") as handle:
        questions = {item["id"]: item for item in tomllib.load(handle)["questions"]}

    assert "gzw-static-control-a" not in questions
    assert "gzw-static-control-b" not in questions
    assert "spire-combat-ui-nocontext" not in questions
    assert "subnautica-night-underwater-nocontext" not in questions
    assert questions["gzw-static-single"]["type"] == "single"
    sequence = questions["gzw-static-sequence"]
    assert sequence["type"] == "sequence"
    assert sequence["frames"] == [
        "../../../recordings/capture/20260823-135202/frame-000001-20260823T175206.287712Z.png",
        "../../../recordings/capture/20260823-135202/frame-000031-20260823T175306.283718Z.png",
    ]
    assert sequence["seconds"] == [0.0, 59.996006]


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
    }
    allowed_contexts = {
        "Grey Zone Warfare",
        "Disco Elysium",
        "Slay the Spire 2",
        "Subnautica 2",
    }

    assert len(questions) == 11
    for question in questions:
        assert set(question) <= allowed_fields
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
