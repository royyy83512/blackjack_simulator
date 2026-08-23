"""Statistics aggregation.

The simulation loop itself calls no methods at all (see runner.py) —
everything accumulates into local variables, and a SessionResult is only
built once a chunk finishes. That way even 1e8 hands aren't dragged down
by method-call overhead.

Variance uses the naive sum / sumsq formula: per-round results are O(1)
in magnitude, sumsq is always much larger than sum²/n, so there's no
catastrophic cancellation to worry about.
"""
import math
from dataclasses import dataclass, field


@dataclass
class SessionResult:
    label: str = ''
    rounds: int = 0
    hands: int = 0            # includes extra hands from splits
    net: float = 0.0
    sumsq: float = 0.0        # sum of squared per-round net results
    initial_wagered: float = 0.0
    total_wagered: float = 0.0
    wins: int = 0
    losses: int = 0
    pushes: int = 0
    player_bj: int = 0
    dealer_bj: int = 0
    player_bust: int = 0
    dealer_bust: int = 0
    doubles: int = 0
    splits: int = 0
    surrenders: int = 0
    insurances: int = 0
    dealer_played: int = 0
    dealer_totals: list = field(default_factory=lambda: [0] * 6)  # 17,18,19,20,21,bust
    max_drawdown: float = 0.0   # this chunk's own max drawdown
    peak: float = 0.0           # this chunk's highest cumulative net value
    low: float = 0.0            # this chunk's lowest cumulative net value
    curve: list = field(default_factory=list)   # downsampled bankroll curve
    curve_stride: int = 1
    shuffles: int = 0
    seconds: float = 0.0


def combine(results, label=None):
    """Merge multiple chunks (multi-core / multi-session) into one. Curves
    are not merged."""
    out = SessionResult(label=label or (results[0].label if results else ''))
    for r in results:
        out.rounds += r.rounds
        out.hands += r.hands
        out.net += r.net
        out.sumsq += r.sumsq
        out.initial_wagered += r.initial_wagered
        out.total_wagered += r.total_wagered
        out.wins += r.wins
        out.losses += r.losses
        out.pushes += r.pushes
        out.player_bj += r.player_bj
        out.dealer_bj += r.dealer_bj
        out.player_bust += r.player_bust
        out.dealer_bust += r.dealer_bust
        out.doubles += r.doubles
        out.splits += r.splits
        out.surrenders += r.surrenders
        out.insurances += r.insurances
        out.dealer_played += r.dealer_played
        out.shuffles += r.shuffles
        out.seconds = max(out.seconds, r.seconds)
        for i in range(6):
            out.dealer_totals[i] += r.dealer_totals[i]
        out.max_drawdown = max(out.max_drawdown, r.max_drawdown)
    return out


def stitch(chunks, label=None):
    """Reassemble the results of "one continuous path" that was split into
    several chunks run in parallel.

    Each chunk is independent and identically distributed, so stitched
    together they're still a valid random-walk path; this also computes
    the max drawdown across chunk boundaries exactly (looking within each
    chunk alone would underestimate it).
    """
    out = combine(chunks, label)
    offset = 0.0
    running_peak = 0.0
    max_dd = 0.0
    curve = []
    for c in chunks:
        max_dd = max(max_dd, c.max_drawdown, running_peak - (offset + c.low))
        running_peak = max(running_peak, offset + c.peak)
        curve.extend(v + offset for v in c.curve)
        offset += c.net
    out.max_drawdown = max_dd
    out.peak = running_peak
    out.curve = curve
    out.curve_stride = chunks[0].curve_stride if chunks else 1
    return out


def risk_of_ruin(ev_per_round, var_per_round, bankroll_units):
    """Probability of ruin over an unbounded number of rounds (the
    standard continuous approximation).

    RoR = exp(-2 * B * EV / Var); if EV <= 0, ruin is certain, returns 1.
    """
    if bankroll_units is None or bankroll_units <= 0:
        return None
    if ev_per_round <= 0 or var_per_round <= 0:
        return 1.0
    return math.exp(-2.0 * bankroll_units * ev_per_round / var_per_round)


def summarize(r: SessionResult, bankroll_units=None, base_bet=1.0):
    n = r.rounds
    if n == 0:
        return {}
    mean = r.net / n
    var = max((r.sumsq - r.net * r.net / n) / (n - 1), 0.0) if n > 1 else 0.0
    sd = math.sqrt(var)
    se = sd / math.sqrt(n)

    avg_initial = r.initial_wagered / n
    # house edge uses the original wager as its denominator — this is the
    # definition behind the commonly-cited 0.5% figure
    edge = -r.net / r.initial_wagered if r.initial_wagered else 0.0
    edge_ci = 1.96 * se / avg_initial if avg_initial else 0.0
    action_edge = -r.net / r.total_wagered if r.total_wagered else 0.0

    dp = r.dealer_played or 1
    return {
        'label': r.label,
        'rounds': n,
        'hands': r.hands,
        'net': r.net,
        'ev_per_round': mean,
        'ev_ci95': 1.96 * se,
        'sd_per_round': sd,
        'variance': var,
        'house_edge': edge,                 # positive = house advantage
        'house_edge_ci95': edge_ci,
        'edge_on_action': action_edge,
        'n0': (sd / abs(mean)) ** 2 if mean else float('inf'),
        'risk_of_ruin': risk_of_ruin(mean, var, bankroll_units),
        'bankroll_units': bankroll_units,
        'max_drawdown': r.max_drawdown,
        'win_rate': r.wins / n,
        'loss_rate': r.losses / n,
        'push_rate': r.pushes / n,
        'player_bj_rate': r.player_bj / n,
        'dealer_bj_rate': r.dealer_bj / n,
        'player_bust_rate': r.player_bust / n,
        'double_rate': r.doubles / n,
        'split_rate': r.splits / n,
        'surrender_rate': r.surrenders / n,
        'insurance_rate': r.insurances / n,
        'avg_bet': avg_initial,
        'action_ratio': r.total_wagered / r.initial_wagered if r.initial_wagered else 0,
        'dealer_bust_rate': r.dealer_totals[5] / dp,
        'dealer_dist': [c / dp for c in r.dealer_totals],
        'shuffles': r.shuffles,
        'seconds': r.seconds,
        'hands_per_sec': n / r.seconds if r.seconds else 0.0,
    }


def hands_needed(target_precision, sd_per_round=1.14):
    """How many hands are needed to bring the 95% confidence interval down
    to +/-target_precision (a fraction, e.g. 0.0002)."""
    return (1.96 * sd_per_round / target_precision) ** 2


def format_summary(s, verbose=True):
    L = []
    a = L.append
    a(f"  {'Net result':<22} {s['net']:+,.1f} units   ({s['rounds']:,} rounds / {s['hands']:,} hands)")
    a(f"  {'EV per round':<22} {s['ev_per_round']:+.5f} +/- {s['ev_ci95']:.5f} units")
    a(f"  {'House edge':<22} {s['house_edge']*100:+.4f}% +/- {s['house_edge_ci95']*100:.4f}%  (95% CI)")
    a(f"  {'SD per round':<22} {s['sd_per_round']:.4f}")
    if s['action_ratio']:
        a(f"  {'Action / initial bet':<22} {s['action_ratio']:.4f}   (includes extra wagers from doubles and splits)")
    if s['n0'] != float('inf'):
        a(f"  {'N0 (rounds per 1 SD)':<22} {s['n0']:,.0f} rounds")
    if s['risk_of_ruin'] is not None:
        a(f"  {'Risk of ruin':<22} {s['risk_of_ruin']*100:.3f}%  (bankroll {s['bankroll_units']:g} units)")
    a(f"  {'Max drawdown':<22} {s['max_drawdown']:,.1f} units")
    if verbose:
        a(f"  {'Win / loss / push':<22} {s['win_rate']*100:.2f}% / {s['loss_rate']*100:.2f}% / {s['push_rate']*100:.2f}%")
        a(f"  {'Player BJ / dealer BJ':<22} {s['player_bj_rate']*100:.3f}%  /  {s['dealer_bj_rate']*100:.3f}%")
        a(f"  {'Player/dealer bust':<22} {s['player_bust_rate']*100:.2f}%  /  {s['dealer_bust_rate']*100:.2f}%")
        a(f"  {'Double/split/surrender':<22} {s['double_rate']*100:.2f}% / {s['split_rate']*100:.2f}% / {s['surrender_rate']*100:.2f}%")
        d = s['dealer_dist']
        a("  " + f"{'Dealer final 17-21/bust':<22} " + " / ".join(f"{x*100:.1f}%" for x in d))
    a(f"  {'Speed':<22} {s['hands_per_sec']:,.0f} rounds/sec  ({s['seconds']:.1f}s)")
    return "\n".join(L)
