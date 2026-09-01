# M5-B-T3b2 游戏知识线 V4 收尾报告

## 结论

货架一已经从 V3 嵌套合同切换到 V4 扁平七字段合同，生产代码、提示词、游戏卡、Markdown、短视图与自动验收中的键位要求均已删除。四张卡在同一轮正式联网跑批中全部通过 V4；守望先锋由 `stale/null` 成功初始化，其余三张卡完成整份刷新。旧 `attempts[]` 与场景数据原样保留，旧 V3 `content` 没有机械迁移，而是只在新答案通过完整 V4 校验后被原子替换。

`53e8b78` 已有的异步总截止、单次不重试、失败保留、审计留痕、冷却隔离、原子替换、短视图与会话统计机制没有改动。

## 合同 V4

严格 JSON Schema 使用 `additionalProperties=false`，字段如下：

| 字段 | 响应必填 | 允许 null |
|---|---:|---:|
| `genre` | 是 | 否 |
| `perspective` | 否 | 是 |
| `summary` | 是 | 否 |
| `core_gameplay` | 否 | 是 |
| `game_structure` | 否 | 是 |
| `setting_and_background` | 否 | 是 |
| `release_and_service_status` | 否 | 是 |

可选字段在模型响应中省略时由类型模型写成显式 `null`，因此持久化的 `knowledge.content` 始终是同一套七字段形状。测试覆盖了直接返回 `null` 和省略可选字段两种情况，均不会由本地代码猜补内容。

## 实现删改

- 删除 `GameKnowledgeSystem`、`GameKnowledgeGameplay`、`GameKnowledgeBackground` 及全部规范键位正则、别名映射和校验。
- `GameKnowledgeContent` 改为扁平七字段；只校验存在的字符串非空。
- 生产响应 schema 只强制 `genre` 与 `summary`，其他五项接受字符串或 `null`，拒绝额外字段。
- 解析失败文案从“完整 V3 合同”更新为“完整 V4 合同”；保留尾逗号和单一 JSON 外框剥离两种无歧义清理。
- JSON/Markdown/短视图删除默认键位、嵌套玩法系统和背景对象，改为直接渲染七字段；`null` 在人读 Markdown 中显示为“未确认”，短视图直接省略。
- 删除两套已被生产知识线取代的 T3a 可执行旧探针及对应测试：`knowledge_model_probe.py`、`knowledge_prompt_v2_pilot.py`。历史报告、答案、结果与证据全部保留。
- `AGENTS.md` 使用产品负责人提供的架构师版本更新；更新后与附件 SHA-256 完全一致，均为 954 行。

## V3 → V4 提示词 diff

```diff
@@
-只回答玩家开始游玩前可从官方页面、商店页、游戏内公开说明或可靠公开资料得知的通用知识。若调用环境提供联网工具，先用它核查当前版本与公开资料；若没有联网工具，则使用自身知识。不得把不确定内容猜成事实，不确定时明确写“不确定”。
+只回答玩家开始游玩前可从官方页面、商店页、游戏内公开说明或可靠公开资料得知的通用知识。若调用环境提供联网工具，先用它核查当前版本与公开资料；若没有联网工具，则使用自身知识。不得把不确定内容猜成事实；允许为 null 的字段无法确认时必须写 null。

-内容边界：可以介绍游戏定位、玩法结构、规则系统、公开的世界设定前提与运营状态；不得提供剧情推进、具体任务或关卡解法、角色命运、结局、具体地图内容、隐藏内容或剧透。不要描述 HUD；不要输出社区术语。默认键位只写 PC 默认键盘鼠标，不写主机或控制器键位，不把可由玩家修改的绑定说成唯一操作方式。
+内容边界：可以介绍游戏定位、玩法结构、规则系统、公开的世界设定前提与运营状态；不得提供剧情推进、具体任务或关卡解法、角色命运、结局、具体地图内容、隐藏内容或剧透。不要描述 HUD；不要输出键位、社区术语、手柄、实时状态或玩家身份。不要求穷举角色、地图、装备、技能或版本数值。
@@
 {
   "genre": ["主要类型", "必要时补充子类型"],
-  "perspective": "玩家通常采用的视角；存在多种时说明切换关系",
-  "game_overview": "完整介绍游戏定位、玩家扮演的抽象角色、主要目标、单人或多人形态，以及区别于同类游戏的关键特征",
-  "gameplay": {
-    "player_goal": "玩家在典型游玩过程中追求什么",
-    "core_loop": "按时间顺序详细说明反复发生的核心玩法循环",
-    "major_systems": [
-      {"name": "重要系统名称", "description": "该系统如何影响玩家决策和行动"}
-    ],
-    "modes_and_structure": "一局、一次远征、一个回合或持续世界如何组织；说明合作、对抗或单人结构"
-  },
-  "background": {
-    "setting_and_premise": "不剧透的世界背景、时代或题材前提，只写理解画面与玩法所需内容",
-    "release_and_service_status": "公开的发售、抢先体验、长线运营或重大版本状态；无法确认当前状态时写不确定"
-  },
-  "default_pc_keybinds": {
-    "前进": "W",
-    "快捷物品栏1": "1"
-  }
+  "perspective": "玩家通常采用的视角；存在多种时说明切换关系，无法确认时为 null",
+  "summary": "简短说明这是什么游戏、玩家的抽象身份",
+  "core_gameplay": "用一段话说明目标、核心循环和主要系统，无法确认时为 null",
+  "game_structure": "一局、一次远征、一个回合或持续世界如何组织；说明主要模式，无法确认时为 null",
+  "setting_and_background": "不剧透的世界背景、时代或题材前提，只写理解画面与玩法所需内容，无法确认时为 null",
+  "release_and_service_status": "公开的发售、抢先体验、持续运营或完结状态，无法确认时为 null"
 }
@@
 详细度要求：
-- game_overview 应为信息密度高的完整段落，不是一句话宣传语。
-- gameplay.core_loop 应覆盖开始、进行、反馈与继续循环；major_systems 写 4 至 10 个真正影响玩法的系统。
-- background 只提供理解游戏所需的公开前提，不复述故事。
-- default_pc_keybinds 是“动作名称 → 单一规范化 PC 输入”的对象，动作名称使用简体中文且不可重复。每个值只允许一个具体按键、鼠标输入或用 + 连接的组合键。
-- 移动方向和快捷栏必须逐项展开，例如“前进":"W"、“后退":"S"、“快捷物品栏1":"1”；禁止写成 WASD、1-0、Q/E、“某键或某键”或任何范围／备选缩写。
-- 输入名使用固定英文形式：单字母 A-Z、数字 0-9、F1-F24、Space、Tab、Escape、Enter、Backspace、CapsLock、LeftShift、RightShift、LeftCtrl、RightCtrl、LeftAlt、RightAlt、方向键 ArrowUp/ArrowDown/ArrowLeft/ArrowRight、MouseLeft、MouseRight、MouseMiddle、Mouse1-Mouse5、MouseWheelUp、MouseWheelDown、MouseMove，以及常见标点键名；组合键用 +，例如 LeftCtrl+F。只收录能够从公开资料确认的默认绑定；不确定的条目直接省略，不要猜测。
+- genre 与 summary 必须提供；其余字段允许为 null，不确定就留 null，不许猜。
+- summary 应为信息密度高的简短说明，不是一句话宣传语。
+- core_gameplay 用一段话覆盖目标、反复发生的核心循环和真正影响决策的主要系统。
+- setting_and_background 只提供理解游戏所需的基础设定，不复述故事。
```

除合同扁平化、删除键位和明确 nullable 规则外，没有调整模型、档位、联网方式、超时、冷却、采样参数或其他提示词策略。

## 在线调用结果

同一轮跑批中每个游戏调用一次，全部由生产 `game_knowledge` 档位执行；实际模型均为 `google/gemini-3.1-flash-lite`，provider 均为 `OpenAI`。

| 游戏 | 结果 | 写卡动作 | 耗时 | 花费 | 输入/输出 token |
|---|---|---|---:|---:|---:|
| Don't Starve Together | ok | refreshed | 2.729s | $0.000717000 | 948 / 320 |
| 守望先锋 | ok | initialized | 2.071s | $0.000694000 | 946 / 305 |
| Grey Zone Warfare | ok | refreshed | 1.804s | $0.000681750 | 945 / 297 |
| Slay the Spire 2 | ok | refreshed | 2.189s | $0.000777250 | 949 / 360 |

- 总花费：`$0.002870000`
- 平均耗时：`2.198s`
- P50：`2.130s`
- 最大耗时：`2.729s`
- 限流次数：`0`
- 冷却丢弃：`0`
- 冷却秒数：`0`
- schema 拒绝：`0`
- 本轮 evidence：`eval-reports/m5-b-t3b2/live-v4/evidence.jsonl`，共四条 `game_knowledge`

## 四张卡的 `knowledge.content`

### Don't Starve Together

```json
{
  "genre": ["生存", "沙盒", "动作冒险"],
  "perspective": "俯视角（固定斜向视角）",
  "summary": "《饥荒联机版》是一款支持多人协作的荒野生存游戏，玩家扮演被困在异世界的角色，需通过收集资源、建造基地、制作工具与应对环境威胁来维持生命。",
  "core_gameplay": "游戏的核心循环是“采集资源-制作工具-建设基地-应对季节与威胁”。玩家必须通过探索地图获取食物、燃料和材料，利用科学机器与魔法装置解锁更高级的科技，同时管理饥饿度、生命值与精神值。核心系统包括昼夜交替、季节更迭、复杂的合成配方以及多人协作的资源分配与战斗机制。",
  "game_structure": "采用持续运行的沙盒世界模式，玩家在服务器中进行长期的生存挑战。支持自定义世界生成参数，包含多种游戏模式（如生存、无尽、荒野等），玩家可随时加入或离开服务器。",
  "setting_and_background": "游戏设定在一个充满超自然力量、怪诞生物与诡异科技的异世界。世界由程序随机生成，包含多种生物群落，环境会随季节周期性变化，玩家需在充满敌意的荒野中寻找生存之道。",
  "release_and_service_status": "已正式发售，处于持续运营状态，定期更新内容与活动。"
}
```

### 守望先锋

```json
{
  "genre": ["第一人称射击", "团队竞技", "英雄射击"],
  "perspective": "第一人称视角",
  "summary": "《守望先锋》是一款以团队合作为核心的英雄射击游戏，玩家扮演拥有独特技能的英雄，在多样的地图中通过配合完成目标。",
  "core_gameplay": "游戏以团队对抗为核心，玩家需在坦克、输出、支援三种职责中选择英雄，通过运用各自的武器与技能组合，在占领、护送或控制目标点等模式中击败对手。核心循环为：选择英雄进入战场，通过战术配合与技能释放争夺目标点，积累能量释放终极技能，最终达成地图胜利条件。",
  "game_structure": "基于局域匹配的短时对局制，每局比赛由两支队伍进行对抗，根据地图模式设定胜负条件，支持多种竞技与休闲模式。",
  "setting_and_background": "游戏设定在近未来的地球，世界正处于从全球危机中恢复的时期，各地的英雄们因不同背景与使命集结，在充满科技感与多元文化色彩的全球场景中进行对抗。",
  "release_and_service_status": "已正式发售，目前以《守望先锋：归来》名义进行持续运营的免费游玩服务。"
}
```

### Grey Zone Warfare

```json
{
  "genre": ["战术射击游戏", "第一人称射击", "生存模拟"],
  "perspective": "第一人称视角",
  "summary": "《Grey Zone Warfare》是一款强调高度拟真与战术协作的开放世界第一人称射击游戏，玩家扮演受雇于私人军事公司的承包商，在充满敌意的岛屿上执行任务。",
  "core_gameplay": "玩家通过接受任务进入开放世界地图，在与AI及其他玩家的对抗中收集资源、完成目标并撤离。核心循环围绕着战术规划、拟真的武器改装与弹道系统、以及受伤后的精细化医疗处理展开，玩家需在复杂的战场环境中管理装备负重与生存状态。",
  "game_structure": "采用持续在线的开放世界模式，玩家在基地与任务区域之间往返，通过完成任务推进进度并获取装备，支持多人组队协作。",
  "setting_and_background": "游戏背景设定在东南亚一座因神秘事件而被封锁的虚构岛屿“拉曼岛”（Lamang），岛上存在多个敌对派系，玩家作为私人军事承包商被派往该地进行调查与行动。",
  "release_and_service_status": "目前处于抢先体验（Early Access）阶段，持续进行版本更新与优化。"
}
```

### Slay the Spire 2

```json
{
  "genre": ["Roguelike", "卡牌构建游戏"],
  "perspective": "2D 静态视角",
  "summary": "《杀戮尖塔2》是《杀戮尖塔》的续作，是一款结合了卡牌构建与 Roguelike 元素的策略游戏。玩家扮演一名攀登尖塔的挑战者，通过构建独特的卡组、获取遗物并应对随机生成的遭遇来不断向上攀登。",
  "core_gameplay": "玩家在每一轮游戏中通过战斗、事件和商店获取卡牌与遗物，构建并优化自己的卡组。核心循环为：在回合制战斗中通过消耗能量打出卡牌进行攻击或防御，击败敌人以推进层数，若生命值归零则游戏结束并重置进度。游戏强调根据随机出现的卡牌和遗物组合，实时调整策略以应对不同类型的敌人与首领。",
  "game_structure": "游戏采用 Roguelike 结构，每一局游戏都是一次从塔底向塔顶的独立远征。地图由多个节点组成，包含战斗、精英怪、商店、休息点和随机事件，玩家需在路径选择中进行决策。死亡后进度重置，但会根据表现解锁新的卡牌、遗物或角色能力。",
  "setting_and_background": "游戏设定在一个充满魔法与怪物的神秘尖塔世界。尖塔本身是一个不断变化的实体，玩家作为挑战者，目标是深入塔内探索其未知的秘密并击败守护者。",
  "release_and_service_status": "预计于 2025 年开启抢先体验。"
}
```

## 机械验收

- T3b2 定向及相邻机制回归：`132 passed`
- 非语音全量：`700 passed, 4 deselected`
- TD-30 隔离复跑：`1 passed`
- 全部四张卡可由 `GameCard` V4 严格解析。
- 四张 `gamecard.md` 与对应 JSON 经过渲染器逐字比较，全部可重生。
- 四张短视图均不超过 512 UTF-8 字节。
- `default_pc_keybinds` / `keybind` 在生产 Python、生产提示词与配置中零命中。
- 历史 `eval-reports` 中的 V3 答案、失败原因和键位记录保留不动。
- 删除旧探针后全量测试由 T3b 的 718 项变为 700 项，差额正好是被删除的 18 项旧探针测试；不存在新增失败。

四项 OneCore 语音测试按 TD-53 现状 deselect：

- `test_chinese_text_produces_nonempty_audio_with_duration`
- `test_mixed_text_synthesizes_without_an_audio_device`
- `test_stop_returns_real_long_playback_within_interruption_limit`
- `test_new_utterance_interrupts_real_previous_playback_within_limit`

## 短视图消费者确认

`render_game_knowledge_short_view` 当前只在 `core/gamecard.py` 定义，并由 `tests/test_game_knowledge.py` 验证；生产代码没有调用方。因此 V4 短视图仍未注入帧观察线，符合 D-44，实际接入留给 B-T8。

## 产品负责人判卷提示

结构通过不代表事实已经人工确认。建议重点检查：

1. 守望先锋的 `game_structure` 中“局域匹配”是否为错误措辞；它可能原本意图表达“匹配制”。
2. Slay the Spire 2 的 `release_and_service_status` 仍写“预计于 2025 年开启抢先体验”，在当前验收日期下明显可能过期。
3. Grey Zone Warfare 的中文地名译法与当前抢先体验状态。
4. 四张卡的背景段是否都只停留在基础设定，没有越过剧透红线。

按 D-37，模型答案仍是 `modality=inferred` 的背景提示，代码没有为了“答对”本地修改字段或把联网答案升级为事实。

## 与规格的偏差及未完成项

1. 产品负责人明确要求先更新随附 `AGENTS.md`，因此执行了该文档替换；这覆盖了规格模板中的“不要改 AGENTS.md”。仓库版与附件逐字、逐字节一致。
2. 产品负责人随后明确要求直接在 `main` 工作，因此没有创建规格指定的 `codex/m5-b-t3b2-knowledge-v4` 分支。`main` 原先不含 `53e8b78`，先以非改写历史的 merge 合入 T3b 基线，再实施本任务。
3. 四张卡的结构验收、Overwatch 通过、无键位和无占位均已完成；四卡逐字段事实正确性仍需产品负责人完成人工判卷，尤其是上节两项高风险内容。
4. 附件版 `AGENTS.md` 的 D-51 表格仍残留“游戏知识线：UI 惯例、默认键位”，与同一文件的 D-37、D-40、TD-54 及 V4 合同冲突。本任务按要求保持附件原文，没有擅自改架构文档；建议架构师下次修订该行。
