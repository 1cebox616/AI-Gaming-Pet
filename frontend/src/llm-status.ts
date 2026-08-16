export interface LlmState {
  mode: "ai" | "template";
  reason: string;
  consecutive_failures: number;
  call_count: number;
  cost_usd: number | null;
}

export function parseLlmState(value: unknown): LlmState | undefined {
  if (typeof value !== "object" || value === null) {
    return undefined;
  }

  const state = value as Partial<LlmState>;
  if (
    (state.mode !== "ai" && state.mode !== "template") ||
    typeof state.reason !== "string" ||
    typeof state.consecutive_failures !== "number" ||
    typeof state.call_count !== "number" ||
    (typeof state.cost_usd !== "number" && state.cost_usd !== null)
  ) {
    return undefined;
  }
  return state as LlmState;
}

export function formatLlmMode(state: LlmState | undefined): string {
  if (state?.mode === "ai") {
    return "当前：AI 模式";
  }

  switch (state?.reason) {
    case "型号未配置":
    case "环境变量缺失":
      return "当前：模板模式（未配置）";
    case "连续失败":
      return "当前：模板模式（连续失败）";
    default:
      return "当前：模板模式（未启用）";
  }
}

export function formatLlmCost(state: LlmState | undefined): string {
  if (state?.cost_usd === undefined) {
    return "本次花费：未提供";
  }
  if (state.cost_usd === null) {
    return "本次花费：未提供";
  }
  return `本次花费：$${state.cost_usd.toFixed(4)}`;
}
