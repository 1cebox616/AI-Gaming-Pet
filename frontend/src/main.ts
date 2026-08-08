const BACKEND_HOST = "127.0.0.1";
const BACKEND_PORT = 8737;
const HEALTH_URL = `http://${BACKEND_HOST}:${BACKEND_PORT}/health`;
const REQUEST_TIMEOUT_MS = 3_000;

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

async function requestBackendStatus(): Promise<string> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const response = await fetch(HEALTH_URL, { signal: controller.signal });
    if (!response.ok) {
      return "连接失败";
    }

    const payload: unknown = await response.json();
    return isHealthResponse(payload) ? `ok（版本 ${payload.version}）` : "连接失败";
  } catch {
    return "连接失败";
  } finally {
    window.clearTimeout(timeout);
  }
}

async function renderBackendStatus(): Promise<void> {
  const app = document.querySelector<HTMLDivElement>("#app");
  if (app === null) {
    return;
  }

  app.textContent = `后端状态：${await requestBackendStatus()}`;
}

void renderBackendStatus();
