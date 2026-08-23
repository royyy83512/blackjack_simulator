#!/usr/bin/env python3
"""Tests for core/presets.py: casino rule presets applied in one click.

Usage: python3 test_presets.py
"""
import sys

from core.presets import load, describe, available, PresetError
from core.rules import (SURRENDER_EARLY, SURRENDER_LATE, DOUBLE_ANY2,
                        LOSS_ORIGINAL, LOSS_ALL)

PASS, FAIL = [], []


def check(name, ok, detail=''):
    (PASS if ok else FAIL).append(name)
    mark = '\033[32mPASS\033[0m' if ok else '\033[31mFAIL\033[0m'
    print(f"  [{mark}] {name}" + (f"  {detail}" if detail else ''))


def main():
    print("[Preset files exist and can be listed]")
    names = available()
    check("at least one preset exists", len(names) > 0, str(names))
    check("wynn_macau is in the list", 'wynn_macau' in names)

    items = describe()
    check("describe() returns the same count as available()", len(items) == len(names))
    check("describe() returns 3 fields per entry: (filename, display name, description)",
          all(len(item) == 3 for item in items))

    print("\n[Wynn Macau: rule content should exactly match the given spec]")
    r, notes = load('wynn_macau')
    check("CSM on", r.continuous_shuffle is True)
    check("S17 (stands on soft 17)", r.dealer_hits_soft_17 is False)
    check("double on any two cards", r.double_rule == DOUBLE_ANY2)
    check("no-peek", r.dealer_peek is False)
    check("early surrender", r.surrender == SURRENDER_EARLY)
    check("no surrender vs. dealer ace", r.surrender_vs_ace is False)
    check("OBO (only loses the original bet)", r.dealer_bj_loss == LOSS_ORIGINAL)
    check("BJ pays 3:2", abs(r.blackjack_pays - 1.5) < 1e-9)
    check("aces can't be resplit (split only once)", r.resplit_aces is False)
    check("no hitting after splitting aces (one card each)", r.hit_split_aces is False)
    check("has a rule-conflict note about CSM making penetration irrelevant", any('CSM' in n for n in notes))

    print("\n[Walkerhill Seoul: rule content should exactly match the given spec]")
    r2, notes2 = load('walkerhill_seoul')
    check("has peek", r2.dealer_peek is True)
    check("late surrender", r2.surrender == SURRENDER_LATE)
    check("surrender vs. dealer ace allowed", r2.surrender_vs_ace is True)
    check("6 decks", r2.num_decks == 6)
    check("75% penetration", abs(r2.penetration - 0.75) < 1e-9)
    check("dealer BJ crushes all bets (LOSS_ALL)", r2.dealer_bj_loss == LOSS_ALL)
    check("aces can be resplit (up to 4 hands, a fixed constant)", r2.resplit_aces is True)
    check("no hitting after splitting aces (one card each)", r2.hit_split_aces is False)
    check("double on any two cards", r2.double_rule == DOUBLE_ANY2)
    check("CSM off (this casino doesn't use one)", r2.continuous_shuffle is False)
    check("no rule-conflict notes (the given rules are already consistent)",
          not any('CSM' in n or 'penetration' in n for n in notes2))

    print("\n[Error handling]")
    try:
        load('a-casino-that-does-not-exist')
        check("loading a nonexistent preset should raise an error", False)
    except PresetError as e:
        check("loading a nonexistent preset should raise an error", True, str(e))

    import tempfile
    from pathlib import Path
    import core.presets as pm
    tmp = Path(tempfile.mkdtemp())
    (tmp / 'bad.json').write_text('{"rules": {"not_a_real_field": true}}')
    old_dir = pm.PRESET_DIR
    pm.PRESET_DIR = tmp
    try:
        load('bad')
        check("an unrecognized rule field should raise an error", False)
    except PresetError as e:
        check("an unrecognized rule field should raise an error", True, str(e))
    finally:
        pm.PRESET_DIR = old_dir

    print()
    print("=" * 70)
    if FAIL:
        print(f"\033[31m{len(FAIL)} failed\033[0m: " + ", ".join(FAIL))
        return 1
    print(f"\033[32mAll {len(PASS)} passed\033[0m")
    return 0


if __name__ == '__main__':
    sys.exit(main())
