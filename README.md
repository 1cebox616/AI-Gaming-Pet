# AI Gaming Pet

一个常驻 Windows 桌面的 AI 电子宠物：在用户游玩 CS2 时进行实时观战、解说和吐槽。本里程碑仅建立后端健康检查与桌面窗口的最小端到端链路。

## 开发环境

- Windows 11
- Python 3.12
- Node.js 24.19.0 或更高版本
- Rust toolchain 1.97.1 或更高版本（MSVC 工具链）

## 启动后端

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m pet.main
```

后端直接使用 Windows 11 已安装的 OneCore 中文语音，不下载模型或语音资源。如果日志提示没有中文语音，请打开“设置 > 时间和语言 > 语言和区域”，选择“中文（简体，中国）”右侧的“…” >“语言选项”，在“语言功能”中下载“文本到语音转换”。

服务仅监听 `http://127.0.0.1:8737`。可在浏览器访问 `http://127.0.0.1:8737/health` 检查状态。

## 启动前端

请先按上文启动后端，然后在另一个 PowerShell 窗口运行：

```powershell
cd frontend
npm.cmd install
npm.cmd run tauri dev
```

窗口会连接本机后端的 WebSocket，显示宠物与气泡。后端未启动或连接断开时，宠物会变暗并自动重连。

在 Windows PowerShell 中请使用 `npm.cmd`；直接使用 `npm` 可能因 PowerShell 脚本执行策略而失败。

验证前端构建：

```powershell
npm.cmd run build
```
