# CS2.md —— CS2 适配
最后更新：M4 重构落地，本文件首版（内容承接自 AGENTS.md 历史版本）

本文件是 games/cs2/ 的唯一权威文档。改 CS2 适配前必读；主干与流程见 AGENTS.md。

## 模块清单（backend/src/pet/games/cs2/）

    adapter.py           实现端口协议：把下列模块接成管线，组装 SpeechRequest
    gsi.py               GSI 数据接收、GameSnapshot 解析、录制、写入 CS2 配置
    session.py           会话状态与数据主体识别
    events.py            事件检测（只做"发生了什么"，不含优先级）
    situation.py         局势累计（只做"现在是什么状况"，不产出事件）
    policy.py            发言策略：优先级、冷却、每回合上限、连杀收敛
    fact_sentences.py    把事实渲染成喂给模型的事实句（原 event_card.py）
    template_speech.py   模板路径的话术映射、填空、去重、地图过滤
    template_lines.py    模板语料数据（无逻辑）
    template_rules.py    共享事实检查黑名单（点位词、未替代脏字）
    eval/                离线评测工具：bench、replay、scenario_synth、
                         fact_sentence_audit、style_review/diversity/experiment
                         （生产模块不得 import eval/，分层测试强制）

分层职责：gsi 只接收解析；session 只判会话与主体；events 与 situation 是
GameSnapshot 的两个平行消费者、互不引用；policy 只做"该不该说"、不感知语音
播放状态；fact_sentences 只渲染已有事实、不新增判断不访问网络；
timeline 只被 fact_sentences 消费，不得被 policy 读取。

配置段（config.toml）：
    [games.cs2.gsi]         record
    [games.cs2.events]      thrown_away_max_survival_seconds,
                            thrown_away_min_equip_value, death_after_kill_max_seconds
    [games.cs2.policy]      cooldown_seconds, max_lines_per_round,
                            alive_priority_threshold, cooldown_override_priority,
                            minimum_gap_seconds, streak_settle_seconds,
                            follow_up_max_age_seconds
    [games.cs2.personality] style（brother / caster，仅模板路径）

提示词与数据资产：
    prompts/cs2/vocabulary.md            词库与绑定表（绑定表由 core/gate.py 解析）
    prompts/cs2/gate-requirements.json   闸门校验用的事件名与场景标签清单
                                         【与代码清单是两份抄本，同步测试见技术债 1】
    prompts/cs2/brother.md caster.md     模板路径人设（线上不用）
    data/cs2/scenarios/                  35 个合成罕见场景（回归资产）
    data/cs2/eval-assets/                冻结答案键与合成依据（回归资产）
    docs/cs2/gsi-capabilities.md         GSI 能给什么不能给什么
    docs/cs2/community-slang.md          社群黑话与梗（产品负责人维护）

## 核心契约（与代码逐字段一致，改前先与架构师确认）

GameSnapshot（gsi.py）—— CS2 某一瞬间的状态，除 ts 外全部字段可为 None：

    身份与场次：ts, player_steamid, provider_steamid, activity,
      map_mode, map_name, map_phase, round_number, round_phase, round_win_team,
      round_wins, bomb_state, score_ct, score_t,
      ct_consecutive_round_losses, t_consecutive_round_losses
    本人状态：team, health, armor, helmet, money, equip_value, has_defusekit,
      flashed（实测 0/1 开关量）, smoked（实测 0–255 强度）,
      burning（0–255，从未观测到非零）, round_kills, round_killhs,
      active_weapon, weapons
    全场统计：match_kills, match_assists, match_deaths, match_mvps, match_score

    字段生命周期规则：解析后无消费者的字段一律不得保留，
    不因"以后可能有用"而保留无人读取的字段。

WeaponSlot（gsi.py）：name, type, ammo_clip, ammo_clip_max, ammo_reserve,
    state（active / holstered 等，用于判断手持）

RoundSituation（situation.py）—— 单张快照表达不了的本回合累计量：
    flash_count, flashed_seconds_total, longest_flash_seconds,
    smoked_seconds_total, max_smoke_intensity, burn_count,
    total_damage_taken, lowest_health_while_alive, health_before_death,
    primary_weapons_used, bought_equipment,
    bomb_planted_at_ts, seconds_since_bomb_planted, self_team, timeline

    规则一：只在数据主体是本人时累计（subject_is_self），否则观战期间
    队友状态会污染统计。
    规则二：回合边界与对局边界都必须重置。
    规则三：观战期间 observe() 返回死亡那一回合的旧数据，消费者必须比对
    round_number 才能使用。
    规则四：比分、金钱、装备价值一律不复制进本结构，直接从 GameSnapshot 取。
    例外：self_team 是有意例外——观战期 snapshot.team 是队友阵营，
    而回合结算恰恰发生在观战期。
    时长类字段一律用相邻快照 ts 差计算，不得用 payload 条数换算。
    timeline 记录的是状态变化不是事件，不得赋予类型语义或优先级，
    不得被 policy 读取，唯一消费者是 fact_sentences。

GameState（session.py）：state（offline/menu/warmup/playing/spectating/
    round_over/match_over）, mode, map, round, score_ct, score_t,
    subject_steamid, subject_is_self

GameEvent（events.py）：id, ts, subject_steamid, subject_is_self, round_number, facts
    type: kill / kill_headshot / multi_kill / death / death_after_kill /
          death_thrown_away / round_win / round_loss
    facts 按类型携带：
      击杀类  round_kill_index, delta, weapon, self_team, self_score,
              opponent_score, score_situation, team_consecutive_round_losses
      多杀    count + 同上（self_score 等的存在理由：把"谁领先"从模型收回代码，
              实测中模型曾把落后读成领先）
      死亡类  survival_seconds, round_kills, seconds_since_last_kill,
              equip_value, score_situation, team_consecutive_round_losses
      回合类  method, score_ct, score_t, score_situation,
              team_consecutive_round_losses

回合号规则（必须只有一处实现，M1–M2 曾分叉过）：
    唯一实现 gsi.human_round_number()，session/events/situation 都必须调用它。
    round_phase 为 "over" 或 round_win_team 有值 → 描述刚结束的回合，
    回合号 = map.round；其余 → 进行中，回合号 = map.round + 1。

HTTP POST /gsi（经 adapter 的 http_router 挂载）：无鉴权（仅绑定本机）。
    任何非法输入都必须返回成功，绝不能让游戏端收到错误或超时。

## 发言策略规则（policy.py，改动前必须先与架构师确认）

连杀收敛（M3-T12 系列确立）：
    multi_kill 一律不直接播报，全部进入单槽暂存；更高杀数覆盖并刷新计时；
    释放条件 =「收敛」（距槽内时间戳 ≥ streak_settle_seconds）或「终局」
    （本批出现死亡或回合结算事件），且未静音、且距上次发言 ≥ minimum_gap_seconds；
    超期 / 换回合 / reset() 清除。连杀豁免冷却与每回合上限，选中后照常计数。
    设计依据（实测）：连杀内击杀间隔约 1.8 秒，一句话占约 4 秒，产得比说得快；
    任何允许中间级别出声的规则都会挤掉最高潮——间隔 2.5/3.0/3.5/4.0 秒
    四个值全部在 four_kill 场景丢四杀。**一波连杀只说一句，说最终数字。**

alive_priority_threshold = 0：不做交火期禁言，实测确认即时反馈是核心卖点。

## 事实句规则（fact_sentences.py）

- 只发一行抬头 + 【刚刚】焦点，不发完整卡；收窄判断范围同时提升准确率降低成本
- 事实句不出现读数：血量、掉血、用弹数、秒数改写成评价（残血/几枪就解决）
- 时间锚定「正式开打」，购买阶段用负秒数，模型不做减法
- CS 回合内血量只降不升，禁用「一度」「曾经」；掉血与击杀先后必须区分
- 多杀用概括不逐次罗列；枪法评价按平均每杀用弹
- 观战期分段判断：【我】【全场】来自 player 段必须省略；【本回合】回合号一致必须渲染
- 事实句是输入不设长度上限；宠物输出上限 19–30 汉字（201 条语料 P90 + 语音时长）

场景标签（fact_sentences.SCENE_TAGS，当前 36 个）：
    弹药五档：颗秒（仅 AK 与沙鹰，1 发）/ 秒杀 2-5 / 普通击杀 6-9 /
              有些吃力 10-14 / 马完了 ≥15；狙击枪走「狙击击杀」不进五档
    连杀：连续双杀/三杀/四杀/五杀（间隔 ≤5 秒，每杀刷新）
    多杀：多杀2/3/4/5+（5+ 仅休闲可能出现）
    击杀处境：对枪胜利 / 白着打 / 踩火杀 / 摸烟击杀 / 换枪后立刻杀
    死亡性质：白给 / 击杀后被补枪 / 马枪死 / 送狙 / 对枪输了 /
              一枪没开就没了 / 打空了还是没打过
    死亡处境：白着被打死 / 烟里死 / 出烟就没了 / 切雷时被打死 / 切刀时被打死
    本回合：白惨了 / 烧惨了 / 血皮撑住了 / 大狙空枪 / 连续空枪
    规则：代码限 3 个并按观战关注顺序排序；矛盾或蕴含的标签由代码消解；
    标签名不得出现在事实句【过程】里；vocabulary.md 末尾绑定表以标签名为第三列。

## 数据边界（GSI 永久做不到；部分将由 M5 视觉补上，事件检测仍留在代码）

- 玩家位置（player_position 仅观察者可用）；场上剩几人、残局（无 allplayers_*）
- 谁杀了你、你杀了谁、对面用什么枪有多少钱；敌方经济装备对比
- 伤害来源归因（燃烧与致盲有独立字段，手雷不行）
- 回合剩余时间（phase_countdowns 实测无内容）
- 「没打中」也是不知道的事——只知道开火没拿到击杀，一律说「没打死」

## GSI 实测数据（休闲模式，详见 docs/cs2/gsi-capabilities.md）

- 推送：有变化中位 0.314 秒（约 3 Hz），静置约 30 秒心跳。任何依赖"某时刻
  会有一帧"的机制必须容忍数秒级空档（合成场景观测过击杀后 5.86 秒才有下一帧）
- 死亡观战时 player 段整体切换为被观战者，必须比对两个 steamid
- flashed 实测 0/1 开关量（文档曾误载 0–255）；smoked 确为 0–255；burning 恒零
- 炸弹状态用 round.bomb（可用），不要请求顶层 bomb 组
- 一次击杀用弹量必须跨帧累计（同枪间隔 ≤2 秒为一段）；
  凡基于相邻两帧差值的推导都要怀疑跨帧问题
- 事实句链路 P95 0.49 毫秒，比推送间隔快六百倍，不是瓶颈

## 产品负责人纠正过的游戏事实（新增纠错记入本节）

- M4A1 一发爆头秒不了人（至少两发），「颗秒」只适用 AK 与沙鹰
- 狙击枪击杀走专属语料，不进枪法五档
- 连杀窗口 5 秒（再杀刷新），与事实句连续事件段 3 秒各管各的
- 多杀 5 以上只可能出现在休闲
- 「送狙」判据：本回合库存有过 AWP 且零杀阵亡，不是死亡瞬间手持
- 「打了半天」「磨了一会儿」禁用：一弹匣打空也就两三秒
- 回合内血量不回复；低血阈值 ≤30 HP 称丝血/残血；「大后期」指回合最后 20 秒

## CS2 待办清单

- 地图战术建议（原 M3-T14）：回合开始触发、可预生成，随时可开工
- 语句多样性调优（AGENTS.md 技术债 4）：提示词 / 温度实验，用 eval/ 工具跑对照
- gate-requirements.json 同步测试（AGENTS.md 技术债 1）：下一个 CS2 任务顺带
- M5 起：CS2 视觉增强（补数据边界中可见项：谁杀了你、场上剩几人等）
