# AGENTS.md
最后更新：M3-T11 已验收，AGENTS.md 精简重写

## 项目概况

目标：一个常驻 Windows 桌面的 AI 电子宠物，在用户玩 CS2 时实时观战、解说、吐槽。

技术栈：
- 后端：Python 3.12.x（>=3.12,<3.13）+ FastAPI 0.141.1 + uvicorn 0.52.1
  + pydantic 2.13.4 + websockets 17.0.1 + httpx 0.28.1。仅绑定 127.0.0.1:8737
- 前端：Tauri 2.11.5 + TypeScript 7.0.2 + Vite 8.2.1 + Prettier 3.9.6，不使用 UI 框架
- 构建环境：Node.js 24.x、npm 11.x、Rust 1.97.x（MSVC）
- 语音：Windows OneCore 系统语音（当前 Microsoft Yaoyao zh-CN），
  经 subprocess 调用 PowerShell 访问 Windows Runtime 合成 WAV，
  再用 ctypes 直调 WinMM waveOut 播放（可从任意线程立即中断）。
  零模型、零第三方依赖、严禁占用 GPU
- 语言模型：**经 OpenRouter 调用，OpenAI 兼容协议**。产品负责人无中国大陆手机号，
  无法注册阿里云百炼，因此不走厂商直连。
  M3-T1 使用的评测基线为 `qwen/qwen3-235b-a22b-2507`，
  **这是基线不是选型结论**；正式横向选型排在 M3-T6。
  **任何代码与文档都不得写死型号名或上游服务商名**，一律作为参数传入
- 截图：Windows Graphics Capture（尚未接入，M5）
- 打包：PyInstaller + Tauri bundler（尚未启用，bundle.active = false）

目录结构：
- /backend/src/pet —— 后端源码包
    main.py                  组装应用、端点、生命周期
    network.py               共享端口常量
    config.py                配置读取与分段校验
    gsi.py                   CS2 数据接收、GameSnapshot 解析、录制、写入 CS2 配置
    session.py               会话状态与数据主体识别
    events.py                事件检测（只做"发生了什么"）
    situation.py             局势累计（只做"现在是什么状况"）
    policy.py                发言策略：优先级、场合、冷却、每回合上限
    commentary.py            事件到模板话术的映射、填空、去重、地图过滤
    commentary_templates.py  模板语料数据（无逻辑）
    commentary_rules.py      共享事实检查黑名单（点位词、未替代脏字）
    lines.py                 待机话术与 Utterance
    speech.py                系统语音合成与播放
    bridge.py                WebSocket 通道、定时广播、运行时开关
    replay.py                录制回放与数据清单工具及 CLI
    llm.py                   语言模型客户端（业务无关）
    event_card.py            把事实渲染成喂给模型的 GSI 事件卡文本
    bench.py                 离线话术评测台与 CLI
- /backend/tests             后端测试与 fixtures
- /backend/prompts/          提示词文件，**产品负责人可直接编辑，改完重启后端生效**，
                             coding agent 不得改动其中的措辞，只负责加载与拼接：
    reading.md    读卡指南、CS 概念词汇表、值得说的组合场景（性格无关，所有性格共用）
    brother.md    损友人设与文风
    caster.md     解说人设与文风
    inference.md  评测专用：只要正确推断、不要文风
- /backend/bench-reports/    评测台产出的对照报告（随代码提交，用于回溯提示词变更效果）
- /backend/config.toml       默认配置（随代码提交）
- /backend/config.local.toml 可选本地覆盖（已忽略）
- /backend/recordings/       GSI 原始录制（已忽略）
- /frontend                  Vite + TypeScript 前端
- /frontend/src-tauri        Tauri 桌面外壳（Rust）
- /frontend/src-tauri/capabilities  Tauri v2 权限声明（默认全关，需逐项授予）
- /docs                      项目文档，由架构师与产品负责人维护，
                             coding agent 不得修改内容：
    gsi-capabilities.md               CS2 数据接口能给什么、不能给什么
    中文CS社群常见梗和语录.md          社群黑话与梗，语料改写依据 + 提示词样例池
    CS2地图战术知识.md                 分地图战术知识库，运行时按当前地图切片注入
                                      （由产品负责人用外部强模型生成，M3-T5 接入）

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
- 后端与前端目前由人手动分别启动，M4 之前不做自动拉起
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
      burning（两次录制约 330 条样本中从未非零，未经验证）,
      round_kills, round_killhs, active_weapon, weapons

    全场统计
      match_kills, match_assists, match_deaths, match_mvps, match_score

    字段生命周期规则：解析后无消费者的字段一律不得保留。
    M2-T9 曾据此删除十一个孤儿字段，M3-T2 因GSI 事件卡出现了消费者而将其中十个恢复。
    这条规则按设计工作，继续执行——不要因为"以后可能有用"而保留无人读取的字段。

WeaponSlot（gsi.py）—— player_weapons 中的一把武器：
    name, type, ammo_clip, ammo_clip_max, ammo_reserve, state
    （state 为 active / holstered 等，用于判断哪把是手持）

RoundSituation（situation.py）—— 单张快照表达不了的本回合累计量：
    flash_count, flashed_seconds_total, longest_flash_seconds,
    smoked_seconds_total, max_smoke_intensity, burn_count,
    total_damage_taken, lowest_health_while_alive, health_before_death,
    primary_weapons_used, bought_equipment,
    bomb_planted_at_ts, seconds_since_bomb_planted

    规则一：只在数据主体是本人时累计（subject_is_self 为真），
    否则死亡观战期间队友的状态会污染统计。
    规则二：回合边界与对局边界都必须重置。
    规则三：观战期间 observe() 返回的是死亡那一回合的旧数据，
    消费者必须比对 round_number 才能使用，不得直接当作本回合状态。
    规则四：**比分、金钱、装备价值一律不复制进本结构。**
    组装GSI 事件卡时直接从 GameSnapshot 取，避免出现两份可能分叉的比分与经济。
    本结构只放"单张快照表达不了的东西"。

    时长类字段一律用相邻快照的 ts 差计算，**不得用 payload 条数换算**：
    GSI 是"有变化就推、最小间隔 0.1 秒、静止时 30 秒心跳"，不是固定频率。

    另含（M3-T5.5 至 T5.9.2 新增）：
      回合阶段标签：开局 / 前期 / 中期 / 后期；下包后按阵营改为 守包(T) / 反攻包点(CT)。
                   未观测到正式开打时返回 None，不猜。
                   **这两个下包后标签含"包点"字样，但产品负责人判定它们表达的是
                   回合相位而非玩家位置，不构成位置声明，予以保留。**
      击杀瞬间的确定性标注：满血或残血、近同时掉血（供概括为赢下对枪）、
                   出烟后 N 秒（供概括为摸烟击杀）、击杀后很快阵亡（供概括为被补）
      投掷物计数、换弹与近似用时、拿包丢包、MVP、燃烧状态变化
      连续事件段（整段跨度不超过 3 秒，只出现一次，段内标唯一「本次焦点」）
      阶段多杀由 event_card 预先累计后标注，模型不再自行计数
      【事件必答】：把模型易漏的确定性关系写成必答项，
                   多杀另生成 30 字以内的确定性推荐骨架

    另含两项（M3-T4 新增）：
      self_team          最后一次数据主体为本人时观测到的阵营。
                         这是对"不复制 GameSnapshot 字段"规则的**有意例外**：
                         观战期间 snapshot.team 是队友的阵营，
                         而回合结算恰恰发生在观战期间
      timeline           本回合的**状态变化流水**，有序，每条带相对本回合起点的秒数

    timeline 的边界（必须严格遵守）：
      它记录的是**状态变化**，不是事件。不得赋予类型语义、优先级或"值不值得说"的判断，
      不得被 policy 或任何发言决策读取。它的唯一消费者是GSI 事件卡渲染，
      以及将来的 M3-T11 局内记忆。
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

    self_team / self_score / opponent_score 由 M3-T5.5 加入，
    目的是把"谁领先"这道题从模型手里收回代码——M3-T5 实测中模型曾把落后读成领先。
      死亡类   survival_seconds, round_kills, seconds_since_last_kill,
               equip_value, score_situation, team_consecutive_round_losses
      回合类   method, score_ct, score_t, score_situation,
               team_consecutive_round_losses

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
                  cooldown_override_priority, minimum_gap_seconds
    [personality] style（brother / caster）

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
    policy      只做"该不该说"，不生成宠物话术
    commentary  只做模板路径的"说什么"，不改变策略结论
    commentary_templates  只有数据，没有逻辑
    event_card  只把已有事实渲染成中文文本，不新增任何判断、不访问网络
    llm         只负责把消息发给模型并把结果带回来，不知道 CS2 的存在
    bench       只做离线评测，绝不被线上链路引用
    replay      只做离线回放与数据清单，绝不被线上链路引用

    events 与 situation 是 GameSnapshot 的两个平行消费者，互不引用。

## 当前状态

当前里程碑：M3 进行中。**核心目标已达成**——产品负责人已在真实对局中实测数局，
宠物说的每一句话由大模型当场生成，效果获认可。

M3 目标：宠物在 CS2 里说的每一句话都由大模型当场生成，且**说得准**——
准确结合场上数据判断局势，其次才是说得像人话；
生成失败时回退模板不能变哑；花了多少钱在界面上能看见。
验证方式：打两局，听宠物说的话。

已实现：
- **M1 桌面宠物**：透明无边框置顶悬浮窗、可拖动、Ctrl+Alt+P 显隐、
  托盘与右键共用六项菜单（含语音与自动说话两个开关）、代码绘制的宠物与五种表情、
  文字气泡、本地系统语音（可随时中断）、前后端常驻 WebSocket、待机播报、
  交互区域收敛到宠物本体
- **M2 看得懂 CS2**：GSI 接入与配置自动安装、原始数据录制与离线回放、
  会话状态与数据主体识别、八类事件检测、发言策略（优先级/场合/冷却/每回合上限）、
  双性格模板话术 201 条、菜单显示游戏状态、对局生命周期收敛
- **M3 事实层**：GameSnapshot 扩容到三组字段；`situation.py` 维护本回合时间线
  （十九种状态变化条目）与 36 个确定性场景标签；`event_card.py` 把事实渲染成
  四段式事实句（抬头/【事件】/【过程】/【场景标签】），**由代码确定性生成、
  单元测试保证、零成本验证**
- **M3 文风层**：`prompts/` 下人设与词库外置由产品负责人维护；
  模型只做"把事实句用网友口气重说一遍"；`hard_gate.py` 从词库的绑定表
  自动解析运行时闸门
- **M3 线上接入**：模型异步接入生产链路（/gsi 中位 0.448ms、P95 0.670ms 未退化）；
  失败/空输出/闸门命中一律回退模板、绝不重试；连续失败 3 次锁定本局模板模式；
  托盘显示 AI/模板模式细分与会话花费
- **M3 评测基建**：离线评测台、35 个合成罕见场景、55 题冻结答案（已降级为
  代码回归网）、双温度对照、多样性采样、词库利用率统计

未实现（滚动细化）：
- M3-T12 追补发言：连杀时第二句能否紧接第一句（需先实测语音打断行为）
- M3-T13 里程碑验收：真实对局实测与调优
- M3-T14 地图战术建议：回合开始触发，可在回合开始前预生成，
  用外部生成的地图知识文档。**排在验收之后**，因为它属于另一类发言，
  其价值必须在游戏里判断
- M4 打包分发，朋友可安装
- M5 读屏补充 GSI 拿不到的事实（谁杀了你、场上剩几人）
- M6 外观自定义与心情系统
- M7 跨局长期记忆
- M8 语音对话输入
- M9 AI 玩杀戮尖塔
- M10 AI 玩文明 6

## 关键设计决策（仍在生效，不要重新讨论）

### 分工

**确定性的事实用代码算，需要品味的表达用大模型生成。**
事件判定留在 `events.py`、局势判定留在 `situation.py`、事实句由 `event_card.py`
确定性生成；模型只负责"把这句事实用网友口气重说一遍"。

**代码写平实事实，不写社群黑话。** 代码写「开打18秒零杀阵亡」，
「纯白给」由模型说。让代码写黑话等于把品味判断收回代码。

**代码写的评价不得使用内部档位名。** 事实句写「几枪就解决」，
不写档位名 `秒杀`——否则模型照抄，等于又造了一层需要解码的编码。

**场景标签只是事实约束与调性提示，不是语料索引键。**
词库整份注入不做筛选（输入几乎免费：多 1200 token 仅多 0.119 秒）。
按标签去语料库取片段的设计已被推翻——真实事件是多元素组合，
且文风层应整体覆盖在情境感知之上，绑死标签集会导致每加一个功能就要重写文风层。

**模型的事实准确率不再是评测指标。** 链路变成「代码写事实 → 文风层润色」之后，
事实由代码保证，程序对准确率就是 100%。需要检验的是文风好不好听，
**只能由产品负责人肉眼判断**。三件事要分开：事实准不准（代码保证，单元测试）、
会不会漏说（文风取舍，人耳判断）、**会不会编造（仍需运行时闸门防范）**。
55 题冻结答案降级保留，作为改代码时的回归网。

### 事实句

**只发一行抬头 + 【刚刚】焦点**，不发完整卡。
收窄模型的判断范围会同时提升准确率、降低成本与延迟。

**事实句中不得出现读数**：具体血量、掉血量、用弹数、秒数一律不写，
改写成评价（残血/丝血、几枪就解决/开了很多枪）。

**时间锚定「正式开打」**，购买阶段用负秒数。模型不该做减法。

**CS 回合内血量只降不升**，禁用「一度」「曾经」这类措辞。
掉血与击杀的先后必须区分：「赢下对枪打成残血」≠「丝血还杀了一个」。

**互相矛盾或蕴含的标签由代码消解**，只保留信息量最大的那个。
标签由代码限 3 个并按观战关注顺序排序。

**多杀用概括不逐次罗列**，枪法评价按平均每杀用弹。

**观战期间分段判断**：【我】【全场】来自 player 段必须省略；
【本回合】来自 RoundSituation 按设计只累计本人数据，回合号一致就必须渲染。

### 产品决策

**模板路径永久保留**，模型失败时回退，宠物永不哑火。
**一次事件最多调用一次模型，绝不重试**——重试就是再等几秒，那个情绪瞬间已过去。
不设花费熔断，只做花费显示。预算 ¥20/小时，实际约 ¥0.5/小时。

**单一"网友"性格**：一个正在观战的、了解中文社区梗的玩家。
双性格已取消。人设与词库外置在 `prompts/`，产品负责人可直接编辑、重启生效。

**宠物输出上限 19–30 汉字**（来自 201 条手写语料的 P90 与语音时长）。
**事实句是输入不是输出，不设长度上限**。

**回合结束不发言**，槽位留给 OCR 之后的长记忆反馈。
播报触发收敛为击杀 / 死亡 / 特殊事件。

**架构分两层**：聚焦事件层只依赖 GSI 与【刚刚】那一行；
SA 层依赖 GSI 时间线 + OCR，是局内滚动记忆，**M5 视觉层就位后才做**。
长记忆反馈、`MatchSituation`、随机吐槽均推迟到 OCR 之后。

**当前配置已经够用**：温度 0.9、无额外多样性约束，实质多样性 8/10。
提高温度或加约束各只多 1 张卡，却都要吐回准确率。

### 数据边界（永久做不到）

- 玩家位置：`player_position` 仅观察者可用
- 场上剩几人、残局：`allplayers_*` 不提供
- 谁杀了你、你杀了谁、对面用什么枪与有多少钱
- 伤害来源（子弹/手雷/其他）：燃烧与致盲有独立字段可归因，手雷不行
- 回合剩余时间：`phase_countdowns` 实测无内容
- 敌方经济与装备对比

**「没打中」也是我们不知道的事**——只知道开了火没拿到击杀，
一律说「没打死」。

### 产品负责人纠正过的游戏事实

- M4 一发爆头秒不了人（至少两发），故「颗秒」只适用于 AK 与沙鹰
- 狙击枪击杀走专属语料，不进枪法五档
- 连杀窗口 5 秒（再杀一个即刷新），与事件卡的连续事件段 3 秒各管各的
- 多杀 5 以上只可能出现在休闲
- 「送狙」判据是本回合库存中有过 AWP 且零杀阵亡，不是死亡瞬间手持
- 「打了半天」「磨了一会儿」禁用：CS 里一弹匣打空也就两三秒
- 不做交火期禁言：及时的情绪反馈本身就是卖点（`alive_priority_threshold` = 0）

## 反复出现的失误模式（引以为戒）

这些是本项目实际发生过的错误，同类形状出现过多次。

**把未验证的分析当成诊断。** 曾按回合号分组统计事件间隔，
而录制含三局比赛、回合号重复，于是虚构出一个不存在的"重复死亡"缺陷并据此施工一轮。
**规则：从数据得出的结论必须先核对数据的组织结构。**

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
更根本的修法是取消编解码层——事实句直接写人话。

**加新机制前不查既有机制。** 曾在 policy 层加事件缓冲，
而事件卡的「连续事件段」已在做同一件事且粒度更细，实测合并 0 组。

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
`speech.py` 的 `speak()` 会先 `stop()`——**新句子掐掉正在播的那句，不是排队**
（此结论与 M1 记录相反，待 M3-T12 实测确认）。

**GSI**（休闲模式，详见 `docs/gsi-capabilities.md`）：
- 推送频率有变化时中位 0.314 秒（约 3 Hz），静置约 30 秒心跳
- 死亡观战队友时 player 段整体切换为被观战者，必须比对两个 steamid
- `flashed` 实测为 0/1 开关量（文档曾误载 0–255）；`smoked` 确为 0–255；
  `burning` 确为 0–255（曾因数据清单只覆盖部分录制而误判为恒零）
- 炸弹状态用 `round.bomb`（实测可用），不要请求 `bomb` 组
- 一次击杀的用弹量必须跨帧累计（同枪间隔 ≤2 秒为一段）：
  按相邻两帧计算会把一梭子切成数段，曾使「干净解决」四分之三是假的。
  **凡是基于相邻两帧差值的推导都要怀疑跨帧问题**
- 事件卡链路 P95 0.49 毫秒，**比 GSI 推送间隔快六百倍，不是瓶颈**

**语言模型**（`qwen/qwen3.5-122b-a10b`，锁定 Alibaba，`reasoning_effort="none"`）：
- 事件 P95 约 0.8 秒；输入中位约 2730 token；约 ¥0.5/小时
- **必须显式传 `reasoning_effort="none"`**：`low` 会稳定输出英文推理并耗尽输出预算，
  且慢约 1.6 秒
- **输入几乎免费，输出是延迟大头**：多 1200 输入 token 仅多 0.119 秒，
  多 87 输出 token 多 2.234 秒。因此提示词想写多长写多长，但生产必须坚持单行输出
- 上游锁定前延迟不可比：OpenRouter 默认多家分发，单次运行出现过八家
- **固定种子会让温度失效**：温度 0.9 下同卡跑 5 次输出逐字相同。
  多样性采样必须不传种子；正式评测保留固定种子以便复现

## 约束

AGENTS.md 的修改权限（M3-T5.5 起生效）：
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
因此授予的是"机械执行替换"的权限，不是"自行维护文档"的权限。
架构师每轮验收的第一个动作仍然是拉仓库、与自己的副本逐字 diff。

禁止修改（coding agent 一律不得改动内容，但需在提交中带上）：
- /docs 下全部文件

编码规范：
- Python 全量类型注解；跨模块数据必须是 dataclass 或 pydantic 模型，禁止裸 dict
- 后端只绑定 127.0.0.1
- 端口 8737 在前后端各自只允许有一处常量定义
- 密钥只从配置文件或环境变量读取，禁止出现在源码、测试或提交历史
- 大模型型号 ID 与上游服务商名一律作为参数传入，禁止在源码中写死默认值。
  各家型号迭代很快（已观察到有模型公布了下架日期），写死会导致某天突然全线报错
- **任何提交进仓库的产物（报告、清单、fixtures、日志样例）都不得包含
  真实玩家身份**：`player.name`、`player.steamid`、`provider.steamid` 及其在
  `previously` / `added` 下的对应项，取值一律替换为占位符。
  仓库是公开的，同局陌生人的昵称与 SteamID 对照表不该被发布。
  注意 `player.weapons.*.name`、`map.name`、`provider.name` 是武器名、地图名、
  程序名，**不属于身份信息，不得误脱敏**
- 禁止裸 except 后静默吞异常
- 涉及网络调用的自动化测试一律不得真实联网，必须注入假客户端
- 任何读取 player 段的逻辑必须先确认数据主体是本人（subject_is_self），
  否则死亡观战期间会把队友的数据当成玩家自己的
- Tauri v2 权限默认全关，新增能力必须在 capabilities/default.json 显式授予最小权限
- 窗口尺寸与位置一律以逻辑像素（DIP）为准，按 scale_factor 换算，
  禁止硬编码任何像素补偿常量
- 前端由 Prettier 统一格式化；Rust 必须通过 cargo fmt --check
- 前端视觉组件封装为独立模块，其他代码不得直接访问其内部 DOM
- 实现方案偏离本文件记载的技术栈或分层职责时，必须在完成报告中显式标出
- 每个任务完成后必须 commit 并 push；提交信息以任务 ID 开头

技术债：
1. Steam appmanifest 的 installdir 未做路径归属校验，被篡改的 manifest
   可使程序在预期目录之外覆盖同名配置文件。偿还时机：M4 打包前
2. test_idle_broadcast.py 的四项测试仍在真实等待，只是把等待从 10 秒级缩短到
   1 秒级（套件从 65 秒降至 13.80 秒）。原定的修法是注入时钟，尚未做到。
   另外这些测试用 IdleConfig.model_construct() 绕过了生产的 10 秒下限校验，
   若 IdleConfig 将来新增带校验器的字段，测试与生产会分叉。偿还时机：M4
3. 三条语料事实检查是有限黑名单，只能拦截已枚举的词。
   M3 之后语料不再是输出，这三项必须从"测试时检查静态语料"改为
   "运行时拦截模型输出"，并新增"编造事实"这一类检查。偿还时机：M3-T5
4. 模型调用即使锁定上游仍有 15.7 秒级长尾。这不是缺陷而是既成事实，
   但它决定了线上接入必须配短超时与模板回退。M3-T8 落地时必须实测超时阈值
5. 脏字黑名单 `FORBIDDEN_RAW_CURSES` 的单字项会命中常用词：
   「操作」命中「操」、「草丛」命中「草」。「操作」在 CS 解说里是高频词。
   该黑名单在 M3-T7 会变成运行时闸门，误报会直接拦掉正常输出。
   偿还时机：M3-T5.5（点位词的同类问题已在 M3-T5 修复）
6. speech.py 用 powershell.exe -ExecutionPolicy Bypass -EncodedCommand 调用系统语音，
   该组合是恶意脚本的典型特征，杀毒软件可能拦截。偿还时机：M4 打包前必须验证
7. speech.py 每次朗读新建一个 PowerShell 进程用于合成。
   M3 发言频率进一步提高后需实测评估是否改为常驻进程
8. requirements.txt 未区分运行依赖与测试依赖，测试链约占 13.9 MiB。偿还时机：M4
9. main.py 的跨域白名单只覆盖开发来源，打包后的实际前端来源尚未确认。
   偿还时机：M4 打包前必须复核，否则安装版会持续显示"连接失败"
10. 项目自身没有 LICENSE，pyproject.toml 也未声明 license。
   依赖许可证已全量核实，无 GPL/AGPL/非商用项。偿还时机：M4 打包前
11. pet.ts 同时承担宠物渲染与窗口拖动的发起逻辑。偿还时机：M6 更换外观时
12. 前端与 Rust 自动化测试覆盖很薄（Rust 仅 3 个纯函数测试，前端无测试）。
    暂不处理，若出现前端逻辑回归再引入最小测试手段
13. 启动时先 show() 再定位窗口，理论上存在瞬时跳位。两次审计以 1ms 轮询均未观察到
14. backend-bridge.ts 的 lastUtteranceId 去重并非规格要求，属防御性代码。观察中
15. `event_card.py` 从 `commentary_templates` 引入 `WIN_METHOD_LABELS`，
    跨越了"事件卡渲染层"与"模板语料层"的边界。偿还时机：M3-T7
16. 评测放宽值 30 字尚未回归产品目标。产品目标仍是模板语料 P90 的 19 字
    （约 2 秒语音）。30 字念完约 3 秒，比产品基准长 50%，
    而产品负责人已确认当前话密度正好。
    **必须在线上接入前用语音时长实测决定，不得因沿用而变成事实标准。**
    偿还时机：M3-T10 之前
17. 【事件必答】使平均输入涨到约 4935 token。延迟仍达标，
    但提示词中可能已有被它取代的旧规则。偿还时机：有稳定 held-out 集之后
18. 全量后端测试有 4 项失败，均为测试进程无法加载 OneCore 中文语音，
    与业务逻辑无关。需确认是环境问题还是测试设计问题。偿还时机：M4 打包前

暂不做：
- 读取游戏内存、注入游戏进程、任何规避反作弊的手段（永久禁止）
- 让 AI 操控 CS2 或任何竞技网游（永久禁止，等同作弊）
- macOS / Linux 支持
- 账号系统、云端存储、自动更新
- 让语音或视觉推理占用 GPU
- 预先创建空模块、空目录、占位文件
- 用户自定义性格提示词（M4 之后再议）
- 截图作为待机话术的上下文来源（隐私原因，永久禁止）
- 玩家位置相关的一切功能（数据层面不可得）
- 手雷伤害归因（数据层面不可得）
- 敌方经济、装备对比、"我方装备落后于对面"一类判断：
  需要 allplayers_* 才能知道对面的钱和枪，GSI 对普通玩家不提供，永久不做
- 一切基于 burning 字段的功能（该字段至今未观测到非零值）
- 把进烟（smoked）做成发言事件（信息量低且会反复触发）
- 请求 phase_countdowns 与顶层 bomb 数据组（已实测确认无内容）

## 工程原则

1. 不留向后兼容：过时代码直接删，不加兼容层、不写 fallback。
   （例外：已有真实数据的存储结构变更须先确认，不得静默丢数据。
   另一例外：M3 的模板回退路径是产品决策，不是兼容层，必须保留。）
2. 选满足当前需求的最简实现。不做预防性抽象，不加没人用的配置层。
3. 先跑通最小端到端版本再往上加。绝不为未完成的功能拆掉已经能跑的东西。
4. 关注点分离，按已存在的职责边界拆模块，不为设想中的未来拆。
5. 优先用成熟且在维护的库。没有明确理由不自己造。
6. 加新依赖或自己写之前，先查项目现有依赖能否解决，并说明查过什么。
7. 难以回退的决策（数据模型、核心接口、技术栈）按长期方案定，不接受"先这样以后再换"；
   易替换的实现按原则 2 从简。
8. 先看成熟产品如何解决同类问题，用已验证的模式，但按本项目规模裁剪，
   不照搬为更大规模设计的方案。
9. 涉及浏览器、操作系统 API、GPU 合成、网络链路、外部服务这类底层行为时，
   不要把假设写成诊断。规格应写成"按 a→b→c 顺序尝试，每步测量，够用就停"，
   让实测来选。本项目已有四次因违反此条而误判的记录。
10. 修好一处规则之后，主动搜索还有哪些地方适用同一条规则。
    本项目曾因只改 events.py 未查 session.py 而使回合号分叉一个里程碑。
11. 关于"能做什么"的清单，优先用程序从真实数据中生成，而不是由人或 agent 撰写。
    生成出来的清单会自己暴露"这个字段其实一次都没出现过"，撰写出来的不会。
12. 「已实现」与「未实现」是**覆盖语义，不是追加**。任务完成后必须同时
    在「已实现」加一行、并从「未实现」删掉它那一条。
    本项目曾因只加不删使三条任务在两个清单中重复了四轮。

    **自查方法（必须按此执行，不得扩大范围）**：
    只比对两个清单中**以 `- ` 开头的条目行**的任务 ID，
    即正则 `^- (M3-T[\d.]+|M[0-9]+)` 在两段中的匹配集合是否有交集。

    **不要在全文范围内统计 ID 出现次数。** 第 2 行的门禁字符串、
    正文中的交叉引用（例如"将来的 M3-T11 局内记忆"）都会出现任务 ID，
    它们不是清单条目，不构成重复。
    本项目曾因把这条自查理解成全文查重而连续两轮误报停工。
