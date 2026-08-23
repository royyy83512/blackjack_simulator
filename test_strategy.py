#!/usr/bin/env python3
"""策略載入器的測試：格子語法、規則差異、算牌偏離、錯誤訊息。

跑法： python3 test_strategy.py
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
    # 注意 True == 1，不先擋掉 bool 的話會被印成動作名稱
    return NAME[v] if (isinstance(v, int) and not isinstance(v, bool) and v in NAME) else v


def check(name, got, want):
    ok = got == want and type(got) is type(want)
    (PASS if ok else FAIL).append(name)
    mark = '\033[32mPASS\033[0m' if ok else '\033[31mFAIL\033[0m'
    g = show(got)
    w = show(want)
    print(f"  [{mark}] {name:<52} 得 {g}  期望 {w}")


def act(strat, cards, up, rules, legal=ALL, count=0.0):
    return strat.decide(Hand(list(cards), 1.0), up, legal, FakeShoe(count), rules)


print("\n【格子語法】")
R = Rules()
b = st.make('basic', R)
check("11 對 6 -> 加倍",            act(b, [5, 6], 6, R), ACT_DOUBLE)
check("11 對 6 但不能加倍 -> 補牌",  act(b, [5, 6], 6, R, ALL & ~ACT_DOUBLE), ACT_HIT)
check("A,7 對 4 -> 加倍 (Ds)",      act(b, [1, 7], 4, R), ACT_DOUBLE)
check("A,7 對 4 但不能加倍 -> 停牌", act(b, [1, 7], 4, R, ALL & ~ACT_DOUBLE), ACT_STAND)
check("16 對 10 -> 投降 (Rh)",      act(b, [10, 6], 10, R), ACT_SURRENDER)
check("16 對 10 但不能投降 -> 補牌", act(b, [10, 6], 10, R, ALL & ~ACT_SURRENDER), ACT_HIT)
check("8,8 對 10 -> 分牌（不是投降）", act(b, [8, 8], 10, R), ACT_SPLIT)
check("5,5 對 6 -> 不分牌，當硬 10 加倍", act(b, [5, 5], 6, R), ACT_DOUBLE)
check("10,10 對 6 -> 不分牌，當硬 20 停牌", act(b, [10, 10], 6, R), ACT_STAND)
check("A,A 對 6 但不能分 -> 當軟 12 補牌",
      act(b, [1, 1], 6, R, ALL & ~ACT_SPLIT), ACT_HIT)

print("\n【Ph：有 DAS 才分牌】")
das = st.make('basic', Rules(double_after_split=True))
nodas = st.make('basic', Rules(double_after_split=False))
check("2,2 對 2 有 DAS -> 分牌",       act(das, [2, 2], 2, R), ACT_SPLIT)
check("2,2 對 2 無 DAS -> 當硬 4 補牌", act(nodas, [2, 2], 2, Rules(double_after_split=False)), ACT_HIT)
check("4,4 對 5 有 DAS -> 分牌",       act(das, [4, 4], 5, R), ACT_SPLIT)
check("4,4 對 5 無 DAS -> 當硬 8 補牌", act(nodas, [4, 4], 5, Rules(double_after_split=False)), ACT_HIT)
check("6,6 對 3 兩種規則都分牌",        act(nodas, [6, 6], 3, Rules(double_after_split=False)), ACT_SPLIT)

print("\n【overrides：H17 的差異格】")
H = Rules(dealer_hits_soft_17=True)
h = st.make('basic', H)
check("S17：11 對 A -> 補牌", act(b, [5, 6], 1, R), ACT_HIT)
check("H17：11 對 A -> 加倍", act(h, [5, 6], 1, H), ACT_DOUBLE)
check("S17：A,8 對 6 -> 停牌", act(b, [1, 8], 6, R), ACT_STAND)
check("H17：A,8 對 6 -> 加倍", act(h, [1, 8], 6, H), ACT_DOUBLE)
check("S17：17 對 A -> 停牌", act(b, [10, 7], 1, R), ACT_STAND)
check("H17：17 對 A -> 投降", act(h, [10, 7], 1, H), ACT_SURRENDER)
check("H17：8,8 對 A -> 投降（優於分牌）", act(h, [8, 8], 1, H), ACT_SURRENDER)
check("H17：8,8 對 A 不能投降 -> 分牌",
      act(h, [8, 8], 1, H, ALL & ~ACT_SURRENDER), ACT_SPLIT)

print("\n【-fixed：不套用 overrides】")
hf = st.make('basic-fixed', H)
check("basic-fixed 在 H17 底下 11 對 A 仍是補牌", act(hf, [5, 6], 1, H), ACT_HIT)
check("basic 套用了 override", bool(h.applied_overrides), True)
check("basic-fixed 沒套用 override", bool(hf.applied_overrides), False)

print("\n【extends 繼承】")
hl = st.make('hi-lo', R)
check("hi-lo 繼承到 basic 的表（16 對 10 投降）", act(hl, [10, 6], 10, R), ACT_SURRENDER)
check("hi-lo 有計數標籤", hl.count_tags is not None, True)
check("basic 沒有計數標籤", b.count_tags is None, True)
check("hi-lo 的 A 標籤 = -1", hl.count_tags[1], -1)
check("hi-lo 的 5 標籤 = +1", hl.count_tags[5], 1)

print("\n【下注階梯與保險】")
check("真數 0 -> 1 倍", hl.bet(FakeShoe(0), R, 1.0), 1.0)
check("真數 2 -> 2 倍", hl.bet(FakeShoe(2), R, 1.0), 2.0)
check("真數 3.5 -> 4 倍", hl.bet(FakeShoe(3.5), R, 1.0), 4.0)
check("真數 99 -> 12 倍", hl.bet(FakeShoe(99), R, 1.0), 12.0)
check("真數 2 不買保險", hl.take_insurance(FakeShoe(2), R), False)
check("真數 3 買保險", hl.take_insurance(FakeShoe(3), R), True)
check("basic 從不買保險", b.take_insurance(FakeShoe(99), R), False)

print("\n【deviations：Illustrious 18】")
NS = Rules(surrender=SURRENDER_NONE)
i18 = st.make('hi-lo-i18', NS)
L = ALL & ~ACT_SURRENDER
check("16 對 10：真數 -1 -> 補牌", act(i18, [10, 6], 10, NS, L, -1), ACT_HIT)
check("16 對 10：真數  0 -> 停牌", act(i18, [10, 6], 10, NS, L, 0), ACT_STAND)
check("12 對 4：真數 +1 -> 停牌",  act(i18, [10, 2], 4, NS, L, 1), ACT_STAND)
check("12 對 4：真數 -1 -> 補牌",  act(i18, [10, 2], 4, NS, L, -1), ACT_HIT)
check("10,10 對 6：真數 0 -> 停牌", act(i18, [10, 10], 6, NS, L, 0), ACT_STAND)
check("10,10 對 6：真數 4 -> 分牌", act(i18, [10, 10], 6, NS, L, 4), ACT_SPLIT)
check("11 對 A：真數 0 -> 補牌",   act(i18, [5, 6], 1, NS, L, 0), ACT_HIT)
check("11 對 A：真數 2 -> 加倍",   act(i18, [5, 6], 1, NS, L, 2), ACT_DOUBLE)
hl_ns = st.make('hi-lo', NS)
check("沒有偏離的 hi-lo：16 對 10 真數 0 仍補牌",
      act(hl_ns, [10, 6], 10, NS, L, 0), ACT_HIT)

print("\n【KO：不平衡系統用流水數】")
ko6 = st.make('ko', Rules(num_decks=6))
ko2 = st.make('ko', Rules(num_decks=2))
check("KO 用流水數不用真數", ko6.use_true_count, False)
check("KO 6 副牌起始流水數 = 4-4*6", ko6.start_count, -20)
check("KO 2 副牌起始流水數 = 4-4*2", ko2.start_count, -4)
check("KO 的 7 也算 +1（跟 Hi-Lo 不同）", ko6.count_tags[7], 1)
check("Hi-Lo 的 7 算 0", hl.count_tags[7], 0)

print("\n【effective_cell_label 要看得到 early surrender 前置檢查】")
from core.presets import load as load_preset

r_wynn, _ = load_preset('wynn_macau')
b_wynn = st.make('basic', r_wynn)
cell_h14_10 = b_wynn.hard[14][8]   # 硬 14 對 10

check("不傳 strategy：hard 14 對 10 只看表格本身，顯示補牌",
      st.effective_cell_label(cell_h14_10, 10, r_wynn, 14, False), 'H')
check("傳 strategy：hard 14 對 10 該顯示投降（early surrender 的 es_vs_ten 涵蓋）",
      st.effective_cell_label(cell_h14_10, 10, r_wynn, 14, False, b_wynn), 'R')

cell_h14_ace = b_wynn.hard[14][9]  # 硬 14 對 A
label_h14_ace = st.effective_cell_label(cell_h14_ace, 1, r_wynn, 14, False, b_wynn)
check("澳門永利莊家 A 不可投降：hard 14 對 A 即使在 es_vs_ace 名單裡也不該顯示投降",
      label_h14_ace != 'R', True)

print("\n【錯誤訊息】")


def expect_error(name, spec, fragment):
    with tempfile.TemporaryDirectory() as d:
        Path(d, 'bad.json').write_text(json.dumps(spec), encoding='utf-8')
        old = st.STRATEGY_DIR
        st.STRATEGY_DIR = Path(d)
        try:
            st.make('bad', Rules())
            got = '(沒有報錯)'
        except (st.StrategyError, SystemExit) as e:
            got = str(e)
        finally:
            st.STRATEGY_DIR = old
    ok = fragment in got
    (PASS if ok else FAIL).append(name)
    mark = '\033[32mPASS\033[0m' if ok else '\033[31mFAIL\033[0m'
    print(f"  [{mark}] {name:<52} {got.splitlines()[0][:46]}")


ROW = "H  H  H  H  H  H  H  H  H  H"
expect_error("格子數不對會報錯",
             {"name": "bad", "tables": {"hard": {"5": "H H H"}}}, "需要 10 格")
expect_error("看不懂的格子會報錯",
             {"name": "bad", "tables": {"hard": {"5": "H H H H H H H H H X"}}},
             "看不懂的格子")
expect_error("缺少 hard 表會報錯",
             {"name": "bad", "tables": {"soft": {"12": ROW}}}, "沒有 hard 表")
expect_error("override 指到不存在的欄位會報錯",
             {"name": "bad", "tables": {"hard": {"5": ROW}},
              "overrides": [{"when": {"no_such_rule": True}, "cells": []}]},
             "不是規則欄位")
expect_error("override 指到不存在的列會報錯",
             {"name": "bad", "tables": {"hard": {"5": ROW}},
              "overrides": [{"when": {}, "cells": [
                  {"table": "hard", "row": "99", "dealer": "A", "action": "S"}]}]},
             "不存在的列")

print()
print("=" * 78)
if FAIL:
    print(f"\033[31m{len(FAIL)} 項未通過\033[0m / 共 {len(PASS) + len(FAIL)} 項：" + "、".join(FAIL))
    sys.exit(1)
print(f"\033[32m全部 {len(PASS)} 項通過\033[0m")
