"""無限牌組的精確計算（不抽樣，零誤差）。

存在的意義是當引擎的「金標準」：對所有牌序按機率加權求和，
拿來跟蒙地卡羅模擬對照。兩者吻合 => 補牌/停牌/加倍/莊家邏輯/結算正確。

分牌沒有納入（精確處理分牌要另外一套遞迴，工程量差一個量級），
所以交叉驗證時模擬端也要關掉分牌。
"""
from functools import lru_cache

from .engine import ACT_STAND, ACT_DOUBLE, ACT_HIT
from .rules import Rules, SURRENDER_NONE
from .strategy import make

# 無限牌組下各點數的機率
P = tuple((r, 4 / 52 if r < 10 else 16 / 52) for r in range(1, 11))


@lru_cache(None)
def dealer_from(s, aces, h17):
    """從 (點數和, A 數) 打到底：回傳 (17,18,19,20,21,爆) 的機率。"""
    soft = aces > 0 and s + 10 <= 21
    t = s + 10 if soft else s
    if t > 21:
        return (0., 0., 0., 0., 0., 1.)
    if t > 17 or (t == 17 and not (soft and h17)):
        return tuple(1. if k == t - 17 else 0. for k in range(5)) + (0.,)
    out = [0.] * 6
    for c, p in P:
        sub = dealer_from(s + c, aces + (c == 1), h17)
        for k in range(6):
            out[k] += p * sub[k]
    return tuple(out)


@lru_cache(None)
def dealer_distribution(h17=False):
    """莊家起手兩張打到底的非條件終局分布（21 含 blackjack）。"""
    out = [0.] * 6
    for c1, p1 in P:
        for c2, p2 in P:
            sub = dealer_from(c1 + c2, (c1 == 1) + (c2 == 1), h17)
            for k in range(6):
                out[k] += p1 * p2 * sub[k]
    return tuple(out)


@lru_cache(None)
def dealer_given_up(up, h17):
    """莊家明牌為 up 且已確認沒有 BJ 時的終局分布。"""
    holes = [(h, p) for h, p in P if not ((up == 1 and h == 10) or (up == 10 and h == 1))]
    z = sum(p for _h, p in holes)
    out = [0.] * 6
    for h, p in holes:
        sub = dealer_from(up + h, (up == 1) + (h == 1), h17)
        for k in range(6):
            out[k] += (p / z) * sub[k]
    return tuple(out)


def p_dealer_bj(up):
    return 16 / 52 if up == 1 else (4 / 52 if up == 10 else 0.0)


def _ev_stand(total, up, h17):
    d = dealer_given_up(up, h17)
    ev = d[5]
    for k in range(5):
        dt = 17 + k
        ev += d[k] * (1 if total > dt else (-1 if total < dt else 0))
    return ev


def no_split_edge(h17=False, blackjack_pays=1.5):
    """精確賭場優勢：無限牌組、不分牌、不投降、任兩張可加倍。

    用的是 core.strategy 的同一份表，所以能直接跟模擬對照。
    """
    rules = Rules(dealer_hits_soft_17=h17, surrender=SURRENDER_NONE,
                  insurance_offered=False)
    tbl = make('basic-nosplit', rules)
    memo = {}

    def player_ev(s, aces, up, can_double):
        key = (s, aces, up, can_double)
        hit = memo.get(key)
        if hit is not None:
            return hit
        soft = aces > 0 and s + 10 <= 21
        total = s + 10 if soft else s
        if total > 21:
            return -1.0
        if total == 21:
            return _ev_stand(21, up, h17)
        d = 9 if up == 1 else up - 2
        # 策略表的每一格是 (主要動作, 不能做時的退路)。
        # 這裡不模擬分牌也不模擬投降，所以合法動作只有補／停／(可加倍時)加倍，
        # 其餘一律走退路 —— 跟引擎的解析方式一致。
        act, fallback = (tbl.soft if soft else tbl.hard)[total][d]
        legal = ACT_HIT | ACT_STAND | (ACT_DOUBLE if can_double else 0)
        if not (act & legal):
            act = fallback if (fallback and fallback & legal) else ACT_HIT
        if act == ACT_STAND:
            ev = _ev_stand(total, up, h17)
        elif act == ACT_DOUBLE:
            ev = 0.0
            for c, p in P:
                s2, a2 = s + c, aces + (c == 1)
                t2 = s2 + 10 if (a2 and s2 + 10 <= 21) else s2
                ev += p * (-1.0 if t2 > 21 else _ev_stand(t2, up, h17))
            ev *= 2.0
        else:
            ev = 0.0
            for c, p in P:
                ev += p * player_ev(s + c, aces + (c == 1), up, False)
        memo[key] = ev
        return ev

    total_ev = 0.0
    for c1, p1 in P:
        for c2, p2 in P:
            pbj = (c1 == 1 and c2 == 10) or (c1 == 10 and c2 == 1)
            for up, pu in P:
                pd = p_dealer_bj(up)
                if pbj:
                    ev = (1 - pd) * blackjack_pays
                else:
                    ev = -pd + (1 - pd) * player_ev(
                        c1 + c2, (c1 == 1) + (c2 == 1), up, True)
                total_ev += p1 * p2 * pu * ev
    return -total_ev
