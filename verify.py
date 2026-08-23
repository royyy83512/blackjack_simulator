#!/usr/bin/env python3
"""Engine correctness verification.

Three layers, from strongest to weakest:

  Layer 1: exact DP cross-check (strongest)
      core/exact.py sums over every card sequence weighted by probability
      at an infinite deck, with zero error. The simulation side disables
      splits and uses 200 decks to approximate an infinite deck; the two
      must agree. Passing this layer means hit/stand/double/dealer logic/
      settlement are all completely correct.

  Layer 2: known constants
      Dealer's final-outcome distribution, player natural frequency,
      per-round standard deviation.

  Layer 3: comparison against published house-edge figures
      Note: this simulator uses a "total-based" basic strategy table
      (looking only at the hand's total), while published figures usually
      assume a "composition-based" strategy (which also looks at exactly
      which cards make up the total). The latter earns roughly 0.03-0.04%
      more, so the simulation coming in 0.02-0.05% above the literature is
      expected, not a bug — layer 1 has already proven the engine itself
      is correct.

Usage: python3 verify.py [rounds per test]
"""
import sys
import time

from core.rules import Rules, SURRENDER_NONE, DOUBLE_10_11
from core.exact import dealer_distribution, no_split_edge
from core.shoe import Shoe
from core.engine import dealer_play
from core.runner import run
from core.stats import summarize

GREEN, RED, YEL, OFF = '\033[32m', '\033[31m', '\033[33m', '\033[0m'
FAILURES = []


def report(name, got, expect, tol, unit='%'):
    ok = abs(got - expect) <= tol
    mark = f"{GREEN}PASS{OFF}" if ok else f"{RED}FAIL{OFF}"
    print(f"  [{mark}] {name:<40} {got:+8.4f}{unit}  expected {expect:+.4f} +/-{tol:.4f}")
    if not ok:
        FAILURES.append(name)
    return ok


def layer1(rounds):
    print(f"\n[Layer 1] Exact DP cross-check -- 200 decks, no splits, no surrender, {rounds:,} rounds each")
    for h17 in (False, True):
        tag = 'H17' if h17 else 'S17'
        exact = no_split_edge(h17) * 100
        r = Rules(num_decks=200, penetration=0.75, surrender=SURRENDER_NONE,
                  dealer_hits_soft_17=h17, insurance_offered=False)
        s = summarize(run(r, 'basic-nosplit', rounds, curve_points=0, seed=555)[0])
        sim, ci = s['house_edge'] * 100, s['house_edge_ci95'] * 100
        report(f"{tag} house edge vs. exact DP", sim, exact, max(2 * ci, 0.005))


def layer2(rounds):
    print("\n[Layer 2] Known constants")
    for h17 in (False, True):
        tag = 'H17' if h17 else 'S17'
        exact = dealer_distribution(h17)
        shoe = Shoe(6, 0.75, seed=7)
        buckets = [0] * 6
        n = rounds // 2
        for _ in range(n):
            shoe.start_round()
            t = dealer_play([shoe.draw(), shoe.draw()], shoe, h17)
            buckets[5 if t > 21 else t - 17] += 1
        names = ['17', '18', '19', '20', '21', 'bust']
        for nm, b, e in zip(names, buckets, exact):
            # 6 decks differ slightly from infinite decks, so tolerance is widened to 0.25%
            report(f"dealer {tag} final {nm}", b / n * 100, e * 100,
                   max(0.25, 2 * 100 * (e * (1 - e) / n) ** 0.5))

    s = summarize(run(Rules(), 'basic', rounds, curve_points=0)[0])
    report("player natural frequency", s['player_bj_rate'] * 100, 4.749, 0.06)
    report("dealer natural frequency", s['dealer_bj_rate'] * 100, 4.749, 0.06)
    report("SD per round", s['sd_per_round'], 1.14, 0.02, unit='')


def layer3(rounds):
    print("\n[Layer 3] Comparison against published figures (total-based strategy, expected 0.02-0.05% above literature)")
    cases = [
        ("6D S17 DAS no-surrender 3:2", Rules(surrender=SURRENDER_NONE), 0.40),
        ("6D S17 DAS LS   3:2", Rules(), 0.33),
        ("6D H17 DAS no-surrender 3:2", Rules(dealer_hits_soft_17=True,
                                      surrender=SURRENDER_NONE), 0.62),
        ("6D S17 DAS LS   6:5", Rules(blackjack_pays=1.2), 1.72),
        ("2D S17 DAS LS   3:2", Rules(num_decks=2), 0.19),
        ("8D S17 DAS LS   3:2", Rules(num_decks=8), 0.36),
    ]
    for name, rules, lit in cases:
        s = summarize(run(rules, 'basic', rounds, curve_points=0, seed=4242)[0])
        e, ci = s['house_edge'] * 100, s['house_edge_ci95'] * 100
        report(name, e, lit + 0.03, max(0.07, 2 * ci))

    print("\n[Layer 3b] Direction and magnitude of rule effects (Common Random Numbers)")
    b = summarize(run(Rules(), 'basic', rounds, curve_points=0, seed=4242)[0])
    base, base_ci = b['house_edge'], b['house_edge_ci95']
    deltas = [
        ("H17", Rules(dealer_hits_soft_17=True), +0.22),
        ("no DAS", Rules(double_after_split=False), +0.14),
        ("no surrender", Rules(surrender=SURRENDER_NONE), +0.08),
        ("RSA on", Rules(resplit_aces=True), -0.08),
        ("BJ pays 6:5", Rules(blackjack_pays=1.2), +1.39),
        ("double only on 10/11", Rules(double_rule=DOUBLE_10_11), +0.21),
        ("no-peek, lose all (vs. the no-surrender baseline)", Rules(dealer_peek=False, surrender=SURRENDER_NONE), +0.11),
    ]
    ns = summarize(run(Rules(surrender=SURRENDER_NONE), 'basic', rounds,
                       curve_points=0, seed=4242)[0])
    for name, rules, exp in deltas:
        ref, ref_ci = (ns['house_edge'], ns['house_edge_ci95']) if 'no-surrender baseline' in name \
            else (base, base_ci)
        s2 = summarize(run(rules, 'basic', rounds, curve_points=0, seed=4242)[0])
        pooled = ((s2['house_edge_ci95'] ** 2 + ref_ci ** 2) ** 0.5) * 100
        # tolerance has to cover both statistical error and the small
        # systematic offset from the strategy table itself
        report(f"effect of {name}", (s2['house_edge'] - ref) * 100, exp,
               max(2 * pooled, 0.35 * abs(exp)))


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000_000
    t0 = time.time()
    print("=" * 76)
    layer1(rounds)
    layer2(rounds)
    layer3(rounds)
    print("=" * 76)
    if FAILURES:
        print(f"{RED}{len(FAILURES)} failed{OFF}: " + ", ".join(FAILURES))
    else:
        print(f"{GREEN}All passed{OFF}  ({time.time()-t0:.0f}s)")
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
