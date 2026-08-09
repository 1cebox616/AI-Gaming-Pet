"""Editable Chinese template data for CS2 game commentary."""

from dataclasses import dataclass
from typing import Literal

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


COMMENTARY_TEMPLATES: dict[CommentaryCategory, tuple[CommentaryTemplate, ...]] = {
    "kill": (
        CommentaryTemplate("{kill_detail}稳稳收下。", "happy"),
        CommentaryTemplate("这波处理得很干净。", "neutral"),
        CommentaryTemplate("好枪，人数优势拿到了。", "happy"),
        CommentaryTemplate("目标解决，继续看下一处。", "neutral"),
        CommentaryTemplate("{kill_detail}拿下一个，先别急着往前冲。", "neutral"),
    ),
    "kill_headshot": (
        CommentaryTemplate("{kill_detail}一枪到位，漂亮！", "happy"),
        CommentaryTemplate("这颗头点得真干脆。", "surprised"),
        CommentaryTemplate("准星很听话嘛，爆头拿下。", "happy"),
        CommentaryTemplate("好快的爆头，我都没来得及眨眼。", "surprised"),
        CommentaryTemplate("{kill_detail}爆头收工，手感不错。", "happy"),
    ),
    "multi_2": (
        CommentaryTemplate("双杀到手，节奏起来了！", "happy"),
        CommentaryTemplate("一口气两个，打得很顺。", "happy"),
        CommentaryTemplate("双杀！这波没有给他们喘气。", "surprised"),
        CommentaryTemplate("连续拿下两个，位置别急着送回去。", "neutral"),
        CommentaryTemplate("两个都收了，干净利落。", "happy"),
    ),
    "multi_3": (
        CommentaryTemplate("三杀！这回合你接管了。", "surprised"),
        CommentaryTemplate("三个了，手感已经热起来了！", "happy"),
        CommentaryTemplate("三杀到手，剩下的也得小心你。", "happy"),
        CommentaryTemplate("一口气三个，这波真有东西。", "surprised"),
        CommentaryTemplate("三杀！稳住，还有机会继续。", "happy"),
    ),
    "multi_4": (
        CommentaryTemplate("四杀！全场都得看你了！", "surprised"),
        CommentaryTemplate("四个！差一点就全包了！", "happy"),
        CommentaryTemplate("这回合已经被你打穿了，四杀！", "surprised"),
        CommentaryTemplate("四杀到手，最后一步也别着急。", "happy"),
        CommentaryTemplate("连拿四个，这火力谁顶得住啊！", "surprised"),
    ),
    "multi_5": (
        CommentaryTemplate("五杀！全收了！太夸张了吧！", "surprised"),
        CommentaryTemplate("五个全是你的！这回合封神！", "happy"),
        CommentaryTemplate("五杀啊！我宣布这回合归你管！", "surprised"),
        CommentaryTemplate("一个没留，五杀清场！太漂亮了！", "happy"),
        CommentaryTemplate("五杀！快让我缓缓，这也太猛了！", "surprised"),
    ),
    "multi_general": (
        CommentaryTemplate("连续拿人，节奏已经到你手里了。", "happy"),
        CommentaryTemplate("这一串打得漂亮，继续稳住。", "happy"),
        CommentaryTemplate("连着收下好几个，我看精神了。", "surprised"),
        CommentaryTemplate("连续得手，对面要开始怕你了。", "happy"),
        CommentaryTemplate("这波连杀很提气，别把优势送回去。", "neutral"),
    ),
    "death": (
        CommentaryTemplate("{survival_detail}没事，下一回合再拿回来。", "neutral"),
        CommentaryTemplate("这次倒了，先看看队友怎么处理。", "neutral"),
        CommentaryTemplate("可惜，就差那么一点。", "speechless"),
        CommentaryTemplate("这波对面抓得挺准，记住他的位置。", "surprised"),
        CommentaryTemplate("先缓口气，下一回合重新来。", "neutral"),
    ),
    "death_thrown_away": (
        CommentaryTemplate("{survival_detail}这波有点着急啦，下回合慢半拍。", "speechless"),
        CommentaryTemplate("{equip_detail}结果这么快就交代了，有点亏哦。", "speechless"),
        CommentaryTemplate("这次冲得太直了，我的小本本记下一笔。", "angry"),
        CommentaryTemplate("装备还没热乎呢，人先没了。下次稳一点。", "speechless"),
        CommentaryTemplate("好嘛，这波算交学费，下一回合别再白给啦。", "angry"),
    ),
    "round_win_elimination": (
        CommentaryTemplate("{score_detail}灭队拿下，收得真干净。", "happy"),
        CommentaryTemplate("一个不留，这回合赢得漂亮。", "happy"),
        CommentaryTemplate("对面全倒，回合稳稳收入囊中。", "happy"),
        CommentaryTemplate("清场完成，这波配合很舒服。", "happy"),
        CommentaryTemplate("灭队结束，气势打出来了。", "surprised"),
    ),
    "round_win_bomb": (
        CommentaryTemplate("{score_detail}炸弹顺利引爆，这回合拿下。", "happy"),
        CommentaryTemplate("守包成功，滴滴声就是胜利倒计时。", "happy"),
        CommentaryTemplate("炸弹开花，回合到手。", "surprised"),
        CommentaryTemplate("包点守住了，对面没能拆掉。", "happy"),
        CommentaryTemplate("引爆成功，这回合的残局处理不错。", "happy"),
    ),
    "round_win_defuse": (
        CommentaryTemplate("{score_detail}拆包成功，稳稳救下这一分。", "happy"),
        CommentaryTemplate("钳子一夹，危险解除。", "happy"),
        CommentaryTemplate("包拆掉了，这回合有惊无险。", "surprised"),
        CommentaryTemplate("拆得漂亮，最后几秒很冷静。", "happy"),
        CommentaryTemplate("炸弹解除，回合安全拿下。", "neutral"),
    ),
    "round_win_time": (
        CommentaryTemplate("{score_detail}时间耗尽，防线守住了。", "happy"),
        CommentaryTemplate("拖到最后一秒，这回合守得住。", "happy"),
        CommentaryTemplate("对面没赶上时间，这一分归我们。", "neutral"),
        CommentaryTemplate("时间也是武器，这回合用得不错。", "happy"),
        CommentaryTemplate("钟声一响，防守任务完成。", "neutral"),
    ),
    "round_win_general": (
        CommentaryTemplate("{score_detail}回合拿下，继续保持。", "happy"),
        CommentaryTemplate("漂亮，这一分稳稳到手。", "happy"),
        CommentaryTemplate("回合赢了，节奏继续抓住。", "neutral"),
        CommentaryTemplate("这一分很提气，下一回合也稳住。", "happy"),
        CommentaryTemplate("好，这回合是我们的。", "happy"),
    ),
    "round_loss_elimination": (
        CommentaryTemplate("{score_detail}这回合被清场了，下回合重整。", "neutral"),
        CommentaryTemplate("人都倒完了，先把这一分翻篇。", "speechless"),
        CommentaryTemplate("对面这波火力很整齐，下次别逐个送。", "angry"),
        CommentaryTemplate("被灭队有点难受，下一回合抱团些。", "neutral"),
        CommentaryTemplate("这一分没守住，先想想怎么换个打法。", "neutral"),
    ),
    "round_loss_bomb": (
        CommentaryTemplate("{score_detail}炸弹爆了，这一分只能让掉。", "speechless"),
        CommentaryTemplate("没来得及拆包，可惜了。", "neutral"),
        CommentaryTemplate("滴到最后还是炸了，下回合早点回防。", "angry"),
        CommentaryTemplate("包点没救下来，下一回合再算账。", "neutral"),
        CommentaryTemplate("炸弹引爆，这次残局没赶上。", "speechless"),
    ),
    "round_loss_defuse": (
        CommentaryTemplate("{score_detail}包被拆了，这回合守包没守住。", "speechless"),
        CommentaryTemplate("对面拆包成功，下次别给他这么多时间。", "angry"),
        CommentaryTemplate("差一点守住包，可惜。", "neutral"),
        CommentaryTemplate("炸弹被解除，这一分得复盘一下站位。", "neutral"),
        CommentaryTemplate("让他把包拆完了，下回合盯紧点。", "angry"),
    ),
    "round_loss_time": (
        CommentaryTemplate("{score_detail}时间到了，这回合推进得太慢。", "speechless"),
        CommentaryTemplate("没赶上时间，可惜这一分。", "neutral"),
        CommentaryTemplate("钟都走完了，下回合得早点动。", "angry"),
        CommentaryTemplate("时间耗尽，这次节奏被拖住了。", "neutral"),
        CommentaryTemplate("差几秒也不行呀，下回合果断一点。", "speechless"),
    ),
    "round_loss_general": (
        CommentaryTemplate("{score_detail}这一分丢了，下回合再追。", "neutral"),
        CommentaryTemplate("回合没拿到，先别让心态跟着掉。", "neutral"),
        CommentaryTemplate("可惜，这一分让对面收走了。", "speechless"),
        CommentaryTemplate("翻篇翻篇，下一回合还有机会。", "happy"),
        CommentaryTemplate("这回合不顺，换口气再来。", "neutral"),
    ),
}
