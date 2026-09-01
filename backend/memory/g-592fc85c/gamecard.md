# 游戏卡：守望先锋

- 游戏 ID：`g-592fc85c`
- 初始化时间：2026-08-27T21:18:16.270174+00:00
- 版本：2

## 游戏知识

- 状态：refreshed
- 最近核查：2026-09-01T18:34:36.262458+00:00
- 模型：`google/gemini-3.1-flash-lite`
- 模式：web
- 请求 ID：`gk-4de9041e-41c4-4804-84f8-b4ccb4084111`

- 类型：第一人称射击、英雄射击、MOBA融合类
- 视角：第一人称
- 游戏概述：由暴雪娱乐开发的团队竞技类英雄射击游戏，玩家扮演不同职能的英雄，在快节奏的对局中通过团队协作、技能配合与战术反制达成目标。
- 核心玩法：游戏以5v5团队对抗为核心，玩家需在重装、输出、支援三种职能中选择英雄，通过运用独特的技能组、管理终极技能充能节奏以及针对敌方阵容进行灵活反制，在占领要点、运载目标、混合模式等多种地图目标中取得胜利。
- 游戏结构：采用赛季制运营，包含竞技排位、快速游戏、街机模式（含多种非常规玩法）及PVE剧情任务等模块。单局对战节奏较快，通常在10-20分钟内完成。
- 背景设定：以近未来地球为背景，世界各地因智械危机与英雄组织的兴衰而动荡，玩家扮演来自全球各地的英雄，在不同风格的地图场景中进行对抗。
- 发售与运营：持续运营中。游戏于2022年10月以《守望先锋2》名义上线并取代原版，后于2026年2月正式更名为《守望先锋》。

### 核查尝试

- 2026-09-01T15:05:52.624615+00:00：failed；原因：OpenRouter网络请求失败：[WinError 10013] An attempt was made to access a socket in a way forbidden by its access permissions [occurred_at=2026-09-01T15:05:52.624615+00:00]
- 2026-09-01T16:29:51.671264+00:00：schema_reject；原因：未通过完整 V3 合同：1 validation error for GameKnowledgeContent Value error, game knowledge keybind '技能1' is not canonical: 'Shift' [type=value_error, input_value={'genre': ['第一人称...快捷物品栏4': '4'}}, input_type=dict] For further information visit https://errors.pydantic.dev/2.13/v/value_error
- 2026-09-01T16:32:12.496587+00:00：schema_reject；原因：未通过完整 V3 合同：1 validation error for GameKnowledgeContent Value error, game knowledge keybind '技能1' is not canonical: 'Shift' [type=value_error, input_value={'genre': ['第一人称...快捷物品栏4': '4'}}, input_type=dict] For further information visit https://errors.pydantic.dev/2.13/v/value_error
- 2026-09-01T16:34:38.129862+00:00：schema_reject；原因：未通过完整 V3 合同：1 validation error for GameKnowledgeContent Value error, game knowledge keybind '技能1' is not canonical: 'Shift' [type=value_error, input_value={'genre': ['第一人称...快捷物品栏4': '4'}}, input_type=dict] For further information visit https://errors.pydantic.dev/2.13/v/value_error
- 2026-09-01T17:32:59.237123+00:00：ok
- 2026-09-01T18:34:36.262458+00:00：ok

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
