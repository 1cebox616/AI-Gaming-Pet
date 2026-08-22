# CS2 GSI 数据清单

- 录制文件：`gsi-20260810-154052-044137.jsonl`
- payload 条数：163
- 覆盖回合数：5
- 生成时间：2026-08-10T17:27:34-04:00

> “是否已解析”依据源码中的人工维护路径清单判断，可能随解析器改动而滞后。

## 表一：原始字段清单

| 字段路径 | 出现次数 | 取值样例 | 是否已解析 |
|---|---:|---|:---:|
| `added.map.round_wins` | 1 | true | 否 |
| `added.map.round_wins.2` | 1 | true | 否 |
| `added.map.round_wins.3` | 1 | true | 否 |
| `added.map.round_wins.4` | 1 | true | 否 |
| `added.player.state.defusekit` | 1 | true | 否 |
| `added.player.weapons.*` | 15 | true | 否 |
| `added.player.xpoverload` | 1 | true | 否 |
| `added.round.bomb` | 2 | true | 否 |
| `added.round.win_team` | 4 | true | 否 |
| `map.mode` | 161 | "casual" | 是 |
| `map.name` | 161 | "de_overpass" | 是 |
| `map.num_matches_to_win_series` | 161 | 最小 0 / 最大 0 | 否 |
| `map.phase` | 161 | "live" | 是 |
| `map.round` | 161 | 最小 0 / 最大 4 | 是 |
| `map.round_wins.1` | 148 | "ct_win_defuse" | 是 |
| `map.round_wins.2` | 91 | "t_win_elimination" | 是 |
| `map.round_wins.3` | 72 | "ct_win_elimination" | 是 |
| `map.round_wins.4` | 32 | "ct_win_elimination" | 是 |
| `map.team_ct.consecutive_round_losses` | 161 | 最小 0 / 最大 1 | 是 |
| `map.team_ct.matches_won_this_series` | 161 | 最小 0 / 最大 0 | 否 |
| `map.team_ct.score` | 161 | 最小 0 / 最大 3 | 是 |
| `map.team_ct.timeouts_remaining` | 161 | 最小 1 / 最大 1 | 否 |
| `map.team_t.consecutive_round_losses` | 161 | 最小 0 / 最大 2 | 是 |
| `map.team_t.matches_won_this_series` | 161 | 最小 0 / 最大 0 | 否 |
| `map.team_t.score` | 161 | 最小 0 / 最大 1 | 是 |
| `map.team_t.timeouts_remaining` | 161 | 最小 1 / 最大 1 | 否 |
| `player.activity` | 161 | "menu"、"playing" | 是 |
| `player.match_stats.assists` | 159 | 最小 0 / 最大 0 | 是 |
| `player.match_stats.deaths` | 159 | 最小 0 / 最大 1 | 是 |
| `player.match_stats.kills` | 159 | 最小 0 / 最大 6 | 是 |
| `player.match_stats.mvps` | 159 | 最小 0 / 最大 0 | 是 |
| `player.match_stats.score` | 159 | 最小 0 / 最大 13 | 是 |
| `player.name` | 161 | <已脱敏> | 否 |
| `player.observer_slot` | 159 | 最小 2 / 最大 8 | 否 |
| `player.state.armor` | 159 | 最小 0 / 最大 100 | 是 |
| `player.state.burning` | 159 | 最小 0 / 最大 0 | 是 |
| `player.state.defusekit` | 146 | true | 是 |
| `player.state.equip_value` | 159 | 最小 1600 / 最大 5500 | 是 |
| `player.state.flashed` | 159 | 最小 0 / 最大 1 | 是 |
| `player.state.health` | 159 | 最小 0 / 最大 100 | 是 |
| `player.state.helmet` | 159 | true、false | 是 |
| `player.state.money` | 159 | 最小 50 / 最大 5250 | 是 |
| `player.state.round_killhs` | 159 | 最小 0 / 最大 1 | 是 |
| `player.state.round_kills` | 159 | 最小 0 / 最大 3 | 是 |
| `player.state.smoked` | 159 | 最小 0 / 最大 255 | 是 |
| `player.steamid` | 161 | <已脱敏> | 是 |
| `player.team` | 159 | "T"、"CT" | 是 |
| `player.weapons.*.ammo_clip` | 157 | 最小 0 / 最大 30 | 是 |
| `player.weapons.*.ammo_clip_max` | 157 | 最小 7 / 最大 30 | 是 |
| `player.weapons.*.ammo_reserve` | 157 | 最小 0 / 最大 3 | 是 |
| `player.weapons.*.name` | 157 | "weapon_knife_t"、"weapon_deagle"、"weapon_flashbang"、"weapon_knife"、"weapon_hkp2000"、"weapon_usp_silencer"、"weapon_m4a1_silencer"、"weapon_smokegrenade"、"weapon_glock"、"weapon_mac10" | 是 |
| `player.weapons.*.paintkit` | 157 | "default"、"gs_deagle_exo"、"usps_silent_boy"、"cu_m4a1_flashback"、"aq_glock_dark-fall"、"mac10_video_cam"、"cu_ak47_asiimov" | 否 |
| `player.weapons.*.state` | 157 | "holstered"、"active"、"reloading" | 是 |
| `player.weapons.*.type` | 157 | "Knife"、"Pistol"、"Grenade"、"Rifle"、"Submachine Gun" | 是 |
| `player.xpoverload` | 12 | 最小 6 / 最大 6 | 否 |
| `previously.map.round` | 4 | 最小 0 / 最大 3 | 否 |
| `previously.map.team_ct.consecutive_round_losses` | 2 | 最小 0 / 最大 1 | 否 |
| `previously.map.team_ct.score` | 3 | 最小 0 / 最大 2 | 否 |
| `previously.map.team_t.consecutive_round_losses` | 4 | 最小 0 / 最大 1 | 否 |
| `previously.map.team_t.score` | 1 | 最小 0 / 最大 0 | 否 |
| `previously.player` | 2 | true | 否 |
| `previously.player.match_stats.deaths` | 2 | 最小 0 / 最大 0 | 否 |
| `previously.player.match_stats.kills` | 8 | 最小 0 / 最大 5 | 否 |
| `previously.player.match_stats.score` | 9 | 最小 0 / 最大 12 | 否 |
| `previously.player.name` | 2 | <已脱敏> | 否 |
| `previously.player.observer_slot` | 4 | 最小 2 / 最大 8 | 否 |
| `previously.player.state.armor` | 13 | 最小 0 / 最大 100 | 否 |
| `previously.player.state.defusekit` | 1 | true | 否 |
| `previously.player.state.equip_value` | 15 | 最小 1600 / 最大 5500 | 否 |
| `previously.player.state.flashed` | 4 | 最小 0 / 最大 1 | 否 |
| `previously.player.state.health` | 14 | 最小 0 / 最大 100 | 否 |
| `previously.player.state.helmet` | 3 | true、false | 否 |
| `previously.player.state.money` | 22 | 最小 50 / 最大 5250 | 否 |
| `previously.player.state.round_killhs` | 4 | 最小 0 / 最大 1 | 否 |
| `previously.player.state.round_kills` | 10 | 最小 0 / 最大 3 | 否 |
| `previously.player.state.smoked` | 8 | 最小 0 / 最大 255 | 否 |
| `previously.player.steamid` | 2 | <已脱敏> | 否 |
| `previously.player.team` | 2 | "CT"、"T" | 否 |
| `previously.player.weapons.*.ammo_clip` | 66 | 最小 0 / 最大 30 | 否 |
| `previously.player.weapons.*.ammo_clip_max` | 6 | 最小 7 / 最大 30 | 否 |
| `previously.player.weapons.*.ammo_reserve` | 21 | 最小 0 / 最大 3 | 否 |
| `previously.player.weapons.*.name` | 16 | "weapon_knife_t"、"weapon_deagle"、"weapon_flashbang"、"weapon_hkp2000"、"weapon_smokegrenade"、"weapon_knife"、"weapon_usp_silencer"、"weapon_m4a1_silencer"、"weapon_glock"、"weapon_mac10" | 否 |
| `previously.player.weapons.*.paintkit` | 16 | "default"、"gs_deagle_exo"、"usps_silent_boy"、"cu_m4a1_flashback"、"aq_glock_dark-fall"、"mac10_video_cam" | 否 |
| `previously.player.weapons.*.state` | 68 | "active"、"holstered"、"reloading" | 否 |
| `previously.player.weapons.*.type` | 15 | "Knife"、"Pistol"、"Grenade"、"Rifle"、"Submachine Gun" | 否 |
| `previously.player.xpoverload` | 1 | 最小 6 / 最大 6 | 否 |
| `previously.round.bomb` | 3 | "planted"、"defused" | 否 |
| `previously.round.phase` | 12 | "live"、"over"、"freezetime" | 否 |
| `previously.round.win_team` | 4 | "CT"、"T" | 否 |
| `provider.appid` | 163 | 最小 730 / 最大 730 | 否 |
| `provider.name` | 163 | "Counter-Strike: Global Offensive" | 否 |
| `provider.steamid` | 163 | <已脱敏> | 是 |
| `provider.timestamp` | 163 | 最小 1786390859 / 最大 1786391284 | 否 |
| `provider.version` | 163 | 最小 14174 / 最大 14174 | 否 |
| `round.bomb` | 34 | "planted"、"defused" | 是 |
| `round.phase` | 161 | "live"、"over"、"freezetime" | 是 |
| `round.win_team` | 21 | "CT"、"T" | 是 |

## 表二：推导事实清单

| 事实名 | 依赖字段 | 推导规则 | 本录制中的表现 |
|---|---|---|---|
| `flash_count` | `player.state.flashed` | 被闪值从 0 或缺失变为大于 0 时加一 | 在 5 个回合中取值范围 0–1 |
| `flashed_seconds_total` | `player.state.flashed + ts` | 按相邻本人快照时间差累计被闪时长 | 在 5 个回合中取值范围 0–1.4002890586853 |
| `longest_flash_seconds` | `player.state.flashed + ts` | 取本回合单次连续被闪的最长时长 | 在 5 个回合中取值范围 0–1.4002890586853 |
| `smoked_seconds_total` | `player.state.smoked + ts` | 按相邻本人快照时间差累计处于烟雾中的时长 | 在 5 个回合中取值范围 0–6.84921360015869 |
| `max_smoke_intensity` | `player.state.smoked` | 取本回合观测到的最大烟雾强度 | 在 5 个回合中取值范围 0–255 |
| `burn_count` | `player.state.burning` | 燃烧值从 0 或缺失变为大于 0 时加一 | 在 5 个回合中取值范围 0–0 |
| `total_damage_taken` | `player.state.health` | 累加相邻快照中血量的下降量 | 在 5 个回合中取值范围 0–100 |
| `lowest_health_while_alive` | `player.state.health` | 取本回合出现过的大于 0 的最低血量 | 在 5 个回合中取值范围 41–100 |
| `health_before_death` | `player.state.health` | 记录血量归零前最后一个非零值 | 在 5 个回合中取值范围 41–41（无法判断 4 回合） |
| `primary_weapons_used` | `player.weapons.*.name + player.weapons.*.type` | 按首次出现顺序记录本回合不同主武器 | 在 5 个回合中：()、(`weapon_m4a1_silencer`)、(`weapon_m4a1_silencer`, `weapon_ak47`)、(`weapon_ak47`) |
| `bought_equipment` | `player.state.money + player.state.equip_value` | 同次更新中金钱下降且装备价值上升即为真 | 真 4 次 / 假 1 次 / 无法判断 0 次 |
| `bomb_planted_at_ts` | `round.bomb + ts` | 记录本回合 bomb 首次变为 planted 的时间 | 在 5 个回合中取值范围 1786390943.30068–1786391031.64349（无法判断 3 回合） |
| `seconds_since_bomb_planted` | `round.bomb + ts` | 当前本人快照时间减去首次安放时间 | 在 5 个回合中取值范围 21.7787108421326–21.9612357616425（无法判断 3 回合） |
| `is_low_health` | `player.state.health` | 血量大于 0 且不高于 30 | 真 0 次 / 假 147 次 / 无法判断 16 次 |
| `is_eco_round` | `player.state.money + player.state.equip_value` | 金钱低于 1500 且装备价值低于 2000 | 真 0 次 / 假 147 次 / 无法判断 16 次 |
| `is_low_ammo` | `player.weapons.*` | 手持武器弹夹余弹不高于 1 | 真 1 次 / 假 108 次 / 无法判断 54 次 |
| `armor_status` | `player.state.armor + player.state.helmet` | 按护甲值与头盔组合返回无甲、有甲无头或满甲 | 无甲 1 次 / 有甲无头 0 次 / 满甲 146 次 / 无法判断 16 次 |
| `held_weapon` | `player.weapons.*.state` | 返回状态为 active 的武器 | 手持武器样例 `weapon_hkp2000`、`weapon_knife`、`weapon_usp_silencer`、`weapon_m4a1_silencer`、`weapon_flashbang` / 无法判断 23 次 |
| `is_currently_flashed` | `player.state.flashed` | 被闪标记大于 0 | 真 2 次 / 假 145 次 / 无法判断 16 次 |
| `is_currently_smoked` | `player.state.smoked` | 烟雾强度大于 0 | 真 7 次 / 假 140 次 / 无法判断 16 次 |
| `is_carrying_bomb` | `player.weapons.*.type` | 武器列表已知且含 C4 类型 | 真 0 次 / 假 147 次 / 无法判断 16 次 |
