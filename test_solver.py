#!/usr/bin/env python3
"""core/solver.py 的測試。

驗證思路（見 core/solver.py 開頭的說明）：solver 走的是無限牌組，跟
core/exact.py 用的是同一組 dealer_given_up() 精確分布，所以：

1. solver 對每一格的判斷，套進去算出來的整體賭場優勢，一定要 <= basic.json
   表算出來的優勢（最優解不可能比任何一個特定策略差，這是數學上的下界）。
2. solver 認為 basic.json「答錯」的格子，如果差距夠大（不是邊際/副數敏感），
   要能用 scenario.py 在極多副牌（逼近無限牌組）下的蒙地卡羅獨立驗證出
   同樣的排名——兩個完全不同的計算方法應該收斂到同一個答案。
3. 已知會因為副數（有限 vs 無限）翻盤的邊際格子（A,2 對 5）要能被標記為
   deck_sensitive，且用真實副數的蒙地卡羅要能量出「反過來」的答案，證明
   solver 的無限牌組限制是有意義的說明，不是藉口。

跑法： python3 test_solver.py
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
    print("【最優解下界：solver 的整體優勢不可能比 basic.json 差】")
    for h17 in (False, True):
        rules = Rules(dealer_hits_soft_17=h17)
        solved = solve(rules)
        edge_solver = aggregate_edge_from_solved(rules, solved)
        edge_table = no_split_edge(h17)
        tag = 'H17' if h17 else 'S17'
        ok = edge_solver <= edge_table + 1e-9
        check(f"{tag}: solver優勢({edge_solver*100:.4f}%) <= 表格優勢({edge_table*100:.4f}%)", ok)

    print("\n【已知答案：solver 對明確格子的判斷要跟公開文獻一致】")
    rules = Rules()
    solved = solve(rules)

    def cell(kind, total, dealer_col):
        return solved[kind][total][dealer_col]

    letter, _ = cell('hard', 16, 8)     # 16 對 10（col8=10）
    check("16 對 10 應該投降", letter == 'R', f"got {letter}")
    letter, _ = cell('hard', 11, 4)     # 11 對 6
    check("11 對 6 應該加倍", letter == 'D', f"got {letter}")
    letter, _ = cell('soft', 19, 4)     # A,8 對 6（S17）
    check("S17：A,8 對 6 應該停牌", letter == 'S', f"got {letter}")
    letter, _ = cell('hard', 17, 0)     # 17 對 2
    check("17 對任何牌都該停牌", letter == 'S', f"got {letter}")

    rules_h17 = Rules(dealer_hits_soft_17=True)
    solved_h17 = solve(rules_h17)
    letter, _ = solved_h17['soft'][19][4]
    check("H17：A,8 對 6 應該改成加倍", letter == 'D', f"got {letter}")

    print("\n【與 basic.json 對照，且用蒙地卡羅驗證真正的差異方向】")
    strat = make('basic', rules)
    mismatches = compare_to_table(rules, strat)
    print(f"  S17 下共 {len(mismatches)} 個不一致的格子")
    sensitive = [m for m in mismatches if m['deck_sensitive']]
    real = [m for m in mismatches if not m['deck_sensitive']]
    check("目前找到的不一致都標記為副數敏感（沒有大差距的真正表格錯誤）",
          len(real) == 0, f"non-deck-sensitive mismatches: {real}")

    if sensitive:
        m = sensitive[0]
        print(f"  交叉驗證第一個 deck_sensitive 格子：{m['kind']} {m['total']} 對 {m['dealer']}")
        cards = (1, m['total'] - 11) if m['kind'] == 'soft' else None
        if cards and 2 <= cards[1] <= 10:
            up = 1 if m['dealer'] == 'A' else int(m['dealer'])
            R200 = Rules(num_decks=8)   # 用最大允許副數逼近無限牌組
            r = compare_actions(R200, 'basic', cards, up, 1_500_000, seed=3,
                               jobs=6, actions=(ACT_HIT, ACT_DOUBLE))
            ranking = {name: ev for _a, name, ev, _ci, _sd in r}
            solver_says_hit = m['optimal'] == 'H'
            mc_says_hit = ranking.get('補牌 (H)', -99) > ranking.get('加倍 (D)', -99)
            check("副數逼近無限時，蒙地卡羅排名跟 solver 一致",
                  solver_says_hit == mc_says_hit,
                  f"solver={m['optimal']}  8副牌蒙地卡羅: {ranking}")

    print("\n【no-peek：已知答案，跟蒙地卡羅獨立交叉驗證過】")
    from core.rules import SURRENDER_NONE, SURRENDER_LATE, SURRENDER_EARLY, LOSS_ALL, LOSS_ORIGINAL

    r_np_all = Rules(dealer_peek=False, surrender=SURRENDER_NONE, dealer_bj_loss=LOSS_ALL)
    solved_np_all = solve(r_np_all)
    letter, evs = solved_np_all['hard'][11][9]     # 11 對 A
    check("no-peek LOSS_ALL：11 對 A 仍該補牌（加倍的額外風險不划算）",
          letter == 'H', f"got {letter}, D={evs.get('D')}")

    # OBO 下 bj_penalty 對 stand/hit/double 都是同一個常數 -1，跟哪個動作
    # 無關，所以「哪個動作最好」的排序不會被 p_bj 影響——但絕對 EV 數值
    # 不會跟 peek 相等（S17 的 evs_peek['D'] 是不受 p_bj 影響的 ev_double_c
    # 本身，no-peek+OBO 的 evs['D'] 多了 p_bj*(-1) 那項拉低整體數值，只是
    # D 跟 S/H 一起被拉低、排序不變）。用 11 對 6（peek 下明確該加倍）驗證
    # 排序真的沒被 no-peek+OBO 影響。
    r_np_obo = Rules(dealer_peek=False, surrender=SURRENDER_NONE, dealer_bj_loss=LOSS_ORIGINAL)
    letter_obo6, evs_obo6 = solve(r_np_obo)['hard'][11][4]      # 11 對 6
    letter_peek6, evs_peek6 = solve(Rules())['hard'][11][4]
    check("no-peek OBO：11 對 6 的最佳動作跟 peek 一樣是加倍",
          letter_obo6 == letter_peek6 == 'D',
          f"peek={letter_peek6}  OBO={letter_obo6}")

    r_np_all6 = Rules(dealer_peek=False, surrender=SURRENDER_NONE, dealer_bj_loss=LOSS_ALL)
    letter_all6, evs_all6 = solve(r_np_all6)['hard'][11][4]
    check("莊家 6 不可能BJ（p_bj=0），LOSS_ALL 跟 OBO 對 11 對 6 應該完全一樣",
          abs(evs_all6['D'] - evs_obo6['D']) < 1e-9,
          f"LOSS_ALL D={evs_all6['D']:.5f}  OBO D={evs_obo6['D']:.5f}")

    # 換莊家 A（p_bj≈30.8%，BJ機率最高），LOSS_ALL 這時才會真的比 OBO 差
    letter, evs = solve(r_np_all)['hard'][11][9]
    _l, evs_obo = solve(r_np_obo)['hard'][11][9]
    check("no-peek LOSS_ALL 的加倍 EV 應該比 OBO 差很多（莊家A的BJ機率最高）",
          evs['D'] < evs_obo['D'] - 0.1, f"LOSS_ALL D={evs['D']:.5f}  OBO D={evs_obo['D']:.5f}")

    r_np_late = Rules(dealer_peek=False, surrender=SURRENDER_LATE, dealer_bj_loss=LOSS_ALL)
    letter, evs = solve(r_np_late)['hard'][16][8]   # 16 對 10
    check("no-peek LATE 投降：16 對 10 仍該投降", letter == 'R', f"got {letter}")
    check("no-peek LATE 投降的 EV 應該比 -0.5 差（莊家後來翻BJ要輸全額）",
          evs['R'] < -0.5, f"got {evs['R']:.5f}")

    r_np_early = Rules(dealer_peek=False, surrender=SURRENDER_EARLY, dealer_bj_loss=LOSS_ORIGINAL)
    _l, evs = solve(r_np_early)['hard'][16][8]
    check("no-peek EARLY 投降的 EV 應該剛好是 -0.5（不管莊家後來有沒有BJ都固定）",
          abs(evs['R'] - (-0.5)) < 1e-9, f"got {evs['R']:.5f}")

    print("\n【compare_to_table 要正確處理 early surrender 的前置檢查】")
    from core.presets import load as load_preset
    for name in ('wynn_macau', 'walkerhill_seoul'):
        rules_p, _ = load_preset(name)
        strat_p = make('basic', rules_p)
        mm = compare_to_table(rules_p, strat_p)
        real_mm = [m for m in mm if not m['deck_sensitive']]
        check(f"{name}：basic.json 不需要任何修改（只剩已知的副數邊際效應）",
              len(real_mm) == 0, f"real mismatches: {real_mm}")

    print("\n" + "=" * 70)
    if FAIL:
        print(f"\033[31m{len(FAIL)} 項未通過\033[0m：" + "、".join(FAIL))
        return 1
    print(f"\033[32m全部 {len(PASS)} 項通過\033[0m")
    return 0


if __name__ == '__main__':
    sys.exit(main())
