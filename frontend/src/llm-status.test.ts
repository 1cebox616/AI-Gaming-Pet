import { formatLlmCost, formatLlmMode, parseLlmState } from "./llm-status.ts";

function assert(condition: boolean, message: string): void {
  if (!condition) {
    throw new Error(message);
  }
}

const ai = parseLlmState({
  mode: "ai",
  reason: "",
  consecutive_failures: 0,
  call_count: 3,
  cost_usd: 0.0123,
});
assert(ai !== undefined, "valid llm state should parse");
assert(formatLlmMode(ai) === "当前：AI 模式", "AI mode should be visible");
assert(
  formatLlmCost(ai) === "本次花费：$0.0123",
  "reported cost should be formatted",
);
assert(
  parseLlmState(undefined) === undefined,
  "missing llm field should be accepted",
);
assert(
  formatLlmMode(parseLlmState(undefined)) === "当前：模板模式（未启用）",
  "old backends should fall back to the disabled template label",
);
assert(
  formatLlmMode(
    parseLlmState({
      mode: "template",
      reason: "连续失败",
      consecutive_failures: 3,
      call_count: 3,
      cost_usd: null,
    }),
  ) === "当前：模板模式（连续失败）",
  "continuous failure should remain distinguishable",
);
