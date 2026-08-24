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
   `image_url` data URL 内容块。每次调用可设全局最大边长；考卷用可重复的
   `--send-width` 测试整图上传宽度，`0` 表示原生分辨率。
3. 默认只跑 `region-mode=off + width=1280`。可重复传入上传宽度与区域提示模式，
   工具会对二者作笛卡尔积；完整建议组合是 3 档提示 × 2 档宽度。
4. 模型 ID 不写死。`--model` 可重复，也可写 `profile:<档位名>` 读取现有
   `[llm.profiles.*]`。每个目标必须用 `--price` 明确给出输入/输出百万 token 美元
   单价；缺价时在上传前拒绝执行，不猜单价，也不拿上游账单代替配置折算。
5. 合法回答必须是字段恰为 `scene`、`notable_events`、`game_guess`、`confidence`
   的 JSON。网络失败、拒绝、超时和非法 JSON 都保留错误原文并继续后续题目；非法
   JSON 的回答原文仍进入判卷表，便于人工判断。
6. sequence 时间轴明确列出已采样帧与未采样间隔，并反复告诉模型这是稀疏截图，
   不是连续视频，避免让模型把采样间隙脑补成可见过程。

## 给产品负责人的出卷指引

正式、经审计的考卷 manifest 放在 `backend/data/generic/vision-exam/`；其中可记录指向
本机 `recordings/` 的相对路径，但截图本身继续由 Git 忽略。临时试卷仍可放
在 `backend/eval-reports/`。路径相对 TOML 所在目录解析，也可以写绝对路径。

最小 single 题：

```toml
version = 1

[[questions]]
id = "game-a-001"
type = "single"
frames = ["C:/本地路径/frame.png"]
seconds = [0.0]
region_grid = ["r3c5", "r3c6", "r4c5"]
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

一次对比三档区域提示与 1280/原生两档整图宽度：

```powershell
.venv\Scripts\python -m pet.games.generic.eval.vision_exam `
  eval-reports\my-exam.toml `
  --model "<候选模型ID>" `
  --price "<候选模型ID>=<输入单价>,<输出单价>" `
  --region-mode off --region-mode sparse --region-mode always `
  --send-width 1280 --send-width 0 `
  --region-sparsity-max 0.25
```

命令会先打印每个模型、单价、变体和所有待上传文件。逐项看完后输入大写 `YES`；
自动脚本可传 `--yes`，但仍会先打印同一份清单。每个 `--model` 会逐一跑完整份考卷，
单题失败不会打断其他题。

## 怎么判卷

每次运行写到 `backend/eval-reports/vision-exam-<时间戳>/`：

- `results.csv`：机器可读的每次调用明细，包括上传宽度、区域模式、格子占比、
  是否实际注入、实际图像像素尺寸与字节数，以及回答、错误、延迟、token 和花费。
- `report.md`：产品负责人主要填写的判卷表。每题 × 每变体 × 每模型一行，在最后
  三列填写「准确性判定」「漏了什么」「编造了什么」。不要让工具替人打分。
- `run.json`：本次参数、单价、起止时间、上传文件、目标、变体，以及按模型、题号、
  变体分组的汇总统计。

报告中的模型表给出延迟中位/P90；题号表和变体表给出同组成功数、延迟、每次平均
token 和配置花费；上传宽度表另列延迟中位/P90 与平均输入 token。比较模型质量时
仍以逐题人工三列为准，自动汇总不代表选型结论。

## 假客户端全流程产物样例

验收测试用两道合成题、3×2 变体和一个假客户端生成 12 行。CSV 的结构如下：

```text
题号,题型,变体,上传宽度,区域提示模式,本题变化格子占比,本次是否实际注入了提示,本次实际上传的图像像素尺寸,本次实际上传的图像字节数,目标档位,请求模型,实际模型,服务商,回答原文,错误原文,往返毫秒,输入token,输出token,配置折算花费美元,上游报告花费美元
synthetic-single,single,region-off__width-1280,1280,off,0.020833333,false,1280x960,123229,fake/model,fake/model,fake/actual,fake-provider,"{...}",,125.000,120,30,0.000180000,0.009000000
```

`report.md` 的人工表结构如下；测试同时断言三个判卷列初始为空：

```text
| 题号 | 变体 | 上传宽度 | 区域提示模式 | 变化格子占比 | 实际注入 | 图像像素尺寸 | 图像字节数 | 模型/档位 | 回答原文 | 错误原文 | 准确性判定 | 漏了什么 | 编造了什么 |
| synthetic-single | ... | 1280 | off | 0.020833333 | false | 1280x720 | ... | fake/model | {...} |  |  |  |  |
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

1. 全部 13 题先跑 `without-region-grid + without-crops` 基线：13 次调用。
2. 对有机械 `region_grid` 的 7 题跑 `with-region-grid + without-crops`：7 次调用。
3. 同 7 题再跑 `without-region-grid + with-crops` 和
   `with-region-grid + with-crops`：14 次调用。

合计每个档位 34 次，3 个档位共 102 次调用、168 张图像附件。以 1280 宽图像和短 JSON
回答估算，整轮大约是 9 万至 28 万输入 token 等价值、1 万至 3 万输出 token；视觉
token 计算随候选模型而异，这只是预算量级，不作为最终账单。单价暂留空，跑卷当天
按货架用 `--price` 填入；实际 token、延迟和花费以 `run.json` 为准。

如果首轮预算较紧，可先跑 1 个均衡档位的 34 次调用完成卷面校准，确认 manifest、
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

## M5-T2.6：消除考卷泄题

### 清除证明

1. 题目专属提示机制已从数据类、解析器、manifest 和请求构造中删除；解析器现在只
   接受 `id`、`type`、`game_context`、`frames`、`seconds`、`region_grid`、`crops`。
   CLI 也不再接受自定义系统提示文件，运行时固定逐字读取
   `prompts/generic/observation.md`。
2. `build_user_prompt()` 不再拼入题号。题号只进入 `ExamRecord`、CSV、Markdown 判卷表
   与汇总索引。假客户端测试逐题断言 user message 不含 `question_id`。
3. 人工语义区域文字已经移除。区域变体只会使用固定模板：
   `画面被划分为 16 行 9 列的网格。与上一采样帧相比，以下格子发生了变化：<坐标>。`
   测试同时拒绝「注意」「远处」「小目标」「对照题」「必须为空」进入 user message。
4. M5-T2.5 的人工直升机裁剪
   `frame-000092-helicopter-native.png` 已删除；`Test-Path` 实际返回 `False`。
5. 13 道题的 `game_context` 只出现四个纯游戏名：`Grey Zone Warfare`、
   `Disco Elysium`、`Slay the Spire 2`、`Subnautica 2`；两道无上下文题仍不含该字段。

静态检索：

```text
rg -n "prompt_override|region_hint|题号：|注意|远处|小目标|对照题|必须为空" data/generic/vision-exam/manifest.toml src/pet/games/generic/eval/vision_exam.py src/pet/games/generic/eval/region_assets.py
（无输出）

rg -n -- "--prompt|with-region-hint|without-region-hint" src/pet/games/generic/eval
（无输出）
```

### 机械生成口径与实际运行

复现脚本为 `src/pet/games/generic/eval/region_assets.py`。它实例化现有
`FrameChangeDetector(block_grid=(9, 16))`；参数顺序是“列、行”，因此实际网格为规格
要求的 16 行 × 9 列。缩放宽度、灰度算法与块阈值均沿用检测器，其中块平均差阈值为
12。脚本逐块计算坐标后，还断言坐标数量占比与检测器返回的 `block_change` 完全一致。

裁剪是变化格子的原生分辨率外接矩形，在四边增加整帧对应方向 2% 的固定边距后夹紧
到图像边界。`CROP_MARGIN_RATIO = 0.02` 写在脚本中。没有格子或没有严格相邻前帧时，
脚本不生成裁剪。运行命令：

```text
.venv\Scripts\python.exe -m pet.games.generic.eval.region_assets data/generic/vision-exam/manifest.toml --write-crops
```

实际输出整理如下；坐标列表没有人工增删：

| 题号 | 所用前一帧 | 实际变化格子 | 裁剪像素范围 `(左,上,右,下)` |
|---|---|---|---|
| `gzw-helicopter-landing` | 无：未落盘 `frame-000091` | 无 | 无 |
| `disco-dialogue-panel` | `frame-000034-20260823T191026.342745Z.png` | `r1c6、r1c7、r1c8、r1c9、r2c2、r2c3、r2c4、r2c6、r2c9、r3c1、r3c2、r3c3、r3c4、r3c5、r3c6、r3c9、r4c1、r4c2、r4c3、r4c4、r4c5、r4c6、r4c9、r5c1、r5c2、r5c3、r5c4、r5c5、r5c6、r5c9、r6c1、r6c2、r6c3、r6c4、r6c5、r6c6、r6c7、r6c9、r7c1、r7c2、r7c3、r7c4、r7c5、r7c6、r7c7、r7c9、r8c1、r8c2、r8c3、r8c4、r8c5、r8c6、r8c7、r8c8、r8c9、r9c1、r9c2、r9c3、r9c4、r9c5、r9c6、r9c7、r9c8、r9c9、r10c1、r10c2、r10c3、r10c4、r10c5、r10c6、r10c7、r10c9、r11c1、r11c2、r11c3、r11c4、r11c5、r11c6、r11c7、r11c8、r11c9、r12c1、r12c2、r12c3、r12c4、r12c5、r12c6、r12c8、r12c9、r13c2、r13c3、r13c4、r13c5、r13c6、r13c8、r13c9、r14c2、r14c3、r14c4、r14c5、r14c6、r14c7、r14c9、r15c2、r15c3、r15c4、r15c5、r15c7、r15c8、r16c2、r16c3、r16c4、r16c5` | `(0, 0, 2560, 1440)` |
| `gzw-near-black` | 无：当前是首帧 | 无 | 无 |
| `spire-combat-ui` | `frame-000057-20260824T001852.718566Z.png` | `r4c5、r5c4、r5c5、r6c4、r6c5、r7c2、r7c4、r7c5、r7c6、r8c2、r8c4、r8c5、r8c6、r8c7、r9c2、r9c3、r9c4、r9c5、r9c7、r10c2、r10c5、r11c5、r12c4、r12c5、r13c5、r14c4、r14c5、r15c4、r15c5、r15c6、r16c4、r16c5` | `(233, 241, 2044, 1440)` |
| `subnautica-night-underwater` | `frame-000012-20260823T223850.437531Z.png` | `r1c4、r1c5、r1c6、r2c5、r2c6、r3c5、r3c6、r3c7、r3c9、r4c4、r4c5、r4c6、r4c7、r5c5、r5c6、r5c7、r6c5、r6c6、r6c7、r6c8、r7c5、r7c6、r7c7、r7c8、r8c5、r8c6、r8c7、r8c8、r9c5、r9c6、r9c7、r9c8、r10c6、r10c7、r10c8、r11c7、r11c8、r12c6、r12c7、r13c4、r13c5、r13c6、r13c7、r14c4、r14c5、r14c6、r14c7、r15c3、r15c4、r15c7、r15c9、r16c3、r16c4、r16c7` | `(517, 0, 2560, 1440)` |
| `gzw-static-control-a` | 无：当前是首帧 | 无 | 无 |
| `gzw-static-control-b` | 无：未落盘 `frame-000030` | 无 | 无 |
| `gzw-rain-small-helicopter` | 无：未落盘 `frame-000091` | 无 | 无 |
| `subnautica-wave-surface` | 无：未落盘 `frame-000045` | 无 | 无 |
| `disco-task-switch-sequence` | `frame-000044-20260823T201139.413813Z.png` | `r5c1、r5c2、r5c3、r5c5、r5c6、r8c1、r8c2、r8c3、r9c1、r9c2、r9c3、r10c1、r10c2、r10c3` | `(0, 331, 1758, 929)` |
| `subnautica-wave-sequence` | `frame-000007-20260823T223148.805555Z.png` | `r6c1、r7c1、r7c2、r7c3、r7c4、r8c1、r8c2、r8c3、r8c4、r8c6、r9c1、r9c2、r9c3、r9c4、r9c5、r9c6、r9c7、r9c8、r9c9、r10c1、r10c2、r10c3、r10c4、r10c5、r10c6、r10c7、r10c8、r10c9、r11c1、r11c2、r11c3、r11c4、r11c5、r11c6、r11c7、r11c8、r11c9、r12c1、r12c2、r12c3、r12c4、r12c5、r12c6、r12c7、r12c8、r12c9、r13c1、r13c2、r13c3、r13c4、r13c5、r13c8、r13c9、r14c1、r14c2、r14c3、r14c4、r14c5、r14c6、r14c7、r14c8、r14c9、r15c1、r15c2、r15c3、r15c4、r15c5、r15c6、r15c8、r15c9、r16c1、r16c2、r16c3、r16c4、r16c5、r16c6、r16c7、r16c9` | `(0, 421, 2560, 1440)` |
| `spire-combat-ui-nocontext` | `frame-000057-20260824T001852.718566Z.png` | 同 `spire-combat-ui`；机械复算结果逐项一致 | `(233, 241, 2044, 1440)` |
| `subnautica-night-underwater-nocontext` | `frame-000012-20260823T223850.437531Z.png` | 同 `subnautica-night-underwater`；机械复算结果逐项一致 | `(517, 0, 2560, 1440)` |

脚本写出 7 个题目裁剪引用，对应 5 张唯一 PNG；尺寸与字节数实测：

```text
frame-000035-20260823T191028.340744Z-grid-crop.png size=(2560, 1440) bytes=3765536
frame-000045-20260823T201141.415143Z-grid-crop.png size=(1758, 598) bytes=517719
frame-000008-20260823T223150.809244Z-grid-crop.png size=(2560, 1019) bytes=1960868
frame-000013-20260823T223852.427674Z-grid-crop.png size=(2043, 1440) bytes=2031989
frame-000058-20260824T001854.718295Z-grid-crop.png size=(1811, 1199) bytes=1688995
```

这些文件均由 `.gitignore` 的 `backend/recordings/` 规则命中，不进入仓库。对话题的
机械外接范围覆盖整帧，水面题也保留大块无关背景；两者均未人工收窄。

### Manifest 解析

```text
.venv\Scripts\python.exe -c "... load_manifest(Path('data/generic/vision-exam/manifest.toml')) ..."
manifest_ok questions=13 region_grid=7 crops=7
fields=id,type,game_context,frames,seconds,region_grid,crops
mechanical_match questions=13 with_grid=7 unique_crops=5
```

### M5-T2.6 测试

```text
.venv\Scripts\python.exe -m pytest tests/test_vision_exam.py tests/test_vision_exam_region_assets.py tests/test_llm.py tests/test_layering.py -q --basetemp .pytest-m5-t2-6-final
47 passed, 1 warning in 0.56s

.venv\Scripts\python.exe -m pytest tests/ -q --basetemp .pytest-m5-t2-6-full
463 passed, 4 failed, 3 warnings in 16.49s
```

4 项失败仍全部是 `tests/test_speech.py` 的既有真实 OneCore 中文语音环境项；当前执行
环境无法枚举已安装中文语音。其余测试（含 `test_layering.py`）全部通过。

### 分层说明

生成器必须调用主干现有 `FrameChangeDetector`，因此分层警报器增加了一个精确到文件的
只读例外：仅 `games/generic/eval/region_assets.py` 可 import `pet.core.capture`。其他
游戏生产代码、其他评测工具和其他 core 模块许可均未放宽。

### M5-T2.6 偏差与未完成项

- 无规格偏差。6 道无 `region_grid` 的题均因不存在严格相邻的落盘前帧，不是检测器
  计算失败，也没有用较早的兜底帧冒充前一采样帧。
- 本任务未调用模型、未联网、未评分；产品负责人修订答案草稿与正式跑卷仍属后续流程。

## M5-T2.7：整图上传与稀疏区域提示

本节覆盖前文所有涉及旧图像附件变体的操作建议；旧段落只保留为历史验收记录。
当前每次请求永远只上传题目的完整画面。变体轴只有上传宽度和区域提示模式：

- 上传宽度：可重复传 `--send-width`；`0` 表示原生分辨率，正整数表示按该宽度等比缩放。
- 区域模式：可重复传 `--region-mode off|sparse|always`。
- `sparse` 仅在 `变化格子数 / 144 <= --region-sparsity-max` 时注入中性格子模板。
  默认上限 0.25 是待实测初值，不代表已选定阈值。
- `always` 是“即使区域很密也发送”的干扰对照；`off` 从不发送区域信息。

### 旧机制清除与整图尺寸验证

`ExamQuestion`、manifest 解析、消息构造、CLI、变体名和机械生成器均已移除旧的第二类
图像附件。此前位于 Git 忽略目录下的 5 张 `*-grid-crop.png` 已逐个删除；正式截图
没有删除。当前静态检索范围是生产考卷代码、正式 manifest、合成 manifest 与考卷测试：

```text
rg -n "crops|with-crops|without-crops|write-crops|grid-crop|crop_bounds|crop_paths" src/pet/games/generic/eval tests/test_vision_exam.py tests/test_vision_exam_region_assets.py data/generic/vision-exam/manifest.toml tests/fixtures/vision-exam-example.toml
（无输出）
```

测试通过真实 `OpenRouterClient` + `httpx.MockTransport` 解码实际消息体中的 base64 PNG，
同一张 20×10 合成图得到：`--send-width 0 -> 20×10`、`10 -> 10×5`、
`40 -> 40×20`。因此断言的是实际上传内容，不是仅检查声明字段。

### 13 题变化格子占比与默认抑制结果

占比由正式 manifest 中机械生成的 `region_grid` 数量除以固定 16×9=144 格得出；
无严格相邻前帧的题没有客观格子数据，在 `sparse` 和 `always` 下都不会注入。

| 题号 | 变化格子 | 占比 | `sparse`（0.25） |
|---|---:|---:|---|
| `gzw-helicopter-landing` | 无数据 | 无数据 | 不注入 |
| `disco-dialogue-panel` | 113 | 0.784722222 | 抑制 |
| `gzw-near-black` | 无数据 | 无数据 | 不注入 |
| `spire-combat-ui` | 32 | 0.222222222 | 注入 |
| `subnautica-night-underwater` | 54 | 0.375000000 | 抑制 |
| `gzw-static-control-a` | 无数据 | 无数据 | 不注入 |
| `gzw-static-control-b` | 无数据 | 无数据 | 不注入 |
| `gzw-rain-small-helicopter` | 无数据 | 无数据 | 不注入 |
| `subnautica-wave-surface` | 无数据 | 无数据 | 不注入 |
| `disco-task-switch-sequence` | 14 | 0.097222222 | 注入 |
| `subnautica-wave-sequence` | 78 | 0.541666667 | 抑制 |
| `spire-combat-ui-nocontext` | 32 | 0.222222222 | 注入 |
| `subnautica-night-underwater-nocontext` | 54 | 0.375000000 | 抑制 |

默认阈值下明确被稀疏度规则抑制的 4 题是 `disco-dialogue-panel`、
`subnautica-night-underwater`、`subnautica-wave-sequence`、
`subnautica-night-underwater-nocontext`。另有 6 题因没有严格相邻前帧而没有区域信息；
这不是“占比为零”，CSV 中对应占比留空。

生产解析器实际输出：

```text
manifest_ok questions=13
disco-dialogue-panel grid=113 fraction=0.784722222 sparse_0.25=suppressed
spire-combat-ui grid=32 fraction=0.222222222 sparse_0.25=inject
subnautica-night-underwater grid=54 fraction=0.375000000 sparse_0.25=suppressed
disco-task-switch-sequence grid=14 fraction=0.097222222 sparse_0.25=inject
subnautica-wave-sequence grid=78 fraction=0.541666667 sparse_0.25=suppressed
spire-combat-ui-nocontext grid=32 fraction=0.222222222 sparse_0.25=inject
subnautica-night-underwater-nocontext grid=54 fraction=0.375000000 sparse_0.25=suppressed
```

### 当前跑卷建议

建议仍选 3 个不同成本/能力档位，不在代码或报告中写死型号。每个档位对全部 13 题跑：

```powershell
.venv\Scripts\python -m pet.games.generic.eval.vision_exam `
  data\generic\vision-exam\manifest.toml `
  --model "<候选模型或 profile:档位>" `
  --price "<同一目标>=<输入百万token美元>,<输出百万token美元>" `
  --region-mode off --region-mode sparse --region-mode always `
  --send-width 1280 --send-width 0 `
  --region-sparsity-max 0.25
```

这构成 6 个变体。正式卷每个档位实际调用 `13 × 6 = 78` 次；3 个档位共 234 次。
每个档位共上传 90 张完整画面附件（11 道 single × 6 + 2 道双帧 sequence × 6），
3 个档位共 270 张附件。参考既有短 JSON 预算并考虑原生图视觉 token 的模型差异，
整轮输入约为 20 万至 90 万 token 等价值、输出约 2 万至 7 万 token；这只是跑卷前
量级预算，不是计费结果。单价留空到执行当天填写，实际 token、延迟和花费以
`run.json` 为准。

假客户端用 2 道合成题完整跑过 3×2 组合，实际产生 12 次调用和 12 行 CSV；
`results.csv` 会逐行记录上传宽度、区域模式、格子占比、是否实际注入、每张实际上传
PNG 的像素尺寸与字节数。`report.md` 与 `run.json` 另按上传宽度汇总延迟中位/P90 和
平均输入 token，用来比较原生分辨率的额外延迟与成本。

### M5-T2.7 测试

```text
.venv\Scripts\python -m pytest tests/test_vision_exam.py tests/test_vision_exam_region_assets.py tests/test_llm.py tests/test_layering.py -q --basetemp .codex-tmp\pytest-targeted-2 -p no:cacheprovider
51 passed in 4.74s

.venv\Scripts\python -m pytest tests/ -q --basetemp .codex-tmp\pytest-full -p no:cacheprovider
467 passed, 4 failed, 1 warning in 20.11s
```

4 项失败仍全部是 `tests/test_speech.py` 的既有真实 OneCore 中文语音环境项，报错为当前
执行环境无法枚举已安装中文语音。其余测试（包括 `test_layering.py`）全部通过。

### M5-T2.7 偏差与未完成项

- 无已知规格偏差；没有改观察提示词、中性区域模板、题目选帧、截屏或变化检测算法。
- 本任务未调用模型、未联网、未评分；参考答案仍须产品负责人离线复核，正式跑卷尚未开始。

## M5-T2.8：权威答案与快/深双输出

本节覆盖前文的卷面数量、输出格式与跑卷建议。正式卷现为 11 题：8 道 single、
3 道 sequence；生产始终免费取得游戏名，因此两道无上下文对照已经删除。

### 变更文件清单

- `data/generic/vision-exam/answer-key.md`：合并产品负责人快/慢答案，增加【核心】、
  【细节】、【存疑】与两线判卷规则，重排静止题，删除无上下文题。
- `data/generic/vision-exam/manifest.toml`：静止单帧改名并新增静止双帧题，删除两道
  无上下文题；其他题目的选帧和区域格子不变。
- `prompts/generic/observation-fast.md`、`observation-deep.md`：新增跨游戏纯文本提示词；
  原 `observation.md` 删除。
- `src/pet/games/generic/eval/vision_exam.py`：新增输出模式轴、固定输出预算、纯文本结果、
  CSV 字段及按输出模式汇总。
- `tests/test_vision_exam.py`：覆盖双提示词、固定 token、纯文本、剪枝组合、静止题时间、
  答案标签和本地录制缺失时跳过。
- `audit/m5-t2-vision-exam-report.md`：本节。

### 答案合并规则与完整样例

快回答中的内容标为【核心】；慢回答中快回答未覆盖的内容标为【细节】；现实世界型号
比附统一标为【存疑】。快线只按核心判覆盖率，深线按核心+细节；存疑不计分。
UI 布局与第一人称持枪视角不加分不扣分，“不得出现的内容”对两线相同。

`gzw-static-single` 的“产品负责人判定”完整内容如下：

```text
- 【核心】玩家位于一条铺砌道路上，前方远处有一名 NPC 站立。
- 【核心】右侧为混凝土建筑与植被花坛，左侧有围墙和堆叠的木箱/货箱，背景是阴天下的山林。
- 【核心】UI 显示距离目标 58 米，罗盘指向南/西南；画面左下角标注“EA Pre-Alpha”。
- 【细节】场景位于被植被环绕的混凝土建筑群外；右侧近景是种有棕榈植物和红白花卉的花坛及灰色墙体。
- 【细节】左侧中景可见铁丝网围墙、简易棚屋及大量堆叠的棕色补给箱，远景为雾气笼罩的茂密山林。
- 【细节】顶部 UI 显示 58 米处有友方单位，罗盘为南偏西（约 210—240 度）；左上角姿态图标显示玩家站立，另有“71米”参数；版本信息为“EA Pre-Alpha | 0.4.7.2”。
```

现实型号降级的已知命中包括：直升机“类似 UH-60 或 AS332”，以及两处 EOTech
瞄具品牌判断。它们均只在【存疑】中出现，不进入覆盖率。

### 合并中保留的差异

- `gzw-helicopter-landing`：产品负责人快回答写直升机“停着”，慢回答写“在空中
  飞行/悬浮”；原离线复核也无法确定即时运动。两种原文均保留，不要求唯一阶段。
- `gzw-near-black`：产品负责人判定为高对比度夜视效果；原离线复核没有看见绿色
  夜视成像。判卷采用产品负责人描述，但不据此扩展夜视仪开关状态。
- `spire-combat-ui`：产品负责人把 6/6 识别为右侧敌人的蓝色能量条，并称中央防御牌
  “被选中”；原离线复核曾把 6/6 读作左侧单位且无法区分卡牌交互状态。均已移入
  不确定项并注明以产品负责人为准。
- `gzw-rain-small-helicopter`：快回答写直升机“停着”，慢回答确认低空作业、旋翼与
  下洗水雾，并推测刚着陆或即将起飞；不要求模型确定唯一飞行阶段。
- `disco-task-switch-sequence`：产品负责人用口语写“约3秒”，文件名实算为
  `2.001330` 秒。请求时间轴采用机械值，判卷不因口语近似扣分。

### 静止题重排与时间计算

`gzw-static-control-a` 改为 `gzw-static-single`，仍只含首帧；原 a、b 两帧组成
`gzw-static-sequence`，原 b 单帧题删除。文件名 UTC 时间相减：

```text
2026-08-23T17:53:06.283718Z - 2026-08-23T17:52:06.287712Z
= 59.996006 秒
```

产品负责人补充说明：画面左上 UI 显示的是游戏内经过 8 分钟；上述 `59.996006` 秒是
现实采样时间，两者不冲突。manifest 实际解析结果：

```text
manifest_ok questions=11 single=8 sequence=3
gzw-static-single type=single seconds=(0.0,) frames=1
gzw-static-sequence type=sequence seconds=(0.0, 59.996006) frames=2
```

### 快/深输出与纯文本

- `fast` 逐字加载 `observation-fast.md`，`max_tokens=60`。
- `deep` 逐字加载 `observation-deep.md`，`max_tokens=1600`。
- 两个预算均是“初始值，待考卷实测修订”。
- 两线都返回纯文本。JSON 外壳实测约占 25—30 输出 token、约 0.7 秒，快线预算
  无法承担；游戏身份也已由窗口标题查表取代。
- 静态检索 `game_guess|notable_events|confidence` 在考卷代码和两份提示词中无输出。

`results.csv` 当前表头与一条假客户端实测样例如下：

```text
题号,题型,变体,输出模式,max_tokens,上传宽度,区域提示模式,本题变化格子占比,本次是否实际注入了提示,本次实际上传的图像像素尺寸,本次实际上传的图像字节数,目标档位,请求模型,实际模型,服务商,回答原文,错误原文,往返毫秒,输入token,实际输出token,配置折算花费美元,上游报告花费美元
synthetic-single,single,output-fast__region-off__width-1280,fast,60,1280,off,0.020833333,false,1280x960,123229,fake/model,fake/model,fake/actual,fake-provider,合成测试画面的亮区发生了变化。,,125.000,120,30,0.000180000,0.009000000
```

### 剪枝后的跑卷建议

不跑全笛卡尔积；每个候选档位只跑以下 4 个组合：

| 输出模式 | 上传宽度 | 区域模式 |
|---|---:|---|
| fast | 1280 | off |
| fast | 1280 | sparse |
| deep | 1280 | off |
| deep | 原生（0） | off |

正式卷 11 题，因此每个档位 `11 × 4 = 44` 次；建议 3 个不同成本/能力档位，共
132 次调用。每个档位上传 56 张完整画面附件，3 个档位共 168 张。按 T2.7 同类视觉
输入预算线性折算，整轮输入约 10 万至 50 万 token 等价值；输出硬上限为
`3 × (11 × 2 × 60 + 11 × 2 × 1600) = 109,560` token，实际值由跑卷记录，
不预先编造。模型单价留空，执行当天通过 `--price` 填写。

CLI 的轴本身仍可组合；为避免误跑全积，建议把上述四组分为四次命令执行。假客户端
测试用同一组四个变体跑两道合成题，实际得到 8 次调用、8 行 CSV，并验证 fast/deep
各 4 次。无上下文题已经删除，不再有额外分支。

### 可移植性

依赖正式截图的答案格子一致性测试会先检查 manifest 中的录制文件。缺文件时以
“该测试依赖产品负责人本机的录制数据”明确跳过；合成缺文件测试捕获并断言了
`pytest.skip.Exception`。其他结构、提示词、CLI 与假客户端测试只依赖提交内 fixture。

### M5-T2.8 测试

```text
.venv\Scripts\python -m pytest tests/test_vision_exam.py tests/test_vision_exam_region_assets.py tests/test_llm.py tests/test_layering.py -q --basetemp .codex-tmp\pytest-targeted-final -p no:cacheprovider
53 passed in 5.26s

.venv\Scripts\python -m pytest tests/ -q --basetemp .codex-tmp\pytest-full-final -p no:cacheprovider
469 passed, 4 failed, 1 warning in 22.64s
```

4 项失败仍全部是当前机器无法枚举 OneCore 中文语音导致的既有环境项；其余测试，
包括 `test_layering.py`，全部通过。

### M5-T2.8 偏差与未完成项

- 无已知规格偏差。
- 本任务没有调用模型、没有联网、没有评分；正式跑卷与人工判卷尚未开始。
