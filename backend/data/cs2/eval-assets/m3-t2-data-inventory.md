# CS2 GSI 数据清单

- 录制文件：`gsi-20260810-114649-321103.jsonl`
- payload 条数：178
- 覆盖回合数：5
- 生成时间：2026-08-10T17:27:39-04:00

> “是否已解析”依据源码中的人工维护路径清单判断，可能随解析器改动而滞后。

## 表一：原始字段清单

| 字段路径 | 出现次数 | 取值样例 | 是否已解析 |
|---|---:|---|:---:|
| `added.map.round_wins` | 1 | true | 否 |
| `added.map.round_wins.2` | 1 | true | 否 |
| `added.map.round_wins.3` | 1 | true | 否 |
| `added.map.round_wins.4` | 1 | true | 否 |
| `added.player.state.defusekit` | 5 | true | 否 |
| `added.player.weapons.*` | 21 | true | 否 |
| `added.player.weapons.*.ammo_clip` | 2 | true | 否 |
| `added.player.weapons.*.ammo_clip_max` | 2 | true | 否 |
| `added.player.xpoverload` | 1 | true | 否 |
| `added.round.win_team` | 4 | true | 否 |
| `map.mode` | 175 | "casual" | 是 |
| `map.name` | 175 | "de_nuke" | 是 |
| `map.num_matches_to_win_series` | 175 | 最小 0 / 最大 0 | 否 |
| `map.phase` | 175 | "live" | 是 |
| `map.round` | 175 | 最小 0 / 最大 4 | 是 |
| `map.round_wins.1` | 174 | "ct_win_elimination" | 是 |
| `map.round_wins.2` | 124 | "ct_win_elimination" | 是 |
| `map.round_wins.3` | 77 | "t_win_elimination" | 是 |
| `map.round_wins.4` | 41 | "ct_win_elimination" | 是 |
| `map.team_ct.consecutive_round_losses` | 175 | 最小 0 / 最大 1 | 是 |
| `map.team_ct.matches_won_this_series` | 175 | 最小 0 / 最大 0 | 否 |
| `map.team_ct.score` | 175 | 最小 0 / 最大 3 | 是 |
| `map.team_ct.timeouts_remaining` | 175 | 最小 1 / 最大 1 | 否 |
| `map.team_t.consecutive_round_losses` | 175 | 最小 0 / 最大 2 | 是 |
| `map.team_t.matches_won_this_series` | 175 | 最小 0 / 最大 0 | 否 |
| `map.team_t.score` | 175 | 最小 0 / 最大 1 | 是 |
| `map.team_t.timeouts_remaining` | 175 | 最小 1 / 最大 1 | 否 |
| `player.activity` | 177 | "menu"、"playing" | 是 |
| `player.match_stats.assists` | 174 | 最小 0 / 最大 3 | 是 |
| `player.match_stats.deaths` | 174 | 最小 0 / 最大 4 | 是 |
| `player.match_stats.kills` | 174 | 最小 0 / 最大 7 | 是 |
| `player.match_stats.mvps` | 174 | 最小 0 / 最大 1 | 是 |
| `player.match_stats.score` | 174 | 最小 0 / 最大 16 | 是 |
| `player.name` | 177 | <已脱敏> | 否 |
| `player.observer_slot` | 174 | 最小 0 / 最大 18 | 否 |
| `player.state.armor` | 174 | 最小 0 / 最大 100 | 是 |
| `player.state.burning` | 174 | 最小 0 / 最大 0 | 是 |
| `player.state.defusekit` | 41 | true | 是 |
| `player.state.equip_value` | 174 | 最小 1200 / 最大 6550 | 是 |
| `player.state.flashed` | 174 | 最小 0 / 最大 1 | 是 |
| `player.state.health` | 174 | 最小 0 / 最大 100 | 是 |
| `player.state.helmet` | 174 | false、true | 是 |
| `player.state.money` | 174 | 最小 100 / 最大 5050 | 是 |
| `player.state.round_killhs` | 174 | 最小 0 / 最大 1 | 是 |
| `player.state.round_kills` | 174 | 最小 0 / 最大 3 | 是 |
| `player.state.smoked` | 174 | 最小 0 / 最大 0 | 是 |
| `player.steamid` | 177 | <已脱敏> | 是 |
| `player.team` | 174 | "T"、"CT" | 是 |
| `player.weapons.*.ammo_clip` | 166 | 最小 0 / 最大 35 | 是 |
| `player.weapons.*.ammo_clip_max` | 166 | 最小 5 / 最大 50 | 是 |
| `player.weapons.*.ammo_reserve` | 166 | 最小 1 / 最大 4 | 是 |
| `player.weapons.*.name` | 166 | "weapon_knife_stiletto"、"weapon_deagle"、"weapon_flashbang"、"weapon_knife_t"、"weapon_glock"、"weapon_ak47"、"weapon_smokegrenade"、"weapon_knife"、"weapon_hkp2000"、"weapon_m4a1_silencer" | 是 |
| `player.weapons.*.paintkit` | 166 | "aq_oiled"、"aq_deagle_naga"、"default"、"cu_ak47_mastery"、"am_doppler_phase1"、"cu_usp_spitfire"、"hy_doomkitty"、"sp_dapple"、"cu_glock_moon_rabbit"、"aq_blued" | 否 |
| `player.weapons.*.state` | 166 | "holstered"、"active"、"reloading" | 是 |
| `player.weapons.*.type` | 166 | "Knife"、"Pistol"、"Grenade"、"Rifle"、"C4"、"SniperRifle"、"Submachine Gun" | 是 |
| `player.xpoverload` | 1 | 最小 1 / 最大 1 | 否 |
| `previously.map` | 1 | true | 否 |
| `previously.map.round` | 4 | 最小 0 / 最大 3 | 否 |
| `previously.map.team_ct.consecutive_round_losses` | 2 | 最小 0 / 最大 1 | 否 |
| `previously.map.team_ct.score` | 3 | 最小 0 / 最大 2 | 否 |
| `previously.map.team_t.consecutive_round_losses` | 4 | 最小 0 / 最大 2 | 否 |
| `previously.map.team_t.score` | 1 | 最小 0 / 最大 0 | 否 |
| `previously.player` | 1 | true | 否 |
| `previously.player.activity` | 1 | "playing" | 否 |
| `previously.player.match_stats.assists` | 11 | 最小 0 / 最大 3 | 否 |
| `previously.player.match_stats.deaths` | 13 | 最小 0 / 最大 4 | 否 |
| `previously.player.match_stats.kills` | 19 | 最小 0 / 最大 7 | 否 |
| `previously.player.match_stats.mvps` | 10 | 最小 0 / 最大 1 | 否 |
| `previously.player.match_stats.score` | 23 | 最小 0 / 最大 16 | 否 |
| `previously.player.name` | 16 | <已脱敏> | 否 |
| `previously.player.observer_slot` | 19 | 最小 0 / 最大 18 | 否 |
| `previously.player.state.armor` | 28 | 最小 0 / 最大 100 | 否 |
| `previously.player.state.burning` | 1 | 最小 0 / 最大 0 | 否 |
| `previously.player.state.defusekit` | 5 | true | 否 |
| `previously.player.state.equip_value` | 30 | 最小 1200 / 最大 6550 | 否 |
| `previously.player.state.flashed` | 3 | 最小 0 / 最大 1 | 否 |
| `previously.player.state.health` | 31 | 最小 0 / 最大 100 | 否 |
| `previously.player.state.helmet` | 12 | false、true | 否 |
| `previously.player.state.money` | 30 | 最小 100 / 最大 5050 | 否 |
| `previously.player.state.round_killhs` | 7 | 最小 0 / 最大 1 | 否 |
| `previously.player.state.round_kills` | 16 | 最小 0 / 最大 3 | 否 |
| `previously.player.state.smoked` | 1 | 最小 0 / 最大 0 | 否 |
| `previously.player.steamid` | 16 | <已脱敏> | 否 |
| `previously.player.team` | 10 | "T"、"CT" | 否 |
| `previously.player.weapons.*.ammo_clip` | 104 | 最小 0 / 最大 35 | 否 |
| `previously.player.weapons.*.ammo_clip_max` | 17 | 最小 5 / 最大 50 | 否 |
| `previously.player.weapons.*.ammo_reserve` | 24 | 最小 1 / 最大 4 | 否 |
| `previously.player.weapons.*.name` | 20 | "weapon_knife_stiletto"、"weapon_deagle"、"weapon_flashbang"、"weapon_knife_t"、"weapon_glock"、"weapon_ak47"、"weapon_smokegrenade"、"weapon_knife"、"weapon_hkp2000"、"weapon_m4a1_silencer" | 否 |
| `previously.player.weapons.*.paintkit` | 18 | "aq_oiled"、"aq_deagle_naga"、"default"、"cu_ak47_mastery"、"hy_doomkitty"、"am_doppler_phase1"、"cu_usp_spitfire"、"sp_dapple"、"cu_glock_moon_rabbit"、"aq_blued" | 否 |
| `previously.player.weapons.*.state` | 57 | "holstered"、"active"、"reloading" | 否 |
| `previously.player.weapons.*.type` | 18 | "Grenade"、"Knife"、"Pistol"、"Rifle"、"C4"、"SniperRifle"、"Submachine Gun" | 否 |
| `previously.player.xpoverload` | 1 | 最小 1 / 最大 1 | 否 |
| `previously.round` | 1 | true | 否 |
| `previously.round.phase` | 12 | "live"、"over"、"freezetime" | 否 |
| `previously.round.win_team` | 4 | "CT"、"T" | 否 |
| `provider.appid` | 178 | 最小 730 / 最大 730 | 否 |
| `provider.name` | 178 | "Counter-Strike: Global Offensive" | 否 |
| `provider.steamid` | 178 | <已脱敏> | 是 |
| `provider.timestamp` | 178 | 最小 1786376808 / 最大 1786377246 | 否 |
| `provider.version` | 178 | 最小 14174 / 最大 14174 | 否 |
| `round.phase` | 175 | "live"、"over"、"freezetime" | 是 |
| `round.win_team` | 12 | "CT"、"T" | 是 |

## 表二：推导事实清单

| 事实名 | 依赖字段 | 推导规则 | 本录制中的表现 |
|---|---|---|---|
| `flash_count` | `player.state.flashed` | 被闪值从 0 或缺失变为大于 0 时加一 | 在 5 个回合中取值范围 0–1 |
| `flashed_seconds_total` | `player.state.flashed + ts` | 按相邻本人快照时间差累计被闪时长 | 在 5 个回合中取值范围 0–0.688703060150146 |
| `longest_flash_seconds` | `player.state.flashed + ts` | 取本回合单次连续被闪的最长时长 | 在 5 个回合中取值范围 0–0.688703060150146 |
| `smoked_seconds_total` | `player.state.smoked + ts` | 按相邻本人快照时间差累计处于烟雾中的时长 | 在 5 个回合中取值范围 0–0 |
| `max_smoke_intensity` | `player.state.smoked` | 取本回合观测到的最大烟雾强度 | 在 5 个回合中取值范围 0–0（无法判断 1 回合） |
| `burn_count` | `player.state.burning` | 燃烧值从 0 或缺失变为大于 0 时加一 | 在 5 个回合中取值范围 0–0 |
| `total_damage_taken` | `player.state.health` | 累加相邻快照中血量的下降量 | 在 5 个回合中取值范围 0–100 |
| `lowest_health_while_alive` | `player.state.health` | 取本回合出现过的大于 0 的最低血量 | 在 5 个回合中取值范围 25–77（无法判断 1 回合） |
| `health_before_death` | `player.state.health` | 记录血量归零前最后一个非零值 | 在 5 个回合中取值范围 25–77（无法判断 1 回合） |
| `primary_weapons_used` | `player.weapons.*.name + player.weapons.*.type` | 按首次出现顺序记录本回合不同主武器 | 在 5 个回合中：()、(`weapon_ak47`) |
| `bought_equipment` | `player.state.money + player.state.equip_value` | 同次更新中金钱下降且装备价值上升即为真 | 真 4 次 / 假 1 次 / 无法判断 0 次 |
| `bomb_planted_at_ts` | `round.bomb + ts` | 记录本回合 bomb 首次变为 planted 的时间 | 在 5 个回合中均无法判断 |
| `seconds_since_bomb_planted` | `round.bomb + ts` | 当前本人快照时间减去首次安放时间 | 在 5 个回合中均无法判断 |
| `is_low_health` | `player.state.health` | 血量大于 0 且不高于 30 | 真 12 次 / 假 115 次 / 无法判断 51 次 |
| `is_eco_round` | `player.state.money + player.state.equip_value` | 金钱低于 1500 且装备价值低于 2000 | 真 0 次 / 假 127 次 / 无法判断 51 次 |
| `is_low_ammo` | `player.weapons.*` | 手持武器弹夹余弹不高于 1 | 真 4 次 / 假 98 次 / 无法判断 76 次 |
| `armor_status` | `player.state.armor + player.state.helmet` | 按护甲值与头盔组合返回无甲、有甲无头或满甲 | 无甲 6 次 / 有甲无头 0 次 / 满甲 121 次 / 无法判断 51 次 |
| `held_weapon` | `player.weapons.*.state` | 返回状态为 active 的武器 | 手持武器样例 `weapon_glock`、`weapon_ak47`、`weapon_knife_t`、`weapon_smokegrenade` / 无法判断 61 次 |
| `is_currently_flashed` | `player.state.flashed` | 被闪标记大于 0 | 真 3 次 / 假 124 次 / 无法判断 51 次 |
| `is_currently_smoked` | `player.state.smoked` | 烟雾强度大于 0 | 真 0 次 / 假 127 次 / 无法判断 51 次 |
| `is_carrying_bomb` | `player.weapons.*.type` | 武器列表已知且含 C4 类型 | 真 13 次 / 假 114 次 / 无法判断 51 次 |
