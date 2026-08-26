# M5-T7 检测器参数落地与通用视觉接管线完成报告

统计日期：2026-08-26（America/New_York）

## 交付结果

通用视觉适配已作为端口 v1 的内置适配注册，但默认关闭。只有同时设置
`[active].game = "generic"` 与 `[games.generic].enabled = true` 才会初始化 WGC、
抓取前台目标窗口并调用快线视觉档位。本任务没有递交任何 `SpeechRequest`。

检测器生产默认值已按 M5-T6 带约束筛选落地：`noise_window=10`、
`noise_multiplier=1.2`、`noise_margin=4/255`、`persistence_polls=1`。依据为相对旧值
字幕局部剧变漏检 3→0、A-world 命中 14→19、灰区暴雨静止上传率保持 2.0%。

## 变更文件

- `AGENTS.md`：产品负责人提供的 567 行新版，原文随提交带上，coding agent 未改写。
- `backend/config.toml`：新增默认关闭的通用视觉段与 `vision_fast` 档位。
- `backend/data/generic/window-title-map.toml`：真实录过游戏及已有适配游戏的确定性标题表。
- `backend/src/pet/core/capture.py`：落地 N/k/margin 默认值，并引用唯一稀疏上限常量。
- `backend/src/pet/core/config.py`：通用视觉配置、档位单价字段、共享稀疏上限。
- `backend/src/pet/core/llm.py`：图像附件支持内存位图及 JPEG 编码；原文件路径/PNG 行为保持默认。
- `backend/src/pet/games/__init__.py`、`backend/src/pet/main.py`：注册并装载 generic 适配。
- `backend/src/pet/games/generic/adapter.py`：截屏、帧选取、并发快线、时序写入、状态与花费。
- `backend/src/pet/games/generic/eval/vision_exam.py`：复用同一个稀疏上限常量，数值未改。
- `frontend/src/backend-bridge.ts`、`frontend/src/watch-status.ts`：头顶观看指示与泛化状态/花费显示。
- `frontend/src-tauri/src/main.rs`：托盘“当前游戏”加入“通用视觉”，按后端状态勾选。
- `backend/tests/test_capture.py`、`test_config.py`、`test_llm.py`、`test_generic_adapter.py`：参数与生产链路回归。

## 转向后 10 秒窗口测量（技术债 17）

数据源：`explore-3a` 会话 `20260824-232313`，309 帧。该旧会话缺单调秒，工具按既有
兼容规则整段回退 UTC 墙钟并仅标注一次。鼠标位移 P90 阈值为 1027.792，共 31 个锚点
轮次；相邻锚点机械合并为 20 个转向段。每段从最后一个锚点之后取 10 秒，重叠帧在
汇总中只计一次。重放参数为 N=10、k=1.2、margin=4/255、P=1。

| 范围 | 帧数 | persistent_change | forced | suppressed_min_interval | no_change |
|---|---:|---:|---:|---:|---:|
| 整段 | 309 | 195（63.11%） | 1（0.32%） | 88（28.48%） | 25（8.09%） |
| 转向后窗口并集 | 128 | 82（64.06%） | 0（0.00%） | 40（31.25%） | 6（4.69%） |

20 段逐段的 `no_change` 数依次为：0、0、0、0、0、0、0、0、0、0、1、1、1、0、0、3、
0、0、1、0。该测量没有观察到转向后窗口的 `no_change` 比整段更高，因此本会话不支持
“转视角后噪声地板恢复造成额外盲区”的假设；但本结果只是一段录制，不据此删除技术债。
`suppressed_min_interval` 表示已经检测出变化、但因 1 秒最小上传间隔防连拍而没有上传，
不等同于检测器漏检。本测量只报告，不改变任何生产行为。

## 关键实现决策

1. `games/generic/adapter.py` 只依赖端口允许的 core 模块。WGC 与选择器工厂由内置注册表
   注入，既复用主干能力，又保持 `test_layering.py` 的游戏适配边界。
2. 截图先在内存等比缩到 896 宽并编码为 JPEG，再构造 OpenAI 兼容图像内容块；生产
   适配没有截图落盘调用。`record-all` 仍只属于前台探针。
3. 每个被选帧立即成为独立在飞任务，最多四个。超时、调用错误和并发已满都写成 dropped
   记录，不重试、不排队补偿。连续十次真实调用失败只标 degraded，主循环继续运行。
4. 每个任务携带帧的单调秒；完成结果先进入重排缓冲，只有更早帧已完成或已丢弃后才写
   JSONL，避免模型返回顺序污染观察时间线。
5. 最近五条成功观察以文字形式加入下一帧上下文；区域格子只采用选择器已经执行过
   0.25 稀疏抑制后的结果。上传决定与提示决定仍互相独立。
6. 花费完全按 `vision_fast` 档位配置的输入/输出单价与上游 token 计数累计。折算时速
   越过警戒值只记录 WARNING 与状态标记，不熔断。

## 验证

- 导入来源：`backend/src/pet/__init__.py`，确认测试使用当前工作区。
- 后端聚焦回归：103 passed。
- 后端全量：552 passed；4 项失败均为本机未安装 OneCore 中文语音的既有环境项。
- `test_layering.py`：通过，包含在上述测试中。
- 前端：`npm.cmd run build`、`npm.cmd run format:check`、`npm.cmd test -- --run` 通过。
- Tauri：`cargo check`、`cargo fmt --check` 通过。
- 日志目录忽略验证：`git check-ignore` 命中根 `.gitignore` 的 `backend/recordings/`。
- AGENTS 核对：第二行是“最后更新：M5-T7 下发前（T6 已验收，检测器参数定案，进入接管线阶段）”，总行数 567。

## 产品负责人真机自查

1. 在同一个 PowerShell 中设置 `OPENROUTER_API_KEY`，不要把密钥写进任何文件。
2. 临时把 `backend/config.toml` 的 `[active].game` 改为 `"generic"`，并把
   `[games.generic].enabled` 改为 `true`。默认提交值仍是关闭。
3. 后端运行：`cd backend`，执行 `.venv\Scripts\python -m pet.main`。
4. 另开 PowerShell：`cd frontend`，执行 `npm.cmd run tauri dev`。
5. 切到要测试的游戏并玩约 10 分钟。宠物头顶应持续显示“正在观看：<游戏名>”；
   托盘“当前游戏”应勾选“通用视觉”，状态行应显示本会话累计美元花费。
6. 停止后打开最新的
   `backend/recordings/observation/<会话时间戳>/observations.md`，逐行判断观察是否对应
   当时画面。机器原始记录在同目录 `observations.jsonl`，参数、丢弃、失败率与花费在
   `session.json`。
7. 用任务管理器结束游戏窗口或退出后端，确认 WGC 会话立即释放。日志目录只含文字，
   不应出现 PNG/JPEG 截图。

## 偏差、原因与未完成项

- 规格偏差：无。
- 本次自动化没有替产品负责人完成真实游戏十分钟的主观验收，也没有主动上传其游戏画面；
  这一步必须由产品负责人显式开启默认关闭的适配后执行。代码层以假截屏与假 LLM 完整
  验证，未产生付费调用。
- OneCore 中文语音环境项 4 个仍失败；本任务未修改语音代码或测试。
