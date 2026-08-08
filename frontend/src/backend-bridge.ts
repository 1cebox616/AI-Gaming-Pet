import { isPetExpression, setPetDimmed, setPetExpression } from "./pet";
import { showSpeech } from "./bubble";

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

let connection: WebSocket | undefined;
let reconnectTimer: number | undefined;
let reconnectDelayMs = INITIAL_RECONNECT_DELAY_MS;
let bridgeStarted = false;
let lastUtteranceId: string | undefined;

function setPetDisconnected(): void {
  setPetExpression("speechless");
  setPetDimmed(true);
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
