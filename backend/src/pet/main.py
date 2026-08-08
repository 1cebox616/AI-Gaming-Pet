"""Local HTTP entry point for the AI Gaming Pet backend."""

from importlib.metadata import version

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

HOST = "127.0.0.1"
PORT = 8737
PACKAGE_NAME = "pet"


class HealthResponse(BaseModel):
    """The backend health payload returned to local clients."""

    status: str
    version: str


app = FastAPI(title="AI Gaming Pet")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:1420",
        "http://localhost:1420",
        "tauri://localhost",
    ],
    allow_methods=["GET"],
    allow_headers=[],
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Return the installed project's health and metadata version."""
    return HealthResponse(status="ok", version=version(PACKAGE_NAME))


def run() -> None:
    """Run the local-only development server."""
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    run()
