"""模擬執行器：緊迴圈 + 多核平行 + 共用亂數。

平行化的單位是「chunk」。同一條路徑被切成多個 chunk 分給不同核心，
各 chunk 之間獨立同分布，接回去仍是合法的隨機漫步（stats.stitch 會
把跨 chunk 的最大回撤補算正確）。

共用亂數（Common Random Numbers）
    比較不同規則／策略時，用同一組 seed 產生同一副洗好的牌靴。
    多數手牌兩邊決策相同、拿到的牌也相同，差值為 0，
    因此「A 減 B」的變異數遠小於各自的變異數，同樣手數能分辨更小的差異。
    但書：一旦某手決策不同，之後的牌序就會錯開，配對不是完美的。
"""
import os
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from . import strategy as strategy_mod
from .engine import (play_round, F_PLAYER_BJ, F_DEALER_BJ, F_DOUBLED, F_SPLIT,
                     F_SURRENDER, F_PLAYER_BUST, F_INSURANCE)
from .shoe import Shoe, HI_LO
from .stats import SessionResult, stitch, combine


def run_chunk(args):
    """跑一段回合。這是整個程式唯一的熱路徑，刻意全部用區域變數。"""
    (rules, strat_name, rounds, base_bet, seed, curve_points, label) = args

    # 先建策略：牌靴的計數標籤由策略檔決定（Hi-Lo 與 KO 的標籤不同）
    strat = strategy_mod.make(strat_name, rules)
    shoe = Shoe(rules.num_decks, rules.penetration, seed,
                tags=strat.count_tags or HI_LO,
                start_count=strat.start_count, csm=rules.continuous_shuffle)
    play = play_round

    net = 0.0
    sumsq = 0.0
    hands = 0
    initial_w = 0.0
    total_w = 0.0
    wins = losses = pushes = 0
    p_bj = d_bj = p_bust = 0
    doubles = splits = surrenders = insurances = 0
    dealer_played = 0
    dt17 = dt18 = dt19 = dt20 = dt21 = dtbust = 0

    peak = 0.0
    low = 0.0
    max_dd = 0.0
    stride = max(1, rounds // curve_points) if curve_points else 0
    curve = []

    t0 = time.perf_counter()
    for i in range(rounds):
        r = play(shoe, strat, rules, base_bet)
        v = r[0]
        net += v
        sumsq += v * v
        initial_w += r[1]
        total_w += r[2]
        hands += r[3]
        dt = r[4]
        f = r[5]

        if v > 0:
            wins += 1
        elif v < 0:
            losses += 1
        else:
            pushes += 1

        if f:
            if f & F_PLAYER_BJ:
                p_bj += 1
            if f & F_DEALER_BJ:
                d_bj += 1
            if f & F_DOUBLED:
                doubles += 1
            if f & F_SPLIT:
                splits += 1
            if f & F_SURRENDER:
                surrenders += 1
            if f & F_PLAYER_BUST:
                p_bust += 1
            if f & F_INSURANCE:
                insurances += 1

        if dt and not (f & F_DEALER_BJ):
            dealer_played += 1
            if dt > 21:
                dtbust += 1
            elif dt == 17:
                dt17 += 1
            elif dt == 18:
                dt18 += 1
            elif dt == 19:
                dt19 += 1
            elif dt == 20:
                dt20 += 1
            else:
                dt21 += 1

        if net > peak:
            peak = net
        elif peak - net > max_dd:
            max_dd = peak - net
        if net < low:
            low = net

        if stride and i % stride == 0:
            curve.append(net)

    elapsed = time.perf_counter() - t0
    return SessionResult(
        label=label, rounds=rounds, hands=hands, net=net, sumsq=sumsq,
        initial_wagered=initial_w, total_wagered=total_w,
        wins=wins, losses=losses, pushes=pushes,
        player_bj=p_bj, dealer_bj=d_bj, player_bust=p_bust, dealer_bust=dtbust,
        doubles=doubles, splits=splits, surrenders=surrenders,
        insurances=insurances, dealer_played=dealer_played,
        dealer_totals=[dt17, dt18, dt19, dt20, dt21, dtbust],
        max_drawdown=max_dd, peak=peak, low=low,
        curve=curve, curve_stride=stride or 1,
        shuffles=shoe.shuffles, seconds=elapsed,
    )


# 單一 chunk 的回合上限。取消只在 chunk 邊界生效，所以 chunk 不能太大，
# 否則按下取消要等很久；一個 chunk 大約 0.5 秒是舒服的折衷。
MAX_CHUNK = 250_000


def plan_chunks(total_rounds, n_chunks, min_chunk=20_000, max_chunk=MAX_CHUNK):
    """把回合數平均切段。至少 n_chunks 段，且每段不超過 max_chunk。"""
    n = max(1, min(n_chunks, max(1, total_rounds // min_chunk)))
    n = max(n, -(-total_rounds // max_chunk))          # 無條件進位
    base, extra = divmod(total_rounds, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def _seed_for(base_seed, session, chunk):
    return base_seed + session * 100003 + chunk * 10007


def run(rules, strat_name, total_rounds, sessions=1, base_bet=1.0,
        seed=20240514, jobs=None, curve_points=2000, label=None,
        progress=None, executor=None):
    """跑一組（規則, 策略）。

    total_rounds : 總回合數（所有 session 加起來）
    sessions     : 獨立實驗次數 —— 決定畫出幾條資金曲線、結果分布有幾個樣本
    seed         : 同 seed + 同切法 => 不同策略拿到同一副牌（共用亂數）

    回傳 (合併結果, [每個 session 的結果])
    """
    jobs = jobs or os.cpu_count() or 1
    label = label or f"{strat_name} | {rules.label()}"
    sessions = max(1, sessions)
    per_session = max(1, total_rounds // sessions)

    # 總 chunk 數抓 jobs 的數倍，兼顧負載平衡與進度回報密度
    sizes = plan_chunks(per_session, max(1, round(jobs * 6 / sessions)))
    per_chunk_curve = max(1, curve_points // len(sizes)) if curve_points else 0

    tasks, owner = [], []
    for s in range(sessions):
        for k, sz in enumerate(sizes):
            tasks.append((rules, strat_name, sz, base_bet,
                          _seed_for(seed, s, k), per_chunk_curve, label))
            owner.append(s)

    total_planned = sum(t[2] for t in tasks)
    results = [None] * len(tasks)
    done = 0

    if jobs == 1 or len(tasks) == 1:
        for i, t in enumerate(tasks):
            results[i] = run_chunk(t)
            done += t[2]
            if progress:
                progress(done, total_planned)
    else:
        own = executor is None
        ex = executor or ProcessPoolExecutor(max_workers=jobs)
        try:
            futures = {ex.submit(run_chunk, t): i for i, t in enumerate(tasks)}
            for fut in as_completed(futures):
                i = futures[fut]
                results[i] = fut.result()
                done += tasks[i][2]
                if progress:
                    progress(done, total_planned)
        finally:
            if own:
                ex.shutdown(cancel_futures=True)

    per_session_results = []
    for s in range(sessions):
        parts = [r for r, o in zip(results, owner) if o == s]
        per_session_results.append(stitch(parts, f"{label} #{s + 1}"))
    return combine(per_session_results, label), per_session_results


def compare(configs, total_rounds, sessions=1, base_bet=1.0, seed=20240514,
            jobs=None, curve_points=2000, progress=None):
    """用同一組 seed 跑多個設定 —— 共用亂數，讓彼此的差異更容易分辨。

    configs: [(label, Rules, strategy_name), ...]
    回傳    : [(label, 合併結果, [session 結果]), ...]
    """
    jobs = jobs or os.cpu_count() or 1
    out = []
    total_work = total_rounds * len(configs)
    done_before = [0]

    def sub_progress(done, _tot):
        if progress:
            progress(done_before[0] + done, total_work)

    ex = ProcessPoolExecutor(max_workers=jobs)
    try:
        for label, rules, strat_name in configs:
            merged, per_s = run(rules, strat_name, total_rounds, sessions,
                                base_bet, seed, jobs, curve_points, label,
                                sub_progress, executor=ex)
            out.append((label, merged, per_s))
            done_before[0] += total_rounds
    finally:
        ex.shutdown(cancel_futures=True)
    return out
