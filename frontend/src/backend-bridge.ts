import { invoke } from "@tauri-apps/api/core";

import { isPetExpression, setPetDimmed, setPetExpression } from "./pet";
import { showSpeech } from "./bubble";
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
  game: GameState;
  llm?: LlmState;
}

type GameSessionState =
  | "offline"
  | "menu"
  | "warmup"
  | "playing"
  | "spectating"
  | "round_over"
  | "match_over";

interface GameState {
  state: GameSessionState;
  mode: string | null;
  map: string | null;
  round: number | null;
  score_ct: number | null;
  score_t: number | null;
  subject_steamid: string | null;
  subject_is_self: boolean | null;
}

let connection: WebSocket | undefined;
let reconnectTimer: number | undefined;
let reconnectDelayMs = INITIAL_RECONNECT_DELAY_MS;
let bridgeStarted = false;
let lastUtteranceId: string | undefined;

function setPetDisconnected(): void {
  setPetExpression("speechless");
  setPetDimmed(true);
  updatePetMenuState(false, false, false, "CS2：未知（后端未连接）", "—", "—");
}

function updatePetMenuState(
  connected: boolean,
  speechEnabled: boolean,
  muted: boolean,
  gameStatus: string,
  llmMode: string,
  llmCost: string,
): void {
  void invoke("update_pet_menu_state", {
    connected,
    speechEnabled,
    muted,
    gameStatus,
    llmMode,
    llmCost,
  }).catch((error: unknown) => {
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
    isGameState(message.game) &&
    (message.llm === undefined || parseLlmState(message.llm) !== undefined)
  );
}

function isGameState(value: unknown): value is GameState {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const game = value as Partial<GameState>;
  return (
    isGameSessionState(game.state) &&
    isNullableString(game.mode) &&
    isNullableString(game.map) &&
    isNullableNumber(game.round) &&
    isNullableNumber(game.score_ct) &&
    isNullableNumber(game.score_t) &&
    isNullableString(game.subject_steamid) &&
    (typeof game.subject_is_self === "boolean" || game.subject_is_self === null)
  );
}

function isGameSessionState(value: unknown): value is GameSessionState {
  return (
    value === "offline" ||
    value === "menu" ||
    value === "warmup" ||
    value === "playing" ||
    value === "spectating" ||
    value === "round_over" ||
    value === "match_over"
  );
}

function isNullableString(value: unknown): value is string | null {
  return typeof value === "string" || value === null;
}

function isNullableNumber(value: unknown): value is number | null {
  return typeof value === "number" || value === null;
}

function formatGameStatus(game: GameState): string {
  if (game.state === "offline") {
    return "CS2：未运行";
  }
  if (game.state === "menu") {
    return "CS2：主菜单";
  }

  const mode = formatGameMode(game.mode);
  if (game.state === "warmup") {
    return formatGameParts(mode, "热身");
  }
  if (game.state === "spectating") {
    return formatGameParts(mode, "观战中");
  }
  if (game.state === "round_over") {
    return formatGameParts(mode, "回合结束");
  }
  if (game.state === "match_over") {
    return formatGameParts(mode, "比赛结束");
  }

  const parts = mode.length > 0 ? [mode] : [];
  if (game.round !== null) {
    parts.push(`第 ${game.round} 回合`);
  }
  if (game.score_ct !== null && game.score_t !== null) {
    parts.push(`${game.score_ct}:${game.score_t}`);
  }
  if (parts.length === 0) {
    parts.push("游戏中");
  }
  return `CS2：${parts.join(" · ")}`;
}

function formatGameMode(mode: string | null): string {
  if (mode === "casual") {
    return "休闲";
  }
  return mode ?? "";
}

function formatGameParts(mode: string, state: string): string {
  return `CS2：${mode.length > 0 ? `${mode} · ` : ""}${state}`;
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
    updatePetMenuState(
      true,
      message.speech_enabled,
      message.muted,
      formatGameStatus(message.game),
      formatLlmMode(message.llm),
      formatLlmCost(message.llm),
    );
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
