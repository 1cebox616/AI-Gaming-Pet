# M5-B-T3a 游戏知识线模型选型结论

## 结论

当前选择 `google/gemini-3.1-flash-lite` 作为游戏知识线候选模型。

这是基于结构合同、延迟、通路可用性和实测费用的机械选型，不预填游戏知识正确性判断。产品负责人仍应阅读两名 Gemini 决赛候选的原始答案；若人工判卷显示 Gemini 3.7 Flash 的事实质量有显著优势，再考虑用约 5.75 倍本轮实测费用和更高延迟换取更长输出。

## 候选与入围理由

| 候选 | 入围理由 | 官方页面 |
|---|---|---|
| `google/gemini-3.1-flash-lite` | 低延迟、低价格档；官方声明支持工具调用与结构化输出。 | [OpenRouter](https://openrouter.ai/google/gemini-3.1-flash-lite) |
| `openai/gpt-5.4-mini` | 中档能力与成本；用于验证 OpenAI 系列在同一参数组合下的可用性。 | [OpenRouter](https://openrouter.ai/openai/gpt-5.4-mini) |
| `google/gemini-3.7-flash` | 较新的高能力 Flash 档；输出更长，用于检验质量与延迟的交换。 | [OpenRouter](https://openrouter.ai/google/gemini-3.7-flash) |

DeepSeek V4 Instant 不再作为本轮候选：此前两批共 10 题原样合同合规为 0/10；加入网关 `response-healing` 后仍只有 1/3，且 P50 为 31.102 秒。

## 同条件实测

共同参数：同一份详细游戏 context 提示词、联网模式、Exa 全请求最多 5 条结果、temperature=0、reasoning=minimal、max_tokens=8000、`response_format=json_schema`、`require_parameters=true`、吞吐优先路由、网关 `response-healing`。原样合同合规指客户端收到的 `response_text` 无需本地修复即可通过完整字段与 PC 键位合同。

| 模型 | 尝试 | 返回 | 原样合同合规 | 可机械解析 | P50 / P90 / 最大（秒） | ≤10 秒 | 总花费 | 平均输出 token |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Gemini 3.1 Flash Lite | 8 | 8/8 | 7/8 | 7/8 | 4.123 / 5.076 / 5.986 | 8/8 | $0.020908500 | 859.8 |
| GPT-5.4 Mini | 3 | 0/3 | 0/3 | 0/3 | — | 0/3 | $0 | — |
| Gemini 3.7 Flash | 8 | 8/8 | 6/8 | 7/8 | 7.949 / 10.725 / 13.261 | 7/8 | $0.120261250 | 1379.0 |

GPT-5.4 Mini 的 3 次请求均被 OpenRouter 以 HTTP 404 拒绝：没有端点能同时处理当前要求的联网工具、结构化输出、推理参数与插件组合。按探针规则没有改参数补跑。额外用 `openai/gpt-5.4-nano` 复核同系列通路，3/3 得到相同 404，不作为第四候选计分。

## 失败明细

- Gemini 3.1 Flash Lite：守望先锋把规范值 `LeftShift` 写成歧义值 `Shift`，因此 1/8 未通过合同。
- Gemini 3.7 Flash：守望先锋用反引号字符代替 `Backquote`；原神输出 `NumLock`，而本轮固定合同尚未包含该输入名。两题均不计原样合规。
- GPT-5.4 Mini：3/3 在模型调用前被网关拒绝，无答案、无模型费用。

## 选择理由

Gemini 3.1 Flash Lite 是唯一在全部 8 次成功返回中都达到 10 秒目标的候选，并且原样合同合规率最高、总花费最低。相比 Gemini 3.7 Flash，它的 P50 约快 48%，本轮费用约为后者的 17.4%。其输出更短，但仍由 JSON Schema 强制包含完整介绍、玩法循环、4 至 10 个主要系统、公开背景和 PC 默认键位对象。

本结论不声称 7/8 等于格式“有保证”。生产实现仍须在本地严格校验并失败关闭；任何未通过合同的响应不得写入游戏 context。不得用本地语义猜测把 `Shift` 自动改成左或右 Shift。

## 人工判卷入口

- [Gemini 3.1 Flash Lite：高压 3 题原始答案](../selection-gemini-3.1-flash-lite/answers.md)
- [Gemini 3.1 Flash Lite：决赛 5 题原始答案](../finalist-gemini-3.1-flash-lite-5games/answers.md)
- [Gemini 3.7 Flash：高压 3 题原始答案](../selection-gemini-3.7-flash/answers.md)
- [Gemini 3.7 Flash：决赛 5 题原始答案](../finalist-gemini-3.7-flash-5games/answers.md)
- [GPT-5.4 Mini：通路失败报告](../selection-gpt-5.4-mini/report.md)

不预填任何事实对错判断。

## 限流、偏差与未完成项

- 所有 Gemini 调用的 429、累计冷却与冷却丢弃均为 0。
- 本轮采用先 3 题硬门、再让两名决赛候选各跑 5 题的分阶段设计；没有重新跑完整 15 游戏，因为 OpenAI 候选已在硬门阶段无端点，DeepSeek 已被既有数据淘汰。
- 结构、延迟和花费已经实测；游戏知识事实正确性尚待产品负责人人工判卷。
- 零生产改动：未改 `config.toml`，未接管线，未写游戏卡。
