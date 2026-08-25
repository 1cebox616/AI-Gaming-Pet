# M5-T1 / M5-T1.5 / M5-T3 窗口截屏与帧选取报告

最后更新：2026-08-24

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
都会持续交付源帧。底层暂停交帧的触发条件不能仅凭代码推断。

修正后的 Project Zomboid 诊断 PZ-3 中，连续 41 次无新帧期间探针仍保持每 2 秒轮询，
单次抓帧最大 38.172ms，确认应用层无限等待已修复。诊断 PZ-4 又把 WGC 光标合成从
关闭改为开启，游戏内光标仍不可见，截图也不含光标；探针退出后光标立即恢复。
因此旧阻塞路径和光标合成开关都不是充分根因。现有证据只支持“Project Zomboid 与
当前 WGC/zbl 会话存在兼容性问题”，不能泛化到其他游戏或继续归因到某一组件。
全部 Project Zomboid 会话均为诊断数据，已迁至
`audit/m5-t1-project-zomboid-diagnostics.md`，并作为技术债交由后续跨游戏 A/B。

## 代码层验证边界

自动测试全部使用合成图像与 pytest 临时目录，不调用真实截图。除 M5-T1 的相同
画面、轻微噪声、场景切换、落盘上限和非 Windows 错误外，M5-T1.5 还验证了：
低幅环境动效、15% 面积文字弹出、单一平均振幅的区分缺陷、双基线语义、最短
落盘间隔、最长静默强制落盘、CSV 即时 flush、session.json 字段及默认策略兼容性。

M5-T1.5 初始交付时 coding agent 没有在真实游戏上运行探针。此后产品负责人完成了
Grey Zone Warfare、Disco Elysium、Subnautica 2 与 Slay the Spire 2 共 12 次正式
会话；另有 4 次 Project Zomboid 会话只用于兼容性诊断。正式上下文、指标与逐帧
人工复核记录在
`audit/m5-t1-field-test-results.md`，诊断记录单独存放。另补测了 2.0 / 1.0 秒轮询的
CPU 与内存。仍不能由代码检查代替的项目包括独占全屏覆盖、黄色捕获边框、各游戏
开启探针前后帧率，以及 Project Zomboid 光标问题的具体组件责任边界。Slay the
Spire 2 已作为第一组跨游戏负对照：相同 `capture_cursor=false` 下光标正常可见。

自动验收的实际记录：

```text
.venv\Scripts\python -m pytest tests/test_capture.py tests/test_layering.py -q --basetemp .pytest-m5-t15-review-targeted
20 passed

.venv\Scripts\python -m pytest tests/ -q --basetemp .pytest-m5-t15-final-full
431 passed, 4 failed
```

全量测试的 4 项失败均在 `tests/test_speech.py`，原因是受限执行环境无法枚举已安装的
OneCore 中文语音，与本任务前的环境项一致。新增测试、分层测试和其他回归全部通过。

当前实现（含非阻塞取帧与光标 A/B 参数）的实际记录：

```text
.venv\Scripts\python -m pytest tests/test_capture.py tests/test_layering.py -q --basetemp .pytest-m5-t1-architect-summary
25 passed

.venv\Scripts\python -m pytest tests/ -q --basetemp .pytest-m5-t1-pz-debt
436 passed, 4 failed
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

## M5-T3 自适应块检测与性能实测

设计原则：**本地帧选取的首要目标是不漏掉应上传的帧，次要目标才是丢弃冗余帧。**
因此下列值都是偏低的初始值，待新的全量录制用真实游戏重放校准，不是产品定值。

`FrameChangeDetector` 仍是无状态纯计算层，保留 M5-T1.5 的六个旧指标用于 CSV
对照。实际落盘改由 `AdaptiveFrameSelector` 决定：画面缩到宽 320 灰度后分为
16 行 × 9 列；每块相对“上一次真实变化并落盘的帧”计算平均绝对差。每块用最近
20 次差值的滚动中位数 × 2.5 + 4/255 建立噪声地板，连续 2 轮超过地板才确认。
确认块占比达到 0.35 视为镜头移动：照样保存，但不提供无意义的区域格子；其余
持久变化保存具体格子。最长静默强制帧不重置真实变化基线。

默认轮询间隔已从 2.0 秒改为 1.0 秒。`--strategy` 已删除；最短保存间隔 1.0 秒、
最长静默 60 秒两条兜底保留。第一帧没有可比较对象，仍保存用于建立基线，并在
闭集原因中记作 `forced`。每个后续保存帧都会额外保存紧邻的上一次成功轮询帧，
文件名带 `-prev`；首帧之前没有帧，因此首帧是唯一合理例外。

性能测量脚本为 `audit/measure_capture_metrics.py`：预热 5 次，再对 50 张
1920×1080 RGB 合成图计时。范围包括六个旧指标、块差、滚动噪声地板、持久性与
最终决策，不包括合成图生成。M5-T3 的最新实测输出记录在本节末尾；验收时没有
降低 96 / 320 的默认缩放宽度或 16×9 网格来达标。

```text
Input: 1920x1080 synthetic RGB image
Warm-up runs: 5; measured runs: 50
Six legacy metrics + adaptive block statistics median: 9.372 ms
Six legacy metrics + adaptive block statistics maximum: 11.761 ms
```

中位数 9.372 ms，低于 80 ms 验收上限。

M5-T3 验收命令的实际结果：

```text
.venv\Scripts\python -m pytest tests\test_capture.py tests\test_layering.py -q
33 passed

.venv\Scripts\python -m pytest tests\ -q
490 passed, 4 failed
```

4 项失败仍全部是 `tests/test_speech.py` 在本机找不到 OneCore 中文语音的既有环境项；
M5-T3 新增测试、分层测试和其余回归全部通过。

## 产品负责人实测指引

### 怎么运行

打开 PowerShell：

```powershell
cd backend
.venv\Scripts\python -m pet.core.capture --watch --title "窗口标题的一部分" --record-all --label "游戏-场景"
```

建议优先使用 `--title`，例如填游戏窗口标题的一小段。若不传 `--title`，探针会给
3 秒让你切回游戏，然后锁定当时的前台窗口。默认每 1.0 秒轮询一次。建议本轮
校准都加 `--record-all`：横幅会显眼提示正在保存每一次成功轮询，Ctrl+C 后会
打印六项旧指标、判定原因占比、上传率、区域格子平均占比和耗时。

探针的新参数如下，全部是待真实重放校准的初始值：

- `--noise-window 20`：每块保留多少次差值。
- `--noise-multiplier 2.5` 与 `--noise-margin 0.0156862745`：噪声地板公式中的
  `k` 与 `4/255`。
- `--persistence-polls 2`：同一块连续几轮越过地板才确认。
- `--camera-motion-ratio 0.35`：确认块达到 35% 时视为镜头移动。
- `--min-save-interval 1.0`、`--max-silence 60.0`：连拍抑制和最长静默兜底。

全量流默认写到同一会话目录的 `raw/`，宽 640、JPEG 质量固定为 70，独立保留
最多 5000 张或 1 GiB。可以只调整容量与宽度：

```powershell
.venv\Scripts\python -m pet.core.capture --watch --title "窗口标题" --record-all --raw-width 640 --raw-max-files 5000 --raw-max-bytes 1073741824
```

### 离线重放

拿同一份 `raw/` 可以任意重跑检测参数，不必重玩：

```powershell
.venv\Scripts\python -m pet.core.capture --replay "recordings\capture\<会话>\raw" --save-dir "recordings\capture\<会话>-replay-k2" --noise-multiplier 2.0
```

重放只写新的 `metrics.csv` 和 `session.json`，不写 PNG。相同 raw 流与参数的
两次重放结果逐字节相同。在线开启 `--record-all` 时，检测器直接消费刚写入后
重新解码的 JPEG，因此重放看到的像素与在线决策一致。

`--capture-cursor` 是定位 WGC 光标兼容性的 A/B 开关，只决定是否请求 WGC 把光标
合成进捕获画面；默认关闭，普通采样无需使用。Project Zomboid 中开关两种状态都
复现了游戏内光标消失，而 Slay the Spire 2 在默认关闭状态下光标正常可见。因此
该参数不是修复，只用于后续兼容性复现和归因。

截图默认保存在 `backend/recordings/capture/<启动时间>/`，已被 Git 忽略。单次目录
最多保留 500 张或 200MB，先触及哪个上限就从最旧 PNG 开始删除。截图可能含私人
画面，`raw/` 更是全量画面，检查完请按自己的隐私需要处理，绝对不要提交仓库。

每个保存目录还包含：

- `metrics.csv`：每次轮询立即写一行并 flush。除六个旧指标外，新增确认块数/
  占比、镜头移动、变化格子、144 块噪声地板的中位值、判定原因和相邻前帧
  文件名。原因只会是 `persistent_change`、`camera_motion`、`forced`、
  `suppressed_min_interval`、`no_change`。WGC 无新帧仍单独记行，指标留空。
- `session.json`：记录 `--label`、启动参数、开始与结束时间、总轮询数及退出汇总。
  如果进程异常退出，已 flush 的 CSV 行仍在；session.json 至少保留启动信息，
  只有正常退出或 Ctrl+C 才有结束时间与最终汇总。

### 建议重录的游戏类型与时长

为了让 20 轮噪声窗口至少经历多次稳定期，每段建议 8–10 分钟，全程开启
`--record-all`。优先补这三类：

1. 3D 动态环境：雨、浪、植被各至少静止 2 分钟，再插入人物经过、小幅移动与
   大幅转向，用来同时测噪声地板、局部持久变化和镜头移动。
2. 文字/策略 UI：静止文本、切换一个任务标签、弹出大面板、整屏切页各重复数次，
   用来确认小面积变化不会漏掉。
3. 2D 动画游戏：待机动画、小范围角色移动、背包/菜单与换场，观察固定动画区域
   是否在窗口热身后被抑制。

### 每类都记录什么

- 后端是否初始化成功；失败时完整抄下终端的人话错误。
- 保存目录是否主要是首帧和肉眼可见变化帧；静止时只有每到 `--max-silence`
  才出现一次 `forced=true` 的兜底帧，还是仍有大量非强制落盘。
- 明显事件是否在第二个连续轮询后出现 `persistent_change`；大幅转向是否记为
  `camera_motion` 且变化格子为空。漏传比多传优先级更高，发现漏传先记录原始
  时刻，不要直接抬高门槛。
- 任务管理器中该 Python 进程的 CPU 百分比（静止时与激烈变化时各记一次）。
- 游戏开启探针前后的帧率，是否有可感知掉帧或卡顿。
- 窗口或屏幕周围是否出现系统自带的黄色捕获边框；只记录，T2 再决定去留。
- 保存整次会话的 `raw/`、`metrics.csv`、`session.json`；用不同参数重放时每次换
  一个 `--save-dir`，保留原始结果。上述文件可能含窗口标题与私人画面，不要上传。
