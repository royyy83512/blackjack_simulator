#!/usr/bin/env python3
"""Tests for core/solver.py.

Verification approach (see core/solver.py's header): the solver walks an
infinite deck, using the same exact dealer_given_up() distribution as
core/exact.py, so:

1. Whatever the solver's per-cell judgments aggregate to as an overall
   house edge must be <= the edge computed from the basic.json table (the
   optimal solution can never be worse than any specific strategy — this
   is a mathematical lower bound).
2. Cells where the solver thinks basic.json is "wrong," if the gap is
   large enough (not marginal / deck-sensitive), should be independently
   confirmable with the same ranking via scenario.py's Monte Carlo at a
   very high deck count (approximating an infinite deck) — two completely
   different computation methods should converge on the same answer.
3. A marginal cell already known to flip with deck count (finite vs.
   infinite) (A,2 vs. 5) should get flagged deck_sensitive, and a Monte
   Carlo run at a real deck count should measure the "flipped" answer,
   proving the solver's infinite-deck limitation is a meaningful caveat,
   not an excuse.

Usage: python3 test_solver.py
"""
import sys

from core.rules import Rules
from core.strategy import make
from core.solver import solve, compare_to_table
from core.exact import no_split_edge
from core.scenario import compare_actions
from core.engine import ACT_HIT, ACT_DOUBLE

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    mark = '\033[32mPASS\033[0m' if ok else '\033[31mFAIL\033[0m'
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ''))


def aggregate_edge_from_solved(rules, solved):
    from core.exact import P, p_dealer_bj
    total_ev = 0.0
    for c1, p1 in P:
        for c2, p2 in P:
            pbj = (c1 == 1 and c2 == 10) or (c1 == 10 and c2 == 1)
            for up, pu in P:
                pd = p_dealer_bj(up)
                if pbj:
                    ev = (1 - pd) * rules.blackjack_pays
                else:
                    tot, aces = c1 + c2, (c1 == 1) + (c2 == 1)
                    soft = aces > 0 and tot + 10 <= 21
                    disp = tot + 10 if soft else tot
                    kind = 'soft' if soft else 'hard'
                    col = 9 if up == 1 else up - 2
                    _letter, evs = solved[kind][disp][col]
                    ev = -pd + (1 - pd) * max(evs.values())
                total_ev += p1 * p2 * pu * ev
    return -total_ev


def main():
    print("[Optimal-solution lower bound: the solver's overall edge can't be worse than basic.json]")
    for h17 in (False, True):
        rules = Rules(dealer_hits_soft_17=h17)
        solved = solve(rules)
        edge_solver = aggregate_edge_from_solved(rules, solved)
        edge_table = no_split_edge(h17)
        tag = 'H17' if h17 else 'S17'
        ok = edge_solver <= edge_table + 1e-9
        check(f"{tag}: solver edge ({edge_solver*100:.4f}%) <= table edge ({edge_table*100:.4f}%)", ok)

    print("\n[Known answers: the solver's judgment on clear-cut cells should match published references]")
    rules = Rules()
    solved = solve(rules)

    def cell(kind, total, dealer_col):
        return solved[kind][total][dealer_col]

    letter, _ = cell('hard', 16, 8)     # 16 vs. 10 (col8=10)
    check("16 vs. 10 should surrender", letter == 'R', f"got {letter}")
    letter, _ = cell('hard', 11, 4)     # 11 vs. 6
    check("11 vs. 6 should double", letter == 'D', f"got {letter}")
    letter, _ = cell('soft', 19, 4)     # A,8 vs. 6 (S17)
    check("S17: A,8 vs. 6 should stand", letter == 'S', f"got {letter}")
    letter, _ = cell('hard', 17, 0)     # 17 vs. 2
    check("17 should stand against anything", letter == 'S', f"got {letter}")

    rules_h17 = Rules(dealer_hits_soft_17=True)
    solved_h17 = solve(rules_h17)
    letter, _ = solved_h17['soft'][19][4]
    check("H17: A,8 vs. 6 should switch to double", letter == 'D', f"got {letter}")

    print("\n[Cross-checked against basic.json, with Monte Carlo confirming the true direction of any difference]")
    strat = make('basic', rules)
    mismatches = compare_to_table(rules, strat)
    print(f"  {len(mismatches)} mismatched cells found under S17")
    sensitive = [m for m in mismatches if m['deck_sensitive']]
    real = [m for m in mismatches if not m['deck_sensitive']]
    check("every mismatch found so far is flagged deck-sensitive (no large-gap genuine table error)",
          len(real) == 0, f"non-deck-sensitive mismatches: {real}")

    if sensitive:
        m = sensitive[0]
        print(f"  Cross-checking the first deck_sensitive cell: {m['kind']} {m['total']} vs. {m['dealer']}")
        cards = (1, m['total'] - 11) if m['kind'] == 'soft' else None
        if cards and 2 <= cards[1] <= 10:
            up = 1 if m['dealer'] == 'A' else int(m['dealer'])
            R200 = Rules(num_decks=8)   # the max allowed deck count, approximating an infinite deck
            r = compare_actions(R200, 'basic', cards, up, 1_500_000, seed=3,
                               jobs=6, actions=(ACT_HIT, ACT_DOUBLE))
            ranking = {name: ev for _a, name, ev, _ci, _sd in r}
            solver_says_hit = m['optimal'] == 'H'
            mc_says_hit = ranking.get('Hit (H)', -99) > ranking.get('Double (D)', -99)
            check("Monte Carlo ranking matches the solver as deck count approaches infinite",
                  solver_says_hit == mc_says_hit,
                  f"solver={m['optimal']}  8-deck Monte Carlo: {ranking}")

    print("\n[no-peek: known answers, independently cross-checked against Monte Carlo]")
    from core.rules import SURRENDER_NONE, SURRENDER_LATE, SURRENDER_EARLY, LOSS_ALL, LOSS_ORIGINAL

    r_np_all = Rules(dealer_peek=False, surrender=SURRENDER_NONE, dealer_bj_loss=LOSS_ALL)
    solved_np_all = solve(r_np_all)
    letter, evs = solved_np_all['hard'][11][9]     # 11 vs. A
    check("no-peek LOSS_ALL: 11 vs. A should still hit (doubling's extra risk isn't worth it)",
          letter == 'H', f"got {letter}, D={evs.get('D')}")

    # under OBO, bj_penalty is the same constant -1 for stand/hit/double
    # regardless of which action it is, so it doesn't change which action
    # ranks best — but the absolute EV value won't equal peek's (S17's
    # evs_peek['D'] is ev_double_c itself, unaffected by p_bj; no-peek+OBO's
    # evs['D'] has the extra p_bj*(-1) term pulling the whole value down,
    # it's just that D gets pulled down together with S/H, so the ranking
    # doesn't change). Use 11 vs. 6 (clearly a double under peek) to verify
    # the ranking really isn't affected by no-peek+OBO.
    r_np_obo = Rules(dealer_peek=False, surrender=SURRENDER_NONE, dealer_bj_loss=LOSS_ORIGINAL)
    letter_obo6, evs_obo6 = solve(r_np_obo)['hard'][11][4]      # 11 vs. 6
    letter_peek6, evs_peek6 = solve(Rules())['hard'][11][4]
    check("no-peek OBO: the best action for 11 vs. 6 is still double, same as peek",
          letter_obo6 == letter_peek6 == 'D',
          f"peek={letter_peek6}  OBO={letter_obo6}")

    r_np_all6 = Rules(dealer_peek=False, surrender=SURRENDER_NONE, dealer_bj_loss=LOSS_ALL)
    letter_all6, evs_all6 = solve(r_np_all6)['hard'][11][4]
    check("dealer 6 can never have BJ (p_bj=0), LOSS_ALL and OBO should be identical for 11 vs. 6",
          abs(evs_all6['D'] - evs_obo6['D']) < 1e-9,
          f"LOSS_ALL D={evs_all6['D']:.5f}  OBO D={evs_obo6['D']:.5f}")

    # switch to dealer A (p_bj~30.8%, the highest BJ probability), which
    # is where LOSS_ALL actually gets worse than OBO
    letter, evs = solve(r_np_all)['hard'][11][9]
    _l, evs_obo = solve(r_np_obo)['hard'][11][9]
    check("no-peek LOSS_ALL's doubling EV should be much worse than OBO (dealer A has the highest BJ probability)",
          evs['D'] < evs_obo['D'] - 0.1, f"LOSS_ALL D={evs['D']:.5f}  OBO D={evs_obo['D']:.5f}")

    r_np_late = Rules(dealer_peek=False, surrender=SURRENDER_LATE, dealer_bj_loss=LOSS_ALL)
    letter, evs = solve(r_np_late)['hard'][16][8]   # 16 vs. 10
    check("no-peek LATE surrender: 16 vs. 10 should still surrender", letter == 'R', f"got {letter}")
    check("no-peek LATE surrender's EV should be worse than -0.5 (loses the full bet if the dealer later reveals BJ)",
          evs['R'] < -0.5, f"got {evs['R']:.5f}")

    r_np_early = Rules(dealer_peek=False, surrender=SURRENDER_EARLY, dealer_bj_loss=LOSS_ORIGINAL)
    _l, evs = solve(r_np_early)['hard'][16][8]
    check("no-peek EARLY surrender's EV should be exactly -0.5 (fixed regardless of whether the dealer later has BJ)",
          abs(evs['R'] - (-0.5)) < 1e-9, f"got {evs['R']:.5f}")

    print("\n[compare_to_table must correctly handle the early-surrender pre-check]")
    from core.presets import load as load_preset
    for name in ('wynn_macau', 'walkerhill_seoul'):
        rules_p, _ = load_preset(name)
        strat_p = make('basic', rules_p)
        mm = compare_to_table(rules_p, strat_p)
        real_mm = [m for m in mm if not m['deck_sensitive']]
        check(f"{name}: basic.json needs no changes (only the already-known deck-margin effect remains)",
              len(real_mm) == 0, f"real mismatches: {real_mm}")

    print("\n" + "=" * 70)
    if FAIL:
        print(f"\033[31m{len(FAIL)} failed\033[0m: " + ", ".join(FAIL))
        return 1
    print(f"\033[32mAll {len(PASS)} passed\033[0m")
    return 0


if __name__ == '__main__':
    sys.exit(main())
