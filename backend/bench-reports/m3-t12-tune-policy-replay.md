# M3-T12-TUNE 策略回放

本报告只回放策略层；不调用模型。时间是相对各场景首个快照的秒数。
当前策略配置为 `minimum_gap_seconds = 2.0`、
`follow_up_max_age_seconds = 5.0`、`streak_settle_seconds = 2.5`。

每个 `multi_kill` 先暂存。后续升级会替换暂存值并刷新等待起点；
只有连续 2.5 秒没有更高升级，或同批出现终局事件时，才会作为候选参与裁决。

| 场景 | 被选中的事件序列（选中时刻） | 所有相邻选中间隔 ≥ 2.0 秒 |
|---|---|---|
| `triple_kill_same_stage` | +80.004s kill；+82.572s multi_kill(3) | 是 |
| `triple_kill_cross_stage` | +30.654s kill；+80.004s kill；+82.572s multi_kill(3) | 是 |
| `triple_kill_headshot_finish` | +30.654s kill；+78.173s kill；+82.572s multi_kill(3) | 是 |
| `weapon_switch_double_kill` | +29.116s kill_headshot；+44.109s kill | 是 |
| `last_bullet_triple` | +80.004s kill；+82.572s multi_kill(3) | 是 |
| `empty_mag_after_triple` | +80.004s kill；+82.572s multi_kill(3) | 是 |
| `low_health_triple` | +30.654s kill；+78.173s kill；+82.572s multi_kill(3) | 是 |
| `four_kill` | +30.654s kill；+78.173s kill；+83.008s multi_kill(4) | 是 |
| `ace` | +30.654s kill；+33.453s multi_kill(2)；+61.960s kill；+66.306s multi_kill(3)；+83.008s multi_kill(5) | 是 |
| `flash_double_kill` | +78.173s kill；+82.572s multi_kill(2) | 是 |
| `postplant_triple_loss` | +30.654s kill；+78.173s kill；+82.572s multi_kill(3) | 是 |
| `late_defuse` | +17.714s kill；+21.870s multi_kill(2)；+30.505s kill；+33.451s multi_kill(3) | 是 |
| `bomb_explosion_win` | +35.920s death | 是 |

## 验收结论

- `four_kill` 最后一条连杀为 `multi_kill(4)`。
- `ace` 最后一条连杀为 `multi_kill(5)`；前面的双杀与三杀彼此相隔超过一波连杀的
  收敛时间，因此它们是更早的独立连杀，不会与最终五杀合并。
- `low_health_triple`、`triple_kill_headshot_finish`、`postplant_triple_loss` 各只有一条
  `multi_kill(3)`。
- `weapon_switch_double_kill` 的双杀在 +44.109s 被暂存，但之后第一个快照已晚
  5.861 秒，超过 5 秒追补有效期；策略按规则静默过期，避免到下一段仍说过时双杀。
- `bomb_explosion_win` 的检测序列含多杀，但该场景中玩家已经阵亡，策略只选中死亡；
  不存在违反最小间隔的连杀播报。

## 测试

`python -m pytest tests/ -q -k "not test_speech"`：408 passed，5 deselected。
`test_scenario_synth.py` 通过；其中 `weapon_switch_double_kill` 明确断言其多杀被暂存、
且由于无可用释放快照不渲染过期卡，而非为了旧断言放宽 5 秒有效期。
