#!/usr/bin/env python3
"""Tests for core/scenario.py: cross-checked against known standard basic
strategy answers.

These answers come from the standard 6D S17 DAS LS table, verified
repeatedly in published references — if the simulation's "best action
ranking" doesn't match, there's a bug. EV itself carries sampling error,
so only the ranking is verified here, not agreement to several decimal
places.

Usage: python3 test_scenario.py [rounds per action, default 800000]
"""
import sys

from core.rules import Rules
from core.engine import ACT_HIT, ACT_STAND, ACT_DOUBLE, ACT_SPLIT, ACT_SURRENDER
from core.scenario import compare_actions, legal_first_actions, ScenarioError

PASS, FAIL = [], []


def check_best(name, cards, up, expect_best, rounds, rules=None, jobs=4):
    rules = rules or Rules()
    results = compare_actions(rules, 'basic', cards, up, rounds, seed=2024, jobs=jobs)
    got_best = results[0][0]
    ok = got_best == expect_best
    (PASS if ok else FAIL).append(name)
    mark = '\033[32mPASS\033[0m' if ok else '\033[31mFAIL\033[0m'
    ranking = ', '.join(f"{n}={ev:+.3f}" for _a, n, ev, _ci, _sd in results)
    print(f"  [{mark}] {name:<20} best={results[0][1]:<8} expected={results[0][1] if ok else '?'}  ({ranking})")
    return ok


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 800_000
    print(f"{rounds:,} hands per action\n")

    print("[Known answers: 6D S17 DAS LS, standard basic strategy]")
    check_best("8,8 vs. 10 should split", (8, 8), 10, ACT_SPLIT, rounds)
    check_best("16 (10,6) vs. 10 should surrender", (10, 6), 10, ACT_SURRENDER, rounds)
    check_best("A,8 vs. 6 should stand", (1, 8), 6, ACT_STAND, rounds)
    check_best("A,7 vs. 9 should hit", (1, 7), 9, ACT_HIT, rounds)
    check_best("11 vs. 6 should double", (5, 6), 6, ACT_DOUBLE, rounds)
    check_best("A,A vs. anything should split", (1, 1), 7, ACT_SPLIT, rounds)

    print("\n[H17 changes the answer (compared against S17)]")
    H = Rules(dealer_hits_soft_17=True)
    check_best("H17: A,8 vs. 6 should switch to double", (1, 8), 6, ACT_DOUBLE, rounds, rules=H)
    check_best("S17: A,8 vs. 6 is still stand", (1, 8), 6, ACT_STAND, rounds)

    print("\n[Legal-action checks]")
    legal = legal_first_actions((8, 8), 10, Rules())
    ok = bool(legal & ACT_SPLIT) and bool(legal & ACT_SURRENDER)
    (PASS if ok else FAIL).append("8,8 vs. 10: both split and surrender are legal")
    print(f"  [{'PASS' if ok else 'FAIL'}] 8,8 vs. 10: both split and surrender are legal")

    legal2 = legal_first_actions((10, 6), 10, Rules())
    ok2 = not (legal2 & ACT_SPLIT)
    (PASS if ok2 else FAIL).append("10,6 isn't a pair, so split shouldn't be an option")
    print(f"  [{'PASS' if ok2 else 'FAIL'}] 10,6 isn't a pair, so split shouldn't be an option")

    try:
        compare_actions(Rules(), 'basic', (10, 6), 10, 1000, actions=(ACT_SPLIT,))
        FAIL.append("requesting an illegal action should raise an error")
        print("  [FAIL] requesting an illegal action should raise an error")
    except ScenarioError:
        PASS.append("requesting an illegal action should raise an error")
        print("  [PASS] requesting an illegal action should raise an error (correctly raised ScenarioError)")

    print("\n" + "=" * 70)
    if FAIL:
        print(f"\033[31m{len(FAIL)} failed\033[0m: " + ", ".join(FAIL))
        return 1
    print(f"\033[32mAll {len(PASS)} passed\033[0m")
    return 0


if __name__ == '__main__':
    sys.exit(main())
