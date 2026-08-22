# ADAPTER_GUIDE.md —— 游戏适配开发指南
最后更新：M4 定稿（端口 v1）

写给要为本项目新增游戏适配的开发者。读完本文件 + AGENTS.md 即可开工；
CS2.md 与 games/cs2/ 是唯一的完整参考实现。

## 一、你要做的东西是什么

一个适配 = 一个让宠物看懂某个游戏的完整管线，从拿到游戏原始数据，
到决定"现在该说话了"，全部由你负责：

    游戏原始数据 → 解析成状态快照 → 检测事件 / 累计局势
    → 你自己的发言策略（何时开口、说什么优先）→ 事实句
    → 向主干递一张「发言条子」(SpeechRequest)

主干替你做的事（你不用做也不许做）：语音合成与播放、静音开关、
模型调用与超时回退、花费统计、闸门执行、前端显示。

## 二、动手前的三条铁律

1. **你只能改这些路径**：games/<你的游戏>/、prompts/<你的游戏>/、
   data/<你的游戏>/、docs/<你的游戏>/、<你的游戏大写>.md。
   主干（core/、main.py、frontend/）任何改动都经架构师下发。
   tests/test_layering.py 会静态扫描 import，越界当场红灯。
2. **端口不够用就停下来找架构师**，不许在适配里绕路（比如伸手进 core 的
   内部模块、自己开 WebSocket、自己调语音）。现在开发者只有两人，
   端口还能免费改；绕路等于把债转嫁给未来所有适配。
3. **每个阈值要么有实测依据，要么标注"待实测"**。CS2 的所有时间参数都是
   真实对局磨出来的；写规格时凭直觉给数字是本项目失误清单上的惯犯。

## 三、端口 v1 速查（完整定义见 core/adapter_api.py 与 AGENTS.md）

实现 GameAdapter 协议：
    adapter_id="warthunder"（示例）, display_name, port_version=PORT_VERSION,
    http_router（需要接收推送才提供，轮询式适配可为 None）,
    async start(core: CoreServices), async stop()

start() 拿到的 CoreServices 是你对主干的全部认知，五个回调：
    submit_speech(SpeechRequest)   递发言条子
    publish_status(GameStatus)     报游戏状态（game_id/state/summary，
                                   summary 里放你想让托盘和前端显示的字段）
    can_submit_speech() -> bool    没有前端消费者时可省掉整条管线的计算
    speech_is_muted() -> bool      静音时你可以选择不做无用功
                                   （主干无论如何不会播，这只是省钱开关）
    reset_speech_session()         对局边界调用，重置模型连败锁定

SpeechRequest 关键字段：
    fact_text        平实事实句（见第五节），交给模型改写
    urgency          0–100，你自己定标尺，主干只用于仲裁
    fallback_text/emotion  模板兜底句，模型失败时播它——必填，宠物永不哑火
    vocabulary_id    "warthunder" → 主干自动拼 prompts/warthunder/vocabulary.md
                     并启用同目录闸门
    llm_profile      想用别的模型就在 config.toml 定义 [llm.profiles.<名>] 指过来
    interrupt / supersedes_request_id  当前版本未实现差异化（等价于一律打断）。
                     你的游戏需要"先说发射、命中后接一句"这类形状时，
                     找架构师定义语义，不要自己 hack

## 四、目录模板（以战争雷霆为例）

    backend/src/pet/games/warthunder/
        adapter.py         实现端口，组装你的管线
        <你的数据接入>.py   例如 8111 轮询：localhost:8111 的
                           /state /indicators /hudmsg /gamechat /map_obj.json
        events.py situation.py policy.py fact_sentences.py ...
                           按你的游戏需要划分；CS2 的划分仅供参考，不是模板
        eval/              你的离线工具（回放、评测），生产模块不得 import 它
    backend/prompts/warthunder/
        vocabulary.md      词库 + 末尾绑定表（格式抄 CS2 的，闸门引擎自动解析）
        gate-requirements.json  你的事件名与标签清单（供闸门严格校验）
    backend/data/warthunder/   录制、场景、答案键等回归资产
    docs/warthunder/           数据接口能力文档、社群黑话文档
    config.toml 增加 [games.warthunder.*] 段（键名你自己定）

接入主干（这一步由架构师下发给主干侧执行）：games/__init__.py 的
built_in_adapters 注册你的 adapter；[active].game 切到你的 game_id 即可跑。

## 五、CS2 用血换来的经验（强烈建议照做）

1. **事件检测留在代码，模型只管文风。** 击杀数变化是确定性的、亚毫秒级的；
   让模型判断"发生了什么"曾是我们最大的准确率来源问题。
2. **事实句写人话，不写内部标识符。** 写「一发入魂」的判断留给模型；
   代码写「几枪就解决」而不是档位名「秒杀」。让模型解码你的内部编码
   是一整类反复出现的失败。
3. **一句话占 4 秒。** 模型 0.5–1.5 秒 + 语音 2–3.5 秒。你的策略产出速度
   超过这个节拍就必然互相打断或排队变馊。CS2 的答案是"同一事实的逐级升级
   只说最终态"；你的游戏节奏不同，答案可能不同，但这本物理账相同。
4. **数据推送有空档。** 任何"到点会有下一帧"的假设都要容忍数秒级空窗，
   并想清楚过期语义（一句迟到 10 秒的解说比沉默更糟还是更好？）。
5. **先录制、再回放、后评测。** 先做原始数据录制与离线回放，你的每个策略
   改动都能在同一份录制上对比；合成罕见场景做回归资产。这套顺序让 CS2 的
   每次重构都能证明"行为没变"。
6. **主体识别要早做。** 观战 / 换乘载具 / 重生时数据描述的是谁？搞错主体
   会把别人的战绩安在玩家头上，CS2 为此专设了 subject_is_self。
7. **写 <你的游戏>.md**：契约、规则、实测数据、产品负责人（你）纠正过的
   游戏事实，格式照 CS2.md。它是你和架构师之间的共识载体。

## 六、交付与验收

- 每个任务完成必须 commit 并 push，提交信息以任务 ID 开头
- 实机验证由你负责（你玩你的游戏）；**代码审查一律归架构师**——
  以实际代码为准，完成报告只作参考并交叉核对
- 测试要求与主干一致：pytest 全绿（分层测试必须过）、网络测试注入假客户端、
  入库产物不含真实玩家身份（昵称、ID 一律占位符替换，本仓库是公开的）
- 里程碑对标：实时数据适配对标 CS2 的 M2+M3 深度——事件检测、事实句体系、
  词库、社群黑话文档、闸门、真实对局验收，全套齐才算完成
