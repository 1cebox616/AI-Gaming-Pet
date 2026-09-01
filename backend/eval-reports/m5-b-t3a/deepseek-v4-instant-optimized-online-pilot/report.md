# M5-B-T3a DeepSeek V4 Instant 优化提示词小样本报告

本轮只验证修改后的详细游戏 context 提示词，不替换上一轮正式 3 模型 × 15 游戏判卷，也不作选型推荐。

## 模型与样本

- 模型：[DeepSeek V4 Flash 0731 (Instant, optimized)](https://openrouter.ai/deepseek/deepseek-v4-flash-0731)（`deepseek/deepseek-v4-flash-0731`）。
- 请求上游：由 OpenRouter 在当前账户数据策略允许的端点中自动选择；实际返回上游逐次记录。
- 样本：Overwatch 2, Tom Clancy's Rainbow Six Siege, Genshin Impact, Kingdom Come: Deliverance II, Black Myth: Wukong。
- 每个游戏只跑联网模式。
- 联网模式只使用网关内置 [`openrouter:web_search`](https://openrouter.ai/docs/guides/features/server-tools/web-search)，不接独立搜索 API。
- 固定参数：temperature=0.0，max_tokens=2400，reasoning=none，客户端超时=45 秒；产品延迟目标仍按 ≤10 秒统计。
- 搜索：Exa；每次最多 5 条、全请求累计最多 5 条；不限制每条结果字符数。
- 路由：合规上游中按吞吐量优先；要求上游支持请求参数。
- 输出：请求 OpenRouter JSON Schema 严格结构化输出；是否被实际上游执行按原始响应另行记录。
- 这里的 httpx 超时是连接／读取等 I/O 阶段的超时，不是整次请求的总墙钟截止；联网端点持续传输数据时，实测总耗时可以明显超过 45 秒。

## 提示词调整

- 删除社区术语与 HUD 惯例。
- 将一句话核心玩法扩成完整游戏介绍、详细玩法结构与不剧透的公开背景。
- 默认键位统一为可机械解析的“动作名称 → 单一规范化 PC 输入”对象；移动方向与快捷栏逐键展开。
- 输出面向后续每个视觉模型的稳定 context，因此完整性优先于极端短小。

## 耗时与花费

| 模式 | 返回 | 可机械解析 | 原样格式合规 | P50 / P90 / 最大（秒） | ≤10 秒 | 花费（USD） |
|---|---:|---:|---:|---:|---:|---:|
| 联网模式 | 5/5 | 2/5 | 0/5 | 18.081 / 21.445 / 23.085 | 0/5 | $0.049170850 |

## 逐次结果

| 游戏 | 模式 | 状态 | 实际模型／上游 | 耗时（秒） | 输入／输出／推理 token | finish_reason | 花费（USD） |
|---|---|---|---|---:|---:|---|---:|
| Overwatch 2 | online | 格式不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 23.085 | 9841/1855/0 | stop | $0.009431950 |
| Tom Clancy&#x27;s Rainbow Six Siege | online | 格式不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 18.081 | 10333/2664/0 | length | $0.009732600 |
| Genshin Impact | online | 可解析／包络不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 18.985 | 24585/2092/0 | stop | $0.010942400 |
| Kingdom Come: Deliverance II | online | 可解析／包络不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 15.899 | 15499/2252/0 | stop | $0.010470800 |
| Black Myth: Wukong | online | 格式不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 13.755 | 11074/1814/0 | stop | $0.008593100 |

可归属总花费：`$0.049170850`（5/5 个调用有花费元数据）。

## 限流统计

| 模式 | 429 | 累计冷却（秒） | 冷却丢弃 |
|---|---:|---:|---:|
| 联网模式 | 0 | 0.000 | 0 |

## 错误与格式

- `overwatch-2` / `online`：不是合法 JSON：Expecting value（line 1, column 1）
- `rainbow-six-siege` / `online`：不是合法 JSON：Expecting value（line 1, column 1）
- `genshin-impact` / `online`：原始响应含 JSON 外文本；已机械提取唯一完整 JSON 对象
- `kingdom-come-deliverance-2` / `online`：原始响应含 JSON 外文本；已机械提取唯一完整 JSON 对象
- `black-myth-wukong` / `online`：不是合法 JSON：Expecting value（line 1, column 1）

## 说明

- 这是 5 个游戏的小样本探针，不能替代正式跨类型判卷。
- 答案未由脚本判定事实正确性；完整原文见 answers.md。
- parsed-contexts.json 只剥离模型额外输出的文本／代码围栏并验证字段与键位形状，不修改 JSON 内容；原始包络不合仍计入格式错误。
- 详细输出与 10 秒延迟目标存在客观张力，本表保留实测，不据此自动调短提示词。

## 运行信息

- 开始：`2026-09-01T02:50:18.078924+00:00`
- 结束：`2026-09-01T02:51:47.936524+00:00`
