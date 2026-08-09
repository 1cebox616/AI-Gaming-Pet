"""Editable personality-grouped Chinese template data for CS2 commentary."""

from dataclasses import dataclass
from typing import Literal

from pet.config import PersonalityStyle
from pet.lines import Emotion

CommentaryCategory = Literal[
    "kill",
    "kill_headshot",
    "multi_2",
    "multi_3",
    "multi_4",
    "multi_5",
    "multi_general",
    "death",
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


T = CommentaryTemplate

COMMENTARY_TEMPLATES: dict[
    PersonalityStyle,
    dict[CommentaryCategory, tuple[CommentaryTemplate, ...]],
] = {
    "brother": {
        "kill": (
            T("{kill_detail}你这枪nice", "happy"),
            T("兄弟，你把对面当bot点了。", "happy"),
            T("你拉出来就秒，有什么好说的。", "neutral"),
            T("你这发像NiKo借你的，A1都打通了。", "surprised"),
            T("我超，你这枪给对面打成灰屏观光团了。", "surprised"),
        ),
        "kill_headshot": (
            T("{kill_detail}你一枪头！", "happy"),
            T("你把他头打烂了。", "surprised"),
            T("兄弟，你准星里住着s1mple吧。", "happy"),
            T("你这颗头，啪，世界安静了。", "speechless"),
            T("你从中路拉出去那一下，对面头盔跟纸糊的一样。", "surprised"),
        ),
        "multi_2": (
            T("你双杀了！", "happy"),
            T("你一枪一个，nice。", "happy"),
            T("兄弟，你这双杀跟取快递似的。", "surprised"),
            T("你在B洞左右开弓，对面排队领灰屏。", "happy"),
            T("我超，你两个人一起收，连换弹都像多余的。", "surprised"),
        ),
        "multi_3": (
            T("你三杀了！", "happy"),
            T("兄弟，你杀成串了。", "surprised"),
            T("你这波啊，这波是s1mple附体。", "happy"),
            T("你一个人在A点水下游龙，三个全没。", "surprised"),
            T("我超，你把三个人的屏幕一次性全调成黑白了。", "surprised"),
        ),
        "multi_4": (
            T("你四杀了！", "surprised"),
            T("兄弟，你把图杀空了。", "happy"),
            T("你四个全包，ZywOo看了都点头。", "happy"),
            T("你从A点一路卷到中路，对面像没刷新出来。", "surprised"),
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
            T("你从狗洞杀到A1，地图像被你包场了。", "surprised"),
            T("我超，你这连杀滚起来以后，对面复活速度都快跟不上了。", "surprised"),
        ),
        "death": (
            T("{survival_detail}你倒了，唉", "speechless"),
            T("Unlucky，你没了。", "neutral"),
            T("兄弟，你被对面把头打烂了。", "speechless"),
            T("你刚露半个身位，灰屏比你先到。", "surprised"),
            T("你这波人还在Dust2大坑，魂已经飞回出生点了。", "speechless"),
        ),
        "death_thrown_away": (
            T("你白给了！", "angry"),
            T("{equip_detail}兄弟，你这身装备送得挺有排面。", "speechless"),
            T("{survival_detail}你这波像外卖，送到就走。", "angry"),
            T("你枪还没捂热，人先成对面经济了。", "speechless"),
            T("我超，你这波从满配到灰屏，速度堪比A1快递。", "angry"),
        ),
        "round_win_elimination": (
            T("{score_detail}咱们清场！", "happy"),
            T("这波全给扬了。", "happy"),
            T("咱们一人一张票，对面集体回大厅。", "surprised"),
            T("这一分是纯灭队，地上枪比人还多。", "happy"),
            T("我超，咱们这波从A点扫到B洞，地图打扫得真干净。", "surprised"),
        ),
        "round_win_bomb": (
            T("{score_detail}这波炸了！", "surprised"),
            T("咱们听响收分。", "happy"),
            T("这一分滴到最后，boom，GG。", "happy"),
            T("咱们这包一响，对面拆包梦当场断电。", "surprised"),
            T("这波爆炸声一出来，Mirage超市都像在放胜利烟花。", "happy"),
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
            T("{score_detail}咱们赢了！", "happy"),
            T("这一分，拿下。", "happy"),
            T("这波舒服，比分牌往咱们这边跳。", "neutral"),
            T("我们这边又收一分，nice。", "happy"),
            T("咱们这回合从开门打到收尾，结算页终于说了句人话。", "surprised"),
        ),
        "round_loss_elimination": (
            T("{score_detail}咱们没了", "speechless"),
            T("这波全躺平了。", "speechless"),
            T("咱们集体变灰，Unlucky。", "neutral"),
            T("这一分被对面从A点一路清到家。", "angry"),
            T("卧了个槽，我们这边像排队进场，结果排队回了观战席。", "speechless"),
        ),
        "round_loss_bomb": (
            T("{score_detail}这波炸没了", "speechless"),
            T("咱们听了个响。", "neutral"),
            T("这一分boom，对面收走。", "angry"),
            T("这波包响得很准，咱们的分也飞得很快。", "speechless"),
            T("我们这边刚摸到包点，爆炸已经把结算页掀出来了。", "surprised"),
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
            T("{score_detail}咱们丢分", "speechless"),
            T("这一分没了。", "neutral"),
            T("这波Unlucky，翻篇。", "neutral"),
            T("我们这边差一口气，比分牌没给面子。", "speechless"),
            T("咱们这回合打得像Nuke电梯，门一开就直接去了地下层。", "angry"),
        ),
    },
    "caster": {
        "kill": (
            T("{kill_detail}你这枪nice", "happy"),
            T("镜头给你，一拉一颗。", "happy"),
            T("这位选手，你出枪没有前摇！", "surprised"),
            T("观众朋友们，你这一枪把中路直接点亮了。", "happy"),
            T("导播甚至没来得及切镜头，你已经让对面进入灰屏画面。", "surprised"),
        ),
        "kill_headshot": (
            T("{kill_detail}你一枪头", "surprised"),
            T("这位选手，你点头了。", "happy"),
            T("优美的CS，你把准星焊在头上。", "happy"),
            T("画面给你，啪，一颗精准制导。", "surprised"),
            T("解说席刚吸一口气，你已经把对面的头盔打成了片尾字幕。", "surprised"),
        ),
        "multi_2": (
            T("你拿双杀！", "happy"),
            T("吃闪？你白着秒了两个！", "surprised"),
            T("这位选手，你左右各收一位。", "happy"),
            T("镜头不切了，你这双杀值得完整回放。", "surprised"),
            T("观众朋友们，你在B洞完成一穿二，画面像提前写好了剧本。", "happy"),
        ),
        "multi_3": (
            T("你拿三杀！", "surprised"),
            T("这位选手，你接管了。", "happy"),
            T("哇，你一个人在A点水下游龙！", "surprised"),
            T("镜头里的你，三个人，三次谢幕。", "happy"),
            T("观众朋友们，你这波请神请到s1mple，解说席已经站起来了。", "surprised"),
        ),
        "multi_4": (
            T("你拿四杀！", "surprised"),
            T("不是啊，你还在杀！", "surprised"),
            T("这位选手，你把A点变成个人舞台。", "happy"),
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
            T("观众朋友们，你从中路一路游龙，比赛画面快变成个人纪录片了。", "surprised"),
        ),
        "death": (
            T("{survival_detail}你倒下了", "speechless"),
            T("Unlucky，你退场。", "neutral"),
            T("这位选手，你的画面突然黑白。", "speechless"),
            T("镜头给到你，刚探出去就被精准捕捉。", "surprised"),
            T("哎呀，你在Dust2大坑被一枪按掉，解说席只剩半句话。", "speechless"),
        ),
        "death_thrown_away": (
            T("你白给了！", "angry"),
            T("{equip_detail}这位选手，你送出豪华大礼。", "speechless"),
            T("{survival_detail}你这段镜头短得像广告。", "angry"),
            T("画面给你，满配登场，灰屏谢幕。", "speechless"),
            T("观众朋友们，你这波经济转化率惊人，全转成了对面的。", "angry"),
        ),
        "round_win_elimination": (
            T("{score_detail}咱们清场！", "happy"),
            T("这波全数带走。", "happy"),
            T("我们这边完成灭队，优美的CS。", "surprised"),
            T("这一分没有悬念，对面五张灰屏同时亮起。", "happy"),
            T("观众朋友们，咱们从A1清到B洞，整张地图只剩胜利音乐。", "surprised"),
        ),
        "round_win_bomb": (
            T("{score_detail}这波引爆！", "surprised"),
            T("咱们听响拿分。", "happy"),
            T("这一分随着爆炸声正式落袋。", "happy"),
            T("我们这边的倒计时走完，画面定格胜利。", "surprised"),
            T("观众朋友们，这波包点烟花升空，比分牌也跟着完成跳动。", "happy"),
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
            T("{score_detail}咱们拿下！", "happy"),
            T("这一分进账。", "happy"),
            T("这波结束，比分向我们这边移动。", "neutral"),
            T("咱们的回合，现场响起nice。", "happy"),
            T("观众朋友们，我们这边完整收下这一分，比赛画面继续升温。", "surprised"),
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
            T("我们这边回到包点，迎面只剩结算动画。", "speechless"),
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
            T("{score_detail}咱们丢分", "speechless"),
            T("这一分旁落。", "neutral"),
            T("这波落幕，比分没有站在我们这边。", "speechless"),
            T("咱们的回合画上句号，Unlucky。", "neutral"),
            T("观众朋友们，我们这边没能收住结尾，这一分进入对面的高光。", "angry"),
        ),
    },
}
