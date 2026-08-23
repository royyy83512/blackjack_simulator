#!/usr/bin/env python3
"""core/scenario.py 的測試：拿已知的標準基本策略答案對照。

這些答案是公開文獻反覆驗證過的（6D S17 DAS LS 的標準表），模擬結果的
「最佳動作排名」對不上就是有 bug。EV 本身有抽樣誤差，這裡只驗證排名，
不驗證到小數點後幾位。

跑法： python3 test_scenario.py [每個動作的手數，預設 800000]
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
    print(f"  [{mark}] {name:<20} 最佳={results[0][1]:<8} 期望={results[0][1] if ok else '?'}  ({ranking})")
    return ok


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 800_000
    print(f"每個動作 {rounds:,} 手\n")

    print("【已知答案：6D S17 DAS LS，標準基本策略】")
    check_best("8,8 對 10 應該分牌", (8, 8), 10, ACT_SPLIT, rounds)
    check_best("16(10,6) 對 10 應該投降", (10, 6), 10, ACT_SURRENDER, rounds)
    check_best("A,8 對 6 應該停牌", (1, 8), 6, ACT_STAND, rounds)
    check_best("A,7 對 9 應該補牌", (1, 7), 9, ACT_HIT, rounds)
    check_best("11 對 6 應該加倍", (5, 6), 6, ACT_DOUBLE, rounds)
    check_best("A,A 對任何牌應該分牌", (1, 1), 7, ACT_SPLIT, rounds)

    print("\n【H17 會改變答案（跟 S17 對照）】")
    H = Rules(dealer_hits_soft_17=True)
    check_best("H17：A,8 對 6 應該改成加倍", (1, 8), 6, ACT_DOUBLE, rounds, rules=H)
    check_best("S17：A,8 對 6 仍是停牌", (1, 8), 6, ACT_STAND, rounds)

    print("\n【合法動作檢查】")
    legal = legal_first_actions((8, 8), 10, Rules())
    ok = bool(legal & ACT_SPLIT) and bool(legal & ACT_SURRENDER)
    (PASS if ok else FAIL).append("8,8 對 10 分牌與投降都合法")
    print(f"  [{'PASS' if ok else 'FAIL'}] 8,8 對 10 分牌與投降都合法")

    legal2 = legal_first_actions((10, 6), 10, Rules())
    ok2 = not (legal2 & ACT_SPLIT)
    (PASS if ok2 else FAIL).append("10,6 不是對子不該有分牌選項")
    print(f"  [{'PASS' if ok2 else 'FAIL'}] 10,6 不是對子不該有分牌選項")

    try:
        compare_actions(Rules(), 'basic', (10, 6), 10, 1000, actions=(ACT_SPLIT,))
        FAIL.append("要求不合法的動作應該報錯")
        print("  [FAIL] 要求不合法的動作應該報錯")
    except ScenarioError:
        PASS.append("要求不合法的動作應該報錯")
        print("  [PASS] 要求不合法的動作應該報錯（正確丟出 ScenarioError）")

    print("\n" + "=" * 70)
    if FAIL:
        print(f"\033[31m{len(FAIL)} 項未通過\033[0m：" + "、".join(FAIL))
        return 1
    print(f"\033[32m全部 {len(PASS)} 項通過\033[0m")
    return 0


if __name__ == '__main__':
    sys.exit(main())
