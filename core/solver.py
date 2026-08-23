"""精確窮舉最佳策略（不含分牌）。

跟 core/exact.py 一樣走無限牌組的精確 DP，差別是 exact.py 是「照著某張
策略表打，算出那張表的賭場優勢」，這裡是「每個狀態都窮舉補牌/停牌/加倍/
(起手兩張時)投降，直接算出數學上真正最好的動作」——這正是各種基本策略表
原本應該從哪裡推導出來的方法，零抽樣誤差、瞬間算完，可以拿來核對
core/strategy.py 裡任何一張表是不是真的對，或是幫某個沒人推導過的規則組合
生出一張新表。

不含分牌：分牌牽扯到 DAS、RSA、再分牌的組合爆炸，精確解代價太高，
分牌決策改用 core/scenario.py 的蒙地卡羅比較（見 core/scenario.py 開頭）。

已知限制：無限牌組，不是你設定的副數
------------------------------------
這裡走的是無限牌組（每次抽牌機率固定，不管前面發過什麼），不是實際設定的
`rules.num_decks`。對絕大多數格子這無所謂——答案在 2 副牌跟無限牌組下是
一樣的。但少數本來就很接近的邊際格子，副數確實會讓答案翻盤，實測過一個
例子：

    A,2 對 5（S17 DAS）：
      無限牌組精確解：補牌 EV=0.1334 > 加倍 EV=0.1260  -> 補牌
      真實 6 副牌（蒙地卡羅）：加倍 EV=0.1386 > 補牌 EV=0.1379 -> 加倍（但非常接近）

    用 200 副牌去跑蒙地卡羅會跟無限牌組精確解一致，證實差異來自副數，
    不是任何一邊算錯。

所以：這裡的答案適合當「多數格子的快速、零誤差參考值」，但差距很小
（例如 compare_to_table 印出來的 ev_loss 只有千分之幾）的格子，answer
可能會隨你設定的副數翻盤，該用 core/scenario.py 針對你實際的副數跑
蒙地卡羅才是準的。
"""
from .engine import ACT_HIT, ACT_STAND, ACT_DOUBLE, ACT_SURRENDER
from .exact import P, dealer_given_up, p_dealer_bj
from .rules import SURRENDER_NONE, SURRENDER_EARLY, LOSS_ORIGINAL

ACTION_LETTERS = {ACT_STAND: 'S', ACT_HIT: 'H', ACT_DOUBLE: 'D', ACT_SURRENDER: 'R'}
COLUMNS = ('2', '3', '4', '5', '6', '7', '8', '9', '10', 'A')


def _ev_stand(total, up, h17):
    d = dealer_given_up(up, h17)
    ev = d[5]
    for k in range(5):
        dt = 17 + k
        ev += d[k] * (1 if total > dt else (-1 if total < dt else 0))
    return ev


def solve(rules):
    """回傳 {'hard': {total: [(letter, evs_dict), ...10欄]}, 'soft': {...}}。

    letter 已經把「投降是不是比其他都好」考慮進去了（投降只在起手兩張、
    也就是這個函式唯一會查詢的狀態成立，所以每一格都比較過）。
    evs_dict 是這一格所有選項的 EV：S/H 一定有，能加倍就有 D，能投降就有 R。

    no-peek 的正確算法（不是憑感覺猜的，推導寫在這裡）
    ------------------------------------------------
    peek 模式下，莊家 BJ 在玩家做任何決定之前就已經結算掉了，所以
    solve_cell 只需要算「已確認莊家沒 BJ」這個條件下的 EV——這是
    _ev_stand/best_continue 原本就在算的東西。

    no-peek 不一樣：玩家補牌/停牌/加倍時完全不知道莊家有沒有 BJ，所以
    每個動作的「真正」EV 要疊加「莊家真的有 BJ」那個分支：

        EV(動作) = p_bj * EV_莊家有BJ(這個動作最終押了多少注)
                 + (1 - p_bj) * EV_已確認沒BJ(這個動作)

    關鍵化簡：EV_莊家有BJ 只跟「最終押注倍率」有關（LOSS_ALL 就是輸掉
    全部押注、OBO 就是固定只輸原始那 1 注），跟玩家後續補了幾張牌、
    補成什麼點數完全無關。這代表在**同一個押注倍率**之下，要補牌還是
    停牌，peek 跟 no-peek 的最佳選擇是同一個決策——p_bj 那一項只是疊加
    一個不影響誰比較大的常數，所以 best_continue()（補牌後的延續決策，
    押注倍率固定是 1 注）完全不用改，繼續沿用「已確認沒 BJ」的條件機率
    就是對的。

    真正會被 no-peek 影響、需要另外處理的只有兩個「起手」決策：

    1. 加倍——因為加倍會把押注倍率從 1 拉到 2，LOSS_ALL 時等於多冒了
       一注的風險去賭一個「其實已經確定會輸」的莊家 BJ；OBO 沒有這個
       額外風險（多押的那一注會退），所以 OBO 下加倍的門檻其實跟 peek
       一樣。
    2. 投降——三種語意完全不同：
         peek 的 late／任何情況的 early：投降是打從一開始就固定 -0.5，
           不管莊家有沒有 BJ 都一樣（early 甚至比莊家翻牌還早决定）。
         no-peek 的 late：玩家先投降，莊家「之後」才翻出 BJ 的話要輸
           全額，所以投降本身也要按 p_bj 加權：
           p_bj*(-1) + (1-p_bj)*(-0.5)，比固定 -0.5 差，這是 late
           surrender 在 no-peek 下比較不值錢的原因（core/engine.py 的
           F_SURRENDER 分支跟 test_engine.py 的既有測試已經驗證過這個
           語意）。

    peek=True 時 p_bj 直接設 0，公式自動退化回原本的算法，行為完全
    不變（回歸測試會確認這點）。
    """
    h17 = rules.dealer_hits_soft_17
    peek = rules.dealer_peek
    loss_original = rules.dealer_bj_loss == LOSS_ORIGINAL
    surrender_mode = rules.surrender
    memo = {}

    def best_continue(s, aces, up):
        """從補牌後的狀態開始（不再是起手兩張）的最佳 EV。

        沒有加倍選項——規則上加倍只能發生在原始兩張牌，補過牌之後就不能
        再加倍了，所以這裡只比較停牌跟繼續補牌，不用重複判斷 can_double。
        押注倍率固定是 1 注，peek/no-peek 的最佳決策相同（見上面模組
        docstring 的推導），所以繼續沿用「已確認沒 BJ」的條件機率即可。
        """
        key = (s, aces, up)
        if key in memo:
            return memo[key]
        soft = aces > 0 and s + 10 <= 21
        total = s + 10 if soft else s
        if total > 21:
            memo[key] = -1.0
            return -1.0
        best = _ev_stand(total, up, h17)
        if total < 21:
            ev = 0.0
            for c, p in P:
                ev += p * best_continue(s + c, aces + (c == 1), up)
            if ev > best:
                best = ev
        memo[key] = best
        return best

    def solve_cell(s, aces, up):
        """起手兩張（可以投降、可以加倍）的完整比較，回傳 (letter, evs)。"""
        soft = aces > 0 and s + 10 <= 21
        total = s + 10 if soft else s
        ev_stand_c = _ev_stand(total, up, h17)          # 條件在「沒BJ」之下
        ev_hit_c = 0.0
        for c, p in P:
            ev_hit_c += p * best_continue(s + c, aces + (c == 1), up)

        can_double = rules.double_allowed_on(s, soft)
        ev_double_c = None
        if can_double:
            ev_double_c = 0.0
            for c, p in P:
                s2, a2 = s + c, aces + (c == 1)
                t2 = s2 + 10 if (a2 and s2 + 10 <= 21) else s2
                ev_double_c += p * (-1.0 if t2 > 21 else _ev_stand(t2, up, h17))
            ev_double_c *= 2.0

        p_bj = 0.0 if peek else p_dealer_bj(up)   # peek=True 時公式自動退化

        def bj_penalty(wager):
            return -1.0 if loss_original else -float(wager)

        evs = {
            'S': p_bj * bj_penalty(1) + (1 - p_bj) * ev_stand_c,
            'H': p_bj * bj_penalty(1) + (1 - p_bj) * ev_hit_c,
        }
        if can_double:
            evs['D'] = p_bj * bj_penalty(2) + (1 - p_bj) * ev_double_c

        if surrender_mode != SURRENDER_NONE and rules.surrender_allowed_vs(up):
            if peek or surrender_mode == SURRENDER_EARLY:
                evs['R'] = -0.5
            else:   # no-peek + late：投降後莊家才翻BJ的話要輸全額
                evs['R'] = p_bj * (-1.0) + (1 - p_bj) * (-0.5)

        letter = max(evs, key=evs.get)
        return letter, evs

    hard = {}
    for total in range(4, 22):
        row = []
        for up in range(1, 11):
            letter, evs = solve_cell(total, 0, dealer_col_to_up(up))
            row.append((letter, evs))
        hard[total] = row

    soft = {}
    for total in range(12, 22):
        row = []
        for up in range(1, 11):
            # 軟 total：aces=1、s = total - 10（例如軟 18 = A,7 => s=8, aces=1）
            s = total - 10
            letter, evs = solve_cell(s, 1, dealer_col_to_up(up))
            row.append((letter, evs))
        soft[total] = row

    return {'hard': hard, 'soft': soft}


def dealer_col_to_up(col_1_to_10):
    """欄位順序是 2..10,A（跟 core.strategy.COLUMNS 一致），col=10 對應 A。"""
    return 1 if col_1_to_10 == 10 else col_1_to_10 + 1


def compare_to_table(rules, strategy):
    """拿精確解跟已編譯好的策略表對照，回傳不一致的格子列表（不含分牌）。

    strategy 是 core.strategy.make(...) 回傳的物件；它的表是編譯過的
    (action_bitmask, fallback) 元組。拿去比對前要先用
    strategy_mod.effective_cell_label() 依「這格在這桌規則下實際合不合法」
    解析成最終字母——不能直接拿格子裡的「主要動作」比，不然「莊家 A
    不可投降」這種規則會被誤判成表格錯誤（表裡寫 Rh，合法性判斷後其實
    會自動退回補牌，跟策略表檢視器的行為要一致，不能各算各的）。

    early surrender 的前置檢查優先於 hard 表本身這件事，也是
    effective_cell_label() 內建處理的（傳 strategy 進去就會查
    es_vs_ace/es_vs_ten），這裡不用重複判斷一次。
    """
    from . import strategy as strategy_mod   # 延遲 import：避免跟 strategy.py 循環引用

    solved = solve(rules)
    mismatches = []

    for kind, totals in (('hard', range(4, 22)), ('soft', range(12, 22))):
        table = getattr(strategy, kind)
        is_soft = kind == 'soft'
        for total in totals:
            if total not in table:
                continue
            row = table[total]
            hard_total = total - 10 if is_soft else total
            for col in range(10):
                solved_letter, evs = solved[kind][total][col]
                up = dealer_col_to_up(col + 1)

                table_letter = strategy_mod.effective_cell_label(
                    row[col], up, rules, hard_total, is_soft, strategy)
                # 精確解跟策略表在 EV 幾乎打平的格子（差距 < 0.001）不算不一致，
                # 那種格子選哪個都差不多，是浮點數尾數的問題，不是策略表錯了。
                best_ev = evs[solved_letter]
                table_ev = evs.get(table_letter)
                if table_ev is not None and abs(best_ev - table_ev) < 0.001:
                    continue
                if table_letter != solved_letter:
                    loss = best_ev - table_ev if table_ev is not None else None
                    mismatches.append({
                        'kind': kind, 'total': total, 'dealer': COLUMNS[col],
                        'table_says': table_letter, 'optimal': solved_letter,
                        'ev_table': table_ev, 'ev_optimal': best_ev, 'ev_loss': loss,
                        # 差距在 1% 以內的格子，答案常常會隨副數（真實牌桌是
                        # 有限副牌，這裡算的是無限牌組）翻盤，不代表表格錯了，
                        # 該用 core/scenario.py 針對實際副數跑蒙地卡羅才準。
                        # 注意：no-peek 造成的差距也可能落在這個範圍內，這個
                        # 旗標只代表「差距小」，不代表「一定是副數造成的」，
                        # 判斷原因還是要看規則組合。
                        'deck_sensitive': loss is not None and abs(loss) < 0.01,
                    })
    return mismatches
