import { listen } from "@tauri-apps/api/event";

import { setPetExpression } from "./pet";
import {
  requestIdleLine,
  setMuted,
  setSpeechEnabled,
  startBackendBridge,
} from "./backend-bridge";

const PET_NEXT_EXPRESSION_EVENT = "pet-next-expression";
const SPEAK_NEXT_IDLE_LINE_EVENT = "speak-next-idle-line";
const SET_SPEECH_ENABLED_EVENT = "set-speech-enabled";
const SET_MUTED_EVENT = "set-muted";

void listen(PET_NEXT_EXPRESSION_EVENT, () => {
  setPetExpression();
}).catch((error: unknown) => {
  console.error("failed to listen for pet expression changes", error);
});

void listen(SPEAK_NEXT_IDLE_LINE_EVENT, () => {
  requestIdleLine();
}).catch((error: unknown) => {
  console.error("failed to listen for speech requests", error);
});

void listen<boolean>(SET_SPEECH_ENABLED_EVENT, (event) => {
  if (typeof event.payload === "boolean") {
    setSpeechEnabled(event.payload);
  }
}).catch((error: unknown) => {
  console.error("failed to listen for speech switch requests", error);
});

void listen<boolean>(SET_MUTED_EVENT, (event) => {
  if (typeof event.payload === "boolean") {
    setMuted(event.payload);
  }
}).catch((error: unknown) => {
  console.error("failed to listen for automatic speech switch requests", error);
});

startBackendBridge();
