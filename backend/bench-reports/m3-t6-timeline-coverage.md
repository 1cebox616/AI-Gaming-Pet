# M3-T6 时间线覆盖统计

当前代码共有 22 个 TimelineKind。本任务按规格统计其中 18 个动作/状态 kind；
另有 round_live、bought 两个锚点，burn_start、burn_end 因实测始终为 0 而禁止合成。

| kind | 全部真实录制 | 合成集 |
|---|---:|---:|
| `flash_start` | 14 | 25 |
| `flash_end` | 14 | 25 |
| `smoke_start` | 10 | 20 |
| `smoke_end` | 10 | 20 |
| `kill` | 39 | 64 |
| `damage` | 137 | 90 |
| `primary_weapon` | 77 | 59 |
| `ammo_low` | 11 | 23 |
| `reload` | 28 | 48 |
| `grenade_used` | 56 | 61 |
| `grenade_pickup` | 31 | 8 |
| `bomb` | 30 | 21 |
| `bomb_pickup` | 5 | 4 |
| `bomb_drop` | 7 | 1 |
| `assist` | 6 | 1 |
| `mvp` | 2 | 1 |
| `death` | 35 | 23 |
| `round_result` | 46 | 29 |

- 场景数：29
- 合成录制总大小：2004379 bytes
- 模型调用次数：0
