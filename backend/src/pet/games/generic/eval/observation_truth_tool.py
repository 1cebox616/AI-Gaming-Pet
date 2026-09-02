"""Loopback-only annotation server for W-T0 frame truth."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
import sys

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
import uvicorn

from pet.games.generic.eval.observation_truth import (
    TRUTH_SCHEMA_VERSION,
    TruthError,
    TruthFile,
    load_manifest,
    manifest_sha256,
    validate_truth_against_manifest,
    write_truth_atomic,
)

HTML_PATH = Path(__file__).with_name("observation_truth_tool.html")


def display_point_to_normalized(
    x: float,
    y: float,
    *,
    left: float,
    top: float,
    width: float,
    height: float,
) -> tuple[float, float]:
    """Map a displayed-image point to resolution-independent coordinates."""
    if width <= 0 or height <= 0:
        raise ValueError("display dimensions must be positive")
    return (
        min(1.0, max(0.0, (x - left) / width)),
        min(1.0, max(0.0, (y - top) / height)),
    )


def create_app(manifest_path: Path, output_path: Path) -> FastAPI:
    manifest_path = manifest_path.resolve()
    output_path = output_path.resolve()
    manifest = load_manifest(manifest_path)
    digest = manifest_sha256(manifest_path)
    frames = {frame.frame_id: frame for frame in manifest.frames}
    base = manifest_path.parent
    app = FastAPI(title="W-T0 truth tool", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        try:
            return HTML_PATH.read_text(encoding="utf-8")
        except OSError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @app.get("/api/manifest")
    def get_manifest() -> dict[str, object]:
        return manifest.model_dump(mode="json")

    @app.get("/api/truth")
    def get_truth() -> dict[str, object]:
        if not output_path.is_file():
            return TruthFile(
                schema_version=TRUTH_SCHEMA_VERSION,
                manifest_sha256=digest,
                created_at=datetime.now(timezone.utc),
                frames=[],
            ).model_dump(mode="json")
        try:
            truth = TruthFile.model_validate_json(output_path.read_text(encoding="utf-8"))
            validate_truth_against_manifest(truth, manifest, manifest_path)
        except (OSError, ValueError, TruthError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return truth.model_dump(mode="json")

    @app.post("/api/truth")
    def save_truth(truth: TruthFile) -> dict[str, object]:
        try:
            validate_truth_against_manifest(truth, manifest, manifest_path)
            write_truth_atomic(output_path, truth)
        except (OSError, TruthError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return {"saved": True, "frames": len(truth.frames)}

    @app.get("/api/image/{frame_id}/{variant}", response_class=FileResponse)
    def image(frame_id: str, variant: str) -> FileResponse:
        frame = frames.get(frame_id)
        if frame is None or variant not in {"full", "upload"}:
            raise HTTPException(status_code=404, detail="image not found")
        relative = frame.full.path if variant == "full" else frame.upload.path
        path = (base / relative).resolve()
        try:
            path.relative_to(base)
        except ValueError as error:
            raise HTTPException(status_code=403, detail="image path leaves manifest directory") from error
        if not path.is_file():
            raise HTTPException(status_code=404, detail="image file is missing")
        return FileResponse(path, media_type="image/jpeg")

    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Open the local W-T0 frame annotation tool.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8738)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if not 1 <= arguments.port <= 65535:
        print("--port must be between 1 and 65535", file=sys.stderr)
        return 2
    try:
        app = create_app(arguments.manifest, arguments.out)
    except TruthError as error:
        print(f"truth tool failed: {error}", file=sys.stderr)
        return 1
    uvicorn.run(app, host="127.0.0.1", port=arguments.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
