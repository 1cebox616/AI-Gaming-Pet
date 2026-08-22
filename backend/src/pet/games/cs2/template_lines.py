"""Editable personality-grouped Chinese template data for CS2 commentary."""

from dataclasses import dataclass
from typing import Literal

from pet.core.config import PersonalityStyle

Emotion = Literal["neutral", "happy", "angry", "surprised", "speechless"]

CommentaryCategory = Literal[
    "kill",
    "kill_headshot",
    "multi_2",
    "multi_3",
    "multi_4",
    "multi_5",
    "multi_general",
    "death",
    "death_after_kill",
    "death_thrown_away",
    "round_win_elimination",
    "round_win_bomb",
    "round_win_defuse",
    "round_win_time",
    "round_win_general",
    "round_loss_elimination",
    "round_loss_bomb",
    "round_loss_defuse",
    "round_loss_time",
    "round_loss_general",
]


@dataclass(frozen=True, slots=True)
class CommentaryTemplate:
    """One format string and the expression shown while it is spoken."""

    text: str
    emotion: Emotion
    applicable_maps: tuple[str, ...] | None = None


T = CommentaryTemplate

KILL_DETAIL_FORMAT = "本回合第 {kill_index} 杀，"
SURVIVAL_DETAIL_FORMAT = "存活了 {seconds} 秒，"
EQUIP_DETAIL_FORMAT = "还带着 {equip_value} 的装备，"
SCORE_DETAIL_FORMAT = "比分来到 {score_ct}:{score_t}，"

METHOD_CATEGORY_BY_LABEL: dict[str, Literal["elimination", "bomb", "defuse", "time"]] = {
    "灭队": "elimination",
    "炸弹引爆": "bomb",
    "炸弹拆除": "defuse",
    "时间耗尽": "time",
}

COMMENTARY_TEMPLATES: dict[
    PersonalityStyle,
    dict[CommentaryCategory, tuple[CommentaryTemplate, ...]],
] = {
    "brother": {
        "kill": (
            T("{kill_detail}好枪！", "happy"),
            T("这下不算白给了", "happy"),
            T("简单枪不能空", "neutral"),
            T("可以啊，对面打不过你感觉", "surprised"),
            T("还好对过了", "surprised"),
        ),
        "kill_headshot": (
            T("{kill_detail}一枪头！", "happy"),
            T("好枪，兄弟，好枪！", "surprised"),
            T("我起了，一枪秒了，有什么好说的", "happy"),
            T("这波准的呀", "speechless"),
            T("Dink!", "surprised"),
        ),
        "multi_2": (
            T("来一个杀一个，来两个杀一双", "happy"),
            T("好枪，帅得不谈", "happy"),
            T("简简单单一波双杀", "surprised"),
            T("枪够硬啊，兄弟。", "happy"),
            T("接了一个又接一个", "surprised"),
        ),
        "multi_3": (
            T("你三杀了！", "happy"),
            T("兄弟，你杀成串了。", "surprised"),
            T("你这波啊，这波是s1mple附体。", "happy"),
            T("你一个人游龙，三个全没。", "surprised"),
            T("我超，你把三个人的屏幕一次性全调成黑白了。", "surprised"),
        ),
        "multi_4": (
            T("你四杀了！", "surprised"),
            T("兄弟，你把图杀空了。", "happy"),
            T("你四个全包，ZywOo看了都点头。", "happy"),
            T("你一路卷过去，对面像没刷新出来。", "surprised"),
            T("卧了个槽，你这四杀把服务器都打得有点沉默。", "surprised"),
        ),
        "multi_5": (
            T("你五杀了！", "surprised"),
            T("兄弟，你全吃了！", "happy"),
            T("你五个打包，GG，有什么好说的。", "happy"),
            T("你今天不是来打休闲的，你是来收门票的。", "surprised"),
            T("我超，你一人把对面全队送去看结算，Major决赛都没这镜头。", "surprised"),
        ),
        "multi_general": (
            T("你杀疯了！", "surprised"),
            T("兄弟，你还在收。", "happy"),
            T("你这串人头，糖葫芦都没这么齐。", "happy"),
            T("你从头杀到尾，地图像被你包场了。", "surprised"),
            T("我超，你这连杀滚起来以后，对面复活速度都快跟不上了。", "surprised"),
        ),
        "death": (
            T("{survival_detail}，寄！", "speechless"),
            T("Unlucky，这波没办法。", "neutral"),
            T("不是，对面这么准啊。", "speechless"),
            T("可惜可惜", "surprised"),
            T("不是，这波运气不好", "speechless"),
            T("寄了。", "speechless"),
        ),
        "death_after_kill": (
            T("这波不亏。", "happy"),
            T("换到了。", "happy"),
            T("有来有回。", "neutral"),
            T("一换一。", "neutral"),
            T("拿一个，不亏。", "happy"),
        ),
        "death_thrown_away": (
            T("你白给了！", "angry"),
            T("{equip_detail}这波白给。", "angry"),
            T("真就白给啊。", "angry"),
            T("白给少年。", "speechless"),
            T("这也能白给。", "speechless"),
        ),
        "round_win_elimination": (
            T("{score_detail}咱们清场！", "happy"),
            T("这波全给扬了。", "happy"),
            T("咱们一人一张票，对面集体回大厅。", "surprised"),
            T("这一分是纯灭队，地上枪比人还多。", "happy"),
            T("我超，咱们这波从头扫到尾，地图打扫得真干净。", "surprised"),
        ),
        "round_win_bomb": (
            T("{score_detail}这波炸了！", "surprised"),
            T("咱们听响收分。", "happy"),
            T("这一分滴到最后，boom，GG。", "happy"),
            T("咱们这包一响，对面拆包梦当场断电。", "surprised"),
            T("这波爆炸声一出来，整张地图都像在放胜利烟花。", "happy"),
        ),
        "round_win_defuse": (
            T("{score_detail}咱们拆了！", "happy"),
            T("这波钳住了。", "happy"),
            T("这一分剪线收工，有惊无险。", "neutral"),
            T("咱们这钳子一夹，对面的包白埋。", "surprised"),
            T("这波倒计时都贴脸了，我们这边还是把炸弹按回去了。", "surprised"),
        ),
        "round_win_time": (
            T("{score_detail}这一分到手", "happy"),
            T("咱们把钟耗没了。", "neutral"),
            T("这波时间一到，对面原地GG。", "happy"),
            T("我们这边门一关，秒表成了第六个人。", "surprised"),
            T("这一分硬是拖到最后一格，对面连包的影子都没摸着。", "happy"),
        ),
        "round_win_general": (
            T("{score_detail}拿下！", "happy"),
            T("这一分到手。", "happy"),
            T("真不戳。", "happy"),
            T("太常规了。", "neutral"),
            T("计划有变，准备夺冠。", "surprised"),
        ),
        "round_loss_elimination": (
            T("{score_detail}咱们没了", "speechless"),
            T("这波全躺平了。", "speechless"),
            T("咱们集体变灰，Unlucky。", "neutral"),
            T("这一分被对面一路清到家。", "angry"),
            T("卧了个槽，我们这边像排队进场，结果排队回了观战席。", "speechless"),
        ),
        "round_loss_bomb": (
            T("{score_detail}这波炸没了", "speechless"),
            T("咱们听了个响。", "neutral"),
            T("这一分boom，对面收走。", "angry"),
            T("这波包响得很准，咱们的分也飞得很快。", "speechless"),
            T("我们这边刚回到现场，爆炸已经把结算页掀出来了。", "surprised"),
        ),
        "round_loss_defuse": (
            T("{score_detail}这波被拆了", "speechless"),
            T("咱们的包没了。", "neutral"),
            T("这一分钳子一夹，GG。", "angry"),
            T("这波埋得挺响，结果拆得更响。", "speechless"),
            T("我们这边眼看着倒计时走，对面硬把这包剪成了废铁。", "angry"),
        ),
        "round_loss_time": (
            T("{score_detail}这一分超时", "speechless"),
            T("咱们被钟吃了。", "neutral"),
            T("这波人还在，时间先GG。", "angry"),
            T("我们这边跟秒表拉扯，秒表赢了。", "speechless"),
            T("这一分拖到最后连门都没挤进去，计时器笑得比对面大声。", "angry"),
        ),
        "round_loss_general": (
            T("{score_detail}丢分了。", "speechless"),
            T("这一分没了。", "neutral"),
            T("Unlucky。", "neutral"),
            T("白忙活。", "speechless"),
            T("可惜。", "speechless"),
        ),
    },
    "caster": {
        "kill": (
            T("{kill_detail}好枪！", "happy"),
            T("漂亮！", "happy"),
            T("这一枪有力气。", "surprised"),
            T("干净利落。", "happy"),
            T("太常规了。", "neutral"),
        ),
        "kill_headshot": (
            T("{kill_detail}一枪头！", "surprised"),
            T("漂亮的爆头！", "happy"),
            T("Dink!", "surprised"),
            T("精准点头。", "happy"),
            T("一枪秒了！", "surprised"),
        ),
        "multi_2": (
            T("你拿双杀！", "happy"),
            T("吃闪？你白着秒了两个！", "surprised"),
            T("这位选手，你左右各收一位。", "happy"),
            T("镜头不切了，你这双杀值得完整回放。", "surprised"),
            T("观众朋友们，你完成一穿二，画面像提前写好了剧本。", "happy"),
        ),
        "multi_3": (
            T("你拿三杀！", "surprised"),
            T("这位选手，你接管了。", "happy"),
            T("哇，你一个人游龙！", "surprised"),
            T("镜头里的你，三个人，三次谢幕。", "happy"),
            T("观众朋友们，你这波请神请到s1mple，解说席已经站起来了。", "surprised"),
        ),
        "multi_4": (
            T("你拿四杀！", "surprised"),
            T("不是啊，你还在杀！", "surprised"),
            T("这位选手，你把全场变成个人舞台。", "happy"),
            T("四个镜头全归你，导播今晚不用剪片了。", "happy"),
            T("啊？不是啊？你这是人类啊，怎么把四个人打成了背景板。", "surprised"),
        ),
        "multi_5": (
            T("你拿五杀！", "surprised"),
            T("全场看你，ACE！", "happy"),
            T("这位选手，你把五个人全部签收。", "surprised"),
            T("灯光给你，镜头给你，今晚这回合也给你。", "happy"),
            T("观众朋友们，你一个人完成全队清场，这不是集锦，这是直播！", "surprised"),
        ),
        "multi_general": (
            T("你还在杀！", "surprised"),
            T("镜头锁你，别切。", "happy"),
            T("这位选手，你的人头数字还在滚。", "happy"),
            T("导播追不上你，击杀信息已经开始刷屏。", "surprised"),
            T("观众朋友们，你一路游龙，比赛画面快变成个人纪录片了。", "surprised"),
        ),
        "death": (
            T("{survival_detail}倒下了。", "speechless"),
            T("Unlucky。", "neutral"),
            T("这波没办法。", "neutral"),
            T("本回合止步。", "speechless"),
            T("可惜。", "speechless"),
        ),
        "death_thrown_away": (
            T("白给了。", "speechless"),
            T("真就白给。", "angry"),
            T("这一波下饭。", "speechless"),
            T("白给少年。", "angry"),
            T("{equip_detail}满配白给。", "speechless"),
        ),
        "death_after_kill": (
            T("交换成立。", "happy"),
            T("这波不亏。", "happy"),
            T("先收一个。", "happy"),
            T("有来有回。", "neutral"),
            T("先换一个，不亏。", "surprised"),
        ),
        "round_win_elimination": (
            T("{score_detail}咱们清场！", "happy"),
            T("这波全数带走。", "happy"),
            T("我们这边完成灭队，优美的CS。", "surprised"),
            T("这一分没有悬念，对面五张灰屏同时亮起。", "happy"),
            T("观众朋友们，咱们一路清场，整张地图只剩胜利音乐。", "surprised"),
        ),
        "round_win_bomb": (
            T("{score_detail}这波引爆！", "surprised"),
            T("咱们听响拿分。", "happy"),
            T("这一分随着爆炸声正式落袋。", "happy"),
            T("我们这边的倒计时走完，画面定格胜利。", "surprised"),
            T("观众朋友们，这波烟花升空，比分牌也跟着完成跳动。", "happy"),
        ),
        "round_win_defuse": (
            T("{score_detail}咱们拆掉！", "happy"),
            T("这波解除危机。", "neutral"),
            T("这一分钳子落下，现场安全。", "happy"),
            T("我们这边完成拆除，倒计时停在最后画面。", "surprised"),
            T("观众朋友们，这波炸弹最终哑火，解说席终于把气吐出来了。", "surprised"),
        ),
        "round_win_time": (
            T("{score_detail}这一分到手", "happy"),
            T("咱们守到钟响。", "neutral"),
            T("这波秒表完成最后一杀。", "happy"),
            T("我们这边让时间成为了最佳防守队员。", "surprised"),
            T("观众朋友们，这一分在最后一秒封箱，对面连镜头都没赶上。", "happy"),
        ),
        "round_win_general": (
            T("{score_detail}回合拿下！", "happy"),
            T("这一分进账。", "happy"),
            T("太常规了。", "neutral"),
            T("纯粹的CS享受。", "happy"),
            T("计划有变，准备夺冠。", "surprised"),
        ),
        "round_loss_elimination": (
            T("{score_detail}咱们退场", "speechless"),
            T("这波集体灰屏。", "speechless"),
            T("我们这边被全数带走，Unlucky。", "neutral"),
            T("这一分进入对面账户，现场只剩枪声回放。", "angry"),
            T("观众朋友们，咱们从满员打到清空，镜头最终停在一地装备上。", "speechless"),
        ),
        "round_loss_bomb": (
            T("{score_detail}这波爆炸", "speechless"),
            T("咱们没赶上。", "neutral"),
            T("这一分随爆炸声离开画面。", "angry"),
            T("我们这边回到现场，迎面只剩结算动画。", "speechless"),
            T("观众朋友们，这波倒计时无情归零，比分被对面完整带走。", "surprised"),
        ),
        "round_loss_defuse": (
            T("{score_detail}这波被拆", "speechless"),
            T("咱们的包哑火。", "neutral"),
            T("这一分被钳子直接剪走。", "angry"),
            T("我们这边的倒计时停住，画面宣布拆除完成。", "speechless"),
            T("观众朋友们，这波炸弹没能响起，对面把最后几秒全部握住。", "angry"),
        ),
        "round_loss_time": (
            T("{score_detail}这一分超时", "speechless"),
            T("咱们输给秒表。", "neutral"),
            T("这波时间先一步抵达终点。", "angry"),
            T("我们这边还在画面里，回合已经离场。", "speechless"),
            T("观众朋友们，这一分被计时器关上大门，最后的推进留在门外。", "angry"),
        ),
        "round_loss_general": (
            T("{score_detail}这一分旁落。", "speechless"),
            T("回合丢了。", "neutral"),
            T("Unlucky。", "neutral"),
            T("白忙活。", "speechless"),
            T("可惜。", "speechless"),
        ),
    },
}
