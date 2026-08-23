"""Chart output.

Every function returns a matplotlib Figure; the caller decides whether to
save it to a file or embed it in the GUI.

Performance note: bankroll curves are always downsampled. The screen is
only about 2000 px wide, so plotting a million points would have 99.8%
of them stacked on the same pixel — pure waste. The curve only records
curve_points sample points during the simulation itself (see runner.py),
so this module can just plot them directly.
"""
import matplotlib
matplotlib.use('Agg')                 # no interactive window by default; the GUI switches backends itself
import matplotlib.pyplot as plt
import numpy as np

PALETTE = ['#3b82f6', '#ef4444', '#22c55e', '#f59e0b', '#a855f7',
           '#06b6d4', '#ec4899', '#84cc16']


def _style(ax):
    ax.grid(True, alpha=0.25, linewidth=0.6)
    ax.axhline(0, color='#666', linewidth=0.8, zorder=1)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)


def bankroll_curves(groups, title='Bankroll Curves', figsize=(10, 5.5)):
    """groups: [(label, [SessionResult, ...]), ...], one line per session."""
    fig, ax = plt.subplots(figsize=figsize)
    for gi, (label, sessions) in enumerate(groups):
        color = PALETTE[gi % len(PALETTE)]
        for si, s in enumerate(sessions):
            if not s.curve:
                continue
            y = np.asarray(s.curve, dtype=float)
            x = np.arange(len(y), dtype=float) * (s.rounds / max(len(y), 1))
            ax.plot(x, y, color=color, linewidth=0.9,
                    alpha=0.85 if len(sessions) == 1 else 0.5,
                    label=label if si == 0 else None)
        # draw a thicker line for each group's average trend across sessions
        usable = [s.curve for s in sessions if s.curve]
        if len(usable) > 1:
            n = min(len(c) for c in usable)
            mean = np.mean([c[:n] for c in usable], axis=0)
            x = np.arange(n, dtype=float) * (sessions[0].rounds / max(n, 1))
            ax.plot(x, mean, color=color, linewidth=2.2, alpha=0.95)
    _style(ax)
    ax.set_xlabel('Rounds')
    ax.set_ylabel('Cumulative net result (units)')
    ax.set_title(title)
    if len(groups) > 1 or len(groups[0][1]) > 1:
        ax.legend(fontsize=9, framealpha=0.9)
    fig.tight_layout()
    return fig


def result_distribution(groups, title='Distribution of Final Results Across Sessions',
                        bins=30, figsize=(10, 5.5)):
    """groups: [(label, [SessionResult, ...]), ...]"""
    fig, ax = plt.subplots(figsize=figsize)
    for gi, (label, sessions) in enumerate(groups):
        vals = np.array([s.net for s in sessions], dtype=float)
        if vals.size < 2:
            continue
        ax.hist(vals, bins=bins, alpha=0.55, label=f"{label}  (n={vals.size})",
                color=PALETTE[gi % len(PALETTE)], edgecolor='none')
        ax.axvline(vals.mean(), color=PALETTE[gi % len(PALETTE)],
                   linestyle='--', linewidth=1.6)
    _style(ax)
    ax.set_xlabel('Net result at the end of that session (units)')
    ax.set_ylabel('Count')
    ax.set_title(title)
    ax.legend(fontsize=9)
    fig.tight_layout()
    return fig


def edge_comparison(summaries, title='House Edge Comparison (error bars are 95% CI)',
                    figsize=(10, None)):
    """summaries: [dict, ...], from stats.summarize.

    Overlapping error bars mean these two configurations' difference can't
    be resolved at the current sample size — run more hands.
    """
    labels = [s['label'] for s in summaries]
    edges = np.array([s['house_edge'] * 100 for s in summaries])
    errs = np.array([s['house_edge_ci95'] * 100 for s in summaries])
    h = figsize[1] or max(3.0, 0.55 * len(labels) + 1.8)
    fig, ax = plt.subplots(figsize=(figsize[0], h))
    y = np.arange(len(labels))
    colors = ['#ef4444' if e > 0 else '#22c55e' for e in edges]
    ax.barh(y, edges, xerr=errs, color=colors, alpha=0.85,
            error_kw=dict(ecolor='#334155', capsize=4, lw=1.2))
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    ax.axvline(0, color='#666', linewidth=0.9)
    ax.grid(True, axis='x', alpha=0.25, linewidth=0.6)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)
    span = float(max(np.abs(edges) + errs).max()) if len(edges) else 1.0
    for yi, (e, err) in enumerate(zip(edges, errs)):
        ax.text(e + (err + span * 0.03) * (1 if e >= 0 else -1), yi,
                f"{e:+.3f}%", va='center',
                ha='left' if e >= 0 else 'right', fontsize=8.5)
    ax.set_xlim(min(0, float((edges - errs).min())) - span * 0.25,
                max(0, float((edges + errs).max())) + span * 0.25)
    ax.set_xlabel('House edge % (positive = house wins, negative = player wins)')
    ax.set_title(title)
    fig.tight_layout()
    return fig


def save(fig, path, dpi=130):
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    return path
