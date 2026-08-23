#!/usr/bin/env python3
"""Blackjack simulator -- command-line interface.

Examples
    # run one million hands with a single configuration
    python3 cli.py --hands 1000000

    # one hundred million hands, eight cores, eight bankroll curves
    python3 cli.py --hands 100000000 --sessions 8

    # strategy comparison (Common Random Numbers)
    python3 cli.py --hands 5000000 --strategy basic mimic never-bust no-sp-dbl

    # rule sweep: compare 2/4/6/8 decks
    python3 cli.py --hands 20000000 --sweep decks

    # compare two casinos' complete rule sets head-to-head
    python3 cli.py --hands 20000000 --compare-presets wynn_macau walkerhill_seoul

    # custom rules
    python3 cli.py --hands 5000000 --decks 8 --h17 --no-das --surrender none --bj-pays 1.2
"""
import argparse
import json
import os
import sys
import time
from dataclasses import replace

from core.rules import (Rules, normalize, SURRENDER_NONE, SURRENDER_LATE,
                        SURRENDER_EARLY, DOUBLE_ANY2, DOUBLE_9_11,
                        DOUBLE_10_11, LOSS_ALL, LOSS_ORIGINAL)
from core.runner import run, compare
from core.stats import summarize, format_summary, hands_needed
from core.strategy import available, describe
from core import presets as presets_mod


def build_rules(a):
    r = Rules(
        num_decks=a.decks,
        penetration=a.pen,
        continuous_shuffle=a.csm,
        dealer_hits_soft_17=a.h17,
        double_rule=a.double,
        double_after_split=not a.no_das,
        surrender=a.surrender,
        dealer_peek=not a.no_peek,
        resplit_aces=a.rsa,
        hit_split_aces=a.hsa,
        surrender_vs_ace=not a.no_surrender_vs_ace,
        blackjack_pays=a.bj_pays,
        dealer_bj_loss=LOSS_ORIGINAL if a.obo else LOSS_ALL,
    )
    return normalize(r)


SWEEPS = {
    'decks':     [(f"{n} decks", lambda r, n=n: replace(r, num_decks=n)) for n in (1, 2, 4, 6, 8)],
    'csm':       [("cut card (traditional shoe)", lambda r: replace(r, continuous_shuffle=False)),
                  ("continuous shuffling machine (CSM)", lambda r: replace(r, continuous_shuffle=True))],
    'h17':       [("S17", lambda r: replace(r, dealer_hits_soft_17=False)),
                  ("H17", lambda r: replace(r, dealer_hits_soft_17=True))],
    'das':       [("DAS allowed", lambda r: replace(r, double_after_split=True)),
                  ("no DAS", lambda r: replace(r, double_after_split=False))],
    'surrender-ace': [("surrender vs. ace allowed", lambda r: replace(r, surrender_vs_ace=True)),
                      ("surrender vs. ace not allowed", lambda r: replace(r, surrender_vs_ace=False))],
    'surrender': [("no surrender", lambda r: replace(r, surrender=SURRENDER_NONE)),
                  ("late surrender", lambda r: replace(r, surrender=SURRENDER_LATE)),
                  ("early surrender (no-peek)",
                   lambda r: replace(r, surrender=SURRENDER_EARLY, dealer_peek=False))],
    'peek':      [("dealer peek", lambda r: replace(r, dealer_peek=True)),
                  ("no-peek, lose all", lambda r: replace(r, dealer_peek=False, dealer_bj_loss=LOSS_ALL)),
                  ("no-peek, OBO", lambda r: replace(r, dealer_peek=False, dealer_bj_loss=LOSS_ORIGINAL))],
    'rsa':       [("no resplitting aces", lambda r: replace(r, resplit_aces=False)),
                  ("resplit aces allowed", lambda r: replace(r, resplit_aces=True)),
                  ("resplit aces + hit split aces", lambda r: replace(r, resplit_aces=True, hit_split_aces=True))],
    'double':    [("double on any two cards", lambda r: replace(r, double_rule=DOUBLE_ANY2)),
                  ("only 9/10/11", lambda r: replace(r, double_rule=DOUBLE_9_11)),
                  ("only 10/11", lambda r: replace(r, double_rule=DOUBLE_10_11))],
    'bj':        [("BJ pays 3:2", lambda r: replace(r, blackjack_pays=1.5)),
                  ("BJ pays 6:5", lambda r: replace(r, blackjack_pays=1.2))],
}


def make_configs(a, rules):
    """Return [(label, Rules, strategy_name), ...]."""
    if a.sweep:
        base_strat = a.strategy[0]
        out = []
        for name, fn in SWEEPS[a.sweep]:
            r2, _ = normalize(fn(rules))
            out.append((name, r2, base_strat))
        return out
    return [(s, rules, s) for s in a.strategy]


def progress_bar(width=34):
    state = {'last': -1, 't0': time.time()}

    def cb(done, total):
        pct = done / total
        step = int(pct * 200)
        if step == state['last'] and done < total:
            return
        state['last'] = step
        el = time.time() - state['t0']
        eta = el / pct - el if pct > 0 else 0
        fill = int(pct * width)
        sys.stderr.write(
            f"\r  [{'█'*fill}{'·'*(width-fill)}] {pct*100:5.1f}%  "
            f"{done:,}/{total:,}  elapsed {el:.0f}s  eta {eta:.0f}s   ")
        sys.stderr.flush()
        if done >= total:
            sys.stderr.write("\n")
    return cb


def main():
    p = argparse.ArgumentParser(
        description='Blackjack Monte Carlo simulator',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)

    g = p.add_argument_group('simulation')
    g.add_argument('--hands', type=int, default=1_000_000, help='total rounds (default 1e6)')
    g.add_argument('--sessions', type=int, default=8, help='independent trials = number of bankroll curves (default 8)')
    g.add_argument('--bet', type=float, default=1.0, help='base bet')
    g.add_argument('--bankroll', type=float, default=100.0, help='bankroll (units), used for risk of ruin')
    g.add_argument('--seed', type=int, default=20240514, help='random seed; same seed lets configurations share the same shoe')
    g.add_argument('--jobs', type=int, default=os.cpu_count(), help='number of parallel cores')
    g.add_argument('--strategy', nargs='+', default=['basic'],
                   help='one or more strategies (see --list-strategies); more than one means a strategy comparison. '
                        'append -fixed to a name to skip rule-conditional overrides')
    g.add_argument('--list-strategies', action='store_true',
                   help='list every strategy under strategies/ and exit')
    g.add_argument('--sweep', choices=sorted(SWEEPS), help='sweep one rule dimension and compare')
    g.add_argument('--preset', help='apply a casino\'s rule preset in one shot (see --list-presets); '
                   'when given, all the --decks/--h17/etc. rule flags below are ignored')
    g.add_argument('--compare-presets', nargs='+', metavar='NAME',
                   help='compare two or more casino presets head-to-head under the same strategy '
                        '(Common Random Numbers); ignores --preset/--sweep/the individual rule flags')
    g.add_argument('--list-presets', action='store_true',
                   help='list every casino rule preset under presets/ and exit')

    r = p.add_argument_group('casino rules')
    r.add_argument('--decks', type=int, default=6, help='number of decks 2-8 (default 6)')
    r.add_argument('--pen', type=float, default=0.75, help='penetration: fraction dealt before the cut card')
    r.add_argument('--csm', action='store_true',
                   help='continuous shuffling machine: reshuffles every hand, no cut card, penetration ignored, defeats card counting')
    r.add_argument('--h17', action='store_true', help='dealer hits soft 17 (default S17)')
    r.add_argument('--double', default=DOUBLE_ANY2,
                   choices=[DOUBLE_ANY2, DOUBLE_9_11, DOUBLE_10_11], help='when doubling is allowed')
    r.add_argument('--no-das', action='store_true', help='no doubling after a split')
    r.add_argument('--surrender', default=SURRENDER_LATE,
                   choices=[SURRENDER_NONE, SURRENDER_LATE, SURRENDER_EARLY])
    r.add_argument('--no-surrender-vs-ace', action='store_true',
                   help='no surrender when the dealer upcard is an ace (some casinos do this)')
    r.add_argument('--no-peek', action='store_true', help='dealer does not check the hole card (European ENHC)')
    r.add_argument('--obo', action='store_true', help='player only loses the original bet on a dealer BJ (requires --no-peek)')
    r.add_argument('--rsa', action='store_true', help='allow resplitting aces')
    r.add_argument('--hsa', action='store_true', help='allow hitting after splitting aces')
    r.add_argument('--bj-pays', type=float, default=1.5, help='blackjack payout: 1.5=3:2, 1.2=6:5')

    o = p.add_argument_group('output')
    o.add_argument('--out', default='results', help='output directory for charts and JSON')
    o.add_argument('--no-charts', action='store_true')
    o.add_argument('--json', action='store_true', help='also write summary.json')
    o.add_argument('--quiet', action='store_true', help='hide the progress bar')

    a = p.parse_args()

    if a.list_strategies:
        print(f"\nStrategy directory: {__import__('core.strategy', fromlist=['x']).STRATEGY_DIR}\n")
        for name, desc, counting in describe():
            print(f"  {name:<20} {'[counting]' if counting else '          '} {desc}")
        print("\n  Any name can take a -fixed suffix to skip rule-conditional overrides"
              " (e.g. basic-fixed always uses the S17/DAS table)\n")
        return

    if a.list_presets:
        print(f"\nPreset directory: {presets_mod.PRESET_DIR}\n")
        for name, disp, desc in presets_mod.describe():
            print(f"  {name:<16} {disp}")
            print(f"    {desc}")
        print()
        return

    for name in a.strategy:
        if name.removesuffix('-fixed') not in available():
            raise SystemExit(f"Unknown strategy '{name}'. Available: {', '.join(available())}"
                             f" (a -fixed suffix is also accepted)")

    rules = None
    if a.compare_presets:
        if len(a.compare_presets) < 2:
            raise SystemExit("--compare-presets needs at least two preset names")
        preset_display = {n: d for n, d, _ in presets_mod.describe()}
        configs = []
        for name in a.compare_presets:
            try:
                r2, notes2 = presets_mod.load(name)
            except presets_mod.PresetError as e:
                raise SystemExit(f"{e}\nAvailable presets: {', '.join(presets_mod.available())}"
                                 " (use --list-presets for details)")
            disp = preset_display.get(name, name)
            configs.append((disp, r2, a.strategy[0]))
            for n in notes2:
                print(f"  Rule adjustment ({disp}): {n}")
        print(f"\nComparing presets: {', '.join(a.compare_presets)}  (strategy: {a.strategy[0]}, "
              "ignoring --preset/--sweep/individual rule flags)")
    else:
        if a.preset:
            try:
                rules, notes = presets_mod.load(a.preset)
            except presets_mod.PresetError as e:
                raise SystemExit(f"{e}\nAvailable presets: {', '.join(presets_mod.available())}"
                                 " (use --list-presets for details)")
            print(f"  Applying preset: {a.preset} (ignoring the other --decks/--h17/etc. rule flags)")
        else:
            rules, notes = build_rules(a)
        for n in notes:
            print(f"  Rule adjustment: {n}")

        configs = make_configs(a, rules)
        print(f"\nRules: {rules.label()}   penetration {rules.penetration:.0%}")
    print(f"Simulating: {a.hands:,} rounds x {len(configs)} configuration(s) = "
          f"{a.hands*len(configs):,} rounds, {a.jobs} cores, {a.sessions} session(s)")
    prec = 1.96 * 1.14 / (a.hands ** 0.5) * 100
    print(f"Expected precision: about +/-{prec:.4f}% per configuration (95% CI)")
    if prec > 0.05:
        need = hands_needed(0.0005)
        print(f"  Note: resolving a 0.05%-scale rule difference needs about {need:,.0f} rounds per configuration")
    print()

    cb = None if a.quiet else progress_bar()
    t0 = time.time()
    if len(configs) == 1:
        label, rr, sname = configs[0]
        merged, per_s = run(rr, sname, a.hands, a.sessions, a.bet, a.seed,
                            a.jobs, 0 if a.no_charts else 2000, label, cb)
        results = [(label, merged, per_s)]
    else:
        results = compare(configs, a.hands, a.sessions, a.bet, a.seed,
                          a.jobs, 0 if a.no_charts else 2000, cb)
    wall = time.time() - t0

    summaries = []
    for label, merged, per_s in results:
        s = summarize(merged, a.bankroll, a.bet)
        s['label'] = label
        summaries.append(s)
        print(f"\n-- {label} " + "-" * max(2, 62 - len(label)))
        print(format_summary(s))

    if len(summaries) > 1:
        print("\n" + "=" * 78)
        print("Comparison (same shoe / Common Random Numbers)")
        print(f"  {'Configuration':<30}{'House edge':>14}{'95% CI':>11}{'EV/round':>13}{'SD':>8}")
        base = summaries[0]['house_edge']
        for s in summaries:
            print(f"  {s['label']:<30}{s['house_edge']*100:>13.4f}%"
                  f"{s['house_edge_ci95']*100:>10.4f}%"
                  f"{s['ev_per_round']:>13.5f}{s['sd_per_round']:>8.3f}")
        print(f"\n  Difference relative to \"{summaries[0]['label']}\":")
        for s in summaries[1:]:
            d = (s['house_edge'] - base) * 100
            pooled = (s['house_edge_ci95']**2 + summaries[0]['house_edge_ci95']**2) ** 0.5 * 100
            sig = "significant" if abs(d) > pooled else "not resolvable (needs more hands)"
            print(f"    {s['label']:<30}{d:+8.4f}%  +/-{pooled:.4f}  {sig}")

    print(f"\nTotal wall time {wall:.1f}s, overall {a.hands*len(configs)/wall:,.0f} rounds/sec")

    if not a.no_charts:
        os.makedirs(a.out, exist_ok=True)
        import charts
        groups = [(label, per_s) for label, _m, per_s in results]
        paths = [charts.save(charts.bankroll_curves(groups), os.path.join(a.out, 'bankroll.png'))]
        if a.sessions > 1:
            paths.append(charts.save(charts.result_distribution(groups),
                                     os.path.join(a.out, 'distribution.png')))
        if len(summaries) > 1:
            paths.append(charts.save(charts.edge_comparison(summaries),
                                     os.path.join(a.out, 'comparison.png')))
        print("Charts: " + "  ".join(paths))

    if a.json:
        os.makedirs(a.out, exist_ok=True)
        path = os.path.join(a.out, 'summary.json')
        with open(path, 'w') as f:
            json.dump({'rules': rules.to_dict() if rules is not None else None,
                       'presets': a.compare_presets, 'hands': a.hands,
                       'sessions': a.sessions, 'seed': a.seed,
                       'summaries': summaries}, f, ensure_ascii=False, indent=2)
        print("JSON: " + path)


if __name__ == '__main__':
    main()
