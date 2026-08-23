"""統計彙總。

模擬迴圈本身不呼叫任何方法（見 runner.py），只用區域變數累加，
跑完一段才組成 SessionResult。這樣 1e8 手也不會被方法呼叫拖垮。

變異數用 sum / sumsq 的樸素公式即可：每手損益是 O(1) 量級，
sumsq 遠大於 sum²/n，不存在災難性抵銷的問題。
"""
import math
from dataclasses import dataclass, field


@dataclass
class SessionResult:
    label: str = ''
    rounds: int = 0
    hands: int = 0            # 含分牌後的手數
    net: float = 0.0
    sumsq: float = 0.0        # 每回合淨損益的平方和
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
    max_drawdown: float = 0.0   # 本段內部的最大回撤
    peak: float = 0.0           # 本段內累積淨值的最高點
    low: float = 0.0            # 本段內累積淨值的最低點
    curve: list = field(default_factory=list)   # 降採樣後的資金曲線
    curve_stride: int = 1
    shuffles: int = 0
    seconds: float = 0.0


def combine(results, label=None):
    """把多段（多核／多 session）結果合併成一個。曲線不合併。"""
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
    """把「同一條連續路徑」被切成多段平行跑的結果接回去。

    各段之間相互獨立同分布，接起來仍是合法的隨機漫步路徑；
    這裡順便把跨段的最大回撤算精確（只看段內會低估）。
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
    """無限手數下的破產機率（標準連續近似）。

    RoR = exp(-2 * B * EV / Var)；EV <= 0 時終將破產，回傳 1。
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
    # house edge 以「原始注碼」為分母，這是業界引用 0.5% 那個數字的定義
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
        'house_edge': edge,                 # 正值 = 賭場優勢
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
    """要把 95% 信賴區間壓到 ±target_precision（比例，如 0.0002）需要幾手。"""
    return (1.96 * sd_per_round / target_precision) ** 2


def format_summary(s, verbose=True):
    L = []
    a = L.append
    a(f"  淨損益           {s['net']:+,.1f} 單位   ({s['rounds']:,} 回合 / {s['hands']:,} 手)")
    a(f"  每回合 EV        {s['ev_per_round']:+.5f} ± {s['ev_ci95']:.5f} 單位")
    a(f"  賭場優勢         {s['house_edge']*100:+.4f}% ± {s['house_edge_ci95']*100:.4f}%  (95% CI)")
    a(f"  每回合標準差     {s['sd_per_round']:.4f}")
    if s['action_ratio']:
        a(f"  action / 原始注  {s['action_ratio']:.4f}   (含加倍與分牌的追加注)")
    if s['n0'] != float('inf'):
        a(f"  N0（打平 1 SD）  {s['n0']:,.0f} 回合")
    if s['risk_of_ruin'] is not None:
        a(f"  破產機率         {s['risk_of_ruin']*100:.3f}%  (本金 {s['bankroll_units']:g} 單位)")
    a(f"  最大回撤         {s['max_drawdown']:,.1f} 單位")
    if verbose:
        a(f"  勝 / 負 / 和      {s['win_rate']*100:.2f}% / {s['loss_rate']*100:.2f}% / {s['push_rate']*100:.2f}%")
        a(f"  玩家 BJ          {s['player_bj_rate']*100:.3f}%      莊家 BJ  {s['dealer_bj_rate']*100:.3f}%")
        a(f"  玩家爆牌         {s['player_bust_rate']*100:.2f}%      莊家爆牌 {s['dealer_bust_rate']*100:.2f}%")
        a(f"  加倍 / 分牌 / 投降  {s['double_rate']*100:.2f}% / {s['split_rate']*100:.2f}% / {s['surrender_rate']*100:.2f}%")
        d = s['dealer_dist']
        a("  莊家終局 17-21/爆  " + " / ".join(f"{x*100:.1f}%" for x in d))
    a(f"  速度             {s['hands_per_sec']:,.0f} 回合/秒  ({s['seconds']:.1f}s)")
    return "\n".join(L)
