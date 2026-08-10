# AGENTS.md
最后更新：M3-T1 已验收，M3-T2 下发

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
    bench.py                 离线话术评测台与 CLI
- /backend/tests             后端测试与 fixtures
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
      team, health, armor, helmet, money, equip_value,
      flashed, smoked, burning（均为 0–255 强度值）,
      round_kills, round_killhs, active_weapon, weapons

    全场统计
      match_kills, match_assists, match_deaths, match_mvps, match_score

    字段生命周期规则：解析后无消费者的字段一律不得保留。
    M2-T9 曾据此删除十一个孤儿字段，M3-T2 因富卡出现了消费者而将其中十个恢复。
    这条规则按设计工作，继续执行——不要因为"以后可能有用"而保留无人读取的字段。

WeaponSlot（gsi.py）—— player_weapons 中的一把武器：
    name, type, ammo_clip, ammo_clip_max, ammo_reserve, state
    （state 为 active / holstered 等，用于判断哪把是手持）

RoundSituation（situation.py）—— 单张快照表达不了的本回合累计量：
    flash_count, burn_count, total_damage_taken,
    lowest_health, health_before_death, weapon_switch_count, bought_equipment

    规则：只在数据主体是本人时累计（subject_is_self 为真），
    否则死亡观战期间队友的状态会污染统计。
    回合边界与对局边界都必须重置。

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
      击杀类   round_kill_index, delta, weapon
      多杀     count
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

分层职责：
    gsi         只接收与解析，不判断"发生了什么"
    session     只判会话状态与主体，不做事件检测
    events      只做"发生了什么"（离散事件），不含任何优先级信息
    situation   只做"现在是什么状况"（连续状态与本回合累计），不产出事件
    policy      只做"该不该说"，不生成宠物话术
    commentary  只做模板路径的"说什么"，不改变策略结论
    commentary_templates  只有数据，没有逻辑
    llm         只负责把消息发给模型并把结果带回来，不知道 CS2 的存在
    bench       只做离线评测，绝不被线上链路引用
    replay      只做离线回放与数据清单，绝不被线上链路引用

    events 与 situation 是 GameSnapshot 的两个平行消费者，互不引用。

## 当前状态

当前里程碑：M3 进行中，M3-T1 已验收，M3-T2 已下发。

M3 目标：宠物在 CS2 里说的每一句话都由大模型当场生成，
且**说得准**——准确结合场上数据判断局势，其次才是说得像人话；
生成失败时回退模板不能变哑；花了多少钱在界面上能看见。
产品负责人的验证方式：打两局，听宠物说的话，判断它像不像一个懂游戏的人。

已实现：
- M1：桌面宠物完整形态。透明无边框置顶悬浮窗，可拖动、Ctrl+Alt+P 显隐、
  系统托盘与右键宠物共用六项功能菜单（含语音与自动说话两个运行时开关）；
  代码绘制的宠物，五种表情、8Hz 呼吸动画；文字气泡（打字机、换行、截断、淡出）；
  本地系统语音，可随时中断；前后端常驻 WebSocket；配置文件与随机间隔待机播报；
  交互区域收敛到宠物本体，其余区域点击穿透
- M2：宠物看得懂 CS2。接入官方 GSI 数据接口并自动安装配置文件；
  原始数据录制与离线回放工具；会话状态与数据主体识别；
  八类事件检测（击杀、爆头、多杀、死亡、击杀后被补、白给、回合胜负）；
  发言策略（优先级、场合、冷却、每回合上限、高优先级可打断冷却）；
  双性格模板话术共 201 条；游戏进行中暂停待机播报；菜单顶部显示游戏状态；
  对局生命周期收敛（跨对局重置事实与策略、无消费者不生成、回合号单一实现）
- M3-T1：离线话术评测台。业务无关的 OpenRouter 客户端（非流式、绝不重试、
  密钥只走环境变量）；用真实录制跑通「事实 → 提示词 → 模型 → Markdown 报告」；
  报告随代码提交，作为提示词变更效果的回溯记录

未实现（M3 任务序列，滚动细化）：
- M3-T2 数据层扩容（简单推导，依赖 ≤2 个数据类型）+ 自动生成的数据清单，
  交产品负责人批准后方可进入 T2.5。已下发
- M3-T2.5 复杂推导（依赖 3–5 个数据类型）
- M3-T3 富卡组装 + 评测台升级（锁定上游服务商、系统提示词外置为可编辑文件、
  单变体运行）。与 M3-T1 的已提交报告做前后对照，不再做双变体盲测
- M3-T4 新触发时机（回合开始评论、里程碑类、危险状态类）+ policy 扩展
- M3-T5 事实闸门 + 地图战术知识接入
- M3-T6 线上接入（异步、超时回退模板、密钥配置）+ 模型与服务商横向选型
- M3-T7 花费显示
- M3-T8 里程碑验收：真实对局实测与调优
- M3-T9 局内记忆与回响
- M4 打包分发，朋友可安装
- M5 读屏补充 GSI 拿不到的事实（谁杀了你、场上剩几人）
- M6 外观自定义与心情系统
- M7 跨局长期记忆
- M8 语音对话输入
- M9 AI 玩杀戮尖塔
- M10 AI 玩文明 6

## 关键设计决策（仍在生效，不要重新讨论）

分工原则：**确定性的事实用代码算，需要品味的表达用大模型生成。**
- 事件判定留在 events.py，局势判定留在 situation.py，都不交给大模型。
  理由：击杀数 1→2 是可直接算出的事实；交给模型会带来延迟、成本、漏判和编造
- **M3-T1 为这条原则提供了实证。** 在只给「存活秒数、本回合击杀数、装备价值、
  比分态势、连败轮数」的简报下，模型把普通死亡说成"白给"、把有 1750 块的
  中等经济说成"经济崩了"、给回合失败加上凭空的"别急着上头"。
  **凡是给了事实的地方它都引用正确，凡是没给的地方它就填空。**
  结论：模型的自由度应在于"挑哪个事实来说"，绝不在于"认定发生了什么"
- M3 之后仍完全保留：events.py、policy.py、facts 结构、梗文档
- policy.py 在 M3 的价值变大：它挡掉约九成事件，等于把模型调用砍掉九成
- 视觉（M5）的角色是补充观察，不是替代事件判定。
  且应走 OCR 裁剪区域 + 模板匹配的廉价路线，不是把整张图丢给视觉模型。
  选型时认准 ONNX 轻量运行时，不要重蹈 M1 语音的覆辙（PyTorch 拖进 1.2GB 依赖）

**准确先于文风。** 产品负责人明确排序：让宠物准确说出
「7:7 决胜局守包，残血拖到炸弹爆炸」比句子写得漂亮更重要。
"非人感"主要来自它不懂游戏，而不是来自它措辞生硬。
因此 M3 的工作重心是把 GSI 能给的事实榨干并组织好，而不是雕琢样例池。

**富卡（situation card）取代简报。** 喂给模型的不再是触发事件的孤立事实，
而是一份结构化的当前局面清单（对局 / 我的状态 / 本回合累计 / 全场统计 / 刚刚发生）。
理由见上面那条实证。

M3 已拍板的产品决策：
- **模板路径永久保留。** 模型调用失败时回退模板并在托盘菜单显示模板模式。
  附带收益：模板是判断"模型有没有让宠物变好听"的对照组
- **一次事件最多调用一次模型，绝不重试。** 这既堵住唯一的失控花费路径，
  也是正确的产品行为——重试一次就是再等两秒，那个情绪瞬间已经过去了。
  因此不设花费熔断，只做花费显示。产品负责人可接受的开发期预算是 ¥20/小时
- **性格 = 不同的系统提示词 + 不同的样例池 + 各自的模板兜底。**
  系统提示词自 M3-T3 起外置为产品负责人可直接编辑的文件，
  改完重启后端生效，不经过架构师与 coding agent。
  用户自定义性格推迟到 M4 之后
- **模型输出必须有硬性字数上限**，取现有 201 条语料汉字数分布的 P90（当前为 19），
  即用产品负责人自己的口味当基准。理由：模板长度已知（念完约 2 秒），
  模型不受约束则句子数没变但占用耳朵的时间翻倍
- **点位词分场景放行。** 对玩家状态的即时反应（击杀/死亡/受伤）仍全面禁止点位称呼，
  因为 GSI 不提供玩家位置，说了就是猜。但回合开始类的战术闲聊放行，
  因为那是在描述地图结构，不是在猜玩家在哪
- **地图战术知识来自外部生成、产品负责人把关的 `docs/CS2地图战术知识.md`**，
  不依赖模型自带的地图知识。理由：模型的 CS2 地图知识可能又浅又过时，
  而使用者是懂行的老玩家，一听就穿帮。文档按 GSI 地图代号（de_nuke）分节，
  运行时只注入当前地图那一节。使用者休闲与竞技都打，测试以休闲为主，
  因此文档需区分「通用」与「仅竞技」两类建议
- **局内记忆（M3-T9）必须由代码维护，不能靠模型自己记。**
  让模型"回忆"等于把编造问题重新引进来。正确做法是代码维护本局事件流水，
  需要回响时从中挑出真实条目塞进富卡。"什么值得记"直接复用 policy.py
  已有的优先级排序，不另起一套

阈值处理：situation.py 的判定阈值（残血、穷局、弹将尽）在 M3-T2 一律用模块常量，
不做配置项。等 M3-T3 的富卡报告出来后，由产品负责人指出哪些需要可调再加。

events.py 的 facts 使用中文标签是有意的：score_situation 取值为
"大比分领先 / 领先 / 追平 / 落后 / 大比分落后"。理由是这些 facts 的最终目的地
是中文提示词，转成英文枚举只是多一层翻译。约束：events.py 与 situation.py
除这类取值外不得引入任何话术片段。

不做交火期禁言：原设计在玩家存活且回合进行中把门槛设为 75，理由是脚步声就是命。
产品负责人实测后推翻：及时的情绪反馈本身就是卖点，嫌吵可以用静音开关。
alive_priority_threshold 默认为 0，配置项保留。
M2 实测复核：话密度本身没有问题，问题在触发点单调（集中在击杀/多杀/死亡/回合胜负），
这由 M3-T4 处理，不通过调整策略参数解决。

不做残局与存活人数判断：GSI 不提供 allplayers_*（观战期间实测同样为 0 次）。
存在不精确近似但误报代价高于收益。

不做玩家位置相关的任何功能：player_position 仅观察者可用，普通玩家实测拿不到。
划区、"T 跑到警家了"这类点子在数据层面不可能实现，M5 的 OCR 方案同样给不出坐标。

不做伤害来源归因：GSI 不说这血是子弹、手雷还是燃烧弹打掉的。
燃烧（burning）与致盲（flashed）有独立字段可精确归因，手雷伤害不行。
可以说"被打成血皮了"，不能说"被雷炸的"。

不做逐像素贴合宠物轮廓的点击遮罩：交互矩形收紧后面积已减少 65%。

不做开关状态持久化：两个运行时开关重启后回到 config.toml 的值，这是有意的取舍。

语料相关：
- 语料的最终身份是提示词中的风格样例池，但模板路径本身作为失败兜底与对照组永久保留
- 不强制人称。中文口语大量省略主语，强制加"你"会逼出别扭句子。
  唯一的人称约束是否定的：回合胜负类不得暗示是玩家本人执行的
- 审美规则不由测试管，只保留三条事实类检查：
  回合胜负不冒认、无未替代脏字、无地图名与点位（点位检查按上文分场景执行）
- 「建议」分三档：永远禁止空洞建议（稳住、别急、注意心态）；
  M3-T5 之前不做具体战术建议；M3-T5 起做基于地图知识库的回合开局建议，
  质量标准参考「Nuke 进攻方想快速抢 B 通，可以第一时间架 trophy 影子位」
- 已验证的认知：大模型的默认输出风格和"客服体/教练体"高度重合。
  M3-T1 实测发现，在事实充足的条目上模型引用准确、语气也可接受
  （"AK爆头，这枪真秀"、"连丢俩了"），问题集中在事实不足的条目上。
  因此样例池的价值可能低于原先估计，而喂饱事实的价值高于原先估计

已接受的成本：
- 前端静态占用约 408 MiB 工作集 / 7 个 WebView2 进程。这是 WebView2 多进程模型的
  固定成本。真要降低需放弃 WebView 改原生渲染，那会使 M6 的外观自定义不可行

## 关键实测数据

前端（release 构建，16 逻辑核，空闲 60 秒）：
- 原样 4.19% 单核 / 窗口隐藏时 1.12% 单核
- 呼吸动画从 SVG 内部分组移到外层 HTML 元素，并改为 8Hz 脚本更新，
  较优化前下降 88.9%。注意：单纯做 will-change 合成层提升反而更差（53.59%）

后端：启动最坏 0.85 秒；稳定运行 50 MiB 工作集 / 37 MiB 私有内存；
30 分钟无泄漏；/gsi 端点中位 0.56ms、P95 0.70ms。
测试套件 143 通过 / 13.80 秒（Windows）。

语音：20 字中文出声延迟最坏 0.42 秒；播放中调用停止，1.01 秒内返回。

GSI 实测事实（休闲模式，详见 docs/gsi-capabilities.md）：
- 推送频率：有变化时中位 0.314 秒（约 3 Hz），静置时约 30 秒心跳
- 死亡观战队友时 player 段整体切换为被观战者，必须比对两个 steamid
- round_totaldmg 不提供；phase_countdowns、allplayers_*、player_position 均不可用
- map_round_wins 可用，提供每回合获胜方式
- **CS2 配置中已请求 player_weapons 与 player_match_stats 两组数据，
  游戏一直在推送，M3-T2 之前从未解析。恢复这些字段不需要重装配置或重新录制**

语言模型（M3-T1，OpenRouter，`qwen/qwen3-235b-a22b-2507`，共 24 次调用）：
- 延迟 P50 约 0.87 秒。**这个数字比原先假设的 1–3 秒好得多**，
  加上语音合成 0.35 秒，端到端约 1.2 秒，延迟很可能不构成产品问题
- 但 P95 达 7–9 秒，原因是 OpenRouter 默认把请求分发给多家上游
  （单次运行中出现过八家）。**在锁定上游服务商之前，一切延迟数字都不可比**
- 成本：简报提示词约 230 输入 token，全样例提示词约 3840 输入 token，
  输出均约 10 token。按每小时 150 次调用估算，最贵的配置约 ¥0.5/小时，
  远低于 ¥20/小时的预算上限。**成本不构成 M3 的约束条件**
- 事实性检查（超长、点位词、未替代脏字）在两种提示词下违规均为 0，
  但这三项检查覆盖不到"编造事实"，而编造正是实际发生的问题

## 约束

禁止修改（coding agent 一律不得改动内容，但需在提交中带上）：
- /AGENTS.md
- /docs 下全部文件

编码规范：
- Python 全量类型注解；跨模块数据必须是 dataclass 或 pydantic 模型，禁止裸 dict
- 后端只绑定 127.0.0.1
- 端口 8737 在前后端各自只允许有一处常量定义
- 密钥只从配置文件或环境变量读取，禁止出现在源码、测试或提交历史
- 大模型型号 ID 与上游服务商名一律作为参数传入，禁止在源码中写死默认值。
  各家型号迭代很快（已观察到有模型公布了下架日期），写死会导致某天突然全线报错
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
4. bench 报告中的延迟数字在锁定上游服务商之前不可比：
   OpenRouter 默认多家分发，单次运行出现过八家上游。偿还时机：M3-T3
5. speech.py 用 powershell.exe -ExecutionPolicy Bypass -EncodedCommand 调用系统语音，
   该组合是恶意脚本的典型特征，杀毒软件可能拦截。偿还时机：M4 打包前必须验证
6. speech.py 每次朗读新建一个 PowerShell 进程用于合成。
   M3 发言频率进一步提高后需实测评估是否改为常驻进程
7. requirements.txt 未区分运行依赖与测试依赖，测试链约占 13.9 MiB。偿还时机：M4
8. main.py 的跨域白名单只覆盖开发来源，打包后的实际前端来源尚未确认。
   偿还时机：M4 打包前必须复核，否则安装版会持续显示"连接失败"
9. 项目自身没有 LICENSE，pyproject.toml 也未声明 license。
   依赖许可证已全量核实，无 GPL/AGPL/非商用项。偿还时机：M4 打包前
10. pet.ts 同时承担宠物渲染与窗口拖动的发起逻辑。偿还时机：M6 更换外观时
11. 前端与 Rust 自动化测试覆盖很薄（Rust 仅 3 个纯函数测试，前端无测试）。
    暂不处理，若出现前端逻辑回归再引入最小测试手段
12. 启动时先 show() 再定位窗口，理论上存在瞬时跳位。两次审计以 1ms 轮询均未观察到
13. backend-bridge.ts 的 lastUtteranceId 去重并非规格要求，属防御性代码。观察中

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
