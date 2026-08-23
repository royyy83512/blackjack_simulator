"""The simulation runner: tight loop + multi-core parallelism + shared
random numbers.

The unit of parallelism is a "chunk." One run is sliced into several
chunks handed to different cores; the chunks are independent and
identically distributed, so stitching them back together is still a
valid random walk (stats.stitch correctly recomputes max drawdown across
chunk boundaries).

Common Random Numbers (CRN)
    When comparing different rules or strategies, the same seed produces
    the same shuffled shoe. Most hands make the same decisions and draw
    the same cards on both sides, so the difference is 0 — meaning the
    variance of "A minus B" is far smaller than either side's own
    variance, letting the same number of hands resolve much smaller
    differences. Caveat: once one hand's decision diverges, the card
    sequences after it drift apart, so the pairing isn't perfect from
    that point on.
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
    """Run one block of rounds. This is the whole program's only hot path,
    deliberately written with local variables throughout."""
    (rules, strat_name, rounds, base_bet, seed, curve_points, label) = args

    # build the strategy first: the shoe's count tags are decided by the
    # strategy file (Hi-Lo and KO use different tags)
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


# Max rounds per chunk. Cancellation only takes effect at chunk
# boundaries, so a chunk can't be too large or cancelling would take a
# long time to respond; roughly 0.5 seconds per chunk is a comfortable
# tradeoff.
MAX_CHUNK = 250_000


def plan_chunks(total_rounds, n_chunks, min_chunk=20_000, max_chunk=MAX_CHUNK):
    """Split the round count evenly. At least n_chunks pieces, and no
    piece exceeds max_chunk."""
    n = max(1, min(n_chunks, max(1, total_rounds // min_chunk)))
    n = max(n, -(-total_rounds // max_chunk))          # ceiling division
    base, extra = divmod(total_rounds, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def _seed_for(base_seed, session, chunk):
    return base_seed + session * 100003 + chunk * 10007


def run(rules, strat_name, total_rounds, sessions=1, base_bet=1.0,
        seed=20240514, jobs=None, curve_points=2000, label=None,
        progress=None, executor=None):
    """Run one (rules, strategy) combination.

    total_rounds : total round count (summed across all sessions)
    sessions     : number of independent trials — determines how many
                   bankroll curves get drawn and how many samples the
                   result distribution has
    seed         : same seed + same split => different strategies draw the
                   same shoe (Common Random Numbers)

    Returns (merged result, [per-session results]).
    """
    jobs = jobs or os.cpu_count() or 1
    label = label or f"{strat_name} | {rules.label()}"
    sessions = max(1, sessions)
    per_session = max(1, total_rounds // sessions)

    # total chunk count is a multiple of jobs, balancing load against
    # progress-reporting granularity
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
    """Run multiple configurations with the same seed — Common Random
    Numbers makes the differences between them easier to resolve.

    configs: [(label, Rules, strategy_name), ...]
    Returns: [(label, merged result, [session results]), ...]
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
