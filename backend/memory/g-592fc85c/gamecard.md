# 游戏卡：守望先锋

- 游戏 ID：`g-592fc85c`
- 初始化时间：2026-08-27T21:18:16.270174+00:00
- 版本：2

## 游戏知识

- 状态：initialized
- 最近核查：2026-09-01T17:32:59.237123+00:00
- 模型：`google/gemini-3.1-flash-lite`
- 模式：web
- 请求 ID：`gk-63a27b47-b311-41b2-a6fb-402554bdf96c`

- 类型：第一人称射击、团队竞技、英雄射击
- 视角：第一人称视角
- 游戏概述：《守望先锋》是一款以团队合作为核心的英雄射击游戏，玩家扮演拥有独特技能的英雄，在多样的地图中通过配合完成目标。
- 核心玩法：游戏以团队对抗为核心，玩家需在坦克、输出、支援三种职责中选择英雄，通过运用各自的武器与技能组合，在占领、护送或控制目标点等模式中击败对手。核心循环为：选择英雄进入战场，通过战术配合与技能释放争夺目标点，积累能量释放终极技能，最终达成地图胜利条件。
- 游戏结构：基于局域匹配的短时对局制，每局比赛由两支队伍进行对抗，根据地图模式设定胜负条件，支持多种竞技与休闲模式。
- 背景设定：游戏设定在近未来的地球，世界正处于从全球危机中恢复的时期，各地的英雄们因不同背景与使命集结，在充满科技感与多元文化色彩的全球场景中进行对抗。
- 发售与运营：已正式发售，目前以《守望先锋：归来》名义进行持续运营的免费游玩服务。

### 核查尝试

- 2026-09-01T15:05:52.624615+00:00：failed；原因：OpenRouter网络请求失败：[WinError 10013] An attempt was made to access a socket in a way forbidden by its access permissions [occurred_at=2026-09-01T15:05:52.624615+00:00]
- 2026-09-01T16:29:51.671264+00:00：schema_reject；原因：未通过完整 V3 合同：1 validation error for GameKnowledgeContent Value error, game knowledge keybind '技能1' is not canonical: 'Shift' [type=value_error, input_value={'genre': ['第一人称...快捷物品栏4': '4'}}, input_type=dict] For further information visit https://errors.pydantic.dev/2.13/v/value_error
- 2026-09-01T16:32:12.496587+00:00：schema_reject；原因：未通过完整 V3 合同：1 validation error for GameKnowledgeContent Value error, game knowledge keybind '技能1' is not canonical: 'Shift' [type=value_error, input_value={'genre': ['第一人称...快捷物品栏4': '4'}}, input_type=dict] For further information visit https://errors.pydantic.dev/2.13/v/value_error
- 2026-09-01T16:34:38.129862+00:00：schema_reject；原因：未通过完整 V3 合同：1 validation error for GameKnowledgeContent Value error, game knowledge keybind '技能1' is not canonical: 'Shift' [type=value_error, input_value={'genre': ['第一人称...快捷物品栏4': '4'}}, input_type=dict] For further information visit https://errors.pydantic.dev/2.13/v/value_error
- 2026-09-01T17:32:59.237123+00:00：ok

## 场景

### scene:s1

- 标签：主菜单匹配界面
- 标签状态：named
- 待复查：否
- 注释：守望先锋游戏大厅主菜单，当前选中休闲比赛模式并显示正在搜索状态。
- 代表指纹：`af32583d0f4e7389`
- 首次见到：2026-08-27T21:20:00.962893+00:00
- 最近见到：2026-08-27T21:20:09.966310+00:00
- 累计驻留：9.003 秒
- 累计访问段数：1
- 会话簇次数：1
- 证据样本：`f88:scene:1`, `f89:scene:1`, `f90:scene:1`, `f91:scene:1`, `f92:scene:1`
- 命名时间：2026-08-31T19:45:01.818781+00:00
- 场景指纹核查证据：`f89:deep:1`

### scene:s2

- 标签：英雄选择界面
- 标签状态：named
- 待复查：否
- 注释：这是守望先锋对局开始前的英雄选择阶段，玩家正在挑选角色并查看队伍阵容。
- 代表指纹：`a8f4d5a5eb092b4d`
- 首次见到：2026-08-27T21:20:39.681898+00:00
- 最近见到：2026-08-27T21:21:04.505176+00:00
- 累计驻留：23.543 秒
- 累计访问段数：2
- 会话簇次数：1
- 证据样本：`f117:scene:1`, `f118:scene:1`, `f119:scene:1`, `f120:scene:1`, `f121:scene:1`
- 命名时间：2026-08-31T19:45:01.833719+00:00
- 场景指纹核查证据：`f119:deep:1`

### scene:s3

- 标签：主菜单休闲比赛界面
- 标签状态：named
- 待复查：否
- 注释：守望先锋游戏大厅的主菜单，当前选中“休闲比赛”标签页，展示模式选择、英雄预览及小队列表。
- 代表指纹：`de28604b0fdef28d`
- 首次见到：2026-08-27T21:31:15.564961+00:00
- 最近见到：2026-08-27T21:31:25.566327+00:00
- 累计驻留：10.001 秒
- 累计访问段数：1
- 会话簇次数：1
- 证据样本：`f610:scene:1`, `f611:scene:1`, `f612:scene:1`, `f613:scene:1`, `f614:scene:1`
- 命名时间：2026-08-31T19:45:01.850798+00:00
- 场景指纹核查证据：`f613:deep:1`

## HUD 元素

暂无。

## 来源录制

- `recordings/capture/20260827-171815`
