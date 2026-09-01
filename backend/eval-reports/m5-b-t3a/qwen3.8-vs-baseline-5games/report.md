# M5-B-T3a Qwen3.8 Flash 与基线五游戏对照

## 结论

本轮仅按通路、结构合同、延迟、花费和限流作机械比较，不判定游戏知识事实对错。

`google/gemini-3.1-flash-lite` 完成 5/5，全部可机械解析且全部在 10 秒内；`qwen/qwen3.8-flash` 没有产生可判答案：首题在 86.438 秒后因长度上限截断，第二题收到 429，后三题依既定冷却规则在派发前丢弃且未补跑。基于本轮机械门，Qwen3.8 Flash 不能替代当前基线。

## 固定条件

- 游戏：Cyberpunk 2077、Counter-Strike 2、Stardew Valley、Hearthstone、Terraria。
- 仅联网模式；OpenRouter 内置 Exa 搜索，每次最多 5 条、全请求累计最多 5 条，不限制单条字符数。
- 两个模型使用逐字相同的 system prompt 与用户消息模板。
- temperature=0、reasoning=minimal、max_tokens=8000、`response_format=json_schema`、`require_parameters=true`、吞吐优先路由、`response-healing`。
- 每个 `模型 × 游戏` 只允许一次到达网关的请求；失败、限流或冷却丢弃不补跑。

## 汇总

| 模型 | 尝试 | 返回正文 | 可机械解析 | 原样合同合规 | P50 / P90 / 最大（秒） | ≤10 秒成功 | 花费（USD） |
|---|---:|---:|---:|---:|---:|---:|---:|
| Gemini 3.1 Flash Lite | 5 | 5/5 | 5/5 | 4/5 | 4.031 / 4.831 / 5.133 | 5/5 | $0.008559500 |
| Qwen3.8 Flash | 5 | 0/5 | 0/5 | 0/5 | 46.244 / 78.399 / 86.438 | 0/5 | $0.011743586 |

Qwen 的耗时分位数只基于两次实际到达网关且有墙钟耗时的请求；三次冷却前丢弃没有耗时值。首题费用已经高于基线全部五题之和。

## 逐题结果

| 游戏 | Gemini 状态 / 耗时 | Qwen 状态 / 耗时 |
|---|---|---|
| Cyberpunk 2077 | 可解析、原样合规 / 5.133 秒 | length 截断、空答 / 86.438 秒 |
| Counter-Strike 2 | 可解析、原样合规 / 3.821 秒 | HTTP 429 / 6.049 秒 |
| Stardew Valley | 可解析、本地确定性规范化标点键名 / 4.031 秒 | 冷却中派发前丢弃 |
| Hearthstone | 可解析、原样合规 / 3.905 秒 | 冷却中派发前丢弃 |
| Terraria | 可解析、原样合规 / 4.378 秒 | 冷却中派发前丢弃 |

Gemini 的 Stardew Valley 原答使用 `-` 与 `=` 表示快捷栏 11、12；本地只按既有无歧义规则转换为 `Minus` 与 `Equals`，所以计为可解析但不计原样合同合规。

## 限流

| 模型 | 429 | 累计冷却（秒） | 冷却丢弃 |
|---|---:|---:|---:|
| Gemini 3.1 Flash Lite | 0 | 0.000 | 0 |
| Qwen3.8 Flash | 1 | 1.000 | 3 |

## 产物

- Gemini：[报告](../qwen-baseline-gemini-3.1-flash-lite-5games/report.md)、[原始答案](../qwen-baseline-gemini-3.1-flash-lite-5games/answers.md)、[规范化 context](../qwen-baseline-gemini-3.1-flash-lite-5games/parsed-contexts.json)、[逐次原始记录](../qwen-baseline-gemini-3.1-flash-lite-5games/results.json)、[提示词与参数](../qwen-baseline-gemini-3.1-flash-lite-5games/prompt-v3.md)。
- Qwen：[报告](../qwen-baseline-qwen3.8-flash-5games/report.md)、[原始答案](../qwen-baseline-qwen3.8-flash-5games/answers.md)、[逐次原始记录](../qwen-baseline-qwen3.8-flash-5games/results.json)、[提示词与参数](../qwen-baseline-qwen3.8-flash-5games/prompt-v3.md)。
- 人工事实判卷入口：[judging.md](judging.md)。

## 说明与偏差

- 产品负责人仍需人工判断 Gemini 五份答案的事实对错；本报告未预填任何事实判断。
- Qwen 没有可判正文，所以判卷表只保留空白评分位并标明机械失败状态。
- 首次未提权运行被 Windows 网络沙箱在本机 socket 层拦截，五次均未到达 OpenRouter、没有请求 ID、token 或费用；该本地预检失败不计为模型调用，真实运行已覆盖同名目录。
- 零生产改动：未改 `config.toml`，未接管线，未写游戏卡。
