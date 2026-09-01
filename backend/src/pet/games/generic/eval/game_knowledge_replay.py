"""Run the production game-knowledge line against existing replay game cards."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from pet.core.config import load_config, resolve_llm_profile
from pet.core.gamecard import GameCardRepository
from pet.core.llm import LlmDispatchStats, OpenRouterClient
from pet.games.generic.adapter import ObservationLog, WindowTitleMap
from pet.games.generic.game_knowledge import (
    GAME_KNOWLEDGE_MODE,
    GameKnowledgeReader,
)

BACKEND_DIRECTORY = Path(__file__).resolve().parents[5]


@dataclass(frozen=True, slots=True)
class ReplayAttempt:
    game_id: str
    display_name: str
    query_name: str
    outcome: str
    write_action: str
    request_id: str
    latency_seconds: float
    cost_usd: float
    model: str
    actual_model: str | None
    provider: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    failure_reason: str | None
    normalization_actions: tuple[str, ...]


async def run_replay(
    output_directory: Path,
    *,
    memory_root: Path,
) -> tuple[tuple[ReplayAttempt, ...], LlmDispatchStats]:
    configuration = load_config(strict=True)
    settings = configuration.games["generic"].generic
    knowledge = settings.knowledge
    effective = resolve_llm_profile(configuration.llm, knowledge.llm_profile)
    client = OpenRouterClient.from_profile(
        profile_name=knowledge.llm_profile,
        base_url=effective.base_url,
        api_key_env=effective.api_key_env,
        timeout_seconds=effective.timeout_seconds,
    )
    reader = GameKnowledgeReader(
        client,
        effective,
        wall_timeout_seconds=knowledge.wall_timeout_seconds,
    )
    repository = GameCardRepository(memory_root)
    title_map = WindowTitleMap.load()
    log = ObservationLog(
        output_directory,
        {
            "kind": "m5-b-t3b-game-knowledge-replay",
            "profile": knowledge.llm_profile,
            "wall_timeout_seconds": knowledge.wall_timeout_seconds,
        },
        exact_directory=True,
    )
    attempts: list[ReplayAttempt] = []
    try:
        paths = sorted(memory_root.glob("*/gamecard.json"))
        if not paths:
            raise RuntimeError(f"no game cards found under {memory_root}")
        for path in paths:
            card = repository.load(path.parent.name)
            identity = title_map.identify_identity(card.display_name, card.game_id)
            trigger = time.perf_counter()
            result = await reader.read(identity.context_name)
            card, write_action = repository.record_knowledge_attempt(
                card,
                checked_at=datetime.now(timezone.utc),
                model=result.model,
                mode=GAME_KNOWLEDGE_MODE,
                request_id=result.request_id,
                outcome=result.outcome,
                failure_reason=result.failure_reason,
                content=result.content,
            )
            log.append_game_knowledge(
                trigger_monotonic=trigger,
                learned_monotonic=time.perf_counter(),
                result=result,
                write_action=write_action,
                game_id=card.game_id,
            )
            log.update_dispatch_statistics((reader.dispatch_stats(),))
            attempts.append(
                ReplayAttempt(
                    game_id=card.game_id,
                    display_name=card.display_name,
                    query_name=identity.context_name,
                    outcome=result.outcome,
                    write_action=write_action,
                    request_id=result.request_id,
                    latency_seconds=result.latency_ms / 1000.0,
                    cost_usd=result.cost_usd,
                    model=result.model,
                    actual_model=result.actual_model,
                    provider=result.provider,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    failure_reason=result.failure_reason,
                    normalization_actions=result.normalization_actions,
                )
            )
            print(
                f"{card.game_id}: {result.outcome} / {write_action} / "
                f"{result.latency_ms / 1000.0:.3f}s / ${result.cost_usd:.9f}",
                flush=True,
            )
        stats = reader.dispatch_stats()
        (output_directory / "results.json").write_text(
            json.dumps(
                {
                    "attempts": [asdict(item) for item in attempts],
                    "dispatch_stats": asdict(stats),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    finally:
        log.update_dispatch_statistics((reader.dispatch_stats(),))
        log.close()
        reader.close()
    return tuple(attempts), stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--memory-root",
        type=Path,
        default=BACKEND_DIRECTORY / "memory",
    )
    arguments = parser.parse_args()
    asyncio.run(
        run_replay(
            arguments.output.resolve(),
            memory_root=arguments.memory_root.resolve(),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
