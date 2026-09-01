# M5-B-T3a Google Gemini 3.7 Flash finalist 游戏知识探针报告

本轮只验证修改后的详细游戏 context 提示词，不替换上一轮正式 3 模型 × 15 游戏判卷，也不作选型推荐。

## 模型与样本

- 模型：[Google Gemini 3.7 Flash finalist](https://openrouter.ai/google/gemini-3.7-flash)（`google/gemini-3.7-flash`）。
- 请求上游：由 OpenRouter 在当前账户数据策略允许的端点中自动选择；实际返回上游逐次记录。
- 样本：Overwatch 2, Tom Clancy's Rainbow Six Siege, Genshin Impact, Kingdom Come: Deliverance II, Black Myth: Wukong。
- 每个游戏只跑联网模式。
- 联网模式只使用网关内置 [`openrouter:web_search`](https://openrouter.ai/docs/guides/features/server-tools/web-search)，不接独立搜索 API。
- 固定参数：temperature=0.0，max_tokens=8000，reasoning=minimal，客户端超时=45 秒；产品延迟目标仍按 ≤10 秒统计。
- 搜索：Exa；每次最多 5 条、全请求累计最多 5 条；不限制每条结果字符数。
- 路由：合规上游中按吞吐量优先；要求上游支持请求参数。
- 输出：请求 OpenRouter JSON Schema 严格结构化输出；是否被实际上游执行按原始响应另行记录。
- 网关响应修复：启用 OpenRouter `response-healing`；验收仍以客户端收到的 response_text 能否直接通过严格合同为准，本地规范化另列。
- 这里的 httpx 超时是连接／读取等 I/O 阶段的超时，不是整次请求的总墙钟截止；联网端点持续传输数据时，实测总耗时可以明显超过 45 秒。

### 样本理由

| 游戏 | 类型 | 新旧／热度 | 入选理由 |
|---|---|---|---|
| Overwatch 2 | 英雄射击 | 长线运营 | 录像游戏；团队英雄射击与大量英雄专属键位。 |
| Tom Clancy&#x27;s Rainbow Six Siege | 战术射击 | 长线运营 | 检验战术射击、破坏系统与姿态键位。 |
| Genshin Impact | 开放世界动作 RPG | 热门长线运营 | 检验动作 RPG、队伍切换与快捷菜单键位。 |
| Kingdom Come: Deliverance II | 开放世界角色扮演 | 2025 新作 | 检验复杂第一人称 RPG 与上下文动作键位。 |
| Black Myth: Wukong | 动作角色扮演 | 热门近年作品 | 检验第三人称动作游戏与战斗键位。 |

## 提示词调整

- 删除社区术语与 HUD 惯例。
- 将一句话核心玩法扩成完整游戏介绍、详细玩法结构与不剧透的公开背景。
- 默认键位统一为可机械解析的“动作名称 → 单一规范化 PC 输入”对象；移动方向与快捷栏逐键展开。
- 输出面向后续每个视觉模型的稳定 context，因此完整性优先于极端短小。

## 耗时与花费

| 模式 | 返回 | 可机械解析 | 原样格式合规 | P50 / P90 / 最大（秒） | ≤10 秒 | 花费（USD） |
|---|---:|---:|---:|---:|---:|---:|
| 联网模式 | 5/5 | 4/5 | 3/5 | 8.862 / 11.812 / 13.261 | 4/5 | $0.074853250 |

## 逐次结果

| 游戏 | 模式 | 状态 | 实际模型／上游 | 耗时（秒） | 输入／输出／推理 token | finish_reason | 花费（USD） |
|---|---|---|---|---:|---:|---|---:|
| Overwatch 2 | online | 已规范化／原样不合 | google/gemini-3.7-flash / OpenAI | 13.261 | 8308/1405/0 | stop | $0.018499750 |
| Tom Clancy&#x27;s Rainbow Six Siege | online | 成功 | google/gemini-3.7-flash / OpenAI | 7.294 | 4759/1328/0 | stop | $0.015549250 |
| Genshin Impact | online | 格式不合 | google/gemini-3.7-flash / OpenAI | 6.957 | 1295/1710/0 | stop | $0.007383750 |
| Kingdom Come: Deliverance II | online | 成功 | google/gemini-3.7-flash / OpenAI | 9.638 | 6219/1638/0 | stop | $0.017806750 |
| Black Myth: Wukong | online | 成功 | google/gemini-3.7-flash / OpenAI | 8.862 | 4645/1368/0 | stop | $0.015613750 |

可归属总花费：`$0.074853250`（5/5 个调用有花费元数据）。

## 限流统计

| 模式 | 429 | 累计冷却（秒） | 冷却丢弃 |
|---|---:|---:|---:|
| 联网模式 | 0 | 0.000 | 0 |

## 错误与格式

- `overwatch-2` / `online`：键位输入 '`' → 'Backquote'（动作：按键说话）
- `genshin-impact` / `online`：default_pc_keybinds['自动奔跑'] 不是单一规范化 PC 输入：'NumLock'

## 说明

- 这是 5 个游戏的小样本探针，不能替代正式跨类型判卷。
- 答案未由脚本判定事实正确性；完整原文见 answers.md。
- parsed-contexts.json 仅执行确定性规范化：剥离额外文本／代码围栏、移除语法上无歧义的尾随逗号、按白名单转换标点键名；不补全截断内容、不修改游戏知识。解析器拒绝任意层级重复键与 NaN/Infinity 等非标准常量；通过合同后由标准库重新序列化并二次严格解析。每一步都写入 normalization_actions，原始格式不合仍单独记录。
- 详细输出与 10 秒延迟目标存在客观张力，本表保留实测，不据此自动调短提示词。

## 运行信息

- 开始：`2026-09-01T04:16:05.866331+00:00`
- 结束：`2026-09-01T04:16:51.921566+00:00`
