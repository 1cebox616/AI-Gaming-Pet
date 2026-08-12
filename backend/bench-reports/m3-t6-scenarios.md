# M3-T6 合成场景定义

### rare_reload_then_kill —— 换弹完成后用 M4A1-S 击杀并进入回合结算
- 分类：甲类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：沿用模板中的已观测状态变化，不额外改值
- 必答：换弹、M4A1-S、击杀、回合失败
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### rare_ammo_low_death —— 弹匣打空后阵亡
- 分类：甲类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：沿用模板中的已观测状态变化，不额外改值
- 必答：弹匣打空、阵亡、回合失败
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### rare_mvp_round_win —— 本回合取胜并获得 MVP
- 分类：甲类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 104–132 行
- 改造：第132行 set player.match_stats.mvps=1
- 必答：获得MVP、回合胜利
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### rare_assist_round_win —— 回合中新增一次助攻并获胜
- 分类：甲类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 104–132 行
- 改造：第131行 set player.match_stats.assists=1；第132行 set player.match_stats.assists=1
- 必答：助攻、回合胜利
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### rare_grenade_pickup —— 中途捡到一颗闪光弹后完成回合
- 分类：甲类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 104–132 行
- 改造：第124行 set player.weapons.weapon_5.name='weapon_flashbang'；第124行 set player.weapons.weapon_5.type='Grenade'；第124行 set player.weapons.weapon_5.state='holstered'
- 必答：捡到闪光弹、回合胜利
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### rare_primary_switch —— 从 M4A1-S 换到 AK47 后结束回合
- 分类：甲类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 104–132 行
- 改造：沿用模板中的已观测状态变化，不额外改值
- 必答：换枪、AK47、回合胜利
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### rare_flash_interrupted_by_death —— 被闪状态尚未结束便阵亡
- 分类：甲类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：第71行 set player.state.flashed=1；第72行 set player.state.flashed=1；第73行 set player.state.flashed=1
- 必答：被闪、未结束、阵亡、回合失败
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### triple_kill_same_stage —— 反攻包点阶段用 M4A1-S 完成三杀
- 分类：乙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：沿用模板中的已观测状态变化，不额外改值
- 必答：反攻包点、M4A1-S、三杀、回合失败
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### triple_kill_cross_stage —— 前期先杀一人，反攻包点时再连杀两人完成三杀
- 分类：乙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：沿用模板中的已观测状态变化，不额外改值
- 必答：前期、反攻包点、M4A1-S、三杀
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### triple_kill_headshot_finish —— 三杀的最后一次击杀为爆头
- 分类：乙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：第67行 set player.state.round_killhs=1；第68行 set player.state.round_killhs=1；第69行 set player.state.round_killhs=1；第70行 set player.state.round_killhs=1；第71行 set player.state.round_killhs=1；第72行 set player.state.round_killhs=1；第73行 set player.state.round_killhs=1
- 必答：三杀、爆头、M4A1-S、回合失败
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### weapon_switch_double_kill —— 先用 M4A1-S 击杀，换到 AK47 后再杀一人
- 分类：乙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 104–132 行
- 改造：第122行 set player.state.round_kills=1；第123行 set player.state.round_kills=1；第124行 set player.state.round_kills=2；第125行 set player.state.round_kills=2；第126行 set player.state.round_kills=2；第127行 set player.state.round_kills=2；第128行 set player.state.round_kills=2；第129行 set player.state.round_kills=2；第130行 set player.state.round_kills=2；第131行 set player.state.round_kills=2；第132行 set player.state.round_kills=2
- 必答：M4A1-S、换枪、AK47、双杀
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### last_bullet_triple —— 最后一发子弹完成第三次击杀
- 分类：乙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：第67行 set player.weapons.weapon_2.ammo_clip=1；第68行 set player.weapons.weapon_2.ammo_clip=1；第69行 set player.weapons.weapon_2.ammo_clip=1；第70行 set player.weapons.weapon_2.ammo_clip=1；第71行 set player.weapons.weapon_2.ammo_clip=1
- 必答：弹匣仅剩1发、三杀、M4A1-S
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### empty_mag_after_triple —— 完成三杀后把弹匣打空并阵亡
- 分类：乙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：沿用模板中的已观测状态变化，不额外改值
- 必答：三杀、弹匣打空、阵亡、回合失败
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### low_health_triple —— 残血状态下完成本回合三杀
- 分类：乙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：第62行 set player.state.health=41；第63行 set player.state.health=41；第64行 set player.state.health=41；第65行 set player.state.health=41；第66行 set player.state.health=41；第67行 set player.state.health=41；第68行 set player.state.health=41；第69行 set player.state.health=41；第70行 set player.state.health=41；第71行 set player.state.health=41；第72行 set player.state.health=41；第73行 set player.state.health=41；第73行 set player.state.health=0
- 必答：剩41血、三杀、M4A1-S、阵亡
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### flash_kill —— 被闪期间用 M4A1-S 击杀一人
- 分类：丙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：第40行 set player.state.flashed=1；第41行 set player.state.flashed=1；第42行 set player.state.flashed=0
- 必答：被闪、M4A1-S、击杀
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### flash_death —— 被闪期间阵亡
- 分类：丙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：第71行 set player.state.flashed=1；第72行 set player.state.flashed=1；第73行 set player.state.flashed=1
- 必答：被闪、阵亡、回合失败
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### flash_double_kill —— 被闪期间连续完成两次击杀
- 分类：丙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：第62行 set player.state.flashed=1；第63行 set player.state.flashed=1；第64行 set player.state.flashed=1；第65行 set player.state.flashed=1；第66行 set player.state.flashed=1；第67行 set player.state.flashed=1；第68行 set player.state.flashed=0；第62行 set player.state.round_kills=1；第63行 set player.state.round_kills=1；第64行 set player.state.round_kills=1；第65行 set player.state.round_kills=1；第66行 set player.state.round_kills=1；第67行 set player.state.round_kills=2；第68行 set player.state.round_kills=2；第69行 set player.state.round_kills=2；第70行 set player.state.round_kills=2；第71行 set player.state.round_kills=2；第72行 set player.state.round_kills=2；第73行 set player.state.round_kills=2
- 必答：被闪、连续事件、双杀、M4A1-S
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### long_smoke_then_kill —— 在烟中停留较久后用 M4A1-S 击杀
- 分类：丙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：第32行 set player.state.smoked=255；第33行 set player.state.smoked=255；第34行 set player.state.smoked=255；第35行 set player.state.smoked=255；第36行 set player.state.smoked=255；第37行 set player.state.smoked=255；第38行 set player.state.smoked=255；第39行 set player.state.smoked=255；第40行 set player.state.smoked=255；第41行 set player.state.smoked=255；第42行 set player.state.smoked=0
- 必答：进烟、M4A1-S、击杀
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### smoke_exit_death —— 离开烟雾后很快阵亡
- 分类：丙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：第68行 set player.state.smoked=255；第69行 set player.state.smoked=255；第70行 set player.state.smoked=255；第71行 set player.state.smoked=255；第72行 set player.state.smoked=0
- 必答：出烟、阵亡、回合失败
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### four_grenades_then_kill —— 一回合连续投出四颗道具后击杀
- 分类：丙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 104–132 行
- 改造：第120行 set player.weapons.weapon_5.name='weapon_flashbang'；第120行 set player.weapons.weapon_5.type='Grenade'；第120行 set player.weapons.weapon_5.state='holstered'；第121行 delete player.weapons.weapon_5.name；第121行 delete player.weapons.weapon_5.type；第121行 delete player.weapons.weapon_5.state
- 必答：闪光弹×2、烟雾弹×1、手雷×1、M4A1-S、击杀
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### double_flash_then_kill —— 连续被闪两次后完成击杀
- 分类：丙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：第28行 set player.state.flashed=1；第29行 set player.state.flashed=1；第30行 set player.state.flashed=0；第39行 set player.state.flashed=1；第40行 set player.state.flashed=1；第41行 set player.state.flashed=0
- 必答：玩家被闪、闪光影响结束、M4A1-S、击杀
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### smoke_flash_kill —— 烟中又被闪时完成击杀
- 分类：丙类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：第36行 set player.state.smoked=255；第37行 set player.state.smoked=255；第38行 set player.state.smoked=255；第39行 set player.state.smoked=255；第40行 set player.state.smoked=255；第41行 set player.state.smoked=255；第40行 set player.state.flashed=1；第41行 set player.state.flashed=1；第42行 set player.state.smoked=0；第42行 set player.state.flashed=0
- 必答：烟雾、被闪、M4A1-S、击杀
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### bomb_pickup_then_death —— 拿到炸弹后阵亡，死亡不额外记作主动丢包
- 分类：丁类
- 模板来源：gsi-20260810-114649-321103.jsonl 第 65–100 行
- 改造：沿用模板中的已观测状态变化，不额外改值
- 必答：拿到包、阵亡
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### bomb_drop_repickup —— 主动丢包后又重新捡回，随后阵亡
- 分类：丁类
- 模板来源：gsi-20260810-114649-321103.jsonl 第 65–100 行
- 改造：第90行 delete player.weapons.weapon_4.name；第90行 delete player.weapons.weapon_4.type；第90行 delete player.weapons.weapon_4.state；第91行 set player.weapons.weapon_4.name='weapon_c4'；第91行 set player.weapons.weapon_4.type='C4'；第91行 set player.weapons.weapon_4.state='holstered'
- 必答：丢了包、拿到包、阵亡
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### postplant_defuse_win —— 炸弹安放后由 CT 拆除并获胜
- 分类：丁类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 3–16 行
- 改造：沿用模板中的已观测状态变化，不额外改值
- 必答：炸弹已安放、拆除、回合胜利
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### postplant_counterattack_loss —— 下包后反攻阶段阵亡并输掉回合
- 分类：丁类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：沿用模板中的已观测状态变化，不额外改值
- 必答：反攻包点、阵亡、回合失败
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### postplant_triple_loss —— 下包后反攻阶段完成三杀但最终回合失败
- 分类：丁类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：沿用模板中的已观测状态变化，不额外改值
- 必答：反攻包点、三杀、回合失败
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### bomb_pickup_kill —— 拿到炸弹后用 AK47 最后一发击杀
- 分类：丁类
- 模板来源：gsi-20260810-114649-321103.jsonl 第 65–100 行
- 改造：第65行 set player.state.round_kills=0；第66行 set player.state.round_kills=0；第67行 set player.state.round_kills=0；第68行 set player.state.round_kills=0；第69行 set player.state.round_kills=0；第70行 set player.state.round_kills=0；第71行 set player.state.round_kills=0；第72行 set player.state.round_kills=0；第73行 set player.state.round_kills=0；第74行 set player.state.round_kills=0；第75行 set player.state.round_kills=0；第76行 set player.state.round_kills=0；第77行 set player.state.round_kills=0；第78行 set player.state.round_kills=0；第79行 set player.state.round_kills=0；第80行 set player.state.round_kills=0；第81行 set player.state.round_kills=0；第82行 set player.state.round_kills=0；第83行 set player.state.round_kills=0；第84行 set player.state.round_kills=0；第85行 set player.state.round_kills=0；第86行 set player.state.round_kills=0；第87行 set player.state.round_kills=0；第88行 set player.state.round_kills=0；第89行 set player.state.round_kills=0；第90行 set player.state.round_kills=0；第91行 set player.state.round_kills=0；第92行 set player.state.round_kills=0；第93行 set player.state.round_kills=0；第94行 set player.state.round_kills=0；第95行 set player.state.round_kills=0；第96行 set player.state.round_kills=0；第97行 set player.state.round_kills=1；第98行 set player.state.round_kills=1；第99行 set player.state.round_kills=1；第100行 set player.state.round_kills=1
- 必答：拿到包、弹匣仅剩1发、AK47、击杀
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

### bomb_planted_then_death —— 炸弹安放后玩家阵亡，回合随后失败
- 分类：丁类
- 模板来源：gsi-20260810-154052-044137.jsonl 第 18–73 行
- 改造：沿用模板中的已观测状态变化，不额外改值
- 必答：炸弹已安放、阵亡、回合失败
- 禁项：不得出现队友或敌人身份；不得声称玩家所在位置；不得编造伤害来源

## 未生成的越界场景

- `four_kill`：两个数据清单均只观测到 player.state.round_kills 最大为 3
- `ace`：ace 需要 round_kills=5，超出数据清单观测范围 0–3
- `bomb_explosion`：round.bomb 只观测到 planted/defused，从未观测到 exploded
- `extreme_defuse`：真实模板最晚只观测到下包后 27.4 秒拆除，不足以冒充极限拆包
- `burning_combo`：player.state.burning 的观测范围为 0–0，规格明确禁止合成非零值
