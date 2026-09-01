# M5-B-T3a 游戏知识线模型选型探针报告

本报告只列机器测量与原始答复状态，不作选型推荐；答案正确性留给产品负责人在 `judging.md` 判定。

## 候选与入围理由

| 候选 | 模型 ID | 请求锁定上游 | 档次 | 标价（输入／输出，每百万 token） | 入围理由 |
|---|---|---|---|---:|---|
| [Qwen3.8 Flash](https://openrouter.ai/qwen/qwen3.8-flash) | `qwen/qwen3.8-flash` | `Alibaba` | 低价／新模型基线 | $0.15 / $0.47 | 现有场景命名线同款，价格最低且发布新；用于测量低成本快速档是否已足够。 |
| [GPT-5.4 Mini](https://openrouter.ai/openai/gpt-5.4-mini) | `openai/gpt-5.4-mini` | `Azure` | 中价／均衡档 | $0.75 / $4.5 | 价格与能力位于两端候选之间，官方标注知识截止到 2025-08，适合直接观察联网对 2026 新作的增益。 |
| [Claude Sonnet 4.6](https://openrouter.ai/anthropic/claude-sonnet-4.6) | `anthropic/claude-sonnet-4.6` | `Anthropic` | 高价／能力上限 | $3 / $15 | 价格显著最高的强能力对照；用于判断更高档模型在跨类型、结构遵循和长尾游戏知识上是否带来可见收益。 |

三个候选覆盖约 20 倍输入标价、约 32 倍输出标价，并包含低价新模型、中档均衡模型和高价能力上限。每个请求都设置一个上游并令 `allow_fallbacks=false`；实际返回上游逐次记录，若网关没有遵守请求限制则以实际值为准。

## 模式与联网依据

- 知识模式：不提供任何联网工具，只用模型自身知识。
- 联网模式：OpenRouter 官方说明其 [`openrouter:web_search` Server Tool](https://openrouter.ai/docs/guides/features/server-tools/web-search) 可用于任意模型；本探针向每次联网请求提供该网关内置工具，最多 3 个结果。没有接独立搜索 API。
- 两种模式、三个模型使用完全相同的 system prompt 与用户消息模板；唯一按题变化的数据是游戏名称。温度固定为 0，`max_tokens=900`，客户端超时配置为 10 秒。
- 联网工具由模型决定是否调用；“联网模式”表示工具可用，不把是否实际搜索伪装成已知事实。

## 15 款游戏与入选理由

| # | 游戏 | 类型 | 新旧／热度 | 入选理由 |
|---:|---|---|---|---|
| 1 | Overwatch 2（守望先锋 2） | 英雄射击 | 热门长线更新 | 项目有录像；热门团队射击可检验角色分工、HUD 与团队术语。 |
| 2 | Don't Starve Together（饥荒联机版） | 生存／制作 | 经典长线更新 | 项目有录像；老牌合作生存游戏可检验制作、季节与多人术语。 |
| 3 | Gray Zone Warfare（Gray Zone Warfare） | 战术撤离射击 | 较新抢先体验 | 项目有录像；较新的硬核射击可检验长尾知识和复杂操作约定。 |
| 4 | [Slay the Spire 2](https://www.megacrit.com/news/2026-02-19-release-date-trailer/)（杀戮尖塔 2） | 卡牌／Roguelike | 2026-03-05 抢先体验 | 项目有录像；2026 新作，是知识时效与联网价值的直接样本。 |
| 5 | League of Legends（英雄联盟） | MOBA | 热门长线更新 | 高热度竞技游戏，UI、默认操作与社区术语密集。 |
| 6 | Baldur's Gate 3（博德之门 3） | 回合制 CRPG | 2023 热门作品 | 检验队伍制角色扮演、回合制操作和复杂界面惯例。 |
| 7 | Sid Meier's Civilization VII（文明 7） | 4X 策略 | 2025 新作 | 检验宏观策略、多层 UI 与策略社区术语。 |
| 8 | [Forza Horizon 6](https://forza.net/news/forza-horizon-6-coming-may-2026)（极限竞速：地平线 6） | 开放世界竞速 | 2026-05-19 发售 | 2026 新作；检验竞速视角、手柄操作与联网时效。 |
| 9 | [Mario Tennis Fever](https://www.nintendo.com/en-gb/Games/Nintendo-Switch-2-games/Mario-Tennis-Fever-2915160.html)（马力欧网球 狂热） | 街机体育 | 2026-02-12 发售 | 2026 新作且为主机独占；检验体育计分 UI 与控制器键位。 |
| 10 | Street Fighter 6（街头霸王 6） | 格斗 | 2023 热门长线更新 | 检验格斗输入、回合 HUD 与高度专门化的社区术语。 |
| 11 | Microsoft Flight Simulator 2024（微软模拟飞行 2024） | 飞行模拟 | 2024 专业模拟 | 检验复杂模拟类型、多设备输入和仪表／HUD 边界。 |
| 12 | Hollow Knight: Silksong（空洞骑士：丝之歌） | 平台动作／类银河战士恶魔城 | 2025 新作 | 检验横版视角、平台动作与较新作品知识。 |
| 13 | Blue Prince（蓝途王子） | 解谜／Roguelike | 2025 新作 | 较新独立解谜作品，检验长尾类型与非标准 UI。 |
| 14 | [Resident Evil Requiem](https://www.capcom.co.jp/ir/english/news/html/e250609.html)（生化危机：安魂曲） | 生存恐怖 | 2026-02-27 发售 | 2026 新作；检验恐怖游戏惯例与知识时效，且明确排除剧情内容。 |
| 15 | FINAL FANTASY XIV Online（最终幻想 14） | MMORPG | 热门长线更新 | 补足大型多人在线类型，检验热键栏、职业与团队社区术语。 |

其中 Slay the Spire 2、Forza Horizon 6、Mario Tennis Fever、Resident Evil Requiem 均为 2026 年发售或进入抢先体验，超过“至少 3 款 2025 年之后发售或更新”的要求。

## 模型 × 模式耗时、返回率与花费

耗时分布统计所有真正派发且返回了延迟元数据的调用（含失败）；冷却期本地丢弃没有网络耗时，不混入分布。`≤10 秒`比例的分母固定为计划的 15 题，失败、空答和冷却丢弃均不计达标。

| 模型 | 模式 | 返回／15 | P50 / P90 / 最大（秒） | ≤10 秒 | 已报告花费调用 | 总花费（USD） |
|---|---|---:|---:|---:|---:|---:|
| Qwen3.8 Flash | 知识模式 | 1/15 | 9.474 / 16.036 / 16.616 | 0/15（0.0%） | 3/15 | $0.001338 |
| Qwen3.8 Flash | 联网模式 | 0/15 | 6.285 / 14.798 / 16.361 | 0/15（0.0%） | 2/15 | $0.000891 |
| GPT-5.4 Mini | 知识模式 | 15/15 | 2.771 / 4.033 / 11.233 | 14/15（93.3%） | 15/15 | $0.026748 |
| GPT-5.4 Mini | 联网模式 | 14/15 | 6.409 / 8.974 / 10.032 | 14/15（93.3%） | 14/15 | $0.145445 |
| Claude Sonnet 4.6 | 知识模式 | 15/15 | 12.724 / 14.100 / 18.905 | 3/15（20.0%） | 15/15 | $0.162144 |
| Claude Sonnet 4.6 | 联网模式 | 15/15 | 21.656 / 31.885 / 37.072 | 0/15（0.0%） | 15/15 | $2.542083 |

## 限流统计

T7.8 冷却按 `模型 × 模式` 六个独立档位隔离；429 后不重试，仍处于冷却的后续题在编码／派发前直接丢弃。退避起点 1 秒与上限 60 秒仍是待实测保守占位，本报告不把它称为已调优参数。

| 模型 | 模式 | 429 次数 | 累计冷却时长（秒） | 冷却丢弃 | 结束时仍冷却 |
|---|---|---:|---:|---:|---|
| Qwen3.8 Flash | 知识模式 | 12 | 26.000 | 0 | 否 |
| Qwen3.8 Flash | 联网模式 | 13 | 48.000 | 0 | 否 |
| GPT-5.4 Mini | 知识模式 | 0 | 0.000 | 0 | 否 |
| GPT-5.4 Mini | 联网模式 | 0 | 0.000 | 0 | 否 |
| Claude Sonnet 4.6 | 知识模式 | 0 | 0.000 | 0 | 否 |
| Claude Sonnet 4.6 | 联网模式 | 0 | 0.000 | 0 | 否 |
| **合计** |  | **25** | **74.000** | **0** |  |

## 失败、空答与格式

| 模型 | 模式 | 失败 | 空答 | 格式不合 | 截断 | 成功且格式合规 |
|---|---|---:|---:|---:|---:|---:|
| Qwen3.8 Flash | 知识模式 | 12 | 2 | 1 | 3 | 0 |
| Qwen3.8 Flash | 联网模式 | 13 | 2 | 0 | 2 | 0 |
| GPT-5.4 Mini | 知识模式 | 0 | 0 | 0 | 0 | 15 |
| GPT-5.4 Mini | 联网模式 | 1 | 0 | 0 | 0 | 14 |
| Claude Sonnet 4.6 | 知识模式 | 0 | 0 | 15 | 0 | 0 |
| Claude Sonnet 4.6 | 联网模式 | 0 | 0 | 15 | 12 | 0 |

## 每次调用的耗时与花费

| # | 游戏 | 模型 | 模式 | 状态 | 实际模型／上游 | 耗时（秒） | 输入／输出／推理 token | 花费（USD） |
|---:|---|---|---|---|---|---:|---:|---:|
| 1 | Overwatch 2 | Qwen3.8 Flash | 知识模式 | 格式不合 | qwen/qwen3.8-flash / Alibaba | 15.456 | 379/900/862 | $0.000445546 |
| 2 | Overwatch 2 | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 2.485 | 348/405/0 | $0.002083500 |
| 3 | Overwatch 2 | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 10.913 | 487/695/0 | $0.011886000 |
| 4 | Overwatch 2 | Qwen3.8 Flash | 联网模式 | 空答 | qwen/qwen3.8-flash / Alibaba | 16.361 | 379/900/900 | $0.000445546 |
| 5 | Overwatch 2 | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 8.760 | 3218/381/0 | $0.011128000 |
| 6 | Overwatch 2 | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 20.088 | 30331/976/0 | $0.145633000 |
| 7 | Don't Starve Together | Qwen3.8 Flash | 知识模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 8 | Don't Starve Together | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 3.191 | 348/433/0 | $0.002209500 |
| 9 | Don't Starve Together | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 13.606 | 488/788/0 | $0.013284000 |
| 10 | Don't Starve Together | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 11 | Don't Starve Together | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 3.344 | 706/325/0 | $0.001992000 |
| 12 | Don't Starve Together | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 21.629 | 33097/1074/0 | $0.155401000 |
| 13 | Gray Zone Warfare | Qwen3.8 Flash | 知识模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 14 | Gray Zone Warfare | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 3.599 | 347/579/0 | $0.002865750 |
| 15 | Gray Zone Warfare | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 13.115 | 486/722/0 | $0.012288000 |
| 16 | Gray Zone Warfare | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 17 | Gray Zone Warfare | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 5.726 | 4107/379/0 | $0.011785750 |
| 18 | Gray Zone Warfare | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 21.399 | 29257/1092/0 | $0.144151000 |
| 19 | Slay the Spire 2 | Qwen3.8 Flash | 知识模式 | 空答 | qwen/qwen3.8-flash / Alibaba | 16.616 | 382/900/900 | $0.000445996 |
| 20 | Slay the Spire 2 | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 1.124 | 351/87/0 | $0.000654750 |
| 21 | Slay the Spire 2 | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 11.986 | 491/644/0 | $0.011133000 |
| 22 | Slay the Spire 2 | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 23 | Slay the Spire 2 | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 6.409 | 4042/361/0 | $0.011656000 |
| 24 | Slay the Spire 2 | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 21.370 | 31351/1115/0 | $0.150778000 |
| 25 | League of Legends | Qwen3.8 Flash | 知识模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 26 | League of Legends | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 2.151 | 347/364/0 | $0.001898250 |
| 27 | League of Legends | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 13.129 | 486/747/0 | $0.012663000 |
| 28 | League of Legends | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 29 | League of Legends | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 3.237 | 705/399/0 | $0.002324250 |
| 30 | League of Legends | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 22.136 | 29829/1092/0 | $0.145867000 |
| 31 | Baldur's Gate 3 | Qwen3.8 Flash | 知识模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 32 | Baldur's Gate 3 | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 2.991 | 351/420/0 | $0.002153250 |
| 33 | Baldur's Gate 3 | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 18.905 | 489/772/0 | $0.013047000 |
| 34 | Baldur's Gate 3 | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 35 | Baldur's Gate 3 | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 5.594 | 4087/390/0 | $0.011820250 |
| 36 | Baldur's Gate 3 | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 20.702 | 29282/1108/0 | $0.144466000 |
| 37 | Sid Meier's Civilization VII | Qwen3.8 Flash | 知识模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 38 | Sid Meier's Civilization VII | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 3.090 | 350/367/0 | $0.001914000 |
| 39 | Sid Meier's Civilization VII | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 13.168 | 491/739/0 | $0.012558000 |
| 40 | Sid Meier's Civilization VII | Qwen3.8 Flash | 联网模式 | 空答 | qwen/qwen3.8-flash / Alibaba | 12.454 | 381/900/900 | $0.000445846 |
| 41 | Sid Meier's Civilization VII | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 4.881 | 3790/397/0 | $0.011629000 |
| 42 | Sid Meier's Civilization VII | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 32.454 | 62841/1168/0 | $0.256043000 |
| 43 | Forza Horizon 6 | Qwen3.8 Flash | 知识模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 44 | Forza Horizon 6 | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 1.063 | 349/77/0 | $0.000608250 |
| 45 | Forza Horizon 6 | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 12.724 | 489/742/0 | $0.012597000 |
| 46 | Forza Horizon 6 | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | 6.285 | —/—/— | — |
| 47 | Forza Horizon 6 | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 5.169 | 3848/403/0 | $0.011699500 |
| 48 | Forza Horizon 6 | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 21.656 | 48663/1133/0 | $0.202984000 |
| 49 | Mario Tennis Fever | Qwen3.8 Flash | 知识模式 | 失败 | — / Alibaba | 4.380 | —/—/— | — |
| 50 | Mario Tennis Fever | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 0.953 | 347/77/0 | $0.000606750 |
| 51 | Mario Tennis Fever | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 1.912 | 486/71/0 | $0.002523000 |
| 52 | Mario Tennis Fever | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 53 | Mario Tennis Fever | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 7.863 | 4019/393/0 | $0.011782750 |
| 54 | Mario Tennis Fever | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 31.032 | 29040/1072/0 | $0.133200000 |
| 55 | Street Fighter 6 | Qwen3.8 Flash | 知识模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 56 | Street Fighter 6 | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 4.323 | 348/481/0 | $0.002425500 |
| 57 | Street Fighter 6 | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 13.210 | 486/765/0 | $0.012933000 |
| 58 | Street Fighter 6 | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 59 | Street Fighter 6 | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 5.029 | 3146/484/0 | $0.011537500 |
| 60 | Street Fighter 6 | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 21.694 | 30190/1102/0 | $0.147100000 |
| 61 | Microsoft Flight Simulator 2024 | Qwen3.8 Flash | 知识模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 62 | Microsoft Flight Simulator 2024 | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 2.996 | 350/500/0 | $0.002512500 |
| 63 | Microsoft Flight Simulator 2024 | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 12.717 | 489/757/0 | $0.012822000 |
| 64 | Microsoft Flight Simulator 2024 | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | 6.021 | —/—/— | — |
| 65 | Microsoft Flight Simulator 2024 | GPT-5.4 Mini | 联网模式 | 失败 | — / Azure | 10.032 | —/—/— | — |
| 66 | Microsoft Flight Simulator 2024 | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 20.042 | 27097/1111/0 | $0.137956000 |
| 67 | Hollow Knight: Silksong | Qwen3.8 Flash | 知识模式 | 空答 | qwen/qwen3.8-flash / Alibaba | 11.840 | 382/900/900 | $0.000445996 |
| 68 | Hollow Knight: Silksong | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 11.233 | 351/422/0 | $0.002162250 |
| 69 | Hollow Knight: Silksong | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 8.135 | 490/450/0 | $0.008220000 |
| 70 | Hollow Knight: Silksong | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | 6.219 | —/—/— | — |
| 71 | Hollow Knight: Silksong | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 6.651 | 4096/550/0 | $0.012547000 |
| 72 | Hollow Knight: Silksong | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 23.963 | 53516/1127/0 | $0.217453000 |
| 73 | Blue Prince | Qwen3.8 Flash | 知识模式 | 失败 | — / Alibaba | 7.109 | —/—/— | — |
| 74 | Blue Prince | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 2.575 | 346/335/0 | $0.001767000 |
| 75 | Blue Prince | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 10.798 | 484/581/0 | $0.010167000 |
| 76 | Blue Prince | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 77 | Blue Prince | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 9.118 | 3886/394/0 | $0.011687500 |
| 78 | Blue Prince | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 29.746 | 48366/1122/0 | $0.201928000 |
| 79 | Resident Evil Requiem | Qwen3.8 Flash | 知识模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 80 | Resident Evil Requiem | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 1.351 | 349/136/0 | $0.000873750 |
| 81 | Resident Evil Requiem | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 1.941 | 488/71/0 | $0.002529000 |
| 82 | Resident Evil Requiem | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 83 | Resident Evil Requiem | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 8.411 | 3807/448/0 | $0.011871250 |
| 84 | Resident Evil Requiem | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 37.072 | 51690/1126/0 | $0.211960000 |
| 85 | FINAL FANTASY XIV Online | Qwen3.8 Flash | 知识模式 | 失败 | — / Alibaba | 5.770 | —/—/— | — |
| 86 | FINAL FANTASY XIV Online | GPT-5.4 Mini | 知识模式 | 成功 | openai/gpt-5.4-mini / Azure | 2.771 | 350/389/0 | $0.002013000 |
| 87 | FINAL FANTASY XIV Online | Claude Sonnet 4.6 | 知识模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 14.429 | 488/802/0 | $0.013494000 |
| 88 | FINAL FANTASY XIV Online | Qwen3.8 Flash | 联网模式 | 失败 | — / Alibaba | — | —/—/— | — |
| 89 | FINAL FANTASY XIV Online | GPT-5.4 Mini | 联网模式 | 成功 | openai/gpt-5.4-mini / OpenAI | 8.269 | 4162/414/0 | $0.011984500 |
| 90 | FINAL FANTASY XIV Online | Claude Sonnet 4.6 | 联网模式 | 格式不合 | anthropic/claude-sonnet-4.6 / Anthropic | 19.043 | 30956/953/0 | $0.147163000 |
| **合计** | **90 次计划调用** |  |  |  |  |  |  | **$2.878649180** |

花费元数据覆盖 64/90 个逻辑题格；合计只累加本次可恢复且由上游明确报告的花费，不用标价反推缺失值。

## 错误明细

- `dont-starve-together` / `qwen38-flash` / `knowledge`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `dont-starve-together` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `gray-zone-warfare` / `qwen38-flash` / `knowledge`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `gray-zone-warfare` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `slay-the-spire-2` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `league-of-legends` / `qwen38-flash` / `knowledge`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `league-of-legends` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `baldurs-gate-3` / `qwen38-flash` / `knowledge`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `baldurs-gate-3` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `civilization-vii` / `qwen38-flash` / `knowledge`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `forza-horizon-6` / `qwen38-flash` / `knowledge`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `forza-horizon-6` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error [status=429 provider_code=429 request_id=gen-1788224005-GFJqy79tWKVy54sYRvxj request_id_header=x-generation-id occurred_at=2026-09-01T00:53:31.617843+00:00 provider=Alibaba profile_name=m5-b-t3a:qwen38-flash:online]
- `mario-tennis-fever` / `qwen38-flash` / `knowledge`：OpenRouter请求失败（HTTP 429）：Provider returned error [status=429 provider_code=429 request_id=gen-1788224038-vcuPgAcCu63R3NR3vTTO request_id_header=x-generation-id occurred_at=2026-09-01T00:54:02.835396+00:00 provider=Alibaba profile_name=m5-b-t3a:qwen38-flash:knowledge]
- `mario-tennis-fever` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `street-fighter-6` / `qwen38-flash` / `knowledge`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `street-fighter-6` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `microsoft-flight-simulator-2024` / `qwen38-flash` / `knowledge`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `microsoft-flight-simulator-2024` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error [status=429 provider_code=429 request_id=gen-1788224144-gxXOTJ5sYY56oMlEW4MV request_id_header=x-generation-id occurred_at=2026-09-01T00:55:50.626840+00:00 provider=Alibaba profile_name=m5-b-t3a:qwen38-flash:online]
- `microsoft-flight-simulator-2024` / `gpt54-mini` / `online`：OpenRouter 联网请求超时：The read operation timed out [occurred_at=2026-09-01T00:56:00.662920+00:00 provider=Azure profile_name=m5-b-t3a:gpt54-mini:online]
- `hollow-knight-silksong` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error [status=429 provider_code=429 request_id=gen-1788224212-N2Sy8XTZqZujBNkrQItC request_id_header=x-generation-id occurred_at=2026-09-01T00:56:58.152978+00:00 provider=Alibaba profile_name=m5-b-t3a:qwen38-flash:online]
- `blue-prince` / `qwen38-flash` / `knowledge`：OpenRouter请求失败（HTTP 429）：Provider returned error [status=429 provider_code=429 request_id=gen-1788224248-lgqXr6T1EAtETsDDnCri request_id_header=x-generation-id occurred_at=2026-09-01T00:57:35.888560+00:00 provider=Alibaba profile_name=m5-b-t3a:qwen38-flash:knowledge]
- `blue-prince` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `resident-evil-requiem` / `qwen38-flash` / `knowledge`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `resident-evil-requiem` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error
- `final-fantasy-xiv-online` / `qwen38-flash` / `knowledge`：OpenRouter请求失败（HTTP 429）：Provider returned error [status=429 provider_code=429 request_id=gen-1788224357-6pDeDSssPyMLDMrX1y5m request_id_header=x-generation-id occurred_at=2026-09-01T00:59:22.716106+00:00 provider=Alibaba profile_name=m5-b-t3a:qwen38-flash:knowledge]
- `final-fantasy-xiv-online` / `qwen38-flash` / `online`：OpenRouter请求失败（HTTP 429）：Provider returned error

## 与规格的偏差

- 首批 90 个计划项完成网络阶段后，report.md 渲染器在格式计数处触发 TypeError，且原始结果尚未落盘。根据终端错误元数据重建 19 个 HTTP 429 空题并不补跑；其余 71 个有返回题格经产品负责人同意重新调用。
- 首批 71 个有返回调用的答案、耗时、token 与花费无法恢复，不纳入本报告机器统计或总花费；这是相对一次性跑批和总花费完整性的偏差。
- 上游锁定未完全生效：gpt54-mini/online 请求 Azure、实际 OpenAI。相关跨模式延迟同时包含上游差异，不能只归因于联网工具。

## 未完成项

- 首批未持久化调用的逐次答案、耗时、token 与花费无法从网关恢复；本报告仅统计恢复批可归属花费。

## 运行信息

- 开始：`2026-09-01T00:46:59.312440+00:00`
- 结束：`2026-09-01T01:00:07.254671+00:00`
- 计划调用：15 游戏 × 3 模型 × 2 模式 = 90。
- 报告外、无法逐次归属的首批调用：71。
