# M5-T1 / M5-T1.5 窗口截屏与多指标画面变化检测报告

统计日期：2026-08-23

## 截屏后端选型

选型停在第一步 Windows Graphics Capture，没有降级到 DXGI Desktop Duplication、
dxcam 或 mss。

- 先检查了 `windows-capture 2.0.1`。它支持按 HWND 捕获窗口且有 Python 3.12
  可用 wheel，但当前包强制依赖 OpenCV；OpenCV 不在任务允许的新依赖清单内。
- 最终采用等价 WGC 绑定 `zbl 0.7.1`。它有 CPython 3.12 / Windows x64 wheel，
  支持按 HWND 精确选择单窗口，只依赖 NumPy，不需要桌面或显示器捕获。
- `numpy 2.5.2` 用于接收 WGC 像素缓冲与计算归一化平均像素差；现有依赖没有
  数组缓冲与向量化像素运算能力。
- `Pillow 12.3.0` 用于缩放、灰度化和 PNG 编码；现有依赖没有图像编解码能力。

运行时只向 WGC 传入一个 Windows 窗口句柄。初始化失败会说明需要 Windows 10
1903+、本地交互桌面、已安装依赖，以及探针与游戏权限级别需一致，不会静默回退
到全桌面截屏。

### 2026-08-23 实机问题后的取帧修正

13 次实机采样中的 Project Zomboid 样本复现了最长 89.537 秒和 49.028 秒的
单次抓帧阻塞。代码与 `zbl 0.7.1` 上游实现复核后确认有两个通用集成问题：

- 原实现调用阻塞式 `grab()`；WGC 暂无新帧时，上游会循环等待，探针的采样、
  CSV、最长静默兜底和 Ctrl+C 都无法继续。
- 上游另有 32 帧 FIFO；队列满时丢新帧。探针每 2 秒只消费一帧会积压旧画面，
  取到的并不保证是当前最新画面。

修正后只调用现有依赖提供的非阻塞 `try_grab()`：每次轮询排空有界队列，最多
取出 64 个源帧并只保留最后一个；没有帧时立即返回并在 CSV 留下“WGC 暂无新帧”，
下一轮照常进行。64 的上限来自已固定版本的 32 帧队列容量，允许排空期间一次
并发回填，同时保证活跃画面不能让排空循环无限运行。没有新增依赖，没有按游戏名
分支，也没有改变六指标、阈值、基线或落盘策略。

本修正消除了应用层的无限等待和旧帧逐张回放，但不能保证 WGC 对所有游戏呈现路径
都会持续交付源帧。底层暂停交帧的触发条件，以及一次用户可见的游戏内光标消失，
仍需修正后用真实游戏 A/B 验证，不能仅凭代码推断为同一个问题。

## 代码层验证边界

自动测试全部使用合成图像与 pytest 临时目录，不调用真实截图。除 M5-T1 的相同
画面、轻微噪声、场景切换、落盘上限和非 Windows 错误外，M5-T1.5 还验证了：
低幅环境动效、15% 面积文字弹出、单一平均振幅的区分缺陷、双基线语义、最短
落盘间隔、最长静默强制落盘、CSV 即时 flush、session.json 字段及默认策略兼容性。

M5-T1.5 初始交付时 coding agent 没有在真实游戏上运行探针。此后产品负责人已完成
Grey Zone Warfare、Disco Elysium、Subnautica 2、Project Zomboid 共 13 次会话，
并补测 2.0 / 1.0 秒轮询的 CPU 与内存；上下文、指标与逐帧人工复核集中记录在
`audit/m5-t1-field-test-results.md`。仍不能由代码检查代替的项目包括独占全屏覆盖、
黄色捕获边框、各游戏开启探针前后帧率，以及本次非阻塞修正后的重新实测。

自动验收的实际记录：

```text
.venv\Scripts\python -m pytest tests/test_capture.py tests/test_layering.py -q --basetemp .pytest-m5-t15-review-targeted
20 passed

.venv\Scripts\python -m pytest tests/ -q --basetemp .pytest-m5-t15-final-full
431 passed, 4 failed
```

全量测试的 4 项失败均在 `tests/test_speech.py`，原因是受限执行环境无法枚举已安装的
OneCore 中文语音，与本任务前的环境项一致。新增测试、分层测试和其他回归全部通过。

本次非阻塞取帧修正后的实际记录：

```text
.venv\Scripts\python -m pytest tests/test_capture.py tests/test_layering.py -q
24 passed

.venv\Scripts\python -m pytest tests/ -q
435 passed, 4 failed
```

4 项失败仍全部为上述 OneCore 中文语音环境项。`compileall`、`git diff --check` 和
下方网络模块静态检索均通过。

网络模块静态检索命令与实际输出：

```text
rg -n "^\s*(from|import)\s+(httpx|socket|urllib|requests|aiohttp|websockets)(\.|\s|$)" src/pet/core/capture.py
（无输出）
```

依赖一致性检查 `python -m pip check` 的实际输出为
`No broken requirements found.`。

## M5-T1.5 指标与性能实测

`FrameChangeDetector` 本身仍无状态。它对任意一对可注入图像计算
`mean_amplitude`、`changed_area`、`block_change`；探针外层分别保存上一轮询帧与
上一次被策略判为变化并落盘的帧，因而每轮产生 `vs_previous`、`vs_baseline`
两组、合计六个数。最长静默产生的强制帧不重置变化基线。
默认落盘策略仍是 `mean_amplitude_vs_previous`、阈值仍是 `0.02`。默认 2 秒轮询
也长于新增的 1 秒最短落盘间隔，因此未触发最长静默时，逐帧落盘时机与 M5-T1
一致。三个指标及两种基线一共只有六种组合；任务文字中的“九种”与列出的数学
组合不一致，本实现没有虚构另外三种策略。

性能测量脚本：`audit/measure_capture_metrics.py`。它先预热 5 次，再对 50 张
1920x1080 RGB 合成图逐张计时；计时范围包括当前帧的两种灰度缩图，以及相对两种
基线的六个指标，不包括合成图生成。实际输出：

```text
Input: 1920x1080 synthetic RGB image
Warm-up runs: 5; measured runs: 50
Six-metric median: 7.368 ms
Six-metric maximum: 8.322 ms
```

中位数 7.368 ms，低于 50 ms 约束；没有为了达标降低 96 / 320 的默认宽度。

## 产品负责人实测指引

### 怎么运行

打开 PowerShell：

```powershell
cd backend
.venv\Scripts\python -m pet.core.capture --watch --title "窗口标题的一部分"
```

建议优先使用 `--title`，例如填游戏窗口标题的一小段。若不传 `--title`，探针会给
3 秒让你切回游戏，然后锁定当时的前台窗口。启动横幅会显示保存目录和当前策略；
按 Ctrl+C 停止，终端会打印六项指标的中位数、P90、P99、最大值，落盘与强制
落盘次数，以及指标计算、抓帧耗时。

调阈值或间隔：

```powershell
.venv\Scripts\python -m pet.core.capture --watch --title "窗口标题" --interval 2.0 --threshold 0.02 --label "3A-全屏-默认策略"
```

选择其他落盘策略时，把三个指标名与两个基线名组合：

```powershell
.venv\Scripts\python -m pet.core.capture --watch --title "窗口标题" --strategy changed_area_vs_baseline --threshold 0.05 --min-save-interval 1.0 --max-silence 60.0 --label "策略游戏-变化面积-基线"
```

可用指标名为 `mean_amplitude`、`changed_area`、`block_change`；基线名为
`vs_previous`、`vs_baseline`，共六种策略。`--min-save-interval` 防止连续落盘；
`--max-silence` 即使一直没判成变化，也会定时强制保留一帧并标记
`forced=true`。先用默认值采一轮，不要把初始参数当成已定结论。

截图默认保存在 `backend/recordings/capture/<启动时间>/`，已被 Git 忽略。单次目录
最多保留 500 张或 200MB，先触及哪个上限就从最旧 PNG 开始删除。截图可能含私人
画面，检查完请按自己的隐私需要处理该目录。

每个保存目录还包含：

- `metrics.csv`：每次轮询（包括 WGC 暂无新帧）立即写一行并 flush。中文字段依次为序号、时间、
  窗口标题、平均振幅/变化面积/块变化各自对比上一帧和落盘基线的六个值、是否
  落盘、是否强制落盘、落盘文件名、指标计算耗时毫秒、是否取得画面、本次取出
  源帧数、本次抓帧耗时毫秒和截屏状态。无新帧行的六指标留空，不伪装成零变化。
- `session.json`：记录 `--label`、启动参数、开始与结束时间、总轮询数及退出汇总。
  如果进程异常退出，已 flush 的 CSV 行仍在；session.json 至少保留启动信息，
  只有正常退出或 Ctrl+C 才有结束时间与最终汇总。

### 测哪三类游戏

每类至少运行 5 分钟，分别覆盖静止菜单、普通游玩和大幅场景切换：

1. 3D 全屏大作：分别试独占全屏（若游戏提供）与无边框窗口。
2. 策略游戏：观察地图静止、单位小范围移动、打开大型面板三种状态。
3. 2D 独立游戏：观察像素动画、小范围角色移动、整屏换场三种状态。

### 每类都记录什么

- 后端是否初始化成功；失败时完整抄下终端的人话错误。
- 保存目录是否主要是首帧和肉眼可见变化帧；静止时只有每到 `--max-silence`
  才出现一次 `forced=true` 的兜底帧，还是仍有大量非强制落盘。
- 任务管理器中该 Python 进程的 CPU 百分比（静止时与激烈变化时各记一次）。
- 游戏开启探针前后的帧率，是否有可感知掉帧或卡顿。
- 窗口或屏幕周围是否出现系统自带的黄色捕获边框；只记录，T2 再决定去留。
- 保存整次会话的 `metrics.csv`、`session.json`，并记录六项指标分布、指标计算和
  抓帧耗时；CSV 或截图可能含窗口标题与私人画面，不要上传到公开仓库。
- 同一段玩法分别试不同 `--strategy`，每次换一个 `--label`。若误报太多可逐步
  提高阈值，若明显变化不保存可逐步降低；保留每次值，不要只报最后结论。
  `0.02`、`24`、`12`、`16x9`、`96`、`320` 与 `2.0` 都是待实测初值，不是
  已选定的产品标准。本任务不根据合成数据宣布哪个指标更好。
