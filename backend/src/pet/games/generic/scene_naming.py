"""Typed scene-naming proposal parsing and deterministic acceptance law."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict

from pet.core.llm import LlmImage
from pet.core.prompt import PROMPTS_DIRECTORY
from pet.core.scene_fingerprint import SceneCluster
from pet.games.generic.deep_read import DeepReadRequest

SCENE_NAMING_PROMPT_PATH = PROMPTS_DIRECTORY / "generic" / "scene-naming.md"
_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")


class SceneNamingProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matches_existing: bool | None
    label: str
    annotation: str
    modality: Literal["observed", "inferred", "uncertain"]


@dataclass(frozen=True, slots=True)
class SceneNamingFrame:
    root_capture_id: str
    image: Path | Image.Image


@dataclass(frozen=True, slots=True)
class ExistingSceneNaming:
    scene_id: str
    label: str
    annotation: str
    label_status: Literal["named", "uncertain"]


@dataclass(frozen=True, slots=True)
class SceneNamingDecision:
    accepted: bool
    label: str
    annotation: str
    modality: Literal["observed", "inferred", "uncertain"]
    matches_existing: bool | None
    validation_error: str | None


def build_scene_naming_request(
    *,
    game_name: str,
    session_cluster_id: int,
    frames: Sequence[SceneNamingFrame],
    stable_ocr_lines: Sequence[str],
    send_width: int,
    existing_scene: ExistingSceneNaming | None = None,
) -> DeepReadRequest:
    if not frames:
        raise ValueError("scene verification needs at least one representative frame")
    root_ids = ", ".join(frame.root_capture_id for frame in frames)
    ocr_text = (
        "；".join(line.strip() for line in stable_ocr_lines if line.strip())
        or "无可用稳定 OCR 文字"
    )
    existing_text = (
        "游戏卡中没有已命名的候选场景；matches_existing 必须为 null。"
        if existing_scene is None
        else (
            "游戏卡中可能匹配的已命名场景：\n"
            f"- scene_id：{existing_scene.scene_id}\n"
            f"- 当前短名：{existing_scene.label}\n"
            f"- 当前注释：{existing_scene.annotation}\n"
            f"- 当前状态：{existing_scene.label_status}\n"
            "请判断本次画面是否就是这个场景，matches_existing 必须为 true 或 false。"
        )
    )
    user_prompt = (
        f"游戏名（由窗口标题确定）：{game_name}\n"
        f"会话视觉簇：session:c{session_cluster_id}\n"
        f"所看帧：{root_ids}\n"
        f"稳定 OCR 文字：{ocr_text}\n"
        f"{existing_text}\n"
        "请只根据这些画面、文字与给出的当前命名完成场景确认或命名。"
    )
    images = tuple(
        LlmImage(
            frame.image,
            f"代表帧 {index + 1}（{frame.root_capture_id}）",
            target_width=send_width,
            encoding="jpeg",
        )
        for index, frame in enumerate(frames)
    )
    return DeepReadRequest(
        system_prompt=SCENE_NAMING_PROMPT_PATH.read_text(encoding="utf-8").strip(),
        user_prompt=user_prompt,
        images=images,
    )


def parse_scene_naming_proposal(text: str) -> SceneNamingProposal:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            value = "\n".join(lines[1:-1]).strip()
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end < start:
        raise ValueError("scene verification response contains no JSON object")
    try:
        payload = json.loads(value[start : end + 1])
    except json.JSONDecodeError as error:
        raise ValueError(f"scene verification response is invalid JSON: {error}") from error
    return SceneNamingProposal.model_validate(payload)


def validate_scene_naming_proposal(
    cluster: SceneCluster,
    proposal: SceneNamingProposal,
    existing_scene: ExistingSceneNaming | None = None,
) -> SceneNamingDecision:
    problems: list[str] = []
    if not cluster.stable:
        problems.append("referenced cluster has not passed the stable gate")
    if existing_scene is None and proposal.matches_existing is not None:
        problems.append("matches_existing must be null without a named card candidate")
    if existing_scene is not None and proposal.matches_existing is None:
        problems.append("matches_existing must be boolean for a named card candidate")
    reuse_existing = existing_scene is not None and proposal.matches_existing is True
    label = " ".join(
        (existing_scene.label if reuse_existing else proposal.label).split()
    )
    annotation = " ".join(
        (existing_scene.annotation if reuse_existing else proposal.annotation).split()
    )
    modality = (
        "uncertain"
        if reuse_existing and existing_scene.label_status == "uncertain"
        else proposal.modality
    )
    if not label:
        problems.append("label is empty")
    elif len(label) > 24:
        problems.append("label exceeds 24 characters")
    elif _CJK_PATTERN.search(label) is None:
        problems.append("label contains no Chinese character")
    if not annotation:
        problems.append("annotation is empty")
    elif len(annotation) > 160:
        problems.append("annotation exceeds 160 characters")
    return SceneNamingDecision(
        accepted=not problems,
        label=label,
        annotation=annotation,
        modality=modality,
        matches_existing=proposal.matches_existing,
        validation_error="; ".join(problems) if problems else None,
    )
