#!/usr/bin/env python3
"""Tests for the strategy loader: cell syntax, rule-conditional
differences, counting deviations, error messages.

Usage: python3 test_strategy.py
"""
import json
import sys
import tempfile
from pathlib import Path

from core.rules import Rules, SURRENDER_NONE
from core.engine import (ACT_HIT, ACT_STAND, ACT_DOUBLE, ACT_SPLIT,
                         ACT_SURRENDER, Hand)
from core import strategy as st

PASS, FAIL = [], []
ALL = ACT_HIT | ACT_STAND | ACT_DOUBLE | ACT_SPLIT | ACT_SURRENDER
NAME = {ACT_HIT: 'H', ACT_STAND: 'S', ACT_DOUBLE: 'D', ACT_SPLIT: 'P',
        ACT_SURRENDER: 'R'}


class FakeShoe:
    def __init__(self, count=0.0):
        self.tc = count

    true_count = property(lambda self: self.tc)
    running_count = property(lambda self: self.tc)


def show(v):
    # note True == 1, so bool has to be excluded first or it'll print as an action name
    return NAME[v] if (isinstance(v, int) and not isinstance(v, bool) and v in NAME) else v


def check(name, got, want):
    ok = got == want and type(got) is type(want)
    (PASS if ok else FAIL).append(name)
    mark = '\033[32mPASS\033[0m' if ok else '\033[31mFAIL\033[0m'
    g = show(got)
    w = show(want)
    print(f"  [{mark}] {name:<52} got {g}  expected {w}")


def act(strat, cards, up, rules, legal=ALL, count=0.0):
    return strat.decide(Hand(list(cards), 1.0), up, legal, FakeShoe(count), rules)


print("\n[Cell syntax]")
R = Rules()
b = st.make('basic', R)
check("11 vs. 6 -> double",            act(b, [5, 6], 6, R), ACT_DOUBLE)
check("11 vs. 6 but doubling illegal -> hit",  act(b, [5, 6], 6, R, ALL & ~ACT_DOUBLE), ACT_HIT)
check("A,7 vs. 4 -> double (Ds)",      act(b, [1, 7], 4, R), ACT_DOUBLE)
check("A,7 vs. 4 but doubling illegal -> stand", act(b, [1, 7], 4, R, ALL & ~ACT_DOUBLE), ACT_STAND)
check("16 vs. 10 -> surrender (Rh)",      act(b, [10, 6], 10, R), ACT_SURRENDER)
check("16 vs. 10 but surrender illegal -> hit", act(b, [10, 6], 10, R, ALL & ~ACT_SURRENDER), ACT_HIT)
check("8,8 vs. 10 -> split (not surrender)", act(b, [8, 8], 10, R), ACT_SPLIT)
check("5,5 vs. 6 -> don't split, double as a hard 10", act(b, [5, 5], 6, R), ACT_DOUBLE)
check("10,10 vs. 6 -> don't split, stand as a hard 20", act(b, [10, 10], 6, R), ACT_STAND)
check("A,A vs. 6 but splitting illegal -> hit as a soft 12",
      act(b, [1, 1], 6, R, ALL & ~ACT_SPLIT), ACT_HIT)

print("\n[Ph: split only with DAS]")
das = st.make('basic', Rules(double_after_split=True))
nodas = st.make('basic', Rules(double_after_split=False))
check("2,2 vs. 2 with DAS -> split",       act(das, [2, 2], 2, R), ACT_SPLIT)
check("2,2 vs. 2 without DAS -> hit as a hard 4", act(nodas, [2, 2], 2, Rules(double_after_split=False)), ACT_HIT)
check("4,4 vs. 5 with DAS -> split",       act(das, [4, 4], 5, R), ACT_SPLIT)
check("4,4 vs. 5 without DAS -> hit as a hard 8", act(nodas, [4, 4], 5, Rules(double_after_split=False)), ACT_HIT)
check("6,6 vs. 3 splits under either rule",        act(nodas, [6, 6], 3, Rules(double_after_split=False)), ACT_SPLIT)

print("\n[Overrides: the H17 difference cells]")
H = Rules(dealer_hits_soft_17=True)
h = st.make('basic', H)
check("S17: 11 vs. A -> hit", act(b, [5, 6], 1, R), ACT_HIT)
check("H17: 11 vs. A -> double", act(h, [5, 6], 1, H), ACT_DOUBLE)
check("S17: A,8 vs. 6 -> stand", act(b, [1, 8], 6, R), ACT_STAND)
check("H17: A,8 vs. 6 -> double", act(h, [1, 8], 6, H), ACT_DOUBLE)
check("S17: 17 vs. A -> stand", act(b, [10, 7], 1, R), ACT_STAND)
check("H17: 17 vs. A -> surrender", act(h, [10, 7], 1, H), ACT_SURRENDER)
check("H17: 8,8 vs. A -> surrender (beats splitting)", act(h, [8, 8], 1, H), ACT_SURRENDER)
check("H17: 8,8 vs. A, surrender illegal -> split",
      act(h, [8, 8], 1, H, ALL & ~ACT_SURRENDER), ACT_SPLIT)

print("\n[-fixed: overrides not applied]")
hf = st.make('basic-fixed', H)
check("basic-fixed under H17: 11 vs. A is still hit", act(hf, [5, 6], 1, H), ACT_HIT)
check("basic applied an override", bool(h.applied_overrides), True)
check("basic-fixed applied no override", bool(hf.applied_overrides), False)

print("\n[extends inheritance]")
hl = st.make('hi-lo', R)
check("hi-lo inherits basic's table (16 vs. 10 surrenders)", act(hl, [10, 6], 10, R), ACT_SURRENDER)
check("hi-lo has count tags", hl.count_tags is not None, True)
check("basic has no count tags", b.count_tags is None, True)
check("hi-lo's ace tag = -1", hl.count_tags[1], -1)
check("hi-lo's 5 tag = +1", hl.count_tags[5], 1)

print("\n[Bet ramp and insurance]")
check("true count 0 -> 1x", hl.bet(FakeShoe(0), R, 1.0), 1.0)
check("true count 2 -> 2x", hl.bet(FakeShoe(2), R, 1.0), 2.0)
check("true count 3.5 -> 4x", hl.bet(FakeShoe(3.5), R, 1.0), 4.0)
check("true count 99 -> 12x", hl.bet(FakeShoe(99), R, 1.0), 12.0)
check("true count 2 doesn't buy insurance", hl.take_insurance(FakeShoe(2), R), False)
check("true count 3 buys insurance", hl.take_insurance(FakeShoe(3), R), True)
check("basic never buys insurance", b.take_insurance(FakeShoe(99), R), False)

print("\n[Deviations: the Illustrious 18]")
NS = Rules(surrender=SURRENDER_NONE)
i18 = st.make('hi-lo-i18', NS)
L = ALL & ~ACT_SURRENDER
check("16 vs. 10: true count -1 -> hit", act(i18, [10, 6], 10, NS, L, -1), ACT_HIT)
check("16 vs. 10: true count  0 -> stand", act(i18, [10, 6], 10, NS, L, 0), ACT_STAND)
check("12 vs. 4: true count +1 -> stand",  act(i18, [10, 2], 4, NS, L, 1), ACT_STAND)
check("12 vs. 4: true count -1 -> hit",  act(i18, [10, 2], 4, NS, L, -1), ACT_HIT)
check("10,10 vs. 6: true count 0 -> stand", act(i18, [10, 10], 6, NS, L, 0), ACT_STAND)
check("10,10 vs. 6: true count 4 -> split", act(i18, [10, 10], 6, NS, L, 4), ACT_SPLIT)
check("11 vs. A: true count 0 -> hit",   act(i18, [5, 6], 1, NS, L, 0), ACT_HIT)
check("11 vs. A: true count 2 -> double",   act(i18, [5, 6], 1, NS, L, 2), ACT_DOUBLE)
hl_ns = st.make('hi-lo', NS)
check("hi-lo without deviations: 16 vs. 10 at true count 0 still hits",
      act(hl_ns, [10, 6], 10, NS, L, 0), ACT_HIT)

print("\n[KO: an unbalanced system uses the running count]")
ko6 = st.make('ko', Rules(num_decks=6))
ko2 = st.make('ko', Rules(num_decks=2))
check("KO uses the running count, not the true count", ko6.use_true_count, False)
check("KO's 6-deck starting running count = 4-4*6", ko6.start_count, -20)
check("KO's 2-deck starting running count = 4-4*2", ko2.start_count, -4)
check("KO also counts 7 as +1 (unlike Hi-Lo)", ko6.count_tags[7], 1)
check("Hi-Lo counts 7 as 0", hl.count_tags[7], 0)

print("\n[effective_cell_label must see the early-surrender pre-check]")
from core.presets import load as load_preset

r_wynn, _ = load_preset('wynn_macau')
b_wynn = st.make('basic', r_wynn)
cell_h14_10 = b_wynn.hard[14][8]   # hard 14 vs. 10

check("without strategy: hard 14 vs. 10 only looks at the table itself, shows hit",
      st.effective_cell_label(cell_h14_10, 10, r_wynn, 14, False), 'H')
check("with strategy: hard 14 vs. 10 should show surrender (covered by early surrender's es_vs_ten)",
      st.effective_cell_label(cell_h14_10, 10, r_wynn, 14, False, b_wynn), 'R')

cell_h14_ace = b_wynn.hard[14][9]  # hard 14 vs. A
label_h14_ace = st.effective_cell_label(cell_h14_ace, 1, r_wynn, 14, False, b_wynn)
check("Wynn Macau disallows surrender vs. ace: hard 14 vs. A shouldn't show surrender even though it's in es_vs_ace",
      label_h14_ace != 'R', True)

print("\n[Error messages]")


def expect_error(name, spec, fragment):
    with tempfile.TemporaryDirectory() as d:
        Path(d, 'bad.json').write_text(json.dumps(spec), encoding='utf-8')
        old = st.STRATEGY_DIR
        st.STRATEGY_DIR = Path(d)
        try:
            st.make('bad', Rules())
            got = '(no error raised)'
        except (st.StrategyError, SystemExit) as e:
            got = str(e)
        finally:
            st.STRATEGY_DIR = old
    ok = fragment in got
    (PASS if ok else FAIL).append(name)
    mark = '\033[32mPASS\033[0m' if ok else '\033[31mFAIL\033[0m'
    print(f"  [{mark}] {name:<52} {got.splitlines()[0][:46]}")


ROW = "H  H  H  H  H  H  H  H  H  H"
expect_error("wrong cell count raises an error",
             {"name": "bad", "tables": {"hard": {"5": "H H H"}}}, "expected 10 cells")
expect_error("unrecognized cell raises an error",
             {"name": "bad", "tables": {"hard": {"5": "H H H H H H H H H X"}}},
             "unrecognized cell")
expect_error("missing hard table raises an error",
             {"name": "bad", "tables": {"soft": {"12": ROW}}}, "has no hard table")
expect_error("override referencing a nonexistent field raises an error",
             {"name": "bad", "tables": {"hard": {"5": ROW}},
              "overrides": [{"when": {"no_such_rule": True}, "cells": []}]},
             "is not a rules field")
expect_error("override referencing a nonexistent row raises an error",
             {"name": "bad", "tables": {"hard": {"5": ROW}},
              "overrides": [{"when": {}, "cells": [
                  {"table": "hard", "row": "99", "dealer": "A", "action": "S"}]}]},
             "nonexistent row")

print()
print("=" * 78)
if FAIL:
    print(f"\033[31m{len(FAIL)} failed\033[0m / {len(PASS) + len(FAIL)} total: " + ", ".join(FAIL))
    sys.exit(1)
print(f"\033[32mAll {len(PASS)} passed\033[0m")
