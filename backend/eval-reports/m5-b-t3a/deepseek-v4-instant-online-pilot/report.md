# M5-B-T3a DeepSeek V4 提示词 V2 小样本报告

本轮只验证修改后的详细游戏 context 提示词，不替换上一轮正式 3 模型 × 15 游戏判卷，也不作选型推荐。

## 模型与样本

- 模型：[DeepSeek V4 Flash 0731 (Instant)](https://openrouter.ai/deepseek/deepseek-v4-flash-0731)（`deepseek/deepseek-v4-flash-0731`）。
- 请求上游：由 OpenRouter 在当前账户数据策略允许的端点中自动选择；实际返回上游逐次记录。
- 样本：Overwatch 2, Don't Starve Together, Slay the Spire 2。
- 每个游戏只跑联网模式。
- 联网模式只使用网关内置 [`openrouter:web_search`](https://openrouter.ai/docs/guides/features/server-tools/web-search)，不接独立搜索 API。
- 固定参数：temperature=0.0，max_tokens=2400，reasoning=none，客户端超时=45 秒；产品延迟目标仍按 ≤10 秒统计。
- 这里的 httpx 超时是连接／读取等 I/O 阶段的超时，不是整次请求的总墙钟截止；联网端点持续传输数据时，实测总耗时可以明显超过 45 秒。

## 提示词调整

- 删除社区术语与 HUD 惯例。
- 将一句话核心玩法扩成完整游戏介绍、详细玩法结构与不剧透的公开背景。
- 默认键位统一为 PC 键盘鼠标，只保留 action / input。
- 输出面向后续每个视觉模型的稳定 context，因此完整性优先于极端短小。

## 耗时与花费

| 模式 | 返回 | 格式合规 | P50 / P90 / 最大（秒） | ≤10 秒 | 花费（USD） |
|---|---:|---:|---:|---:|---:|
| 联网模式 | 3/3 | 0/3 | 49.468 / 64.200 / 67.883 | 0/3 | $0.154116216 |

## 逐次结果

| 游戏 | 模式 | 状态 | 实际模型／上游 | 耗时（秒） | 输入／输出／推理 token | finish_reason | 花费（USD） |
|---|---|---|---|---:|---:|---|---:|
| Overwatch 2 | online | 格式不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 34.416 | 52760/2310/0 | stop | $0.051189392 |
| Don&#x27;t Starve Together | online | 格式不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 49.468 | 48927/2627/0 | stop | $0.051185467 |
| Slay the Spire 2 | online | 格式不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 67.883 | 43789/2344/0 | stop | $0.051741357 |

可归属总花费：`$0.154116216`（3/3 个调用有花费元数据）。

## 与 V4 Pro 同题对照

| 指标 | V4 Pro 0813（默认思考） | V4 Flash 0731 Instant（无思考） |
|---|---:|---:|
| P50（秒） | 140.359 | 49.468 |
| P90（秒） | 186.699 | 64.200 |
| 最大（秒） | 198.284 | 67.883 |
| ≤10 秒 | 0/3 | 0/3 |
| 严格格式合规 | 3/3 | 0/3 |
| 总花费（USD） | $0.264350126 | $0.154116216 |

Instant 的 P50 约为 Pro 的 35.2%（约 2.84 倍快），总花费约低 41.7%；但三个回答都在 JSON 前输出英文过程说明，严格解析全部失败。原文不修复、不补跑。

## 限流统计

| 模式 | 429 | 累计冷却（秒） | 冷却丢弃 |
|---|---:|---:|---:|
| 联网模式 | 0 | 0.000 | 0 |

## 错误与格式

- `overwatch-2` / `online`：不是合法 JSON：Expecting value（line 1, column 1）
- `dont-starve-together` / `online`：不是合法 JSON：Expecting value（line 1, column 1）
- `slay-the-spire-2` / `online`：不是合法 JSON：Expecting value（line 1, column 1）

## 说明

- 这是 3 个游戏的小样本探针，不能替代正式跨类型判卷。
- 答案未由脚本判定事实正确性；完整原文见 answers.md。
- 详细输出与 10 秒延迟目标存在客观张力，本表保留实测，不据此自动调短提示词。

## 运行信息

- 开始：`2026-09-01T02:04:56.639560+00:00`
- 结束：`2026-09-01T02:07:28.447335+00:00`
