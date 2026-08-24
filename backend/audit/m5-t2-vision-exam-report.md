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
