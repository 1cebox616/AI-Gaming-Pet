# AI Gaming Pet

AI Gaming Pet 是一个常驻 Windows 11 桌面的 CS2 电子宠物。当前开发版本已经具备透明置顶窗口、代码绘制的宠物与表情、文字气泡、系统中文语音、自动待机话术，并已接通 CS2 官方 Game State Integration（GSI）。它能识别主菜单、热身、对局、观战和回合结算状态，检测击杀、爆头、多杀、死亡、击杀后被补枪、白给与回合胜负，再按冷却、每回合上限和场合策略选择事件，用双性格模板话术通过既有气泡、表情与语音链路作出反应。

项目仍处于开发期，前后端需要分别手动启动，尚未提供正式安装包。

## 开发环境

- Windows 11
- Python 3.12；`python` 必须能在 PowerShell 中直接调用。若未加入 PATH，可用 Python 3.12 `python.exe` 的完整路径替代下文的 `python`。安装了 Python Launcher 的机器也可使用 `py -3.12`
- Node.js 24.19.0 或更高版本
- Rust 1.97.1 或更高版本（MSVC 工具链）
- Steam 与 Counter-Strike 2（使用 GSI 时需要）

## 首次安装

克隆仓库后安装后端依赖：

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

安装前端依赖：

```powershell
cd ..\frontend
npm.cmd install
```

在 Windows PowerShell 中请使用 `npm.cmd`；直接使用 `npm` 可能因 PowerShell 脚本执行策略而失败。

后端启动时会自动查找 CS2 并安装 GSI 配置。也可以在 `backend` 目录单独执行一次：

```powershell
.venv\Scripts\python -m pet.games.cs2.gsi --install
```

语音直接使用 Windows 已安装的 OneCore 中文语音，不下载额外模型。若日志提示没有中文语音，请打开“设置 > 时间和语言 > 语言和区域”，选择“中文（简体，中国）”右侧的“…” > “语言选项”，在“语言功能”中下载“文本到语音转换”。

## 日常启动

先在一个 PowerShell 窗口启动后端：

```powershell
cd backend
.venv\Scripts\python -m pet.main
```

服务只监听 `http://127.0.0.1:8737`。浏览器访问 `http://127.0.0.1:8737/health` 可检查后端状态。

再在另一个 PowerShell 窗口启动桌面端：

```powershell
cd frontend
npm.cmd run tauri dev
```

宠物通过本机 WebSocket 与后端通信。后端未启动或连接断开时，宠物会变暗并自动重连。

## CS2 接入

后端从 Windows 注册表读取 Steam 安装路径，解析 Steam 的 `libraryfolders.vdf`，逐个库查找 appid 730 的清单，再定位 CS2 的 `game\csgo\cfg` 目录。启动时若 `gamestate_integration_ai_gaming_pet.cfg` 不存在或内容不正确，后端会自动写入正确配置。该通道是 Valve 官方提供的只读 GSI：CS2 主动向本机 `/gsi` 发送状态，不读取游戏内存，也不注入游戏进程。

确认方法：先启动后端，再打开 CS2。进入主菜单或对局后，控制台应出现每秒至多一条 `CS2 GSI` 摘要。超过 60 秒没有数据时会记录一次说明，恢复推送时也会记录一次。

若自动安装失败，日志会给出目标文件名和完整内容。手动找到 CS2 安装目录下的 `game\csgo\cfg`，新建 `gamestate_integration_ai_gaming_pet.cfg` 并粘贴日志中的内容，然后完全退出并重新打开 CS2。也可以重新运行：

```powershell
cd backend
.venv\Scripts\python -m pet.games.cs2.gsi --install
```

默认不保存原始游戏数据。需要为开发测试录制时，在被 Git 忽略的 `backend/config.local.toml` 中写入：

```toml
[gsi]
record = true
```

每次后端启动会在 `backend/recordings/` 新建一个带时间戳的 JSONL 文件，每行包含本地接收时间和一条原始 payload。录制文件可能包含 Steam ID 等游戏状态，请勿提交或分享未脱敏的文件。

## 录制回放

独立回放工具复用线上同一套事件检测、发言策略与模板生成实现。只查看事实事件时间线：

```powershell
cd backend
.venv\Scripts\python -m pet.games.cs2.eval.replay --replay recordings\gsi-YYYYMMDD-HHMMSS.jsonl
```

同时预览每个策略决定、丢弃原因以及最终话术与表情：

```powershell
.venv\Scripts\python -m pet.games.cs2.eval.replay --replay recordings\gsi-YYYYMMDD-HHMMSS.jsonl --with-policy
```

回放使用固定随机种子，因此同一份录制与配置的输出可重复。正常运行时话术选择仍使用非固定随机源。

## 验证

运行后端测试：

```powershell
cd backend
.venv\Scripts\python -m pytest
```

检查前端类型、构建与格式：

```powershell
cd frontend
npm.cmd run build
npm.cmd run format:check
cd src-tauri
cargo fmt --check
```
