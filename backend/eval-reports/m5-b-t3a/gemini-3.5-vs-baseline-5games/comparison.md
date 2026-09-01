# M5-B-T3a Gemini 3.5 Flash Lite 与 3.1 基线对照

## 机械结论

本轮只比较结构合同、延迟、花费与限流，不判定游戏知识事实对错。

`google/gemini-3.5-flash-lite` 五题均成功返回且均在 10 秒内，但只有 3/5 可机械解析；当前 `google/gemini-3.1-flash-lite` 基线为 5/5 可解析。3.5 的 P50 更慢、实测费用约为基线 6.89 倍，因此仅按机械指标不能替代当前基线。

## 固定条件

- 游戏：Cyberpunk 2077、Counter-Strike 2、Stardew Valley、Hearthstone、Terraria。
- 仅联网模式；OpenRouter 内置 Exa 搜索，每次最多 5 条、全请求累计最多 5 条，不限制单条字符数。
- 两个模型使用逐字相同的 system prompt 与用户消息模板。
- temperature=0、reasoning=minimal、max_tokens=8000、`response_format=json_schema`、`require_parameters=true`、吞吐优先路由、`response-healing`。
- 3.5 每个游戏一次请求；失败不补跑。基线沿用上一轮同条件的五题数据，没有重新调用。

## 汇总

| 模型 | 返回正文 | 可机械解析 | 原样合同合规 | P50 / P90 / 最大（秒） | ≤10 秒 | 花费（USD） | 平均输入 / 输出 token |
|---|---:|---:|---:|---:|---:|---:|---:|
| Gemini 3.1 Flash Lite | 5/5 | 5/5 | 4/5 | 4.031 / 4.831 / 5.133 | 5/5 | $0.008559500 | 1295.2 / 925.4 |
| Gemini 3.5 Flash Lite | 5/5 | 3/5 | 3/5 | 6.710 / 7.460 / 7.790 | 5/5 | $0.058961200 | 6080.8 / 1187.2 |

3.5 的平均输出比基线长约 28.3%，但平均输入 token 约为基线 4.70 倍；在本轮实际计费中，3.5 总费用约为基线 6.89 倍。两个模型均报告 0 reasoning token。

## 逐题结果

| 游戏 | Gemini 3.1 基线 | Gemini 3.5 Flash Lite |
|---|---|---|
| Cyberpunk 2077 | 可解析、原样合规 / 5.133 秒 | 格式不合：`瞄准`=`RightLeft` / 7.790 秒 |
| Counter-Strike 2 | 可解析、原样合规 / 3.821 秒 | 可解析、原样合规 / 6.503 秒 |
| Stardew Valley | 可解析、本地规范化 `-` 与 `=` / 4.031 秒 | 格式不合：`快捷物品栏12`=`Plus` / 6.560 秒 |
| Hearthstone | 可解析、原样合规 / 3.905 秒 | 可解析、原样合规 / 6.710 秒 |
| Terraria | 可解析、原样合规 / 4.378 秒 | 可解析、原样合规 / 6.964 秒 |

`RightLeft` 无法无歧义映射到鼠标右键或其他具体输入；`Plus` 也不等同于提示词规定的物理键名。依既有规则，两者均未由本地程序猜测修复。

## 限流与失败

- Gemini 3.5 Flash Lite：429=0，累计冷却=0 秒，冷却丢弃=0。
- 返回=5，空答=0，网关失败=0，格式不合=2。

## 产物

- 3.5：[探针报告](report.md)、[原始答案](answers.md)、[可用的规范化 context](parsed-contexts.json)、[逐次原始记录](results.json)、[提示词与参数](prompt-v3.md)。
- 3.1 基线：[探针报告](../qwen-baseline-gemini-3.1-flash-lite-5games/report.md)、[原始答案](../qwen-baseline-gemini-3.1-flash-lite-5games/answers.md)、[规范化 context](../qwen-baseline-gemini-3.1-flash-lite-5games/parsed-contexts.json)。

## 未完成项

- 两组答案的事实正确性尚待产品负责人人工判卷。
- 零生产改动：未改 `config.toml`，未接管线，未写游戏卡。
