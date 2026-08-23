"""圖表輸出。

所有函式都回傳 matplotlib Figure，呼叫端自己決定要存檔還是嵌進 GUI。

效能重點：資金曲線一律降採樣。螢幕只有約 2000 px 寬，畫一百萬個點
其中 99.8% 都疊在同一個像素上，純粹浪費。曲線在模擬階段就只記錄
curve_points 個取樣點（見 runner.py），這裡直接畫即可。
"""
import matplotlib
matplotlib.use('Agg')                 # 預設不開互動視窗；GUI 會自己換後端
import matplotlib.pyplot as plt
import numpy as np

PALETTE = ['#3b82f6', '#ef4444', '#22c55e', '#f59e0b', '#a855f7',
           '#06b6d4', '#ec4899', '#84cc16']


def _style(ax):
    ax.grid(True, alpha=0.25, linewidth=0.6)
    ax.axhline(0, color='#666', linewidth=0.8, zorder=1)
    for side in ('top', 'right'):
        ax.spines[side].set_visible(False)


def bankroll_curves(groups, title='資金曲線', figsize=(10, 5.5)):
    """groups: [(標籤, [SessionResult, ...]), ...]，每個 session 畫一條線。"""
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
        # 各 session 的平均走勢畫粗線
        usable = [s.curve for s in sessions if s.curve]
        if len(usable) > 1:
            n = min(len(c) for c in usable)
            mean = np.mean([c[:n] for c in usable], axis=0)
            x = np.arange(n, dtype=float) * (sessions[0].rounds / max(n, 1))
            ax.plot(x, mean, color=color, linewidth=2.2, alpha=0.95)
    _style(ax)
    ax.set_xlabel('回合數')
    ax.set_ylabel('累積損益（單位）')
    ax.set_title(title)
    if len(groups) > 1 or len(groups[0][1]) > 1:
        ax.legend(fontsize=9, framealpha=0.9)
    fig.tight_layout()
    return fig


def result_distribution(groups, title='各次獨立實驗的最終結果分布',
                        bins=30, figsize=(10, 5.5)):
    """groups: [(標籤, [SessionResult, ...]), ...]"""
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
    ax.set_xlabel('該次實驗結束時的淨損益（單位）')
    ax.set_ylabel('次數')
    ax.set_title(title)
    ax.legend(fontsize=9)
    fig.tight_layout()
    return fig


def edge_comparison(summaries, title='賭場優勢比較（誤差棒為 95% 信賴區間）',
                    figsize=(10, None)):
    """summaries: [dict, ...]，來自 stats.summarize。

    誤差棒重疊 = 這兩個設定的差異在目前手數下分辨不出來，要加大樣本。
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
    ax.set_xlabel('賭場優勢 %（正 = 賭場贏，負 = 玩家贏）')
    ax.set_title(title)
    fig.tight_layout()
    return fig


def save(fig, path, dpi=130):
    fig.savefig(path, dpi=dpi)
    plt.close(fig)
    return path


def use_cjk_font():
    """讓中文標題不要變成豆腐方塊。找不到字型就退回英文標題。"""
    from matplotlib import font_manager
    for name in ('PingFang TC', 'PingFang SC', 'Heiti TC', 'Songti TC',
                 'Arial Unicode MS', 'Noto Sans CJK TC'):
        try:
            font_manager.findfont(name, fallback_to_default=False)
        except Exception:
            continue
        plt.rcParams['font.family'] = name
        plt.rcParams['axes.unicode_minus'] = False
        return name
    return None


use_cjk_font()
