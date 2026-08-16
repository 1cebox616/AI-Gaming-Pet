# M3-T10 线上模型接入报告

## 实现边界

- 模型调用在 `OnlineCommentaryRuntime` 的独立 `asyncio.Queue` 工作任务中执行；
  实际同步 HTTP 调用包在 `asyncio.to_thread()` 内。`/gsi` 只解析、记录并把快照
  放进既有的 GSI 监听队列，不会等待模型响应。
- 每个模型失败（超时、HTTP/网络错误、空内容、硬性闸门拒绝）均立即走永久保留的
  `CommentaryGenerator` 模板路径；不重试。
- 连续失败达到 3 次后，本局固定为模板模式；下一局开始重置失败计数并重新尝试模型。
  3 的理由：可区分偶发上游错误，同时避免断网/欠费时每个事件都额外等待 3 秒。
- 线上复用离线硬性闸门：超过 30 汉字、无依据词、用词绑定、经济档位改写、
  eco 局误称手枪局任一命中即丢弃模型原文并回退模板。WARNING 记录命中原因和
  被丢弃原文，不记录密钥或完整提示词。

## `/gsi` 接收性能

本机用 `TestClient` 对真实 `/gsi` 端点发送同一份真实 fixture payload 300 次，
模型默认关闭（该端点不会等待后台模型）：

| 指标 | M3-T10 | 既往约束 |
|---|---:|---:|
| 中位数 | 0.448 ms | 0.56 ms |
| P95 | 0.670 ms | 0.70 ms |
| 最大值 | 1.996 ms | — |

结果未退化。真实模型网络请求的等待发生在工作线程，不能从这项接收端基准推断其
端到端播报延迟，需在真实对局中测量。

## 后端状态与花费

托盘菜单实现在 `frontend/src-tauri/src/main.rs`，本轮按边界未改 Rust 或前端。
后端已通过既有 WebSocket `state` 消息新增可选 `llm` 字段：

```json
{
  "mode": "ai | template",
  "reason": "",
  "consecutive_failures": 0,
  "call_count": 0,
  "cost_usd": 0.0
}
```

上游任一次没有提供 cost 时，`cost_usd` 为 `null`，即 UI 应显示「未提供」，绝不按
token 自行估算。下一轮前端/Rust 只需消费此字段，在现有游戏状态行旁新增模式和会话
花费即可。

## 自动验证

- `359 passed, 4 deselected`：4 项为本机缺少 OneCore 中文语音的环境测试。
- `test_online_commentary.py` 使用假客户端覆盖：未启用、环境变量缺失、超时、HTTP 错、
  网络错、空返回、硬性闸门、连续三次失败和成功花费累计；没有真实联网。
- 全仓库（排除虚拟环境及构建产物）未发现任何疑似 OpenRouter 密钥前缀。

## 需人工验证

1. 实际对局中把模型打开后，听模型播报是否比模板路径晚得可接受；特别观察交火密集时。
2. 断网、错误型号或额度耗尽时，确认前三次失败后本局立即继续说模板，下一局会重新尝试。
3. 确认模型输出的口语在 OneCore 中文语音中自然，且 30 汉字上限不会截断正常表达。
4. 前端/Rust 接入 `state.llm` 后，确认托盘能正确显示 AI/模板模式、失败计数和会话花费。

## 产品负责人启用步骤

1. 在启动后端的同一个 PowerShell 窗口设置密钥：
   ` $env:OPENROUTER_API_KEY = "你的密钥" `。
2. 编辑 `backend/config.toml` 的 `[llm]`：把 `enabled` 改成 `true`，填写 `model`；
   需要锁定上游时填写 `provider`，不锁定则留空。
3. 重启后端。控制台出现模型可用日志且 WebSocket `state.llm.mode` 为 `ai`，说明已生效；
   `template` 则查看 `reason`（未启用、型号未配置、环境变量缺失或连续失败）。
4. 首次实测建议保留默认 `temperature = 0.9`、`timeout_seconds = 3.0`、
   `max_tokens = 256`；它们均可在配置文件调整后重启生效。
