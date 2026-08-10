# AGENTS.md
最后更新：M2 里程碑审计完成（M2-T9 加固待做）

## 项目概况

目标：一个常驻 Windows 桌面的 AI 电子宠物，在用户玩 CS2 时实时观战、解说、吐槽。

技术栈：
- 后端：Python 3.12.x（>=3.12,<3.13）+ FastAPI 0.141.1 + uvicorn 0.52.1
  + pydantic 2.13.4 + websockets 17.0.1。仅绑定 127.0.0.1:8737
- 前端：Tauri 2.11.5 + TypeScript 7.0.2 + Vite 8.2.1 + Prettier 3.9.6，不使用 UI 框架
- 构建环境：Node.js 24.x、npm 11.x、Rust 1.97.x（MSVC）
- 语音：Windows OneCore 系统语音（当前 Microsoft Yaoyao zh-CN），
  经 subprocess 调用 PowerShell 访问 Windows Runtime 合成 WAV，
  再用 ctypes 直调 WinMM waveOut 播放（可从任意线程立即中断）。
  零模型、零第三方依赖、严禁占用 GPU
- 语言/视觉模型：阿里云百炼 通义千问 Qwen3-VL 系列，走 OpenAI 兼容协议（M3 接入）
- 截图：Windows Graphics Capture（尚未接入，M5）
- 打包：PyInstaller + Tauri bundler（尚未启用，bundle.active = false）

目录结构：
- /backend/src/pet —— 后端源码包
    main.py                  组装应用、端点、生命周期
    network.py               共享端口常量
    config.py                配置读取与分段校验
    gsi.py                   CS2 数据接收、GameSnapshot 解析、录制、写入 CS2 配置
    session.py               会话状态与数据主体识别
    events.py                事件检测；另含录制回放工具与 CLI（待拆分）
    policy.py                发言策略：优先级、场合、冷却、每回合上限
    commentary.py            事件到话术的映射、填空、去重、地图过滤
    commentary_templates.py  语料数据
    lines.py                 待机话术与 Utterance
    speech.py                系统语音合成与播放
    bridge.py                WebSocket 通道、定时广播、运行时开关
- /backend/tests             后端测试与 fixtures
- /backend/config.toml       默认配置（随代码提交）
- /backend/config.local.toml 可选本地覆盖（已忽略）
- /backend/recordings/       GSI 原始录制（已忽略）
- /frontend                  Vite + TypeScript 前端
- /frontend/src-tauri        Tauri 桌面外壳（Rust）
- /frontend/src-tauri/capabilities  Tauri v2 权限声明（默认全关，需逐项授予）
- /docs                      项目文档，由架构师与产品负责人维护，
                             coding agent 不得修改内容：
    gsi-capabilities.md               CS2 数据接口能给什么、不能给什么
    中文CS社群常见梗和语录.md          社群黑话与梗，语料改写依据 + M3 提示词样例池

常用命令（Windows PowerShell）：
- 后端安装：cd backend && python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
- 后端运行：.venv\Scripts\python -m pet.main
- 后端测试：.venv\Scripts\python -m pytest
- 录制回放：.venv\Scripts\python -m pet.events --replay <文件> [--with-policy]
- 安装 CS2 接入文件：.venv\Scripts\python -m pet.gsi --install
- 前端安装：cd frontend && npm.cmd install
- 前端开发运行：npm.cmd run tauri dev（需先启动后端）
- 前端构建与格式检查：npm.cmd run build / npm.cmd run format:check

约定：
- Windows PowerShell 中前端命令一律使用 npm.cmd。python 必须可从 PowerShell 直接调用，
  否则用 py -3.12 或完整路径
- 后端与前端目前由人手动分别启动，M4 之前不做自动拉起

## 核心契约

改动前必须先与架构师确认。以下定义与代码实现必须逐字段一致。

GameSnapshot（gsi.py）—— CS2 某一瞬间的状态，全部字段可为 None：
    ts, player_steamid, provider_steamid, activity,
    map_mode, map_name, map_phase, round_number, round_phase, round_win_team,
    round_wins（每回合获胜方式历史）, bomb,
    team, health, armor, helmet, flashed, smoked, burning,
    money, equip_value, round_kills, round_killhs,
    match_kills, match_assists, match_deaths, match_mvps, match_score,
    score_ct, score_t, ct_consecutive_round_losses, t_consecutive_round_losses,
    active_weapon

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

回合号规则（必须只有一处实现，两处曾分叉过）：
    round_phase 为 "over"，或 round_win_team 有值 → 该快照描述刚结束的回合，
                                                    回合号 = map.round
    其余阶段 → 描述进行中的回合，回合号 = map.round + 1

分层职责（M2-T9 将收敛到位）：
    gsi         只接收与解析，不判断"发生了什么"
    session     只判会话状态与主体，不做事件检测
    events      只做事实判断，不含任何优先级或"值不值得说"的信息
    policy      只做"该不该说"，不生成宠物话术
    commentary  只做"说什么"，不改变策略结论
    commentary_templates  只有数据，没有逻辑

## 当前状态

当前里程碑：M2 已完成全部任务，里程碑审计已完成，等待 M2-T9 加固后结案。

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
  双性格模板话术；游戏进行中暂停待机播报；菜单顶部显示游戏状态

未实现：
- M2-T9 里程碑加固（见技术债前五条），M2 结案的前置条件
- M3 接入通义千问，实时生成话术替代模板，双性格，成本控制与显示
- M4 打包分发，朋友可安装
- M5 读屏补充 GSI 拿不到的事实（谁杀了你、场上剩几人）
- M6 外观自定义与心情系统
- M7 跨局长期记忆
- M8 语音对话输入
- M9 AI 玩杀戮尖塔
- M10 AI 玩文明 6

## 关键设计决策（仍在生效，不要重新讨论）

分工原则：**确定性的事实用代码算，需要品味的表达用大模型生成。**
- 事件判定留在 events.py，不交给大模型。理由：击杀数 1→2 是可直接算出的事实；
  交给模型会带来 1-3 秒延迟、每秒三次推送的天价成本、以及漏判与编造；
  且已踩过的六个坑（冷启动、回合归零、跨多杀差值、热身、观战身份、结算重复）
  都要塞进提示词，模型仍会时不时搞错
- M3 之后仍完全保留：events.py、policy.py、facts 结构、梗文档
- policy.py 在 M3 的价值变大：它挡掉约九成事件，等于把模型调用砍掉九成，
  从"防吵"变成"防烧钱"
- 视觉（M5）的角色是补充观察，不是替代事件判定。
  且应走 OCR 裁剪区域 + 模板匹配的廉价路线，不是把整张图丢给视觉模型。
  选型时认准 ONNX 轻量运行时，不要重蹈 M1 语音的覆辙（PyTorch 拖进 1.2GB 依赖）

不做交火期禁言：原设计在玩家存活且回合进行中把门槛设为 75，理由是脚步声就是命。
产品负责人实测后推翻：及时的情绪反馈本身就是卖点，嫌吵可以用静音开关。
alive_priority_threshold 默认为 0，配置项保留。

不做残局与存活人数判断：GSI 不提供 allplayers_*（观战期间实测同样为 0 次）。
存在不精确近似但误报代价高于收益。

不做逐像素贴合宠物轮廓的点击遮罩：交互矩形收紧后面积已减少 65%，
产品负责人认可贴合度。

不做开关状态持久化：两个运行时开关重启后回到 config.toml 的值，这是有意的取舍。

语料相关：
- 语料的最终身份是 M3 提示词中的风格样例池，不是逐条使用的输出
- 不强制人称。中文口语大量省略主语，强制加"你"会逼出别扭句子。
  唯一的人称约束是否定的：回合胜负类不得暗示是玩家本人执行的
  （GSI 不提供获胜的具体执行者）
- 审美规则不由测试管，只保留三条事实类检查：
  回合胜负不冒认、无未替代脏字、无地图名与点位
- 点位称呼一律禁止：GSI 不提供玩家位置，说了就是瞎猜。
  地图名可用，但条目必须标注 applicable_maps
- 「建议」分三档：永远禁止空洞建议（稳住、别急、注意心态）；
  M2 不做具体战术建议；后续里程碑可做按 地图 × 阵营 × 回合阶段 的道具站位建议，
  质量标准参考「Nuke 进攻方想快速抢 B 通，可以第一时间架 trophy 影子位」。
  风险提示：通用大模型的 CS2 战术知识可能浅且过时，真要做很可能需要人工战术库

已接受的成本：
- 前端静态占用约 408 MiB 工作集 / 7 个 WebView2 进程。这是 WebView2 多进程模型的
  固定成本。真要降低需放弃 WebView 改原生渲染，那会使 M6 的外观自定义与
  Live2D 目标不可行。该成本在阶段 2 选择 Tauri 时已一并接受

## 关键实测数据

前端（release 构建，16 逻辑核，空闲 60 秒）：
- 原样 4.19% 单核 / 窗口隐藏时 1.12% 单核
- 呼吸动画从 SVG 内部分组移到外层 HTML 元素，并改为 8Hz 脚本更新，
  较优化前下降 88.9%。注意：单纯做 will-change 合成层提升反而更差（53.59%）

后端：启动最坏 0.85 秒；稳定运行 50 MiB 工作集 / 37 MiB 私有内存；
30 分钟无泄漏；/gsi 端点中位 0.56ms、P95 0.70ms。

语音：20 字中文出声延迟最坏 0.42 秒；播放中调用停止，1.01 秒内返回。

GSI 实测事实（休闲模式，详见 docs/gsi-capabilities.md）：
- 推送频率：有变化时中位 0.314 秒（约 3 Hz），静置时约 30 秒心跳
- 死亡观战队友时 player 段整体切换为被观战者，必须比对两个 steamid
- round_totaldmg 不提供；phase_countdowns、bomb 组、allplayers_* 均不可用
- map_round_wins 可用，提供每回合获胜方式
- previously / added 增量段的存在是"接收链路健康"的信号

## 约束

禁止修改（coding agent 一律不得改动内容，但需在提交中带上）：
- /AGENTS.md
- /docs 下全部文件

编码规范：
- Python 全量类型注解；跨模块数据必须是 dataclass 或 pydantic 模型，禁止裸 dict
- 后端只绑定 127.0.0.1
- 端口 8737 在前后端各自只允许有一处常量定义
- 密钥只从配置文件或环境变量读取，禁止出现在源码、测试或提交历史
- 禁止裸 except 后静默吞异常
- Tauri v2 权限默认全关，新增能力必须在 capabilities/default.json 显式授予最小权限
- 窗口尺寸与位置一律以逻辑像素（DIP）为准，按 scale_factor 换算，
  禁止硬编码任何像素补偿常量
- 前端由 Prettier 统一格式化；Rust 必须通过 cargo fmt --check
- 前端视觉组件封装为独立模块，其他代码不得直接访问其内部 DOM
- 实现方案偏离本文件记载的技术栈或分层职责时，必须在完成报告中显式标出
- 每个任务完成后必须 commit 并 push；提交信息以任务 ID 开头

技术债（前五条为 M2 结案与 M3 开工的前置条件）：
1. SpeechPolicy 的冷却、回合计数、本人血量只在构造时初始化，跨对局不重置。
   M3 中该层直接控制模型调用与费用，必须先建立明确的对局生命周期
2. EventDetector 的 _subjects 基线在 menu / warmup / match_over 均不清空。
   旧基线会与新局统计做差产生假事件，且字典会长期累积
3. 回合号有两套实现且已分叉：session.py 对除 warmup 外所有状态 +1，
   events.py 对 over 或有胜方时用原值。同一时刻 GameState.round 与
   GameEvent.round_number 相差 1，且 session 测试断言了错误值
4. 没有"无消费者不生成"的成本闸门：无论是否有 WebSocket 客户端，
   都会先完成检测、策略、生成。M3 换成模型后会在无人看的情况下持续付费
5. 分层职责尚未达到本文件的约定：events.py 含 CLI 与格式化，
   policy.py 含中文诊断文本与无调用者的回放函数，
   commentary.py 编排检测与策略且内嵌中文片段，templates.py 含地图匹配逻辑
6. 根级未知配置段被静默接受且无告警。M3 会新增模型与密钥配置，拼错会造成意外费用
7. Steam appmanifest 的 installdir 未做路径归属校验，被篡改的 manifest
   可使程序在预期目录之外覆盖同名配置文件。偿还时机：M4 打包前
8. 后端测试套件约 62 秒，其中约 58 秒是定时广播测试的真实等待。
   已越过既定的 60 秒阈值，应为这些测试注入时钟
9. GameSnapshot 中 bomb、armor、helmet、flashed、smoked、burning、
   match_kills、match_assists、match_mvps、match_score 解析后无任何消费者；
   money 只用于日志。偿还时机：M3 前删除或明确写出消费者
10. 三条语料事实检查是有限黑名单，只能拦截已枚举的词，不能证明完整语义规则
11. speech.py 用 powershell.exe -ExecutionPolicy Bypass -EncodedCommand 调用系统语音，
    该组合是恶意脚本的典型特征，杀毒软件可能拦截。偿还时机：M4 打包前必须验证
12. speech.py 每次朗读新建一个 PowerShell 进程用于合成。
    M2 发言频率提高后需实测评估是否改为常驻进程
13. requirements.txt 未区分运行依赖与测试依赖，测试链约占 13.9 MiB。偿还时机：M4
14. main.py 的跨域白名单只覆盖开发来源，打包后的实际前端来源尚未确认。
    偿还时机：M4 打包前必须复核，否则安装版会持续显示"连接失败"
15. 项目自身没有 LICENSE，pyproject.toml 也未声明 license。
    依赖许可证已全量核实，无 GPL/AGPL/非商用项。偿还时机：M4 打包前
16. pet.ts 同时承担宠物渲染与窗口拖动的发起逻辑。偿还时机：M6 更换外观时
17. 前端与 Rust 自动化测试覆盖很薄（Rust 仅 3 个纯函数测试，前端无测试）。
    暂不处理，若出现前端逻辑回归再引入最小测试手段
18. 启动时先 show() 再定位窗口，理论上存在瞬时跳位。两次审计以 1ms 轮询均未观察到
19. backend-bridge.ts 的 lastUtteranceId 去重并非规格要求，属防御性代码。观察中
20. README 内容已明显过期，仍称宠物不会根据游戏事件发言

暂不做：
- 读取游戏内存、注入游戏进程、任何规避反作弊的手段（永久禁止）
- 让 AI 操控 CS2 或任何竞技网游（永久禁止，等同作弊）
- macOS / Linux 支持
- 账号系统、云端存储、自动更新
- 让语音或视觉推理占用 GPU
- 预先创建空模块、空目录、占位文件

## 工程原则

1. 不留向后兼容：过时代码直接删，不加兼容层、不写 fallback。
   （例外：已有真实数据的存储结构变更须先确认，不得静默丢数据。）
2. 选满足当前需求的最简实现。不做预防性抽象，不加没人用的配置层。
3. 先跑通最小端到端版本再往上加。绝不为未完成的功能拆掉已经能跑的东西。
4. 关注点分离，按已存在的职责边界拆模块，不为设想中的未来拆。
5. 优先用成熟且在维护的库。没有明确理由不自己造。
6. 加新依赖或自己写之前，先查项目现有依赖能否解决，并说明查过什么。
7. 难以回退的决策（数据模型、核心接口、技术栈）按长期方案定，不接受"先这样以后再换"；
   易替换的实现按原则 2 从简。
8. 先看成熟产品如何解决同类问题，用已验证的模式，但按本项目规模裁剪，
   不照搬为更大规模设计的方案。
