#!/usr/bin/env python3
"""用「疊好的牌」做確定性單元測試。

統計測試（verify.py）能告訴你整體 EV 對不對，但分牌這類分支出錯時
只會讓數字偏一點點，很難定位。這裡直接指定發牌順序，逐手斷言結果。

跑法： python3 test_engine.py
"""
import sys

from core.rules import (Rules, SURRENDER_NONE, SURRENDER_LATE, SURRENDER_EARLY,
                        DOUBLE_10_11, LOSS_ALL, LOSS_ORIGINAL, normalize)
from core.engine import play_round, F_SPLIT, F_DOUBLED, F_SURRENDER, F_PLAYER_BJ, F_DEALER_BJ
from core.strategy import make
from core.shoe import Shoe

PASS, FAIL = [], []


class StackedShoe:
    """照著給定順序發牌的假牌靴。"""

    def __init__(self, cards):
        self.cards = list(cards)
        self.i = 0

    def start_round(self):
        pass

    def draw(self):
        if self.i >= len(self.cards):
            raise AssertionError(f"牌不夠用了（已發 {self.i} 張）：{self.cards}")
        c = self.cards[self.i]
        self.i += 1
        return c

    tc = 0.0          # 測試可以直接指定計數

    @property
    def true_count(self):
        return self.tc

    @property
    def running_count(self):
        return self.tc

    @property
    def decks_left(self):
        return 6.0


def case(name, cards, rules, expect_net, expect_hands=None, expect_flags=0,
         forbid_flags=0, strategy='basic', leftover=0):
    shoe = StackedShoe(cards)
    strat = make(strategy, rules)
    net, init_w, total_w, n_hands, dealer_total, flags = play_round(shoe, strat, rules, 1.0)
    problems = []
    if abs(net - expect_net) > 1e-9:
        problems.append(f"淨損益 {net:+g} != 預期 {expect_net:+g}")
    if expect_hands is not None and n_hands != expect_hands:
        problems.append(f"手數 {n_hands} != 預期 {expect_hands}")
    if expect_flags and (flags & expect_flags) != expect_flags:
        problems.append(f"缺少旗標 {expect_flags}（實際 {flags}）")
    if forbid_flags and (flags & forbid_flags):
        problems.append(f"不該出現的旗標 {flags & forbid_flags}")
    unused = len(cards) - shoe.i
    if unused != leftover:
        problems.append(f"用掉 {shoe.i} 張，剩 {unused} 張，預期剩 {leftover}")
    if problems:
        FAIL.append((name, problems))
        print(f"  \033[31mFAIL\033[0m {name}\n        " + "\n        ".join(problems))
    else:
        PASS.append(name)
        print(f"  \033[32mPASS\033[0m {name}  (net {net:+g}, {n_hands} 手)")


R = Rules()          # 6D S17 DAS 任兩張 LS peek 3:2

print("\n【分牌】")
# 發牌序：p1, up, p2, hole, 之後依序是第1手補牌 -> 第2手補牌 -> 莊家補牌
case("8,8 對 6 分成兩手，都停 18，莊家爆牌",
     [8, 6, 8, 10, 10, 10, 10], R, expect_net=+2, expect_hands=2, expect_flags=F_SPLIT)

# 對 6 的話 12 以上全都停牌，所以要用莊家 10 才做得出爆牌
case("8,8 對 10：一手 17 贏、一手爆牌，淨值歸零",
     [8, 10, 8, 6,   9,   4, 10,   10], R, expect_net=0, expect_hands=2,
     expect_flags=F_SPLIT)

case("分牌上限 4 手：連續拿到 8 也只能分成 4 手",
     [8, 6, 8, 10,   8, 8, 10,   10, 10, 10,   10], R,
     expect_net=+4, expect_hands=4, expect_flags=F_SPLIT)

print("\n【分 A】")
case("A,A 分牌：各拿一張就停，不能再補",
     [1, 6, 1, 10, 10, 9, 10], R, expect_net=+2, expect_hands=2,
     expect_flags=F_SPLIT, forbid_flags=F_PLAYER_BJ)

case("分 A 後的 21 不算 blackjack（只賠 1 倍不是 1.5 倍）",
     [1, 6, 1, 10, 10, 10, 10], R, expect_net=+2, expect_hands=2)

case("關掉 RSA：分 A 後又拿到 A 也不能再分",
     [1, 6, 1, 10,   1, 9,   10], Rules(resplit_aces=False),
     expect_net=+2, expect_hands=2)

case("開啟 RSA：分 A 後拿到 A 可以再分（第一手也要能分）",
     [1, 6, 1, 10,   1, 10,   9,   10,   10], Rules(resplit_aces=True),
     expect_net=+3, expect_hands=3, expect_flags=F_SPLIT)

case("開啟 HSA：分 A 後可以繼續補牌（軟 16 對 10 補成軟 21）",
     [1, 10, 1, 6,   5, 5,   9,   10], Rules(hit_split_aces=True, resplit_aces=False),
     expect_net=+2, expect_hands=2)

print("\n【加倍】")
case("11 對 6 加倍，拿到 10 變 21",
     [5, 6, 6, 10, 10, 10], R, expect_net=+2, expect_flags=F_DOUBLED)

case("關掉 DAS：分牌後不能加倍",
     [8, 6, 8, 10,   3, 10,   3, 10,   10], Rules(double_after_split=False),
     expect_net=+2, expect_hands=2, forbid_flags=F_DOUBLED)

case("開啟 DAS：分牌後 11 點會加倍",
     [8, 6, 8, 10,   3, 10,   10,   10], Rules(double_after_split=True),
     expect_net=+3, expect_hands=2, expect_flags=F_DOUBLED)

case("只有 10/11 可加倍：軟 17 對 4 不能加倍",
     [1, 4, 6, 10, 5, 10, 10], Rules(double_rule=DOUBLE_10_11),
     expect_net=+1, forbid_flags=F_DOUBLED, leftover=1)

print("\n【投降】")
case("16 對 10 late surrender，輸一半",
     [10, 10, 6, 9], R, expect_net=-0.5, expect_flags=F_SURRENDER)

case("關掉投降：16 對 10 只能補牌",
     [10, 10, 6, 9, 10], Rules(surrender=SURRENDER_NONE),
     expect_net=-1, forbid_flags=F_SURRENDER)

case("分牌之後不能投降（三手 18 全輸給莊家 19）",
     [8, 10, 8, 9,   8, 10,   10,   10], R,
     expect_net=-3, expect_hands=3, forbid_flags=F_SURRENDER)

print("\n【莊家 A 能否投降】")
# 發牌序：p1, 莊家明牌, p2, 底牌
case("莊家 A 可投降：16 對 A 投降輸一半",
     [10, 1, 6, 6], Rules(), expect_net=-0.5, expect_flags=F_SURRENDER)

case("莊家 A 不可投降：16 對 A 只能補牌",
     [10, 1, 6, 6, 10], Rules(surrender_vs_ace=False),
     expect_net=-1, forbid_flags=F_SURRENDER)

case("莊家 A 不可投降不影響對 10 的投降",
     [10, 10, 6, 9], Rules(surrender_vs_ace=False),
     expect_net=-0.5, expect_flags=F_SURRENDER)

case("莊家 A 不可投降時，early surrender 對 A 也失效",
     [10, 1, 6, 10, 6],
     Rules(dealer_peek=False, surrender=SURRENDER_EARLY, surrender_vs_ace=False),
     expect_net=-1, forbid_flags=F_SURRENDER)

print("\n【blackjack 與 peek】")
case("玩家 BJ 賠 3:2",
     [1, 6, 10, 10], R, expect_net=+1.5, expect_flags=F_PLAYER_BJ)

case("玩家 BJ 只賠 6:5",
     [1, 6, 10, 10], Rules(blackjack_pays=1.2), expect_net=+1.2)

case("雙方都 BJ：和局",
     [1, 1, 10, 10], R, expect_net=0, expect_flags=F_PLAYER_BJ | F_DEALER_BJ)

case("莊家 BJ：peek 立刻結算，玩家不動作",
     [10, 1, 6, 10], R, expect_net=-1, expect_flags=F_DEALER_BJ)

print("\n【no-peek / OBO】")
NP = Rules(dealer_peek=False, surrender=SURRENDER_NONE, dealer_bj_loss=LOSS_ALL)
# 11 對 A 在 S17 是補牌不是加倍，所以用莊家 10 明牌 + A 底牌來湊莊家 BJ
case("no-peek 追加注全輸：加倍後遇到莊家 BJ 輸 2 倍",
     [5, 10, 6, 10,   1], NP, expect_net=-2, expect_flags=F_DEALER_BJ | F_DOUBLED)

OBO = Rules(dealer_peek=False, surrender=SURRENDER_NONE, dealer_bj_loss=LOSS_ORIGINAL)
case("no-peek OBO：加倍後遇到莊家 BJ 只輸原始注",
     [5, 10, 6, 10,   1], OBO, expect_net=-1, expect_flags=F_DEALER_BJ | F_DOUBLED)

case("莊家 natural 通殺玩家三張湊成的 21（是輸不是和）",
     [5, 10, 4,   7, 5,   1], NP, expect_net=-1, forbid_flags=F_DOUBLED)

ES = Rules(dealer_peek=False, surrender=SURRENDER_EARLY)
case("early surrender：16 對 A 投降，莊家有 BJ 也只輸一半",
     [10, 1, 6, 10], ES, expect_net=-0.5, expect_flags=F_SURRENDER | F_DEALER_BJ)

LSNP = Rules(dealer_peek=False, surrender=SURRENDER_LATE)
case("no-peek 的 late surrender：莊家 BJ 時要輸全額",
     [10, 1, 6, 10], LSNP, expect_net=-1, expect_flags=F_SURRENDER | F_DEALER_BJ)

print("\n【莊家】")
case("S17：軟 17 停牌，和玩家 17 和局",
     [10, 1, 7, 6], Rules(surrender=SURRENDER_NONE), expect_net=0)

case("H17：軟 17 要補牌，補到爆牌",
     [10, 1, 7, 6,   5, 10], Rules(dealer_hits_soft_17=True, surrender=SURRENDER_NONE),
     expect_net=+1)

print("\n【連續洗牌機 (CSM)】")


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    mark = '\033[32mPASS\033[0m' if ok else '\033[31mFAIL\033[0m'
    print(f"  [{mark}] {name}  (得 {got}，期望 {want})")


s = Shoe(6, 0.75, seed=1, csm=True)
before = s.shuffles
for _ in range(15):
    s.start_round()
    s.draw(); s.draw()
check("CSM 每一手都重洗（15 手應該洗 15 次）", s.shuffles - before, 15)

s2 = Shoe(6, 0.75, seed=1, csm=False)
before2 = s2.shuffles
for _ in range(15):
    s2.start_round()
    s2.draw(); s2.draw()
check("非 CSM 15 手還不到切牌點，不該重洗", s2.shuffles - before2, 0)

s3 = Shoe(6, 0.75, seed=2, csm=True)
tcs = []
for _ in range(200):
    s3.start_round()
    tcs.append(s3.true_count)
    s3.draw(); s3.draw(); s3.draw()
check("CSM 下真數永遠是 0（每手都從剛洗好的牌開始算）",
      all(abs(t) < 1e-9 for t in tcs), True)

r, notes = normalize(Rules(continuous_shuffle=True))
check("CSM 的規則標籤含 CSM", 'CSM' in r.label(), True)
check("CSM 會回報 penetration 被忽略的提示", any('CSM' in n for n in notes), True)

print()
print("=" * 70)
if FAIL:
    print(f"\033[31m{len(FAIL)} 項未通過\033[0m / 共 {len(PASS)+len(FAIL)} 項")
    sys.exit(1)
print(f"\033[32m全部 {len(PASS)} 項通過\033[0m")
