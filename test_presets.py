#!/usr/bin/env python3
"""core/presets.py 的測試：一鍵帶入的賭場規則預設檔。

跑法： python3 test_presets.py
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
    print("【預設檔存在且能列出】")
    names = available()
    check("至少有一個預設檔", len(names) > 0, str(names))
    check("澳門永利在清單裡", 'wynn_macau' in names)

    items = describe()
    check("describe() 回傳的筆數跟 available() 一致", len(items) == len(names))
    check("describe() 每筆是 (檔名, 顯示名稱, 說明) 三個欄位",
          all(len(item) == 3 for item in items))

    print("\n【澳門永利：規則內容要跟使用者給的規則完全對上】")
    r, notes = load('wynn_macau')
    check("CSM 開啟", r.continuous_shuffle is True)
    check("S17（軟17停牌）", r.dealer_hits_soft_17 is False)
    check("任兩張可加倍", r.double_rule == DOUBLE_ANY2)
    check("no-peek", r.dealer_peek is False)
    check("early surrender", r.surrender == SURRENDER_EARLY)
    check("莊家 A 不可投降", r.surrender_vs_ace is False)
    check("OBO（只輸原始注）", r.dealer_bj_loss == LOSS_ORIGINAL)
    check("BJ 賠 3:2", abs(r.blackjack_pays - 1.5) < 1e-9)
    check("A 不能再分（只分一次）", r.resplit_aces is False)
    check("分 A 後不能補牌（各補一張）", r.hit_split_aces is False)
    check("有 CSM 讓 penetration 失效的規則衝突提示", any('CSM' in n for n in notes))

    print("\n【首爾華克山莊：規則內容要跟使用者給的規則完全對上】")
    r2, notes2 = load('walkerhill_seoul')
    check("有 peek", r2.dealer_peek is True)
    check("late surrender", r2.surrender == SURRENDER_LATE)
    check("莊家 A 可投降", r2.surrender_vs_ace is True)
    check("6 副牌", r2.num_decks == 6)
    check("penetration 75%", abs(r2.penetration - 0.75) < 1e-9)
    check("莊家 BJ 通殺（LOSS_ALL）", r2.dealer_bj_loss == LOSS_ALL)
    check("A 可以再分（最多4手，系統常數）", r2.resplit_aces is True)
    check("分 A 後不能補牌（各補一張）", r2.hit_split_aces is False)
    check("任兩張可加倍", r2.double_rule == DOUBLE_ANY2)
    check("CSM 沒開（這家不是CSM）", r2.continuous_shuffle is False)
    check("沒有規則衝突提示（給的規則本身就一致）",
          not any('CSM' in n or 'penetration' in n for n in notes2))

    print("\n【錯誤處理】")
    try:
        load('不存在的賭場')
        check("讀不存在的預設檔要報錯", False)
    except PresetError as e:
        check("讀不存在的預設檔要報錯", True, str(e))

    import tempfile
    from pathlib import Path
    import core.presets as pm
    tmp = Path(tempfile.mkdtemp())
    (tmp / 'bad.json').write_text('{"rules": {"not_a_real_field": true}}')
    old_dir = pm.PRESET_DIR
    pm.PRESET_DIR = tmp
    try:
        load('bad')
        check("不認得的規則欄位要報錯", False)
    except PresetError as e:
        check("不認得的規則欄位要報錯", True, str(e))
    finally:
        pm.PRESET_DIR = old_dir

    print()
    print("=" * 70)
    if FAIL:
        print(f"\033[31m{len(FAIL)} 項未通過\033[0m：" + "、".join(FAIL))
        return 1
    print(f"\033[32m全部 {len(PASS)} 項通過\033[0m")
    return 0


if __name__ == '__main__':
    sys.exit(main())
