"""Exact infinite-deck computation (no sampling, zero error).

This exists to be the engine's "gold standard": sum over every card
sequence weighted by its probability, then cross-check against the Monte
Carlo simulation. If they agree, hitting/standing/doubling/dealer logic/
settlement are all correct.

Splits are not modeled (handling splits exactly would need a whole
separate recursion, an order of magnitude more work), so the simulation
side must also disable splits when cross-checking against this module.
"""
from functools import lru_cache

from .engine import ACT_STAND, ACT_DOUBLE, ACT_HIT
from .rules import Rules, SURRENDER_NONE
from .strategy import make

# probability of each rank in an infinite deck
P = tuple((r, 4 / 52 if r < 10 else 16 / 52) for r in range(1, 11))


@lru_cache(None)
def dealer_from(s, aces, h17):
    """Play out from (total, ace count) to the end: returns the
    probabilities of (17,18,19,20,21,bust)."""
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
    """The dealer's unconditional final-outcome distribution starting from
    two fresh cards (21 includes blackjack)."""
    out = [0.] * 6
    for c1, p1 in P:
        for c2, p2 in P:
            sub = dealer_from(c1 + c2, (c1 == 1) + (c2 == 1), h17)
            for k in range(6):
                out[k] += p1 * p2 * sub[k]
    return tuple(out)


@lru_cache(None)
def dealer_given_up(up, h17):
    """The dealer's final-outcome distribution given upcard `up` and no BJ."""
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
    """Exact house edge: infinite deck, no splits, no surrender, doubling
    allowed on any two cards.

    Uses the same table from core.strategy, so it can be compared directly
    against the simulation.
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
        # each strategy table cell is (primary action, fallback if not allowed).
        # Splits and surrender aren't modeled here, so the only legal actions
        # are hit/stand/(double if allowed), and anything else falls back —
        # matching how the engine itself resolves a cell.
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
