# M5-B-T3a Qwen3.8 Flash challenger 游戏知识探针报告

本轮只验证修改后的详细游戏 context 提示词，不替换上一轮正式 3 模型 × 15 游戏判卷，也不作选型推荐。

## 模型与样本

- 模型：[Qwen3.8 Flash challenger](https://openrouter.ai/qwen/qwen3.8-flash)（`qwen/qwen3.8-flash`）。
- 请求上游：由 OpenRouter 在当前账户数据策略允许的端点中自动选择；实际返回上游逐次记录。
- 样本：Cyberpunk 2077, Counter-Strike 2, Stardew Valley, Hearthstone, Terraria。
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
| Cyberpunk 2077 | 开放世界动作角色扮演 | 热门长线单机作品 | 检验第一人称 RPG、载具、战斗与复杂交互键位。 |
| Counter-Strike 2 | 竞技战术射击 | 热门长线运营 | 检验回合制竞技规则、经济系统与射击键位。 |
| Stardew Valley | 农场生活模拟角色扮演 | 经典长线更新作品 | 检验生活模拟、时间管理、多系统循环与工具栏键位。 |
| Hearthstone | 数字集换式卡牌 | 热门长线运营 | 检验鼠标主导卡牌游戏、模式结构与少量键盘绑定。 |
| Terraria | 二维沙盒生存动作冒险 | 经典长线更新作品 | 检验沙盒探索、建造、战斗与快捷物品栏键位。 |

## 提示词调整

- 删除社区术语与 HUD 惯例。
- 将一句话核心玩法扩成完整游戏介绍、详细玩法结构与不剧透的公开背景。
- 默认键位统一为可机械解析的“动作名称 → 单一规范化 PC 输入”对象；移动方向与快捷栏逐键展开。
- 输出面向后续每个视觉模型的稳定 context，因此完整性优先于极端短小。

## 耗时与花费

| 模式 | 返回 | 可机械解析 | 原样格式合规 | P50 / P90 / 最大（秒） | ≤10 秒 | 花费（USD） |
|---|---:|---:|---:|---:|---:|---:|
| 联网模式 | 0/5 | 0/5 | 0/5 | 46.244 / 78.399 / 86.438 | 0/5 | $0.011743586 |

## 逐次结果

| 游戏 | 模式 | 状态 | 实际模型／上游 | 耗时（秒） | 输入／输出／推理 token | finish_reason | 花费（USD） |
|---|---|---|---|---:|---:|---|---:|
| Cyberpunk 2077 | online | 空答 | qwen/qwen3.8-flash / OpenAI | 86.438 | 6416/8483/8293 | length | $0.011743586 |
| Counter-Strike 2 | online | 失败 | — / — | 6.049 | —/—/— | — | — |
| Stardew Valley | online | 失败 | — / — | — | —/—/— | — | — |
| Hearthstone | online | 失败 | — / — | — | —/—/— | — | — |
| Terraria | online | 失败 | — / — | — | —/—/— | — | — |

可归属总花费：`$0.011743586`（1/5 个调用有花费元数据）。

## 限流统计

| 模式 | 429 | 累计冷却（秒） | 冷却丢弃 |
|---|---:|---:|---:|
| 联网模式 | 1 | 1.000 | 3 |

## 错误与格式

- `cyberpunk-2077` / `online`：输出因 length 截断，无法形成完整合同 JSON
- `counter-strike-2` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error [status=429 provider_code=429 request_id=gen-1788268730-c2Hm2xTmyyn5unbda1Sf request_id_header=x-generation-id occurred_at=2026-09-01T13:18:56.754200+00:00 profile_name=m5-b-t3a-v2:online]
- `stardew-valley` / `online`：模型档位 m5-b-t3a-v2:online 限流冷却中，派发前丢弃请求 [provider_code=429 request_id=gen-1788268730-c2Hm2xTmyyn5unbda1Sf request_id_header=x-generation-id occurred_at=2026-09-01T13:18:56.755260+00:00 profile_name=m5-b-t3a-v2:online cooldown_drop=True cooldown_remaining_seconds=0.998608099995181]
- `hearthstone` / `online`：模型档位 m5-b-t3a-v2:online 限流冷却中，派发前丢弃请求 [provider_code=429 request_id=gen-1788268730-c2Hm2xTmyyn5unbda1Sf request_id_header=x-generation-id occurred_at=2026-09-01T13:18:56.756307+00:00 profile_name=m5-b-t3a-v2:online cooldown_drop=True cooldown_remaining_seconds=0.9978573000116739]
- `terraria` / `online`：模型档位 m5-b-t3a-v2:online 限流冷却中，派发前丢弃请求 [provider_code=429 request_id=gen-1788268730-c2Hm2xTmyyn5unbda1Sf request_id_header=x-generation-id occurred_at=2026-09-01T13:18:56.757652+00:00 profile_name=m5-b-t3a-v2:online cooldown_drop=True cooldown_remaining_seconds=0.9971279999881517]

## 说明

- 这是 5 个游戏的小样本探针，不能替代正式跨类型判卷。
- 答案未由脚本判定事实正确性；完整原文见 answers.md。
- parsed-contexts.json 仅执行确定性规范化：剥离额外文本／代码围栏、移除语法上无歧义的尾随逗号、按白名单转换标点键名；不补全截断内容、不修改游戏知识。解析器拒绝任意层级重复键与 NaN/Infinity 等非标准常量；通过合同后由标准库重新序列化并二次严格解析。每一步都写入 normalization_actions，原始格式不合仍单独记录。
- 详细输出与 10 秒延迟目标存在客观张力，本表保留实测，不据此自动调短提示词。

## 运行信息

- 开始：`2026-09-01T13:17:24.241608+00:00`
- 结束：`2026-09-01T13:18:56.757652+00:00`
