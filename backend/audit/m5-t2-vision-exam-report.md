# M5-T2 视觉模型考卷——实现、验收与使用指引

统计日期：2026-08-23

## 交付边界

本任务新增一个离线出卷/跑卷工具。它只有在产品负责人执行命令、看见完整上传清单，
并输入大写 `YES` 或显式传入 `--yes` 后才会发起请求。没有接入 `main.py`、适配端口、
前端或截屏探针；宠物日常运行不会上传屏幕内容。

`games/generic/` 当前只有 `eval/`，没有 adapter，也没有注册到
`built_in_adapters`。真实截图、考卷运行结果和模型回答继续留在 Git 已忽略的
`recordings/` 与 `eval-reports/` 中。

## 变更文件清单

- `src/pet/core/llm.py`：新增本地图像附件、缩放、base64 内容块与独立多模态调用协议。
- `src/pet/games/generic/__init__.py`、`eval/__init__.py`、`eval/vision_exam.py`：
  generic 离线考卷包和 CLI；没有生产 adapter。
- `prompts/generic/observation.md`：结构化观察提示词初版。
- `tests/fixtures/vision-exam-example.toml` 与三张合成 PPM：两道示例题。
- `tests/test_llm.py`、`tests/test_vision_exam.py`：纯文本回归、图像消息和假客户端
  全流程测试。
- `audit/m5-t2-vision-exam-report.md`：本报告与产品负责人操作指引。

没有修改 `capture.py`、探针、`main.py`、端口、前端、架构文档、依赖清单或任何
真实截图。

## 关键实现决策

1. 保留 `LlmClientProtocol.complete()` 及 `OpenRouterClient.complete()` 的既有纯文本
   签名，另加 `LlmVisionClientProtocol.complete_with_images()`。这样生产语音路径不会
   因视觉附件获得新的可选分支。
2. 图片在本机用 Pillow 读取、缩放并重新编码为无元数据 PNG，再组成 OpenAI 兼容的
   `image_url` data URL 内容块。每次调用可设全局最大边长；考卷还对全图单独应用
   `--send-width`，原生裁剪图不缩放，除非调用方另设全局上限。
3. 默认只跑“不带区域提示、不带裁剪”一个变体。把 with/without 两种开关同时写上，
   工具会对所选状态作笛卡尔积，一次跑完最多四种变体；不需要拆成四份结果目录。
4. 模型 ID 不写死。`--model` 可重复，也可写 `profile:<档位名>` 读取现有
   `[llm.profiles.*]`。每个目标必须用 `--price` 明确给出输入/输出百万 token 美元
   单价；缺价时在上传前拒绝执行，不猜单价，也不拿上游账单代替配置折算。
5. 合法回答必须是字段恰为 `scene`、`notable_events`、`game_guess`、`confidence`
   的 JSON。网络失败、拒绝、超时和非法 JSON 都保留错误原文并继续后续题目；非法
   JSON 的回答原文仍进入判卷表，便于人工判断。
6. sequence 时间轴明确列出已采样帧与未采样间隔，并反复告诉模型这是稀疏截图，
   不是连续视频，避免让模型把采样间隙脑补成可见过程。

## 给产品负责人的出卷指引

建议把真实考卷 TOML 放在 `backend/eval-reports/` 下，或另一个不会入库的位置；不要
把 `recordings/` 的真实截图路径写进仓库文件。路径相对 TOML 所在目录解析，也可以
写绝对路径。

最小 single 题：

```toml
version = 1

[[questions]]
id = "game-a-001"
type = "single"
frames = ["C:/本地路径/frame.png"]
seconds = [0.0]
region_hint = "可选：右下角 UI 区域发生变化。"
crops = ["C:/本地路径/native-crop.png"]
prompt_override = "可选：只分析这道题需要特别观察的内容。"
```

sequence 题把多帧和相对秒数一一对应；秒数必须严格递增：

```toml
[[questions]]
id = "game-a-002"
type = "sequence"
frames = ["frame-1.png", "frame-2.png", "frame-3.png"]
seconds = [0.0, 2.0, 5.0]
```

仓库附带的两题示例是
`tests/fixtures/vision-exam-example.toml`，只引用人工生成的 PPM 色块，不引用任何
真实游戏截图。

## 给产品负责人的跑卷指引

在 `backend` 目录运行。下面的型号与价格都是需要替换的占位符，不是推荐：

```powershell
.venv\Scripts\python -m pet.games.generic.eval.vision_exam `
  eval-reports\my-exam.toml `
  --model "<候选模型ID>" `
  --price "<候选模型ID>=<输入百万token美元>,<输出百万token美元>"
```

跑配置档位：

```powershell
.venv\Scripts\python -m pet.games.generic.eval.vision_exam `
  eval-reports\my-exam.toml `
  --model "profile:<档位名>" `
  --price "profile:<档位名>=<输入单价>,<输出单价>"
```

一次对比区域提示和原生裁剪的四种组合：

```powershell
.venv\Scripts\python -m pet.games.generic.eval.vision_exam `
  eval-reports\my-exam.toml `
  --model "<候选模型ID>" `
  --price "<候选模型ID>=<输入单价>,<输出单价>" `
  --with-region-hint --without-region-hint `
  --with-crops --without-crops `
  --send-width 1280
```

命令会先打印每个模型、单价、变体和所有待上传文件。逐项看完后输入大写 `YES`；
自动脚本可传 `--yes`，但仍会先打印同一份清单。每个 `--model` 会逐一跑完整份考卷，
单题失败不会打断其他题。

## 怎么判卷

每次运行写到 `backend/eval-reports/vision-exam-<时间戳>/`：

- `results.csv`：机器可读的每次调用明细，包括原始回答、错误、延迟、token、配置
  折算花费和上游报告花费。
- `report.md`：产品负责人主要填写的判卷表。每题 × 每变体 × 每模型一行，在最后
  三列填写「准确性判定」「漏了什么」「编造了什么」。不要让工具替人打分。
- `run.json`：本次参数、单价、起止时间、上传文件、目标、变体，以及按模型、题号、
  变体分组的汇总统计。

报告中的模型表给出延迟中位/P90；题号表和变体表给出同组成功数、延迟、每次平均
token 和配置花费。比较模型质量时仍以逐题人工三列为准，自动汇总不代表选型结论。

## 假客户端全流程产物样例

验收测试用两道合成题、两个变体和一个假客户端生成 4 行。CSV 的结构如下：

```text
题号,题型,变体,目标档位,请求模型,实际模型,服务商,回答原文,错误原文,往返毫秒,输入token,输出token,配置折算花费美元,上游报告花费美元
synthetic-single,single,region-off__crops-off__width-1280,fake/model,fake/model,fake/actual,fake-provider,"{...}",,125.000,120,30,0.000180000,0.009000000
```

`report.md` 的人工表结构如下；测试同时断言三个判卷列初始为空：

```text
| 题号 | 变体 | 模型/档位 | 回答原文 | 错误原文 | 准确性判定 | 漏了什么 | 编造了什么 |
|---|---|---|---|---|---|---|---|
| synthetic-single | ... | fake/model | {...} |  |  |  |  |
```

以上 `fake/model` 与回答只存在于合成测试和本文结构样例，不在生产路径中，也不是候选
模型或模型质量数据。

## 验收记录

定向测试（包括 LLM 纯文本回归、图像内容块、考卷全流程与分层警报器）：

```text
.venv\Scripts\python -m pytest tests/test_llm.py tests/test_vision_exam.py tests/test_layering.py -q --basetemp .pytest-m5-t2-commit
35 passed
```

全量后端测试：

```text
.venv\Scripts\python -m pytest tests/ -q --basetemp .pytest-m5-t2-final-full
451 passed, 4 failed
```

4 项失败均为 `tests/test_speech.py` 的真实 OneCore 中文语音环境项：当前受限执行环境
无法枚举已安装语音。新增考卷测试、既有纯文本 LLM 测试、分层测试及其他回归通过。

新增 generic eval 目录没有直接导入或调用网络库：

```text
rg -n "httpx|socket|urllib|requests" src/pet/games/generic/eval
（无输出）
```

对生产源码新增行检索网络库和请求调用也无输出；实际网络仍只由既有
`pet.core.llm.OpenRouterClient` 负责。本任务在该客户端内新增多模态消息构造，但没有
在其他主干模块、游戏生产包、实时入口或探针增加调用点。

## 与规格的偏差及原因

无已知偏差。

## 未完成项

- 未运行任何真实模型，未上传真实截图，未生成或编造模型成绩。
- 未决定候选模型、最佳变体、阈值或实时视觉管线设计；这些都需要产品负责人跑卷并
  完成人工判卷后再交架构师评审。

## M5-T2.5：正式出卷记录与跑卷建议

### 卷面边界

正式卷写入 `data/generic/vision-exam/manifest.toml`，参考答案草稿写入同目录的
`answer-key.md`。卷面共 13 题：11 道 single、2 道稀疏 sequence；11 道提供由窗口
标题和进程名确定的 `game_context`，另外 2 道是同图无上下文对照。

本卷的评分对象是单帧可见内容，不是情境理解。sequence 题只探测模型能否遵守
“离散端点 + 未采样间隔”的边界，不把连续动作还原能力视为本卷目标。

### 题目清单与选帧理由

| 题号 | 类别 | 来源样本 | 选帧理由 |
|---|---|---:|---|
| `gzw-helicopter-landing` | 显著事件 | 2 | 第 92 帧是定时兜底而非指标触发，但左侧直升机清楚可见，直接覆盖规格优先项。 |
| `disco-dialogue-panel` | 大块文字 UI | 5 | 第 35 帧是会话最大变化帧，右侧肖像、正文和继续按钮整体出现。 |
| `gzw-near-black` | 暗场 | 9 | 首帧近乎全黑，仅路灯、围墙、武器轮廓和少量 HUD 可辨，适合检查暗部幻觉。 |
| `spire-combat-ui` | 高信息密度 UI | 12 | 第 58 帧同时包含手牌、放大卡牌、生命、能量、格挡、敌人生命与意图。 |
| `subnautica-night-underwater` | 陌生视觉风格 | 11 | 第 13 帧为低光水下发光海床，兼有鱼形小目标、深度、氧气和手持工具。 |
| `gzw-static-control-a` | 抗幻觉对照 | 1 | 首帧是稳定营地构图，单帧没有直接可见的显著事件。 |
| `gzw-static-control-b` | 抗幻觉对照 | 1 | 第 31 帧与首帧构图近似且只由 60 秒兜底保存，正确 notable_events 仍应为空。 |
| `gzw-rain-small-helicopter` | 小目标 + 裁剪 A/B | 4 | 第 92 帧中央远处直升机在全图占比小；本地原生裁剪可清楚显示直升机与邻近人形。 |
| `subnautica-wave-surface` | 显著环境画面 | 10 | 第 46 帧浪面遮挡画面大部并折射近处结构，单帧视觉难度高。 |
| `disco-task-switch-sequence` | 局部文字变化；时序边界 | 6 | 第 44/45 帧相隔 2.001330 秒，同一日志布局内选中项、标题和正文局部改变。 |
| `subnautica-wave-sequence` | 时序边界 | 10 | 第 7/8 帧相隔 2.003689 秒，水线高度显著不同而视向近似，适合检查稀疏时间轴措辞。 |
| `spire-combat-ui-nocontext` | 无上下文对照 | 12 | 逐像素复用 `spire-combat-ui` 的帧，只去掉 game_context。 |
| `subnautica-night-underwater-nocontext` | 无上下文对照 | 11 | 逐像素复用水下题的帧，只去掉 game_context。 |

Project Zomboid 的 PZ-1 至 PZ-4 均未出现在 manifest。所选帧没有昵称、Steam ID、
好友列表或玩家聊天；Slay the Spire 2 画面右上角的版本/运行字符串没有写入 manifest
或答案草稿，也没有把它解释为玩家身份。

### Manifest 与文件存在性实测

在 `backend/` 下用当前虚拟环境导入生产解析器 `load_manifest()`，解析正式 manifest，
随后逐题遍历 `frames` 与 `crops` 并调用 `Path.is_file()`、`Path.stat()`。实际输出：

```text
manifest_ok questions=13
single=11 sequence=2 with_context=11 without_context=2
gzw-helicopter-landing frame frame-000092-20260823T180114.960830Z.png exists=true bytes=7394500
disco-dialogue-panel frame frame-000035-20260823T191028.340744Z.png exists=true bytes=4460774
gzw-near-black frame frame-000001-20260823T222301.121235Z.png exists=true bytes=2496892
spire-combat-ui frame frame-000058-20260824T001854.718295Z.png exists=true bytes=3081894
subnautica-night-underwater frame frame-000013-20260823T223852.427674Z.png exists=true bytes=3102613
gzw-static-control-a frame frame-000001-20260823T175206.287712Z.png exists=true bytes=6322542
gzw-static-control-b frame frame-000031-20260823T175306.283718Z.png exists=true bytes=6170978
gzw-rain-small-helicopter frame frame-000092-20260823T184110.593912Z.png exists=true bytes=5642399
subnautica-wave-surface frame frame-000046-20260823T223306.813409Z.png exists=true bytes=2624317
disco-task-switch-sequence frame frame-000044-20260823T201139.413813Z.png exists=true bytes=2506080
disco-task-switch-sequence frame frame-000045-20260823T201141.415143Z.png exists=true bytes=2481344
subnautica-wave-sequence frame frame-000007-20260823T223148.805555Z.png exists=true bytes=3451160
subnautica-wave-sequence frame frame-000008-20260823T223150.809244Z.png exists=true bytes=3396565
spire-combat-ui-nocontext frame frame-000058-20260824T001854.718295Z.png exists=true bytes=3081894
subnautica-night-underwater-nocontext frame frame-000013-20260823T223852.427674Z.png exists=true bytes=3102613
gzw-rain-small-helicopter crop frame-000092-helicopter-native.png exists=true bytes=160354
```

裁剪图只存在于已忽略的
`recordings/capture/20260823-143808/vision-exam-crops/`，没有加入 Git。

### 现场记录与离线复核的证据边界

没有发现“现场记录明确说 A、截图明确显示非 A”的直接冲突。以下题目存在必须保留的
证据范围差异，不按冲突自行调和：

- `gzw-helicopter-landing`、`gzw-rain-small-helicopter`：现场记录包含直升机降落
  过程；单帧只能确认直升机靠近停机坪或近地面，不能确认即时运动方向。
- `gzw-static-control-a/b`：现场记录包含植被轻微晃动；单帧不能证明运动，故正确
  notable_events 仍为空。
- `gzw-near-black`：现场记录包含夜视仪、手电筒、移动和转向动作序列；首帧无法
  对应到具体动作步骤。
- `subnautica-night-underwater` 及无上下文复制题：现场记录描述静止/移动阶段；
  单帧不能证明即时运动状态。
- `subnautica-wave-surface`、`subnautica-wave-sequence`：现场记录说明玩家静止且
  海浪持续波动；单帧或稀疏帧对只能确认端点水线差异。

### 无法确认并已写入答案草稿的内容

- 直升机是在悬停、降落、起飞还是已经停稳，以及附近人形的身份和阵营。
- 暗场大面积黑区内是否有对象、局部光线是否包含玩家手电筒、首帧对应动作序列哪一步。
- 放大卡牌处于悬停、选择还是结算状态，以及左侧 6/6 单位的精确机制关系。
- 水下鱼形生物的物种、数量、运动方向，发光点属于植物、生物还是粒子效果。
- 静止对照帧中的植被和远处人形是否在运动。
- 单张水面帧中的浪正在上升还是回落；稀疏帧间发生了多少次波动。
- 任务日志的实际点击时点，以及小号正文无法辨认时的逐字内容。
- 无上下文复制题能否正确猜中游戏名；这是待测变量，不在草稿中预先评分。

### 一题完整答案样例

以下摘录 `gzw-rain-small-helicopter`，用于验收答案结构；正式修订仍只改
`answer-key.md` 中的源条目。

#### 元数据

- 类别：小目标 + 裁剪 A/B；显著事件
- 来源样本：样本 4
- 所用帧文件名：`frame-000092-20260823T184110.593912Z.png`；`frame-000092-helicopter-native.png`
- game_context 原文：`Grey Zone Warfare`

#### 现场记录

> 【现场记录】玩家静止，第一人称持枪视角，与样本 3 视角相似；天气为暴雨。本次没有逐帧人工标注画面内各运动或界面变化的准确发生时间。

#### 离线复核

- 【离线复核】全图为暴雨中的第一人称营地场景，前景有枪械、植被和一名站立人形。
- 【离线复核】画面中央远处可见一架靠近地面的直升机及其附近的另一人形；原生裁剪清楚保留这两个小目标。
- 【离线复核】雨线、湿地面和低能见度可见，但单帧不能确认直升机正在下降还是已经停稳。

#### 参考答案要点

- 【离线复核】不带裁剪时仍应尽量发现中央远处的小型直升机；带裁剪时应更明确描述直升机和邻近人形。
- 【离线复核】应提到明显降雨、湿润营地和第一人称持枪视角。
- 【离线复核】应保持“靠近地面”这一可见状态，不把它升级为确定的降落过程。

#### 不得出现的内容

- 【离线复核】不得声称直升机坠毁、爆炸、开火或正在运送确定身份的人物。
- 【离线复核】不得断言前景或远处人形正在攻击玩家。
- 【离线复核】不得编造裁剪图之外的放大细节、标识或文字。

#### 不确定项

- 【离线复核】直升机是在悬停、降落还是已落地无法由单帧确认。
- 【离线复核】两个人形的阵营、身份和运动状态无法确认。
- 【现场记录】暴雨会话没有逐帧事件时间标注。

### 建议的正式跑卷组合

建议先用 3 个不写死型号的候选档位：一个偏低延迟/低价、一个均衡、一个偏高质量。
先用相同上传宽度和输出上限跑完，再看人工判卷；不要在首轮同时改变型号、宽度和
提示词，避免无法归因。

建议每个档位这样跑：

1. 全部 13 题先跑 `without-region-hint + without-crops` 基线：13 次调用。
2. 只把有真实提示文本的 4 题（直升机、对话面板、小直升机、任务日志）复制到
   Git 忽略的临时子卷，追加跑 `with-region-hint + without-crops`：4 次调用。
3. 小直升机题再跑 `without-region-hint + with-crops` 与
   `with-region-hint + with-crops`：2 次调用。

合计每个档位 19 次，3 个档位共 57 次调用、72 张图像附件。以 1280 宽图像和短 JSON
回答估算，整轮大约是 5 万至 16 万输入 token 等价值、6 千至 1.5 万输出 token；视觉
token 计算随候选模型而异，这只是预算量级，不作为最终账单。单价暂留空，跑卷当天
按货架用 `--price` 填入；实际 token、延迟和花费以 `run.json` 为准。

如果首轮预算较紧，可先跑 1 个均衡档位的 19 次调用完成卷面校准，确认 manifest、
提示和答案草稿没有明显问题后，再扩到另外两个档位。

### M5-T2.5 验收记录

定向测试覆盖正式答案结构、现场记录逐字核对、上下文经假客户端进入消息体、无上下文
消息不含该字段、两组对照题复用同一帧，以及 LLM 回归与分层警报器：

```text
.venv\Scripts\python.exe -m pytest tests/test_vision_exam.py tests/test_llm.py tests/test_layering.py -q --basetemp .pytest-m5-t2-5-final3
40 passed, 1 warning in 0.50s
```

全量后端测试：

```text
.venv\Scripts\python.exe -m pytest tests/ -q --basetemp .pytest-m5-t2-5-full
456 passed, 4 failed, 3 warnings in 17.25s
```

4 项失败仍全是 `tests/test_speech.py` 的既有真实 OneCore 中文语音环境项；错误为当前
执行环境无法枚举已安装中文语音。其余测试（包括 `test_layering.py`）全部通过。

正式 manifest 与答案草稿的通用身份标记检索：

```text
rg -n "765611[0-9]{10}|STEAM_[0-9]|steamid|好友列表|昵称[:：]" data/generic/vision-exam/manifest.toml data/generic/vision-exam/answer-key.md
（无输出）
```

此外，离线看图时见到的准确玩家标识被放在不入库的本地拒绝列表中逐项检索，两个正式
文本产物均无命中；拒绝列表本身不写入公开仓库。答案结构测试还逐条检查所有判断要点
必须以「现场记录」或「离线复核」标源，并确认 13 条现场记录在忽略 Markdown 换行后
逐字存在于原实测汇总。

### M5-T2.5 偏差与未完成项

- 无规格偏差：13 题覆盖全部要求类别；两道 sequence 保留真实时间差；无上下文题
  恰好两道且逐像素复用带上下文题。
- 本任务没有调用模型、没有联网、没有评分，也没有填写判卷表人工列。
- 参考答案仍是 coding agent 草稿；产品负责人尚未修订，正式跑卷尚未开始。
