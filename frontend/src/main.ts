import { listen } from "@tauri-apps/api/event";

import { setPetDimmed, setPetExpression } from "./pet";

const BACKEND_HOST = "127.0.0.1";
const BACKEND_PORT = 8737;
const HEALTH_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}/health`;
const REQUEST_TIMEOUT_MS = 3_000;
const PET_NEXT_EXPRESSION_EVENT = "pet-next-expression";

interface HealthResponse {
  status: "ok";
  version: string;
}

function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const response = value as Partial<HealthResponse>;
  return response.status === "ok" && typeof response.version === "string" && response.version.length > 0;
}

async function isBackendAvailable(): Promise<boolean> {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

    try {
      const response = await fetch(HEALTH_URL, { signal: controller.signal });
      if (!response.ok) {
        return false;
      }

      const payload: unknown = await response.json();
    return isHealthResponse(payload);
  } catch {
    return false;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function updatePetForBackendStatus(): Promise<void> {
  if (await isBackendAvailable()) {
    setPetExpression("neutral");
    setPetDimmed(false);
    return;
  }

  setPetExpression("speechless");
  setPetDimmed(true);
}

void listen(PET_NEXT_EXPRESSION_EVENT, () => {
  setPetExpression();
}).catch((error: unknown) => {
  console.error("failed to listen for pet expression changes", error);
});

void updatePetForBackendStatus();
