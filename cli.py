#!/usr/bin/env python3
"""Blackjack 模擬器 —— 命令列介面。

範例
    # 單一設定跑一百萬手
    python3 cli.py --hands 1000000

    # 一億手，八核，八條資金曲線
    python3 cli.py --hands 100000000 --sessions 8

    # 策略對比（共用亂數）
    python3 cli.py --hands 5000000 --strategy basic mimic never-bust no-sp-dbl

    # 規則掃描：比較 2/4/6/8 副牌
    python3 cli.py --hands 20000000 --sweep decks

    # 自訂規則
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
    'decks':     [(f"{n} 副牌", lambda r, n=n: replace(r, num_decks=n)) for n in (1, 2, 4, 6, 8)],
    'csm':       [("有切牌點（傳統牌靴）", lambda r: replace(r, continuous_shuffle=False)),
                  ("連續洗牌機 (CSM)", lambda r: replace(r, continuous_shuffle=True))],
    'h17':       [("S17", lambda r: replace(r, dealer_hits_soft_17=False)),
                  ("H17", lambda r: replace(r, dealer_hits_soft_17=True))],
    'das':       [("DAS 可加倍", lambda r: replace(r, double_after_split=True)),
                  ("無 DAS", lambda r: replace(r, double_after_split=False))],
    'surrender-ace': [("莊家 A 可投降", lambda r: replace(r, surrender_vs_ace=True)),
                      ("莊家 A 不可投降", lambda r: replace(r, surrender_vs_ace=False))],
    'surrender': [("不可投降", lambda r: replace(r, surrender=SURRENDER_NONE)),
                  ("late surrender", lambda r: replace(r, surrender=SURRENDER_LATE)),
                  ("early surrender (no-peek)",
                   lambda r: replace(r, surrender=SURRENDER_EARLY, dealer_peek=False))],
    'peek':      [("莊家 peek", lambda r: replace(r, dealer_peek=True)),
                  ("no-peek 全輸", lambda r: replace(r, dealer_peek=False, dealer_bj_loss=LOSS_ALL)),
                  ("no-peek OBO", lambda r: replace(r, dealer_peek=False, dealer_bj_loss=LOSS_ORIGINAL))],
    'rsa':       [("不可再分 A", lambda r: replace(r, resplit_aces=False)),
                  ("可再分 A", lambda r: replace(r, resplit_aces=True)),
                  ("可再分 A 且可補牌", lambda r: replace(r, resplit_aces=True, hit_split_aces=True))],
    'double':    [("任兩張可加倍", lambda r: replace(r, double_rule=DOUBLE_ANY2)),
                  ("只有 9/10/11", lambda r: replace(r, double_rule=DOUBLE_9_11)),
                  ("只有 10/11", lambda r: replace(r, double_rule=DOUBLE_10_11))],
    'bj':        [("BJ 賠 3:2", lambda r: replace(r, blackjack_pays=1.5)),
                  ("BJ 賠 6:5", lambda r: replace(r, blackjack_pays=1.2))],
}


def make_configs(a, rules):
    """回傳 [(標籤, Rules, 策略名), ...]。"""
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
            f"{done:,}/{total:,}  已 {el:.0f}s  剩 {eta:.0f}s   ")
        sys.stderr.flush()
        if done >= total:
            sys.stderr.write("\n")
    return cb


def main():
    p = argparse.ArgumentParser(
        description='Blackjack 蒙地卡羅模擬器',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)

    g = p.add_argument_group('模擬')
    g.add_argument('--hands', type=int, default=1_000_000, help='總回合數（預設 1e6）')
    g.add_argument('--sessions', type=int, default=8, help='獨立實驗次數＝資金曲線條數（預設 8）')
    g.add_argument('--bet', type=float, default=1.0, help='基本注碼')
    g.add_argument('--bankroll', type=float, default=100.0, help='本金（單位），用來算破產機率')
    g.add_argument('--seed', type=int, default=20240514, help='亂數種子；相同 seed 讓各設定共用同一副牌')
    g.add_argument('--jobs', type=int, default=os.cpu_count(), help='平行核心數')
    g.add_argument('--strategy', nargs='+', default=['basic'],
                   help='一個以上策略（見 --list-strategies）；多個就是策略對比。'
                        '名稱加上 -fixed 代表不套用規則差異修正')
    g.add_argument('--list-strategies', action='store_true',
                   help='列出 strategies/ 底下所有策略後結束')
    g.add_argument('--sweep', choices=sorted(SWEEPS), help='掃描某個規則維度並比較')
    g.add_argument('--preset', help='一鍵帶入某家賭場的規則（見 --list-presets），'
                   '給了這個就忽略下面所有 --decks/--h17 等個別規則參數')
    g.add_argument('--list-presets', action='store_true',
                   help='列出 presets/ 底下所有賭場規則預設後結束')

    r = p.add_argument_group('賭場規則')
    r.add_argument('--decks', type=int, default=6, help='牌副數 2-8（預設 6）')
    r.add_argument('--pen', type=float, default=0.75, help='penetration，發到幾成放切牌')
    r.add_argument('--csm', action='store_true',
                   help='連續洗牌機：每一手都重洗，沒有切牌點，penetration 被忽略，算牌失效')
    r.add_argument('--h17', action='store_true', help='莊家軟 17 要補牌（預設 S17）')
    r.add_argument('--double', default=DOUBLE_ANY2,
                   choices=[DOUBLE_ANY2, DOUBLE_9_11, DOUBLE_10_11], help='可加倍的時機')
    r.add_argument('--no-das', action='store_true', help='分牌後不可加倍')
    r.add_argument('--surrender', default=SURRENDER_LATE,
                   choices=[SURRENDER_NONE, SURRENDER_LATE, SURRENDER_EARLY])
    r.add_argument('--no-surrender-vs-ace', action='store_true',
                   help='莊家明牌是 A 時不給投降（有些賭場如此）')
    r.add_argument('--no-peek', action='store_true', help='莊家不發底牌（歐式 ENHC）')
    r.add_argument('--obo', action='store_true', help='莊家 BJ 時玩家只輸原始注（需 --no-peek）')
    r.add_argument('--rsa', action='store_true', help='可以再分 A')
    r.add_argument('--hsa', action='store_true', help='分 A 之後還能補牌')
    r.add_argument('--bj-pays', type=float, default=1.5, help='BJ 賠率：1.5=3:2，1.2=6:5')

    o = p.add_argument_group('輸出')
    o.add_argument('--out', default='results', help='圖表與 JSON 的輸出目錄')
    o.add_argument('--no-charts', action='store_true')
    o.add_argument('--json', action='store_true', help='另外輸出 summary.json')
    o.add_argument('--quiet', action='store_true', help='不顯示進度條')

    a = p.parse_args()

    if a.list_strategies:
        print(f"\n策略檔目錄：{__import__('core.strategy', fromlist=['x']).STRATEGY_DIR}\n")
        for name, desc, counting in describe():
            print(f"  {name:<20} {'[算牌]' if counting else '      '} {desc}")
        print("\n  任何名稱都可加 -fixed 後綴，代表不套用規則差異修正"
              "（例如 basic-fixed 永遠用 S17/DAS 的表）\n")
        return

    if a.list_presets:
        print(f"\n預設檔目錄：{presets_mod.PRESET_DIR}\n")
        for name, disp, desc in presets_mod.describe():
            print(f"  {name:<16} {disp}")
            print(f"    {desc}")
        print()
        return

    for name in a.strategy:
        if name.removesuffix('-fixed') not in available():
            raise SystemExit(f"未知策略 '{name}'。可用：{', '.join(available())}"
                             f"（可加 -fixed 後綴）")

    if a.preset:
        try:
            rules, notes = presets_mod.load(a.preset)
        except presets_mod.PresetError as e:
            raise SystemExit(f"{e}\n可用預設：{', '.join(presets_mod.available())}"
                             "（用 --list-presets 看詳細內容）")
        print(f"  套用預設：{a.preset}（忽略了其他 --decks/--h17 等個別規則參數）")
    else:
        rules, notes = build_rules(a)
    for n in notes:
        print(f"  規則調整：{n}")

    configs = make_configs(a, rules)
    print(f"\n規則：{rules.label()}   penetration {rules.penetration:.0%}")
    print(f"模擬：{a.hands:,} 回合 × {len(configs)} 個設定 = "
          f"{a.hands*len(configs):,} 回合，{a.jobs} 核，{a.sessions} 個 session")
    prec = 1.96 * 1.14 / (a.hands ** 0.5) * 100
    print(f"預期精度：每個設定約 ±{prec:.4f}%（95% CI）")
    if prec > 0.05:
        need = hands_needed(0.0005)
        print(f"  提醒：要分辨 0.05% 等級的規則差異，單一設定約需 {need:,.0f} 回合")
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
        print(f"\n── {label} " + "─" * max(2, 62 - len(label)))
        print(format_summary(s))

    if len(summaries) > 1:
        print("\n" + "=" * 78)
        print("對比（同一組牌靴 / 共用亂數）")
        print(f"  {'設定':<30}{'賭場優勢':>14}{'95% CI':>11}{'每回合 EV':>13}{'SD':>8}")
        base = summaries[0]['house_edge']
        for s in summaries:
            print(f"  {s['label']:<30}{s['house_edge']*100:>13.4f}%"
                  f"{s['house_edge_ci95']*100:>10.4f}%"
                  f"{s['ev_per_round']:>13.5f}{s['sd_per_round']:>8.3f}")
        print(f"\n  相對於「{summaries[0]['label']}」的差異：")
        for s in summaries[1:]:
            d = (s['house_edge'] - base) * 100
            pooled = (s['house_edge_ci95']**2 + summaries[0]['house_edge_ci95']**2) ** 0.5 * 100
            sig = "顯著" if abs(d) > pooled else "分辨不出（需更多手數）"
            print(f"    {s['label']:<30}{d:+8.4f}%  ±{pooled:.4f}  {sig}")

    print(f"\n總牆鐘時間 {wall:.1f}s，整體 {a.hands*len(configs)/wall:,.0f} 回合/秒")

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
        print("圖表：" + "  ".join(paths))

    if a.json:
        os.makedirs(a.out, exist_ok=True)
        path = os.path.join(a.out, 'summary.json')
        with open(path, 'w') as f:
            json.dump({'rules': rules.to_dict(), 'hands': a.hands,
                       'sessions': a.sessions, 'seed': a.seed,
                       'summaries': summaries}, f, ensure_ascii=False, indent=2)
        print("JSON：" + path)


if __name__ == '__main__':
    main()
