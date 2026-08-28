import { invoke } from "@tauri-apps/api/core";

import { isPetExpression, setPetDimmed, setPetExpression } from "./pet";
import { showSpeech } from "./bubble";
import { setWatchStatus } from "./watch-status";
import {
  formatLlmCost,
  formatLlmMode,
  parseLlmState,
  type LlmState,
} from "./llm-status";

const BACKEND_HOST = "127.0.0.1";
const BACKEND_PORT = 8737;
const BACKEND_WEBSOCKET_URL = `ws://${BACKEND_HOST}:${BACKEND_PORT}/ws`;
const INITIAL_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_DELAY_MS = 10_000;
const RECONNECT_BACKOFF_MULTIPLIER = 2;

interface UtteranceMessage {
  type: "utterance";
  id: string;
  text: string;
  emotion: string;
}

interface StateMessage {
  type: "state";
  speech_enabled: boolean;
  muted: boolean;
  game: GameStatus;
  llm?: LlmState;
}

interface GameStatus {
  game_id: string;
  state: string;
  summary: Record<string, string | number | null>;
}

let connection: WebSocket | undefined;
let reconnectTimer: number | undefined;
let reconnectDelayMs = INITIAL_RECONNECT_DELAY_MS;
let bridgeStarted = false;
let lastUtteranceId: string | undefined;

type BackendMenuState = readonly [
  connected: boolean,
  speechEnabled: boolean,
  muted: boolean,
  gameId: string,
  gameStatus: string,
  llmMode: string,
  llmCost: string,
];

function setPetDisconnected(): void {
  setPetExpression("speechless");
  setPetDimmed(true);
  setWatchStatus(null);
  updatePetMenuState([false, false, false, "", "后端：未连接", "—", "—"]);
}

function updatePetMenuState(state: BackendMenuState): void {
  void invoke("update_pet_menu_state", { state }).catch((error: unknown) => {
    console.error("failed to synchronize pet menu state", error);
  });
}

function isUtteranceMessage(value: unknown): value is UtteranceMessage {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const message = value as Partial<UtteranceMessage>;
  return (
    message.type === "utterance" &&
    typeof message.id === "string" &&
    message.id.length > 0 &&
    typeof message.text === "string" &&
    message.text.length > 0 &&
    typeof message.emotion === "string"
  );
}

function isStateMessage(value: unknown): value is StateMessage {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const message = value as Partial<StateMessage>;
  return (
    message.type === "state" &&
    typeof message.speech_enabled === "boolean" &&
    typeof message.muted === "boolean" &&
    isGameStatus(message.game) &&
    (message.llm === undefined || parseLlmState(message.llm) !== undefined)
  );
}

function isGameStatus(value: unknown): value is GameStatus {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const game = value as Partial<GameStatus>;
  return (
    typeof game.game_id === "string" &&
    game.game_id.length > 0 &&
    typeof game.state === "string" &&
    game.state.length > 0 &&
    isGameSummary(game.summary)
  );
}

function isGameSummary(
  value: unknown,
): value is Record<string, string | number | null> {
  return (
    typeof value === "object" &&
    value !== null &&
    Object.values(value).every(
      (entry) =>
        entry === null ||
        typeof entry === "string" ||
        typeof entry === "number",
    )
  );
}

function formatGameStatus(game: GameStatus): string {
  if (game.game_id === "generic") {
    const watchedGame = summaryString(game, "game");
    const cost = summaryString(game, "session_cost_usd") ?? "0.000000";
    if (game.state === "disabled") {
      return "通用视觉：已关闭";
    }
    if (game.state === "no_window") {
      return `通用视觉：未找到窗口 · 本会话 $${cost}`;
    }
    const degraded =
      summaryString(game, "degraded") === "yes" ? " · 调用降级" : "";
    return `正在观看：${watchedGame ?? "未知窗口"} · 本会话 $${cost}${degraded}`;
  }
  const displayName = game.game_id === "cs2" ? "CS2" : game.game_id;
  if (game.state === "offline") {
    return `${displayName}：未运行`;
  }
  if (game.state === "menu") {
    return `${displayName}：主菜单`;
  }

  const mode = formatGameMode(summaryString(game, "mode"));
  if (game.state === "warmup") {
    return formatGameParts(displayName, mode, "热身");
  }
  if (game.state === "spectating") {
    return formatGameParts(displayName, mode, "观战中");
  }
  if (game.state === "round_over") {
    return formatGameParts(displayName, mode, "回合结束");
  }
  if (game.state === "match_over") {
    return formatGameParts(displayName, mode, "比赛结束");
  }

  const parts = mode.length > 0 ? [mode] : [];
  const round = summaryNumber(game, "round");
  if (round !== null) {
    parts.push(`第 ${round} 回合`);
  }
  const scoreCt = summaryNumber(game, "score_ct");
  const scoreT = summaryNumber(game, "score_t");
  if (scoreCt !== null && scoreT !== null) {
    parts.push(`${scoreCt}:${scoreT}`);
  }
  if (parts.length === 0) {
    parts.push("游戏中");
  }
  return `${displayName}：${parts.join(" · ")}`;
}

function formatGameMode(mode: string | null): string {
  if (mode === "casual") {
    return "休闲";
  }
  return mode ?? "";
}

function formatGameParts(
  displayName: string,
  mode: string,
  state: string,
): string {
  return `${displayName}：${mode.length > 0 ? `${mode} · ` : ""}${state}`;
}

function summaryString(game: GameStatus, key: string): string | null {
  const value = game.summary[key];
  return typeof value === "string" ? value : null;
}

function summaryNumber(game: GameStatus, key: string): number | null {
  const value = game.summary[key];
  return typeof value === "number" ? value : null;
}

function scheduleReconnect(): void {
  if (!bridgeStarted || reconnectTimer !== undefined) {
    return;
  }

  const delayMs = reconnectDelayMs;
  reconnectDelayMs = Math.min(
    reconnectDelayMs * RECONNECT_BACKOFF_MULTIPLIER,
    MAX_RECONNECT_DELAY_MS,
  );
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = undefined;
    connect();
  }, delayMs);
}

function handleMessage(event: MessageEvent<unknown>): void {
  if (typeof event.data !== "string") {
    console.warn("ignoring non-text pet WebSocket message");
    return;
  }

  let message: unknown;

  try {
    message = JSON.parse(event.data);
  } catch {
    console.warn("ignoring invalid JSON from pet WebSocket server");
    return;
  }

  if (isStateMessage(message)) {
    setWatchStatus(
      message.game.game_id === "generic" && message.game.state === "watching"
        ? summaryString(message.game, "game")
        : null,
    );
    updatePetMenuState([
      true,
      message.speech_enabled,
      message.muted,
      message.game.game_id,
      formatGameStatus(message.game),
      formatLlmMode(message.llm),
      formatLlmCost(message.llm),
    ]);
    return;
  }

  if (!isUtteranceMessage(message)) {
    console.warn("ignoring unknown pet WebSocket message");
    return;
  }

  if (message.id === lastUtteranceId) {
    return;
  }

  lastUtteranceId = message.id;
  if (!isPetExpression(message.emotion)) {
    console.warn(
      "received an invalid pet emotion; falling back to neutral",
      message.emotion,
    );
    setPetExpression("neutral");
  } else {
    setPetExpression(message.emotion);
  }
  showSpeech(message.text);
}

function connect(): void {
  if (!bridgeStarted) {
    return;
  }

  const nextConnection = new WebSocket(BACKEND_WEBSOCKET_URL);
  connection = nextConnection;

  nextConnection.addEventListener("open", () => {
    if (connection !== nextConnection) {
      return;
    }

    reconnectDelayMs = INITIAL_RECONNECT_DELAY_MS;
    lastUtteranceId = undefined;
    setPetExpression("neutral");
    setPetDimmed(false);
  });

  nextConnection.addEventListener("message", handleMessage);

  nextConnection.addEventListener("error", () => {
    nextConnection.close();
  });

  nextConnection.addEventListener("close", () => {
    if (connection !== nextConnection) {
      return;
    }

    connection = undefined;
    setPetDisconnected();
    scheduleReconnect();
  });
}

export function startBackendBridge(): void {
  if (bridgeStarted) {
    return;
  }

  bridgeStarted = true;
  setPetDisconnected();
  connect();
}

export function requestIdleLine(): void {
  if (connection?.readyState !== WebSocket.OPEN) {
    return;
  }

  connection.send(JSON.stringify({ type: "request_idle_line" }));
}

export function setSpeechEnabled(value: boolean): void {
  sendRuntimeSwitch("set_speech_enabled", value);
}

export function setMuted(value: boolean): void {
  sendRuntimeSwitch("set_muted", value);
}

function sendRuntimeSwitch(type: string, value: boolean): void {
  if (connection?.readyState !== WebSocket.OPEN) {
    return;
  }

  connection.send(JSON.stringify({ type, value }));
}
