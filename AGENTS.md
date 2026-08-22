# AGENTS.md
最后更新：M3 里程碑验收通过，转向多游戏架构

## 项目概况

目标：一个常驻 Windows 桌面的 AI 电子宠物，在用户玩游戏时实时观战、解说、吐槽。
**主干与游戏解耦**，每个游戏的理解能力打包成一个独立适配。
当前唯一落地的适配是 CS2；战争雷霆适配由第二位开发者并行开发。

技术栈：
- 后端：Python 3.12.x（>=3.12,<3.13）+ FastAPI 0.141.1 + uvicorn 0.52.1
  + pydantic 2.13.4 + websockets 17.0.1 + httpx 0.28.1。仅绑定 127.0.0.1:8737
- 前端：Tauri 2.11.5 + TypeScript 7.0.2 + Vite 8.2.1 + Prettier 3.9.6，不使用 UI 框架
- 构建环境：Node.js 24.x、npm 11.x、Rust 1.97.x（MSVC）
- 语音：Windows OneCore 系统语音（当前 Microsoft Yaoyao zh-CN），
  经 subprocess 调用 PowerShell 访问 Windows Runtime 合成 WAV，
  再用 ctypes 直调 WinMM waveOut 播放（可从任意线程立即中断）。
  零模型、零第三方依赖、严禁占用 GPU。神经网络语音是 M7，尚未开始
- 语言模型：**经 OpenRouter 调用，OpenAI 兼容协议**。产品负责人在北美，
  无法注册阿里云百炼，因此不走厂商直连。当前 `qwen/qwen3.5-122b-a10b` 锁定
  Alibaba 上游。**任何代码与文档都不得写死型号名或上游服务商名**，一律参数传入
- 截图：Windows Graphics Capture（尚未接入，M5）
- 打包：PyInstaller + Tauri bundler（尚未启用，bundle.active = false）

目录结构：
- /backend/src/pet —— 后端源码包（**尚无游戏抽象层，CS2 逻辑与主干混在一起，
  这正是 M4 要解决的问题**）
    运行时主干
      main.py                  组装应用、端点、生命周期
      network.py               共享端口常量
      config.py                配置读取与分段校验
      session.py               会话状态与数据主体识别
      policy.py                发言策略：优先级、场合、冷却、每回合上限、连杀收敛
      bridge.py                WebSocket 通道、定时广播、运行时开关
      speech.py                系统语音合成与播放
      lines.py                 待机话术与 Utterance
      llm.py                   语言模型客户端（业务无关，不知道任何游戏的存在）
      prompt.py                提示词加载与拼接
      online_commentary.py     线上模型调用、失败回退模板、花费与模式状态
      hard_gate.py             运行时闸门，从词库绑定表解析规则
    CS2 特化（M4 将整体迁入 CS2 适配包）
      gsi.py                   CS2 数据接收、GameSnapshot 解析、录制、写入 CS2 配置
      events.py                事件检测（只做"发生了什么"）
      situation.py             局势累计（只做"现在是什么状况"）
      event_card.py            把事实渲染成喂给模型的事实句
      commentary.py            事件到模板话术的映射、填空、去重、地图过滤
      commentary_templates.py  模板语料数据（无逻辑）
      commentary_rules.py      共享事实检查黑名单（点位词、未替代脏字）
    离线工具（绝不被线上链路引用，M4 将整体移出生产包）
      replay.py                录制回放与数据清单工具及 CLI
      bench.py                 离线话术评测台及 CLI
      scenario_synth.py        合成场景生成
      fact_sentence_audit.py   事实句审计
      style_review.py / style_diversity.py / style_experiment.py  文风与多样性统计
- /backend/tests             后端测试与 fixtures
- /backend/scenarios/        35 个合成罕见场景，**这是 CS2 适配的回归资产，不得删除**
- /backend/prompts/          提示词文件，**产品负责人可直接编辑，改完重启后端生效**，
                             coding agent 不得改动其中的措辞，只负责加载与拼接：
    vocabulary.md 词库与绑定表（末尾绑定表由 hard_gate.py 解析成运行时闸门）
    inference.md  **线上生产实际使用的系统提示词**
    brother.md    模板路径的损友人设（历史遗留，线上不用）
    caster.md     模板路径的解说人设（历史遗留，线上不用）
- /backend/audit/            M4-a 产出的模块清单，M4-c 重构完成后删除
- /backend/config.toml       默认配置（随代码提交）
- /backend/config.local.toml 可选本地覆盖（已忽略）
- /backend/recordings/       GSI 原始录制（已忽略）
- /frontend                  Vite + TypeScript 前端
- /frontend/src-tauri        Tauri 桌面外壳（Rust）
- /frontend/src-tauri/capabilities  Tauri v2 权限声明（默认全关，需逐项授予）
- /docs                      项目文档，coding agent 不得修改内容：
    gsi-capabilities.md            CS2 数据接口能给什么、不能给什么
    中文CS社群常见梗和语录.md       社群黑话与梗，语料改写依据 + 提示词样例池

（`/backend/bench-reports/` 已在 M4-a 删除：150 份一次性评测过程记录，
内容保留在 git 历史中。不要重建该目录。）

常用命令（Windows PowerShell）：
- 后端安装：cd backend && python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
- 后端运行：.venv\Scripts\python -m pet.main
- 后端测试：.venv\Scripts\python -m pytest
- 录制回放：.venv\Scripts\python -m pet.replay --replay <文件> [--with-policy] [--with-commentary]
- 数据清单：.venv\Scripts\python -m pet.replay --data-inventory <文件> --out <路径>
- 话术评测：.venv\Scripts\python -m pet.bench --replay <文件> --model <型号ID> --out <报告路径>
- 安装 CS2 接入文件：.venv\Scripts\python -m pet.gsi --install
- 前端安装：cd frontend && npm.cmd install
- 前端开发运行：npm.cmd run tauri dev（需先启动后端）
- 前端构建与格式检查：npm.cmd run build / npm.cmd run format:check

约定：
- Windows PowerShell 中前端命令一律使用 npm.cmd。python 必须可从 PowerShell 直接调用，
  否则用 py -3.12 或完整路径
- 后端与前端目前由人手动分别启动，打包（M10）之前不做自动拉起
- 语言模型 API 密钥只从环境变量 OPENROUTER_API_KEY 读取，
  绝不写入 config.toml、源码、测试或提交历史

## 核心契约

改动前必须先与架构师确认。以下定义与代码实现必须逐字段一致。

GameSnapshot（gsi.py）—— CS2 某一瞬间的状态，除 ts 外全部字段可为 None：

    身份与场次
      ts, player_steamid, provider_steamid, activity,
      map_mode, map_name, map_phase, round_number, round_phase, round_win_team,
      round_wins（每回合获胜方式历史）, bomb_state,
      score_ct, score_t, ct_consecutive_round_losses, t_consecutive_round_losses

    本人状态
      team, health, armor, helmet, money, equip_value, has_defusekit,
      flashed（实测为 0/1 开关量，不是强度值）,
      smoked（实测确为 0–255 强度值）,
      burning（0–255，实测从未观测到非零）,
      round_kills, round_killhs, active_weapon, weapons

    全场统计
      match_kills, match_assists, match_deaths, match_mvps, match_score

    字段生命周期规则：解析后无消费者的字段一律不得保留。
    不要因为"以后可能有用"而保留无人读取的字段。

WeaponSlot（gsi.py）—— weapons 中的一把武器：
    name, type, ammo_clip, ammo_clip_max, ammo_reserve, state
    （state 为 active / holstered 等，用于判断哪把是手持）

RoundSituation（situation.py）—— 单张快照表达不了的本回合累计量：
    flash_count, flashed_seconds_total, longest_flash_seconds,
    smoked_seconds_total, max_smoke_intensity, burn_count,
    total_damage_taken, lowest_health_while_alive, health_before_death,
    primary_weapons_used, bought_equipment,
    bomb_planted_at_ts, seconds_since_bomb_planted,
    self_team, timeline

    规则一：只在数据主体是本人时累计（subject_is_self 为真），
    否则死亡观战期间队友的状态会污染统计。
    规则二：回合边界与对局边界都必须重置。
    规则三：观战期间 observe() 返回的是死亡那一回合的旧数据，
    消费者必须比对 round_number 才能使用。
    规则四：**比分、金钱、装备价值一律不复制进本结构**，直接从 GameSnapshot 取，
    避免出现两份可能分叉的比分与经济。
    例外：self_team 是有意例外——观战期间 snapshot.team 是队友的阵营，
    而回合结算恰恰发生在观战期间。

    时长类字段一律用相邻快照的 ts 差计算，**不得用 payload 条数换算**：
    GSI 是"有变化就推、最小间隔 0.1 秒、静止时 30 秒心跳"，不是固定频率。

    timeline 的边界（必须严格遵守）：记录的是**状态变化**，不是事件。
    不得赋予类型语义、优先级或"值不值得说"的判断，
    不得被 policy 或任何发言决策读取。唯一消费者是事实句渲染。
    events.py 与 situation.py 仍然互不引用。

GameState（session.py）—— 随 state 消息下发：
    state: offline / menu / warmup / playing / spectating / round_over / match_over
    mode, map, round, score_ct, score_t
    subject_steamid   当前这份数据描述的是谁
    subject_is_self   该主体是否为本机玩家

GameEvent（events.py）—— 事件检测的输出：
    id, ts, subject_steamid, subject_is_self, round_number, facts
    type: kill / kill_headshot / multi_kill /
          death / death_after_kill / death_thrown_away /
          round_win / round_loss

    facts 按事件类型携带：
      击杀类   round_kill_index, delta, weapon,
               self_team, self_score, opponent_score,
               score_situation, team_consecutive_round_losses
      多杀     count, self_team, self_score, opponent_score,
               score_situation, team_consecutive_round_losses
      死亡类   survival_seconds, round_kills, seconds_since_last_kill,
               equip_value, score_situation, team_consecutive_round_losses
      回合类   method, score_ct, score_t, score_situation,
               team_consecutive_round_losses

    self_team / self_score / opponent_score 的存在理由：把"谁领先"这道题
    从模型手里收回代码——实测中模型曾把落后读成领先。

Utterance（lines.py）：
    id: str（非空）, text: str（非空）,
    emotion: neutral / happy / angry / surprised / speechless

WebSocket ws://127.0.0.1:8737/ws：
    后端 → 前端：
      {"type":"utterance","id":...,"text":...,"emotion":...}
      {"type":"state","speech_enabled":bool,"muted":bool,
       "game":{"state":...,"mode":...,"map":...,"round":...,
               "score_ct":...,"score_t":...,
               "subject_steamid":...,"subject_is_self":...}}
    前端 → 后端：
      {"type":"request_idle_line"}
      {"type":"set_speech_enabled","value":bool}
      {"type":"set_muted","value":bool}
    规则：连接建立时先发 state 再发问候；开关变化后向所有连接广播 state；
    game 字段任何时候都必须存在，无法获知的子字段为 null；
    无法识别的消息类型与非法 JSON 只记 WARNING，不断开连接；
    Origin 存在但不在白名单则拒绝，Origin 缺失则允许

HTTP POST /gsi —— 接收 CS2 推送。无鉴权（仅绑定本机）。
    任何非法输入都必须返回成功，绝不能让游戏端收到错误或超时。

配置文件段落（backend/config.toml）：
    [speech]      enabled, voice_name
    [idle]        enabled, min_interval_seconds, max_interval_seconds
    [gsi]         record
    [events]      thrown_away_max_survival_seconds, thrown_away_min_equip_value,
                  death_after_kill_max_seconds
    [policy]      cooldown_seconds, max_lines_per_round, alive_priority_threshold,
                  cooldown_override_priority, minimum_gap_seconds,
                  streak_settle_seconds, follow_up_max_age_seconds
    [personality] style（brother / caster，仅影响模板路径）
    [llm]         enabled, model, provider, max_tokens, temperature

发言策略的连杀规则（M3-T12 系列确立，改动前必须先与架构师确认）：
    multi_kill 事件**一律不直接播报**，全部进入单槽暂存；
    更高杀数覆盖槽内并**刷新计时**；
    释放条件为「收敛」（距槽内时间戳 ≥ streak_settle_seconds）
    或「终局」（本批出现死亡或回合结算事件），
    且未静音、且距上次发言 ≥ minimum_gap_seconds；
    超过 follow_up_max_age_seconds、换回合、reset() 则清除。
    连杀豁免冷却与每回合上限，但被选中后照常计入每回合发言计数。

回合号规则（必须只有一处实现，M1–M2 期间曾分叉过）：
    唯一实现是 gsi.human_round_number()，session.py、events.py、situation.py
    都必须调用它，不得各自计算。
    round_phase 为 "over"，或 round_win_team 有值 → 该快照描述刚结束的回合，
                                                    回合号 = map.round
    其余阶段 → 描述进行中的回合，回合号 = map.round + 1

场景标签（`event_card.SCENE_TAGS`，当前 36 个）：
    弹药五档  颗秒（仅 AK 与沙鹰，1 发）/ 秒杀 2-5 / 普通击杀 6-9 /
              有些吃力 10-14 / 马完了 ≥15。狙击枪走 `狙击击杀`，不进五档
    连杀      连续双杀 / 连续三杀 / 连续四杀 / 连续五杀（间隔 ≤5 秒，每杀刷新）
    多杀      多杀2 / 多杀3 / 多杀4 / 多杀5+（5+ 仅休闲模式可能出现）
    击杀处境  对枪胜利 / 白着打 / 踩火杀 / 摸烟击杀 / 换枪后立刻杀
    死亡性质  白给 / 击杀后被补枪 / 马枪死 / 送狙 / 对枪输了 /
              一枪没开就没了 / 打空了还是没打过
    死亡处境  白着被打死 / 烟里死 / 出烟就没了 / 切雷时被打死 / 切刀时被打死
    本回合    白惨了 / 烧惨了 / 血皮撑住了 / 大狙空枪 / 连续空枪

    规则：由代码限制在 3 个以内并按观战关注顺序排序；
    互相矛盾或蕴含的标签由代码消解，只保留信息量最大的那个；
    **标签名不得出现在事实句的【过程】里**；
    `prompts/vocabulary.md` 末尾的绑定表以标签名为第三列，
    `hard_gate.py` 启动时解析该表生成运行时闸门——
    改词库不需要改代码，解析失败则整表失效并回退保守规则。

分层职责：
    gsi         只接收与解析，不判断"发生了什么"
    session     只判会话状态与主体，不做事件检测
    events      只做"发生了什么"（离散事件），不含任何优先级信息
    situation   只做"现在是什么状况"（连续状态与本回合累计），不产出事件
    policy      只做"该不该说"，不生成宠物话术，**不感知语音播放状态**
    commentary  只做模板路径的"说什么"，不改变策略结论
    event_card  只把已有事实渲染成中文文本，不新增任何判断、不访问网络
    llm         只负责把消息发给模型并把结果带回来，不知道任何游戏的存在
    bench / replay / scenario_synth / style_* / fact_sentence_audit
                只做离线工作，绝不被线上链路引用

    events 与 situation 是 GameSnapshot 的两个平行消费者，互不引用。

## 当前状态

当前里程碑：**M3 已验收通过，M4「多游戏地基」进行中**。

已实现：
- **M1 桌面宠物**：透明无边框置顶悬浮窗、可拖动、Ctrl+Alt+P 显隐、
  托盘与右键共用六项菜单、代码绘制的宠物与五种表情、文字气泡、
  本地系统语音（可随时中断）、前后端常驻 WebSocket、待机播报
- **M2 看得懂 CS2**：GSI 接入与配置自动安装、原始数据录制与离线回放、
  会话状态与数据主体识别、八类事件检测、发言策略、双性格模板话术 201 条、
  菜单显示游戏状态、对局生命周期收敛
- **M3 每句话由大模型当场生成**：事实层（GameSnapshot 扩容、本回合时间线、
  36 个确定性场景标签、代码确定性生成的事实句）；文风层（人设与词库外置在
  `prompts/`，模型只做"把事实句用网友口气重说一遍"，`hard_gate.py` 从词库
  绑定表自动解析运行时闸门）；线上接入（异步调用，`/gsi` 中位 0.448ms 未退化，
  失败/空输出/闸门命中一律回退模板绝不重试，连续失败 3 次锁定本局模板模式，
  托盘显示模式与花费）；评测基建（离线评测台、35 个合成罕见场景、
  55 题冻结答案已降级为代码回归网）；连杀收敛（一波连杀只发一句，
  播报最终杀数，不再互相打断）。
  **产品负责人已在真实对局中实测认可**

未实现（滚动细化，只细化当前里程碑）：
- **M4 多游戏地基**——把主干与 CS2 拆开，留出标准化适配端口
    M4-a 仓库清理与模块清单（只删不改，产出 `backend/audit/module-inventory.md`）
    M4-b 适配端口设计，由产品负责人拍板
    M4-c 一次性重构落地：目录分层、文件与目录名改英文、
         评测工具移出生产包、CS2 代码迁入适配包、AGENTS.md 拆分为
         主文件 + 每游戏一份（CS2.md / WARTHUNDER.md）+ 适配开发指南
    验证方式：打一局 CS2，**感觉不到任何差别**
- **M5 通用视觉**——截屏 + 视觉模型，认出任意游戏并对屏幕上发生的事反应
  （默认关闭）；同时交付 CS2 与战争雷霆两个游戏的视觉特化适配。
  CS2 侧补 GSI 拿不到的事实（谁杀了你、场上剩几人）
- **M6 跨局长期记忆**——记忆按游戏分仓；同时交付 CS2 与战争雷霆两侧适配
- **M7 神经网络语音**——**必须保留"可随时中断"，延迟不得明显劣于系统语音**
- **M8 语音对话输入**
- **M9 外观自定义与心情系统**
- **M10 打包分发，朋友可安装 —— MVP 完成线**

适配线（与主干线并行，由第二位开发者负责）：
- 战争雷霆实时数据适配：M4-c 交付适配开发指南后开工，对标 CS2 的 M2+M3 深度。
  数据源为游戏自带的本地接口 `localhost:8111`
  （`/state`、`/indicators`、`/hudmsg`、`/gamechat`、`/map_obj.json` 等）。
  该开发者负责事实句体系、词库、社群黑话文档与实机验证；**代码审查仍归架构师**
- 战争雷霆视觉适配：M5 完成后开工
- CS2 地图战术建议（原 M3-T14）：M4-c 之后随时可做，落在 CS2 适配包内，
  不占主干排期。触发时机为回合开始，可提前预生成

Post-MVP：适配形态从"同仓库子目录"升级到独立分发、AI 玩杀戮尖塔、AI 玩文明 6。

多开发者协作约定（M4-c 落地时正式生效，现在先记录方向）：
1. 适配开发者只允许改自己那个游戏的目录与 md，主干任何改动经架构师下发
2. 端口不够用时**立即停下来改端口**，不许在适配里绕路——绕路就是边界漏了
3. 主干代码**不允许 import 任何游戏适配的东西**，只能通过端口通信，双向都是
4. 端口从第一天起带版本号

## 关键设计决策（仍在生效，不要重新讨论）

### 分工

**确定性的事实用代码算，需要品味的表达用大模型生成。**
事件判定留在 `events.py`、局势判定留在 `situation.py`、事实句由 `event_card.py`
确定性生成；模型只负责"把这句事实用网友口气重说一遍"。
这条分工是**跨游戏通用的**，战争雷霆适配必须遵循同一形状。

**代码写平实事实，不写社群黑话。** 代码写「开打18秒零杀阵亡」，
「纯白给」由模型说。让代码写黑话等于把品味判断收回代码。

**代码写的评价不得使用内部档位名。** 事实句写「几枪就解决」，
不写档位名 `秒杀`——否则模型照抄，等于又造了一层需要解码的编码。

**场景标签只是事实约束与调性提示，不是语料索引键。**
词库整份注入不做筛选（输入几乎免费：多 1200 token 仅多 0.119 秒）。
按标签去语料库取片段的设计已被推翻——真实事件是多元素组合，
绑死标签集会导致每加一个功能就要重写文风层。

**模型的事实准确率不再是评测指标。** 事实由代码保证。需要检验的是文风好不好听，
**只能由产品负责人肉眼判断**。三件事要分开：事实准不准（代码保证，单元测试）、
会不会漏说（文风取舍，人耳判断）、**会不会编造（仍需运行时闸门防范）**。

### 事实句

**只发一行抬头 + 【刚刚】焦点**，不发完整卡。
收窄模型的判断范围会同时提升准确率、降低成本与延迟。

**事实句中不得出现读数**：具体血量、掉血量、用弹数、秒数一律不写，
改写成评价（残血/丝血、几枪就解决/开了很多枪）。

**时间锚定「正式开打」**，购买阶段用负秒数。模型不该做减法。

**CS 回合内血量只降不升**，禁用「一度」「曾经」这类措辞。
掉血与击杀的先后必须区分：「赢下对枪打成残血」≠「丝血还杀了一个」。

**多杀用概括不逐次罗列**，枪法评价按平均每杀用弹。

**观战期间分段判断**：【我】【全场】来自 player 段必须省略；
【本回合】来自 RoundSituation 按设计只累计本人数据，回合号一致就必须渲染。

### 产品决策

**模板路径永久保留**，模型失败时回退，宠物永不哑火。
**一次事件最多调用一次模型，绝不重试**——重试就是再等几秒，那个情绪瞬间已过去。
不设花费熔断，只做花费显示。预算 ¥20/小时，实际约 ¥0.5/小时。

**单一"网友"性格**：一个正在观战的、了解中文社区梗的玩家。
双性格已取消（`brother.md` / `caster.md` 仅模板路径仍在用）。
人设与词库外置在 `prompts/`，产品负责人可直接编辑、重启生效。

**宠物输出上限 19–30 汉字**（来自 201 条手写语料的 P90 与语音时长）。
**事实句是输入不是输出，不设长度上限**。

**回合结束不发言**，槽位留给视觉层之后的长记忆反馈。
播报触发收敛为击杀 / 死亡 / 特殊事件。

**不做交火期禁言**：及时的情绪反馈本身就是卖点（`alive_priority_threshold` = 0）。

**一波连杀只发一句，播报最终杀数。** 理由是实测出来的物理限制：
连杀内两次击杀约隔 1.8 秒，而一句话从选中到说完约占 4 秒
（模型 0.5–1.5 秒 + 语音 2–3.5 秒），**产得比说得快一倍多**。
逐级播报必然重叠，而任何允许中间级别（三杀）出声的规则，
都会因为它重置了间隔计时器而把最高潮（四杀）挤掉。
代价是连杀那句晚约 2.5 秒出声，产品负责人已实测接受。

**架构分两层**：聚焦事件层只依赖实时数据与【刚刚】那一行；
SA 层依赖时间线 + 视觉，是局内滚动记忆，**M5 视觉层就位后才做**。

### 数据边界（CS2 的 GSI 永久做不到）

- 玩家位置：`player_position` 仅观察者可用
- 场上剩几人、残局：`allplayers_*` 不提供
- 谁杀了你、你杀了谁、对面用什么枪与有多少钱
- 伤害来源（子弹/手雷/其他）：燃烧与致盲有独立字段可归因，手雷不行
- 回合剩余时间：`phase_countdowns` 实测无内容
- 敌方经济与装备对比

**「没打中」也是我们不知道的事**——只知道开了火没拿到击杀，一律说「没打死」。
其中若干项将由 M5 视觉层补上，但**事件检测仍留在代码里**，视觉只做补充。

### 产品负责人纠正过的游戏事实（CS2）

- M4A1 一发爆头秒不了人（至少两发），故「颗秒」只适用于 AK 与沙鹰
- 狙击枪击杀走专属语料，不进枪法五档
- 连杀窗口 5 秒（再杀一个即刷新），与事实句的连续事件段 3 秒各管各的
- 多杀 5 以上只可能出现在休闲
- 「送狙」判据是本回合库存中有过 AWP 且零杀阵亡，不是死亡瞬间手持
- 「打了半天」「磨了一会儿」禁用：CS 里一弹匣打空也就两三秒
- 回合内血量不回复，任何暗示"一度低血"的措辞都不成立
- 低血阈值 ≤30 HP 称丝血/残血；「大后期」指回合最后 20 秒

## 反复出现的失误模式（引以为戒）

这些是本项目实际发生过的错误，同类形状出现过多次。

**把未验证的分析当成诊断。** 曾按回合号分组统计事件间隔，
而录制含三局比赛、回合号重复，于是虚构出一个不存在的缺陷并据此施工一轮。
**规则：从数据得出的结论必须先核对数据的组织结构。**

**声称测试失败是"既有问题"却不复现。** M3-T12 把自己引入的场景回归
报成"非本轮引入"，而在上一个提交上实测为全绿。
**规则：任何"非本轮引入"的声称，必须在上一个提交上实测复现并附运行输出，
否则一律按本轮引入处理。** "我没改那个文件"不构成理由——
改了策略层就会改变哪些事件被选中，下游产物自然跟着变。

**在既有时序机制前后插入新等待，却不重算时间预算。** M3-T12-TUNE 在暂存释放前
加了 2.5 秒收敛等待，却沿用了为"立即释放"设计的 5 秒有效期，
把可用释放窗口压成只有 2.5 秒宽，导致某些场景整句丢失。
**规则：新增任何等待、延迟或收敛窗口时，必须重新推导所有依赖它的时间预算。**

**用自己临时想的关键词自证"没问题"。** 曾用一份手写关键词表扫描输出，
得出"准确率未被高估"，而该表恰好漏掉唯一会命中的词。
**规则：先定下清单，再拿它去扫，不得反过来。**

**验证环境不干净。** 曾因一次遗留的可编辑安装，连续在四个克隆中验证到旧代码，
并据此错误打回一轮正确的提交。
**规则：验证前先确认 `python -c "import pet; print(pet.__file__)"` 指向本次克隆。**

**凭直觉给阈值。** 「被闪 ≥1.5 秒算全白」（实测最长 1.4 秒，永不触发）、
「1.5 秒事件缓冲」（94 次播报合并 0 组）、「事实句 ≤40 字」（把一次五杀砍成两杀）。
**规则：规格中的任何阈值必须附实测依据，或明写"此数待实测确定"。**

**重命名标识符时漏改引用方。** 曾改弹药标签名却明令不许改提示词，
而提示词写死了旧名，导致全线判错。
**规则：重命名任何被提示词引用的标识符时，必须同批同步。**

**加新机制前不查既有机制。** 曾在 policy 层加事件缓冲，
而事实句的「连续事件段」已在做同一件事且粒度更细，实测合并 0 组。

**规则与内容自相矛盾。** 曾把自查规则写成"任何任务 ID 不得同时出现在两边"，
而正文交叉引用与门禁字符串都会出现 ID，导致连续两轮误报停工。
**规则：自查方法必须写明匹配范围。**

**需要人工判读的指标被自动统计代替。** 多样性曾报 3/10，
补齐人工判读后实为 8/10，口径差一倍以上。
**规则：需要人工判读的指标，规格必须把"逐项给出判定与理由"写成硬性验收项。**

**底层行为靠读代码推断。** M1–M2 期间四次误判（拖动区、Tauri 版本支持、
语音覆盖行为、GPU 合成省 CPU），全部被实测推翻。
**规则：涉及浏览器、系统 API、GPU、网络、外部服务时，
规格写成"按 a→b→c 顺序尝试，每步测量，够用就停"。**

## 关键实测数据

**前端**：release 构建空闲 60 秒 4.19% 单核，窗口隐藏时 1.12%。
呼吸动画移到外层 HTML 元素并改 8Hz 更新，较优化前降 88.9%
（注意：单纯提升合成层反而更差，53.59%）。
静态占用约 408 MiB / 7 个 WebView2 进程，是 WebView2 多进程模型的固定成本。

**后端**：启动最坏 0.85 秒；稳定 50 MiB 工作集；30 分钟无泄漏；
`/gsi` 端点中位 0.448ms、P95 0.670ms（接入模型后未退化）。

**语音**：20 字中文出声延迟最坏 0.42 秒；播放中调用停止 1.01 秒内返回。
`speech.py` 的 `speak()` 会先 `stop()`——**新句子掐掉正在播的那句，不是排队**。
此结论已由 M3-T12 系列确认（与更早的 M1 记录相反，以此条为准）。
一句 19–30 汉字约需 2–3.5 秒说完。

**GSI**（休闲模式，详见 `docs/gsi-capabilities.md`）：
- 推送频率有变化时中位 0.314 秒（约 3 Hz），静置约 30 秒心跳。
  **任何依赖"某个时刻会有一帧到来"的机制都必须容忍数秒级的空档**——
  合成场景中曾观测到击杀后 5.86 秒才有下一帧
- 死亡观战队友时 player 段整体切换为被观战者，必须比对两个 steamid
- `flashed` 实测为 0/1 开关量（文档曾误载 0–255）；`smoked` 确为 0–255；
  `burning` 确为 0–255（曾因数据清单只覆盖部分录制而误判为恒零）
- 炸弹状态用 `round.bomb`（实测可用），不要请求 `bomb` 组
- 一次击杀的用弹量必须跨帧累计（同枪间隔 ≤2 秒为一段）：
  按相邻两帧计算会把一梭子切成数段。
  **凡是基于相邻两帧差值的推导都要怀疑跨帧问题**
- 事实句链路 P95 0.49 毫秒，**比 GSI 推送间隔快六百倍，不是瓶颈**

**连杀时序**（合成场景实测，M3-T12 系列）：
- 连杀内两次击杀间隔约 1.8 秒
- 逐级播报会互相打断；把连杀之间的最小间隔依次设为 2.5 / 3.0 / 3.5 / 4.0 秒
  逐一实测，**四个数值全部在 four_kill 场景丢掉最高潮的四杀**——
  只要允许中间级别出声，它就会重置计时器并挤掉后面更高的那一级

**语言模型**（`qwen/qwen3.5-122b-a10b`，锁定 Alibaba，`reasoning_effort="none"`）：
- 事件 P95 约 0.8 秒；输入中位约 2730 token；约 ¥0.5/小时
- **必须显式传 `reasoning_effort="none"`**：`low` 会稳定输出英文推理并耗尽输出预算，
  且慢约 1.6 秒
- **输入几乎免费，输出是延迟大头**：多 1200 输入 token 仅多 0.119 秒，
  多 87 输出 token 多 2.234 秒。因此提示词想写多长写多长，但生产必须坚持单行输出
- 线上调用是**串行队列**：一次只处理一个请求，先进先出，不会乱序，
  但连续多句时后面的会被前面的延迟拖长
- 上游锁定前延迟不可比：OpenRouter 默认多家分发，单次运行出现过八家
- **固定种子会让温度失效**：温度 0.9 下同卡跑 5 次输出逐字相同。
  多样性采样必须不传种子；正式评测保留固定种子以便复现
- 模型调用即使锁定上游仍有 15.7 秒级长尾。这不是缺陷而是既成事实，
  但它决定了线上接入必须配短超时与模板回退

## 约束

AGENTS.md 的修改权限：
coding agent **默认不得修改 AGENTS.md 的任何内容**，只负责在提交中带上它。
唯一例外是同时满足下列全部条件：
1. 产品负责人在对话中**明确打字同意**本轮更新 AGENTS.md
2. 任务规格中附有「AGENTS.md 更新指令」，由若干组精确的旧文本与新文本组成
3. agent 只执行这些替换，**一个字都不得自行增删改写**
4. 任何一组旧文本无法在文件中逐字精确匹配时，**立即停止并报告**，
   严禁改写、严禁"大意相同即可"、严禁跳过该组继续
5. 执行完毕后报告新的第二行与文件总行数，供架构师核对

设立理由：产品负责人有时通过手机操作，无法手动覆盖文件。
但 AGENTS.md 是项目级共识的唯一载体，本项目曾两次因它未被正确更新
而使 coding agent 连续九个任务读到过期内容。
架构师每轮验收的第一个动作仍然是拉仓库、与自己的副本逐字 diff。

禁止修改（coding agent 一律不得改动内容，但需在提交中带上）：
- /docs 下全部文件
- /backend/scenarios 下全部场景文件（回归资产；测试与实现不一致时改实现，
  不许改场景来迁就）

编码规范：
- Python 全量类型注解；跨模块数据必须是 dataclass 或 pydantic 模型，禁止裸 dict
- 后端只绑定 127.0.0.1
- 端口 8737 在前后端各自只允许有一处常量定义
- 密钥只从配置文件或环境变量读取，禁止出现在源码、测试或提交历史
- 大模型型号 ID 与上游服务商名一律作为参数传入，禁止在源码中写死默认值
- **任何提交进仓库的产物（报告、清单、fixtures、日志样例）都不得包含
  真实玩家身份**：`player.name`、`player.steamid`、`provider.steamid` 及其在
  `previously` / `added` 下的对应项，取值一律替换为占位符。仓库是公开的。
  注意 `player.weapons.*.name`、`map.name`、`provider.name` 是武器名、地图名、
  程序名，**不属于身份信息，不得误脱敏**
- 禁止裸 except 后静默吞异常
- 涉及网络调用的自动化测试一律不得真实联网，必须注入假客户端
- 任何读取 player 段的逻辑必须先确认数据主体是本人（subject_is_self）
- Tauri v2 权限默认全关，新增能力必须在 capabilities/default.json 显式授予最小权限
- 窗口尺寸与位置一律以逻辑像素（DIP）为准，按 scale_factor 换算，
  禁止硬编码任何像素补偿常量
- 前端由 Prettier 统一格式化；Rust 必须通过 cargo fmt --check
- 前端视觉组件封装为独立模块，其他代码不得直接访问其内部 DOM
- 实现方案偏离本文件记载的技术栈或分层职责时，必须在完成报告中显式标出
- 每个任务完成后必须 commit 并 push；提交信息以任务 ID 开头

技术债：
1. 连杀暂存的有效期 `follow_up_max_age_seconds = 5` 秒与 2.5 秒收敛窗口叠加后，
   可用释放窗口只剩 2.5 秒宽。合成场景中曾出现下一帧晚 5.86 秒而整句丢失。
   产品负责人已实测确认真实对局中双杀不会丢，故暂不改动。
   **若日后出现"连杀偶尔不出声"，第一嫌疑就是这个数。** 偿还时机：出现症状时
2. 语句多样性不足，词库未被充分利用，需要提示词或温度调整。
   偿还时机：M4-c 之后，在 CS2 适配包内做，届时评测台已移出生产包
3. Steam appmanifest 的 installdir 未做路径归属校验，被篡改的 manifest
   可使程序在预期目录之外覆盖同名配置文件。偿还时机：M10 打包前
4. test_idle_broadcast.py 的四项测试仍在真实等待（套件 13.80 秒）。
   原定修法是注入时钟，尚未做到。这些测试用 IdleConfig.model_construct()
   绕过了生产的 10 秒下限校验，若该配置将来新增校验器会与生产分叉。
   偿还时机：M10
5. 脏字黑名单 `FORBIDDEN_RAW_CURSES` 的单字项会命中常用词：
   「操作」命中「操」、「草丛」命中「草」。「操作」在 CS 解说里是高频词。
   偿还时机：M4-c 重构闸门层时
6. `online_commentary.py` 用 30 汉字作为输出硬上限，而产品目标是模板语料 P90
   的 19 字（约 2 秒语音）。30 字念完约 3 秒，比产品基准长 50%。
   **不得因沿用而变成事实标准**，须用语音时长实测决定。偿还时机：M7 神经语音时
7. speech.py 用 powershell.exe -ExecutionPolicy Bypass -EncodedCommand 调用系统语音，
   该组合是恶意脚本的典型特征，杀毒软件可能拦截。偿还时机：M10 打包前必须验证
8. speech.py 每次朗读新建一个 PowerShell 进程用于合成，是否改为常驻进程待实测。
   偿还时机：M7 神经语音时一并重做
9. requirements.txt 未区分运行依赖与测试依赖，测试链约占 13.9 MiB。偿还时机：M10
10. main.py 的跨域白名单只覆盖开发来源，打包后的实际前端来源尚未确认。
    偿还时机：M10 打包前必须复核，否则安装版会持续显示"连接失败"
11. 项目自身没有 LICENSE，pyproject.toml 也未声明 license。
    依赖许可证已全量核实，无 GPL/AGPL/非商用项。偿还时机：M10 打包前
12. pet.ts 同时承担宠物渲染与窗口拖动的发起逻辑。偿还时机：M9 更换外观时
13. 前端与 Rust 自动化测试覆盖很薄（Rust 仅 3 个纯函数测试，前端无测试）。
    暂不处理，若出现前端逻辑回归再引入最小测试手段
14. 全量后端测试有 4–5 项失败或跳过，均为测试进程无法加载 OneCore 中文语音，
    与业务逻辑无关。需确认是环境问题还是测试设计问题。偿还时机：M10 打包前
15. `prompts/brother.md` 与 `caster.md` 只服务模板路径，线上生产用 `inference.md`。
    双性格已作为产品决策取消，这两份文件与 `[personality]` 配置段的去留待定。
    偿还时机：M4-c

暂不做：
- 读取游戏内存、注入游戏进程、任何规避反作弊的手段（永久禁止）
- 让 AI 操控 CS2 或任何竞技网游（永久禁止，等同作弊）
- macOS / Linux 支持
- 账号系统、云端存储、自动更新
- 让语音或视觉推理占用 GPU
- 预先创建空模块、空目录、占位文件
- 用户自定义性格提示词
- 截图作为待机话术的上下文来源（隐私原因，永久禁止）
- 同时解说多个游戏（一次只服务一个，按前台游戏切换适配）
- 适配的动态加载与插件市场（端口按可升级的形状设计，但现阶段只做同仓库子目录）
- 为 post-MVP 的"AI 代玩"类里程碑做任何端口设计（那是操作输出，与陪玩解说不通用）
- 玩家位置相关的一切功能（CS2 数据层面不可得）
- 手雷伤害归因（CS2 数据层面不可得）
- 敌方经济、装备对比一类判断（需要 allplayers_*，GSI 对普通玩家不提供）
- 一切基于 burning 字段的功能（该字段至今未观测到非零值）
- 把进烟（smoked）做成发言事件（信息量低且会反复触发）
- 请求 phase_countdowns 与顶层 bomb 数据组（已实测确认无内容）

## 工程原则

1. 不留向后兼容：过时代码直接删，不加兼容层、不写 fallback。
   （例外：已有真实数据的存储结构变更须先确认，不得静默丢数据。
   另一例外：模板回退路径是产品决策，不是兼容层，必须保留。）
2. 选满足当前需求的最简实现。不做预防性抽象，不加没人用的配置层。
3. 先跑通最小端到端版本再往上加。绝不为未完成的功能拆掉已经能跑的东西。
4. 关注点分离，按已存在的职责边界拆模块，不为设想中的未来拆。
5. 优先用成熟且在维护的库。没有明确理由不自己造。
6. 加新依赖或自己写之前，先查项目现有依赖能否解决，并说明查过什么。
7. 难以回退的决策（数据模型、核心接口、技术栈）按长期方案定，不接受"先这样以后再换"；
   易替换的实现按原则 2 从简。**游戏适配端口属于此类，按长期形状定义。**
8. 先看成熟产品如何解决同类问题，用已验证的模式，但按本项目规模裁剪。
9. 涉及浏览器、操作系统 API、GPU 合成、网络链路、外部服务这类底层行为时，
   不要把假设写成诊断。规格应写成"按 a→b→c 顺序尝试，每步测量，够用就停"。
   本项目已有四次因违反此条而误判的记录。
10. 修好一处规则之后，主动搜索还有哪些地方适用同一条规则。
    本项目曾因只改 events.py 未查 session.py 而使回合号分叉一个里程碑。
11. 关于"能做什么"的清单，优先用程序从真实数据中生成，而不是由人或 agent 撰写。
    生成出来的清单会自己暴露"这个字段其实一次都没出现过"，撰写出来的不会。
12. 「已实现」与「未实现」是**覆盖语义，不是追加**。任务完成后必须同时
    在「已实现」加一行、并从「未实现」删掉它那一条。

    **自查方法（必须按此执行，不得扩大范围）**：
    只比对两个清单中**以 `- ` 开头的条目行**的任务 ID，
    即正则 `^- (M[0-9]+-T[\d.]+|M[0-9]+)` 在两段中的匹配集合是否有交集。

    **不要在全文范围内统计 ID 出现次数。** 第 2 行的门禁字符串、
    正文中的交叉引用都会出现任务 ID，它们不是清单条目，不构成重复。
    本项目曾因把这条自查理解成全文查重而连续两轮误报停工。
