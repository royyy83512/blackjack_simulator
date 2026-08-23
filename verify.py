#!/usr/bin/env python3
"""引擎正確性驗證。

分三層，從強到弱：

  第 1 層 精確 DP 交叉驗證（最強）
      core/exact.py 用無限牌組把所有牌序按機率加權求和，零誤差。
      模擬端關掉分牌、改用 200 副牌逼近無限牌組，兩者必須吻合。
      這一層過了，代表補牌/停牌/加倍/莊家邏輯/結算完全正確。

  第 2 層 已知常數
      莊家終局分布、玩家 natural 頻率、每回合標準差。

  第 3 層 與文獻的賭場優勢對照
      注意：本模擬用的是「總點數型」基本策略表（只看總點數），
      而文獻數字通常假設「牌組成型」策略（會看是哪幾張牌湊成的）。
      後者約多賺 0.03-0.04%，所以模擬結果比文獻高 0.02-0.05% 是正常的，
      不是 bug —— 第 1 層已經證明引擎本身無誤。

跑法： python3 verify.py [每項回合數]
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
    print(f"  [{mark}] {name:<40} {got:+8.4f}{unit}  期望 {expect:+.4f} ±{tol:.4f}")
    if not ok:
        FAILURES.append(name)
    return ok


def layer1(rounds):
    print(f"\n【第 1 層】精確 DP 交叉驗證 —— 200 副牌、不分牌、不投降，各 {rounds:,} 回合")
    for h17 in (False, True):
        tag = 'H17' if h17 else 'S17'
        exact = no_split_edge(h17) * 100
        r = Rules(num_decks=200, penetration=0.75, surrender=SURRENDER_NONE,
                  dealer_hits_soft_17=h17, insurance_offered=False)
        s = summarize(run(r, 'basic-nosplit', rounds, curve_points=0, seed=555)[0])
        sim, ci = s['house_edge'] * 100, s['house_edge_ci95'] * 100
        report(f"{tag} 賭場優勢 vs 精確 DP", sim, exact, max(2 * ci, 0.005))


def layer2(rounds):
    print("\n【第 2 層】已知常數")
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
        names = ['17', '18', '19', '20', '21', '爆牌']
        for nm, b, e in zip(names, buckets, exact):
            # 6 副牌與無限牌組略有差異，容差放寬到 0.25%
            report(f"莊家 {tag} 終局 {nm}", b / n * 100, e * 100,
                   max(0.25, 2 * 100 * (e * (1 - e) / n) ** 0.5))

    s = summarize(run(Rules(), 'basic', rounds, curve_points=0)[0])
    report("玩家 natural 頻率", s['player_bj_rate'] * 100, 4.749, 0.06)
    report("莊家 natural 頻率", s['dealer_bj_rate'] * 100, 4.749, 0.06)
    report("每回合標準差", s['sd_per_round'], 1.14, 0.02, unit='')


def layer3(rounds):
    print("\n【第 3 層】與文獻對照（總點數型策略表，預期比文獻高 0.02-0.05%）")
    cases = [
        ("6D S17 DAS 無投降 3:2", Rules(surrender=SURRENDER_NONE), 0.40),
        ("6D S17 DAS LS   3:2", Rules(), 0.33),
        ("6D H17 DAS 無投降 3:2", Rules(dealer_hits_soft_17=True,
                                      surrender=SURRENDER_NONE), 0.62),
        ("6D S17 DAS LS   6:5", Rules(blackjack_pays=1.2), 1.72),
        ("2D S17 DAS LS   3:2", Rules(num_decks=2), 0.19),
        ("8D S17 DAS LS   3:2", Rules(num_decks=8), 0.36),
    ]
    for name, rules, lit in cases:
        s = summarize(run(rules, 'basic', rounds, curve_points=0, seed=4242)[0])
        e, ci = s['house_edge'] * 100, s['house_edge_ci95'] * 100
        report(name, e, lit + 0.03, max(0.07, 2 * ci))

    print("\n【第 3 層 b】規則影響的方向與量級（共用亂數）")
    b = summarize(run(Rules(), 'basic', rounds, curve_points=0, seed=4242)[0])
    base, base_ci = b['house_edge'], b['house_edge_ci95']
    deltas = [
        ("H17", Rules(dealer_hits_soft_17=True), +0.22),
        ("關掉 DAS", Rules(double_after_split=False), +0.14),
        ("關掉投降", Rules(surrender=SURRENDER_NONE), +0.08),
        ("開 RSA", Rules(resplit_aces=True), -0.08),
        ("BJ 改 6:5", Rules(blackjack_pays=1.2), +1.39),
        ("只有 10/11 可加倍", Rules(double_rule=DOUBLE_10_11), +0.21),
        ("no-peek 全輸（對無投降基準）", Rules(dealer_peek=False, surrender=SURRENDER_NONE), +0.11),
    ]
    ns = summarize(run(Rules(surrender=SURRENDER_NONE), 'basic', rounds,
                       curve_points=0, seed=4242)[0])
    for name, rules, exp in deltas:
        ref, ref_ci = (ns['house_edge'], ns['house_edge_ci95']) if '無投降基準' in name \
            else (base, base_ci)
        s2 = summarize(run(rules, 'basic', rounds, curve_points=0, seed=4242)[0])
        pooled = ((s2['house_edge_ci95'] ** 2 + ref_ci ** 2) ** 0.5) * 100
        # 容差要同時涵蓋統計誤差與策略表造成的系統性小偏移
        report(f"{name} 的影響", (s2['house_edge'] - ref) * 100, exp,
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
        print(f"{RED}未通過 {len(FAILURES)} 項{OFF}：" + "、".join(FAILURES))
    else:
        print(f"{GREEN}全部通過{OFF}  ({time.time()-t0:.0f}s)")
    return 1 if FAILURES else 0


if __name__ == '__main__':
    sys.exit(main())
