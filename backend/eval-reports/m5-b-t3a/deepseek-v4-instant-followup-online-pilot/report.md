# M5-B-T3a DeepSeek V4 Instant 优化提示词小样本报告

本轮只验证修改后的详细游戏 context 提示词，不替换上一轮正式 3 模型 × 15 游戏判卷，也不作选型推荐。

## 模型与样本

- 模型：[DeepSeek V4 Flash 0731 (Instant, follow-up)](https://openrouter.ai/deepseek/deepseek-v4-flash-0731)（`deepseek/deepseek-v4-flash-0731`）。
- 请求上游：由 OpenRouter 在当前账户数据策略允许的端点中自动选择；实际返回上游逐次记录。
- 样本：Marvel Rivals, Monster Hunter Wilds, Sid Meier's Civilization VII, EA Sports FC 26, Hades II。
- 每个游戏只跑联网模式。
- 联网模式只使用网关内置 [`openrouter:web_search`](https://openrouter.ai/docs/guides/features/server-tools/web-search)，不接独立搜索 API。
- 固定参数：temperature=0.0，max_tokens=8000，reasoning=none，客户端超时=45 秒；产品延迟目标仍按 ≤10 秒统计。
- 搜索：Exa；每次最多 5 条、全请求累计最多 5 条；不限制每条结果字符数。
- 路由：合规上游中按吞吐量优先；要求上游支持请求参数。
- 输出：请求 OpenRouter JSON Schema 严格结构化输出；是否被实际上游执行按原始响应另行记录。
- 这里的 httpx 超时是连接／读取等 I/O 阶段的超时，不是整次请求的总墙钟截止；联网端点持续传输数据时，实测总耗时可以明显超过 45 秒。

### 样本理由

| 游戏 | 类型 | 新旧／热度 | 入选理由 |
|---|---|---|---|
| Marvel Rivals | 第三人称英雄射击 | 热门长线运营 | 检验英雄技能、团队目标与角色差异化键位。 |
| Monster Hunter Wilds | 动作角色扮演 | 2025 新作 | 检验复杂武器动作、狩猎循环与上下文组合键。 |
| Sid Meier&#x27;s Civilization VII | 回合制策略 | 2025 新作 | 检验鼠标主导策略游戏、时代推进与回合结构。 |
| EA Sports FC 26 | 体育模拟 | 2025 年度作品 | 检验以手柄为主流但仍需给出 PC 键盘默认绑定的边界。 |
| Hades II | 动作肉鸽 | 近年持续更新作品 | 检验即时战斗、局内循环与永久成长结构。 |

## 提示词调整

- 删除社区术语与 HUD 惯例。
- 将一句话核心玩法扩成完整游戏介绍、详细玩法结构与不剧透的公开背景。
- 默认键位统一为可机械解析的“动作名称 → 单一规范化 PC 输入”对象；移动方向与快捷栏逐键展开。
- 输出面向后续每个视觉模型的稳定 context，因此完整性优先于极端短小。

## 耗时与花费

| 模式 | 返回 | 可机械解析 | 原样格式合规 | P50 / P90 / 最大（秒） | ≤10 秒 | 花费（USD） |
|---|---:|---:|---:|---:|---:|---:|
| 联网模式 | 5/5 | 3/5 | 0/5 | 18.824 / 23.371 / 25.926 | 0/5 | $0.049386350 |

## 逐次结果

| 游戏 | 模式 | 状态 | 实际模型／上游 | 耗时（秒） | 输入／输出／推理 token | finish_reason | 花费（USD） |
|---|---|---|---|---:|---:|---|---:|
| Marvel Rivals | online | 格式不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 19.539 | 16236/2937/0 | stop | $0.009752650 |
| Monster Hunter Wilds | online | 已规范化／原样不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 17.522 | 14383/2215/0 | stop | $0.010430350 |
| Sid Meier&#x27;s Civilization VII | online | 已规范化／原样不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 18.824 | 14545/1865/0 | stop | $0.009376850 |
| EA Sports FC 26 | online | 格式不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 25.926 | 17833/5079/0 | stop | $0.010415550 |
| Hades II | online | 已规范化／原样不合 | deepseek/deepseek-v4-flash-0731 / OpenAI | 18.757 | 13414/1831/0 | stop | $0.009410950 |

可归属总花费：`$0.049386350`（5/5 个调用有花费元数据）。

## 限流统计

| 模式 | 429 | 累计冷却（秒） | 冷却丢弃 |
|---|---:|---:|---:|
| 联网模式 | 0 | 0.000 | 0 |

## 错误与格式

- `marvel-rivals` / `online`：不是合法 JSON：Expecting value（line 1, column 1）
- `monster-hunter-wilds` / `online`：剥离 JSON 外文本／代码围栏
- `sid-meiers-civilization-vii` / `online`：剥离 JSON 外文本／代码围栏；移除 1 个对象／数组尾随逗号
- `ea-sports-fc-26` / `online`：找到完整 JSON 对象，但未通过严格合同：default_pc_keybinds['战术板快调'] 不是单一规范化 PC 输入：'Up'；不是合法 JSON：Expecting property name enclosed in double quotes（line 6, column 5）
- `hades-ii` / `online`：剥离 JSON 外文本／代码围栏

## 说明

- 这是 5 个游戏的小样本探针，不能替代正式跨类型判卷。
- 答案未由脚本判定事实正确性；完整原文见 answers.md。
- parsed-contexts.json 仅执行确定性规范化：剥离额外文本／代码围栏、移除语法上无歧义的尾随逗号、按白名单转换标点键名；不补全截断内容、不修改游戏知识。解析器拒绝任意层级重复键与 NaN/Infinity 等非标准常量；通过合同后由标准库重新序列化并二次严格解析。每一步都写入 normalization_actions，原始格式不合仍单独记录。
- 详细输出与 10 秒延迟目标存在客观张力，本表保留实测，不据此自动调短提示词。

## 运行信息

- 开始：`2026-09-01T03:55:37.175606+00:00`
- 结束：`2026-09-01T03:57:17.789098+00:00`
