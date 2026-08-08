import { listen } from "@tauri-apps/api/event";

import { setPetExpression } from "./pet";
import { requestIdleLine, startBackendBridge } from "./backend-bridge";

const PET_NEXT_EXPRESSION_EVENT = "pet-next-expression";
const SPEAK_NEXT_IDLE_LINE_EVENT = "speak-next-idle-line";

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

startBackendBridge();
