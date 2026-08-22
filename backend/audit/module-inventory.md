# M4-a 仓库清理与模块清单

## 统计说明

- 统计日期：2026-08-22。
- 统计环境：Windows PowerShell；Python 3.12.13；Git for Windows 的 `find`、`sort`、`xargs`、`wc`；ripgrep。
- 统计脚本：在仓库外运行临时脚本 `inventory_analyze.py`，仅使用 Python 标准库 `ast`、`pathlib`、`re`、`collections` 和 `json`，未引入依赖，也未修改生产代码。脚本逐个解析 `backend/src/pet/*.py` 与 `backend/tests/**/*.py` 的 AST，并解析 `frontend/src/**/*.ts` 的相对 import。
- 后端代码行数口径：物理行数，即文件中的 LF 数，与 `wc -l` 一致；包含空行与注释。
- 后端导入关系口径：只统计 AST 中可静态确定的 `pet.*` 绝对导入和包内相对导入；不把标准库、第三方包、字符串路径或运行时动态导入计入。表中的模块名省略 `pet.` 前缀。
- 生产可达性口径：以 `main.py` 为唯一入口，对上述静态导入图做传递闭包；`python -m pet.main` 会隐式装载包初始化文件，因此将 `__init__.py` 计为生产可达。生产闭包中的模块标为「生产可达」。
- 仅离线/命令行可达口径：用 AST 检出含 `if __name__ == "__main__"` 的模块，以这些模块为入口做传递闭包；不在生产闭包、但在该闭包中的模块标为「仅离线/命令行可达」。两种闭包都不可达的模块标为「无引用」。生产分类优先于命令行分类。
- 命令行入口口径：AST 检出顶层或嵌套的 `if __name__ == "__main__"` 守卫。入口用途根据模块说明、参数解析器和 `main()` 行为归纳，均标注为「人工判断」。
- 前端代码行数口径：物理行数，即文件中的 LF 数。导入关系只统计 `frontend/src/` 内可解析到 `.ts` 或 `index.ts` 的相对静态 import/export-from 与字符串字面量动态 import；第三方包导入不计。
- 测试覆盖判定口径：这里的「触及」是静态模块可达性，不是语句或分支覆盖率。脚本汇总 `backend/tests/**/*.py` 对 `pet` 模块的直接 AST import，再沿同一后端导入图做传递闭包；不在闭包中的模块列为「未被任何测试覆盖」。字符串路径、运行时反射和测试实际是否执行某一函数均不计。因此本项只能说明模块级静态触及，不能证明模块行为已被断言。
- 无法纯工具确定的内容：命令行入口的用途为人工判断；其余表格数据均由上述脚本或命令实际统计。

实际使用的主要命令：

```text
git pull --ff-only origin main
.venv\Scripts\python.exe -X utf8 <仓库外临时脚本 inventory_analyze.py>
rg -n --hidden --glob '!backend/bench-reports/**' --glob '!.git/**' "bench-reports" .
find backend/src/pet -name "*.py" | sort | xargs wc -l
.venv\Scripts\python.exe -m pytest tests/ -q --basetemp <仓库外临时目录> -p no:cacheprovider
```

## 一、删除结果与残留引用

- 已删除：`backend/bench-reports/`。
- 实际删除文件数：230。
- 实际删除总体积：8,141,722 字节。
- 除该目录外，本任务没有删除文件。

删除后全仓库仍有以下 10 处 `bench-reports` 引用。依任务要求，未修改任何引用方：

| 文件 | 行号 | 引用内容或用途 |
|---|---:|---|
| `AGENTS.md` | 74 | 说明该目录已在 M4-a 删除 |
| `backend/tests/test_bench.py` | 47 | 事件答案键路径 |
| `backend/tests/test_bench.py` | 52 | 通用禁用词文件路径 |
| `backend/src/pet/fact_sentence_audit.py` | 30 | `REPORTS_DIRECTORY` |
| `backend/src/pet/fact_sentence_audit.py` | 254 | 从 Git 历史读取 M3-T8.14 报告 |
| `backend/src/pet/fact_sentence_audit.py` | 278 | 从 Git 历史读取 M3-T8.15 报告 |
| `backend/src/pet/scenario_synth.py` | 29 | `REPORTS_DIRECTORY` |
| `backend/src/pet/style_diversity.py` | 15 | 默认报告路径 |
| `backend/src/pet/style_experiment.py` | 28 | 默认报告路径 |
| `backend/src/pet/style_review.py` | 24 | 默认报告路径 |

## 二、后端模块表

| 文件名 | 代码行数 | import 的本项目模块 | 被本项目模块 import | 生产可达性 |
|---|---:|---|---|---|
| `__init__.py` | 1 | — | — | 生产可达 |
| `bench.py` | 2,214 | `commentary`、`commentary_rules`、`commentary_templates`、`config`、`event_card`、`events`、`llm`、`prompt`、`replay` | `fact_sentence_audit`、`scenario_synth`、`style_experiment`、`style_review` | 仅离线/命令行可达 |
| `bridge.py` | 389 | `config`、`lines`、`session`、`speech` | `main`、`online_commentary` | 生产可达 |
| `commentary.py` | 191 | `commentary_rules`、`commentary_templates`、`config`、`events`、`lines` | `bench`、`main`、`online_commentary`、`replay` | 生产可达 |
| `commentary_rules.py` | 53 | — | `bench`、`commentary`、`event_card` | 生产可达 |
| `commentary_templates.py` | 345 | `config`、`lines` | `bench`、`commentary` | 生产可达 |
| `config.py` | 299 | — | `bench`、`bridge`、`commentary`、`commentary_templates`、`events`、`fact_sentence_audit`、`gsi`、`lines`、`main`、`online_commentary`、`policy`、`replay`、`speech` | 生产可达 |
| `event_card.py` | 1,844 | `commentary_rules`、`events`、`gsi`、`session`、`situation` | `bench`、`fact_sentence_audit`、`hard_gate`、`online_commentary` | 生产可达 |
| `events.py` | 435 | `config`、`gsi`、`session` | `bench`、`commentary`、`event_card`、`fact_sentence_audit`、`main`、`online_commentary`、`policy`、`replay`、`scenario_synth` | 生产可达 |
| `fact_sentence_audit.py` | 424 | `bench`、`config`、`event_card`、`events`、`prompt`、`replay`、`scenario_synth` | `style_diversity`、`style_experiment`、`style_review` | 仅离线/命令行可达 |
| `gsi.py` | 707 | `config`、`network` | `event_card`、`events`、`main`、`online_commentary`、`policy`、`replay`、`scenario_synth`、`session`、`situation` | 生产可达 |
| `hard_gate.py` | 403 | `event_card`、`situation` | `online_commentary`、`style_experiment`、`style_review` | 生产可达 |
| `lines.py` | 121 | `config` | `bridge`、`commentary`、`commentary_templates`、`online_commentary`、`replay` | 生产可达 |
| `llm.py` | 525 | — | `bench`、`online_commentary`、`style_diversity`、`style_experiment`、`style_review` | 生产可达 |
| `main.py` | 240 | `bridge`、`commentary`、`config`、`events`、`gsi`、`network`、`online_commentary`、`policy`、`session`、`situation`、`speech` | — | 生产可达 |
| `network.py` | 4 | — | `gsi`、`main` | 生产可达 |
| `online_commentary.py` | 220 | `bridge`、`commentary`、`config`、`event_card`、`events`、`gsi`、`hard_gate`、`lines`、`llm`、`prompt`、`session`、`situation` | `main` | 生产可达 |
| `policy.py` | 332 | `config`、`events`、`gsi`、`session` | `main`、`replay` | 生产可达 |
| `prompt.py` | 41 | — | `bench`、`fact_sentence_audit`、`online_commentary`、`style_diversity`、`style_experiment`、`style_review` | 生产可达 |
| `replay.py` | 997 | `commentary`、`config`、`events`、`gsi`、`lines`、`policy`、`session`、`situation` | `bench`、`fact_sentence_audit`、`scenario_synth` | 仅离线/命令行可达 |
| `scenario_synth.py` | 1,168 | `bench`、`events`、`gsi`、`replay`、`session`、`situation` | `fact_sentence_audit` | 仅离线/命令行可达 |
| `session.py` | 147 | `gsi` | `bridge`、`event_card`、`events`、`main`、`online_commentary`、`policy`、`replay`、`scenario_synth`、`situation` | 生产可达 |
| `situation.py` | 1,514 | `gsi`、`session` | `event_card`、`hard_gate`、`main`、`online_commentary`、`replay`、`scenario_synth` | 生产可达 |
| `speech.py` | 698 | `config` | `bridge`、`main` | 生产可达 |
| `style_diversity.py` | 116 | `fact_sentence_audit`、`llm`、`prompt`、`style_review` | `style_experiment` | 仅离线/命令行可达 |
| `style_experiment.py` | 258 | `bench`、`fact_sentence_audit`、`hard_gate`、`llm`、`prompt`、`style_diversity`、`style_review` | — | 仅离线/命令行可达 |
| `style_review.py` | 290 | `bench`、`fact_sentence_audit`、`hard_gate`、`llm`、`prompt` | `style_diversity`、`style_experiment` | 仅离线/命令行可达 |
| **合计** | **13,976** |  |  |  |

### 生产可达性汇总

- 生产可达：20 个模块（含 `__init__.py`）。
- 仅离线/命令行可达：7 个模块。
- 无引用：无。

## 三、命令行入口清单

以下用途均为人工判断。

| 模块入口 | 用途一句话 |
|---|---|
| `python -m pet.bench` | 离线运行 CS2 事件事实句与模型话术评测、评分或报告生成。 |
| `python -m pet.fact_sentence_audit` | 离线核验并生成确定性事实句覆盖报告。 |
| `python -m pet.gsi` | 管理并安装 CS2 Game State Integration 接入配置。 |
| `python -m pet.main` | 启动仅绑定本机地址的 FastAPI/uvicorn 后端服务。 |
| `python -m pet.replay` | 回放 GSI 录制、打印事件时间线或生成数据清单。 |
| `python -m pet.scenario_synth` | 生成合成 GSI 回归场景及无模型报告。 |
| `python -m pet.style_diversity` | 对固定事实句做无种子的重复采样并生成文风多样性复核报告。 |
| `python -m pet.style_experiment` | 运行多样性约束与硬闸门的离线对照实验并生成报告。 |
| `python -m pet.style_review` | 运行双温度文风对照评测并生成待人工判读的报告。 |

## 四、前端模块表

| 文件名 | 代码行数 | 被哪些文件 import |
|---|---:|---|
| `backend-bridge.ts` | 334 | `main.ts` |
| `bubble.ts` | 162 | `backend-bridge.ts` |
| `llm-status.test.ts` | 41 | — |
| `llm-status.ts` | 51 | `backend-bridge.ts`、`llm-status.test.ts` |
| `main.ts` | 44 | — |
| `pet.ts` | 370 | `backend-bridge.ts`、`main.ts` |
| **合计** | **1,002** |  |

## 五、未被任何测试覆盖的后端模块

按文件开头所述的「测试直接 import + 后端静态导入图传递闭包」口径，共有 2 个模块未被 `backend/tests/` 触及：

- `style_diversity.py`
- `style_experiment.py`

## 六、后端行数校验

在仓库根目录通过 Git for Windows 的 Bash 实际执行：

```text
$ find backend/src/pet -name "*.py" | sort | xargs wc -l
     1 backend/src/pet/__init__.py
  2214 backend/src/pet/bench.py
   389 backend/src/pet/bridge.py
   191 backend/src/pet/commentary.py
    53 backend/src/pet/commentary_rules.py
   345 backend/src/pet/commentary_templates.py
   299 backend/src/pet/config.py
  1844 backend/src/pet/event_card.py
   435 backend/src/pet/events.py
   424 backend/src/pet/fact_sentence_audit.py
   707 backend/src/pet/gsi.py
   403 backend/src/pet/hard_gate.py
   121 backend/src/pet/lines.py
   525 backend/src/pet/llm.py
   240 backend/src/pet/main.py
     4 backend/src/pet/network.py
   220 backend/src/pet/online_commentary.py
   332 backend/src/pet/policy.py
    41 backend/src/pet/prompt.py
   997 backend/src/pet/replay.py
  1168 backend/src/pet/scenario_synth.py
   147 backend/src/pet/session.py
  1514 backend/src/pet/situation.py
   698 backend/src/pet/speech.py
   116 backend/src/pet/style_diversity.py
   258 backend/src/pet/style_experiment.py
   290 backend/src/pet/style_review.py
 13976 total
```

表格合计与 `wc -l` 的 `13976 total` 一致。

## 七、测试对照与未完成项

任务前基线（使用仓库外可写的 `--basetemp`，避免本机默认临时目录权限问题）：

```text
4 failed, 409 passed, 1 warning in 18.56s
```

其中 4 项失败均为本机缺少 OneCore 中文语音。删除后的同命令结果：

```text
13 failed, 400 passed, 1 warning in 12.86s
```

除相同的 4 项语音失败外，新增 9 项失败均为已删除目录的残留引用读取以下 4 个文件时触发 `FileNotFoundError`：

- `backend/bench-reports/m3-t5.6-event-answer-keys.json`
- `backend/bench-reports/m3-t5.9-universal-forbidden.json`
- `backend/bench-reports/m3-t2-data-inventory.md`
- `backend/bench-reports/m3-t6-observed-constraints.json`

受影响测试为 `backend/tests/test_bench.py` 4 项、`backend/tests/test_scenario_synth.py` 5 项。任务要求一方面删除整个目录，另一方面明确要求发现引用后不得修改引用方，因此当前仓库无法同时满足「目录完全删除」与「测试结果和删除前完全一致」。本任务严格保留测试与引用方不变，将这 9 项新增失败列为未完成项，等待后续任务决定这些回归资产的新位置及引用迁移方案。
