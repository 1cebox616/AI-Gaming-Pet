# M5-T1 窗口截屏与画面变化检测报告

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

## 代码层验证边界

自动测试全部使用合成图像与 pytest 临时目录，不调用真实截图。验证了相同画面、
轻微噪声、场景切换、文件数上限、字节上限和非 Windows 错误。

coding agent 没有在真实游戏上运行探针，因此以下项目尚未实测：独占全屏兼容性、
是否出现黄色捕获边框、实际 CPU 占用、对游戏帧率的影响、不同游戏下 0.02 阈值与
2 秒间隔是否合适。它们必须由产品负责人按下页步骤记录，不能用代码审查代替。

自动验收的实际记录：

```text
.venv\Scripts\python -m pytest tests/test_capture.py tests/test_layering.py -q --basetemp .pytest-m5-t1-targeted
9 passed

.venv\Scripts\python -m pytest tests/ -q --basetemp .pytest-m5-t1-full
420 passed, 4 failed
```

全量测试的 4 项失败均在 `tests/test_speech.py`，原因是受限执行环境无法枚举已安装的
OneCore 中文语音，与本任务前的环境项一致。新增测试、分层测试和其他回归全部通过。

网络模块静态检索命令与实际输出：

```text
rg -n "^\s*(from|import)\s+(httpx|socket|urllib|requests|aiohttp|websockets)(\.|\s|$)" src/pet/core/capture.py
（无输出）
```

依赖一致性检查 `python -m pip check` 的实际输出为
`No broken requirements found.`。

## 产品负责人实测指引

### 怎么运行

打开 PowerShell：

```powershell
cd backend
.venv\Scripts\python -m pet.core.capture --watch --title "窗口标题的一部分"
```

建议优先使用 `--title`，例如填游戏窗口标题的一小段。若不传 `--title`，探针会给
3 秒让你切回游戏，然后锁定当时的前台窗口。启动横幅会显示保存目录；只保存首帧
和变化帧。按 Ctrl+C 停止，终端会打印抓帧数、落盘数、差异值分布及抓帧耗时。

调阈值或间隔：

```powershell
.venv\Scripts\python -m pet.core.capture --watch --title "窗口标题" --interval 2.0 --threshold 0.02
```

截图默认保存在 `backend/recordings/capture/<启动时间>/`，已被 Git 忽略。单次目录
最多保留 500 张或 200MB，先触及哪个上限就从最旧 PNG 开始删除。截图可能含私人
画面，检查完请按自己的隐私需要处理该目录。

### 测哪三类游戏

每类至少运行 5 分钟，分别覆盖静止菜单、普通游玩和大幅场景切换：

1. 3D 全屏大作：分别试独占全屏（若游戏提供）与无边框窗口。
2. 策略游戏：观察地图静止、单位小范围移动、打开大型面板三种状态。
3. 2D 独立游戏：观察像素动画、小范围角色移动、整屏换场三种状态。

### 每类都记录什么

- 后端是否初始化成功；失败时完整抄下终端的人话错误。
- 保存目录是否只有首帧和肉眼可见变化帧；静止画面是否仍大量落盘。
- 任务管理器中该 Python 进程的 CPU 百分比（静止时与激烈变化时各记一次）。
- 游戏开启探针前后的帧率，是否有可感知掉帧或卡顿。
- 窗口或屏幕周围是否出现系统自带的黄色捕获边框；只记录，T2 再决定去留。
- 终端汇总中的差异值分布、抓帧耗时中位与最大值。
- 若误报太多，逐步提高 `--threshold`；若明显换场不保存，逐步降低。记录每次值，
  不要只报最后结论。`0.02` 与 `2.0` 都是待实测默认值，不是既定产品标准。
