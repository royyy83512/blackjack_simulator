#!/usr/bin/env python3
"""Deterministic unit tests using a stacked deck.

The statistical tests (verify.py) can tell you whether the overall EV is
right, but when a branch like splitting is broken, it usually only skews
the numbers slightly, which is hard to pin down. Here the deal order is
specified exactly, and results are asserted hand by hand.

Usage: python3 test_engine.py
"""
import sys

from core.rules import (Rules, SURRENDER_NONE, SURRENDER_LATE, SURRENDER_EARLY,
                        DOUBLE_10_11, LOSS_ALL, LOSS_ORIGINAL, normalize)
from core.engine import play_round, F_SPLIT, F_DOUBLED, F_SURRENDER, F_PLAYER_BJ, F_DEALER_BJ
from core.strategy import make
from core.shoe import Shoe

PASS, FAIL = [], []


class StackedShoe:
    """A fake shoe that deals cards in a given fixed order."""

    def __init__(self, cards):
        self.cards = list(cards)
        self.i = 0

    def start_round(self):
        pass

    def draw(self):
        if self.i >= len(self.cards):
            raise AssertionError(f"Ran out of cards (dealt {self.i} so far): {self.cards}")
        c = self.cards[self.i]
        self.i += 1
        return c

    tc = 0.0          # tests can set the count directly

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
        problems.append(f"net result {net:+g} != expected {expect_net:+g}")
    if expect_hands is not None and n_hands != expect_hands:
        problems.append(f"hand count {n_hands} != expected {expect_hands}")
    if expect_flags and (flags & expect_flags) != expect_flags:
        problems.append(f"missing flag(s) {expect_flags} (got {flags})")
    if forbid_flags and (flags & forbid_flags):
        problems.append(f"unexpected flag(s) {flags & forbid_flags}")
    unused = len(cards) - shoe.i
    if unused != leftover:
        problems.append(f"used {shoe.i} cards, {unused} left, expected {leftover} left")
    if problems:
        FAIL.append((name, problems))
        print(f"  \033[31mFAIL\033[0m {name}\n        " + "\n        ".join(problems))
    else:
        PASS.append(name)
        print(f"  \033[32mPASS\033[0m {name}  (net {net:+g}, {n_hands} hand(s))")


R = Rules()          # 6D S17 DAS any-two LS peek 3:2

print("\n[Splitting]")
# deal order: p1, up, p2, hole, then hand 1's hits -> hand 2's hits -> dealer's hits
case("8,8 vs. 6 splits into two hands, both stand on 18, dealer busts",
     [8, 6, 8, 10, 10, 10, 10], R, expect_net=+2, expect_hands=2, expect_flags=F_SPLIT)

# vs. a 6 the dealer stands on any 12+, so an upcard of 10 is needed to force a dealer bust
case("8,8 vs. 10: one hand wins with 17, one hand busts, net result is zero",
     [8, 10, 8, 6,   9,   4, 10,   10], R, expect_net=0, expect_hands=2,
     expect_flags=F_SPLIT)

case("split cap of 4 hands: repeated 8s can still only split into 4 hands",
     [8, 6, 8, 10,   8, 8, 10,   10, 10, 10,   10], R,
     expect_net=+4, expect_hands=4, expect_flags=F_SPLIT)

print("\n[Splitting aces]")
case("A,A split: each hand gets one card and stands, no further hitting",
     [1, 6, 1, 10, 10, 9, 10], R, expect_net=+2, expect_hands=2,
     expect_flags=F_SPLIT, forbid_flags=F_PLAYER_BJ)

case("21 after splitting an ace doesn't count as blackjack (pays 1x, not 1.5x)",
     [1, 6, 1, 10, 10, 10, 10], R, expect_net=+2, expect_hands=2)

case("RSA off: drawing another ace after splitting aces can't be split again",
     [1, 6, 1, 10,   1, 9,   10], Rules(resplit_aces=False),
     expect_net=+2, expect_hands=2)

case("RSA on: drawing another ace after splitting aces can be split again (first hand too)",
     [1, 6, 1, 10,   1, 10,   9,   10,   10], Rules(resplit_aces=True),
     expect_net=+3, expect_hands=3, expect_flags=F_SPLIT)

case("HSA on: can keep hitting after splitting aces (soft 16 vs. 10 hits into soft 21)",
     [1, 10, 1, 6,   5, 5,   9,   10], Rules(hit_split_aces=True, resplit_aces=False),
     expect_net=+2, expect_hands=2)

print("\n[Doubling]")
case("11 vs. 6 doubles, draws a 10 for 21",
     [5, 6, 6, 10, 10, 10], R, expect_net=+2, expect_flags=F_DOUBLED)

case("DAS off: can't double after a split",
     [8, 6, 8, 10,   3, 10,   3, 10,   10], Rules(double_after_split=False),
     expect_net=+2, expect_hands=2, forbid_flags=F_DOUBLED)

case("DAS on: an 11 after a split doubles",
     [8, 6, 8, 10,   3, 10,   10,   10], Rules(double_after_split=True),
     expect_net=+3, expect_hands=2, expect_flags=F_DOUBLED)

case("double only on 10/11: soft 17 vs. 4 can't double",
     [1, 4, 6, 10, 5, 10, 10], Rules(double_rule=DOUBLE_10_11),
     expect_net=+1, forbid_flags=F_DOUBLED, leftover=1)

print("\n[Surrender]")
case("16 vs. 10 late surrender, loses half",
     [10, 10, 6, 9], R, expect_net=-0.5, expect_flags=F_SURRENDER)

case("surrender off: 16 vs. 10 can only hit",
     [10, 10, 6, 9, 10], Rules(surrender=SURRENDER_NONE),
     expect_net=-1, forbid_flags=F_SURRENDER)

case("can't surrender after a split (three 18s all lose to a dealer 19)",
     [8, 10, 8, 9,   8, 10,   10,   10], R,
     expect_net=-3, expect_hands=3, forbid_flags=F_SURRENDER)

print("\n[Whether surrender vs. a dealer ace is allowed]")
# deal order: p1, dealer upcard, p2, hole card
case("surrender vs. ace allowed: 16 vs. A surrenders, loses half",
     [10, 1, 6, 6], Rules(), expect_net=-0.5, expect_flags=F_SURRENDER)

case("surrender vs. ace not allowed: 16 vs. A can only hit",
     [10, 1, 6, 6, 10], Rules(surrender_vs_ace=False),
     expect_net=-1, forbid_flags=F_SURRENDER)

case("surrender vs. ace not allowed doesn't affect surrender vs. a 10",
     [10, 10, 6, 9], Rules(surrender_vs_ace=False),
     expect_net=-0.5, expect_flags=F_SURRENDER)

case("surrender vs. ace not allowed also disables early surrender vs. ace",
     [10, 1, 6, 10, 6],
     Rules(dealer_peek=False, surrender=SURRENDER_EARLY, surrender_vs_ace=False),
     expect_net=-1, forbid_flags=F_SURRENDER)

print("\n[Blackjack and peek]")
case("player BJ pays 3:2",
     [1, 6, 10, 10], R, expect_net=+1.5, expect_flags=F_PLAYER_BJ)

case("player BJ pays only 6:5",
     [1, 6, 10, 10], Rules(blackjack_pays=1.2), expect_net=+1.2)

case("both sides have BJ: push",
     [1, 1, 10, 10], R, expect_net=0, expect_flags=F_PLAYER_BJ | F_DEALER_BJ)

case("dealer BJ: settled immediately under peek, player never acts",
     [10, 1, 6, 10], R, expect_net=-1, expect_flags=F_DEALER_BJ)

print("\n[no-peek / OBO]")
NP = Rules(dealer_peek=False, surrender=SURRENDER_NONE, dealer_bj_loss=LOSS_ALL)
# 11 vs. A stands as a hit, not a double, under S17, so use an upcard of
# 10 plus an ace hole card to construct a dealer BJ instead
case("no-peek, lose all: doubled then hit a dealer BJ, loses 2x",
     [5, 10, 6, 10,   1], NP, expect_net=-2, expect_flags=F_DEALER_BJ | F_DOUBLED)

OBO = Rules(dealer_peek=False, surrender=SURRENDER_NONE, dealer_bj_loss=LOSS_ORIGINAL)
case("no-peek OBO: doubled then hit a dealer BJ, only loses the original bet",
     [5, 10, 6, 10,   1], OBO, expect_net=-1, expect_flags=F_DEALER_BJ | F_DOUBLED)

case("dealer natural crushes a player's 3-card 21 (a loss, not a push)",
     [5, 10, 4,   7, 5,   1], NP, expect_net=-1, forbid_flags=F_DOUBLED)

ES = Rules(dealer_peek=False, surrender=SURRENDER_EARLY)
case("early surrender: 16 vs. A surrenders, only loses half even if the dealer has BJ",
     [10, 1, 6, 10], ES, expect_net=-0.5, expect_flags=F_SURRENDER | F_DEALER_BJ)

LSNP = Rules(dealer_peek=False, surrender=SURRENDER_LATE)
case("no-peek late surrender: loses the full bet if the dealer has BJ",
     [10, 1, 6, 10], LSNP, expect_net=-1, expect_flags=F_SURRENDER | F_DEALER_BJ)

print("\n[Dealer]")
case("S17: stands on soft 17, pushes against a player 17",
     [10, 1, 7, 6], Rules(surrender=SURRENDER_NONE), expect_net=0)

case("H17: hits soft 17, busts",
     [10, 1, 7, 6,   5, 10], Rules(dealer_hits_soft_17=True, surrender=SURRENDER_NONE),
     expect_net=+1)

print("\n[Continuous shuffling machine (CSM)]")


def check(name, got, want):
    ok = got == want
    (PASS if ok else FAIL).append(name)
    mark = '\033[32mPASS\033[0m' if ok else '\033[31mFAIL\033[0m'
    print(f"  [{mark}] {name}  (got {got}, expected {want})")


s = Shoe(6, 0.75, seed=1, csm=True)
before = s.shuffles
for _ in range(15):
    s.start_round()
    s.draw(); s.draw()
check("CSM reshuffles every hand (15 hands should reshuffle 15 times)", s.shuffles - before, 15)

s2 = Shoe(6, 0.75, seed=1, csm=False)
before2 = s2.shuffles
for _ in range(15):
    s2.start_round()
    s2.draw(); s2.draw()
check("non-CSM: 15 hands hasn't reached the cut card yet, shouldn't reshuffle", s2.shuffles - before2, 0)

s3 = Shoe(6, 0.75, seed=2, csm=True)
tcs = []
for _ in range(200):
    s3.start_round()
    tcs.append(s3.true_count)
    s3.draw(); s3.draw(); s3.draw()
check("true count is always 0 under CSM (every hand starts from a freshly shuffled deck)",
      all(abs(t) < 1e-9 for t in tcs), True)

r, notes = normalize(Rules(continuous_shuffle=True))
check("CSM's rule label includes CSM", 'CSM' in r.label(), True)
check("CSM reports that penetration is ignored", any('CSM' in n for n in notes), True)

print()
print("=" * 70)
if FAIL:
    print(f"\033[31m{len(FAIL)} failed\033[0m / {len(PASS)+len(FAIL)} total")
    sys.exit(1)
print(f"\033[32mAll {len(PASS)} passed\033[0m")
