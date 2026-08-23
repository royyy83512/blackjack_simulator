#!/usr/bin/env python3
"""Blackjack 模擬器 —— Tkinter 圖形介面。

啟動： python3 ui.py

設計說明
* 模擬跑在背景執行緒裡（該執行緒再去驅動多核 process pool），
  主執行緒只負責畫面，所以跑一億手時視窗仍然可以動、可以按取消。
* 進度靠 queue 傳回主執行緒，用 after() 輪詢，不跨執行緒碰 Tk 物件。
* 規則之間的衝突（peek / surrender / OBO）會即時鎖定對應的控制項。
* 「每次模擬手數」欄位（self.hands）在這裡是 per-session 的手數，
  使用者直接輸入不用心算；core.runner.run()/compare() 吃的是總回合數
  （所有 session 加總），所以呼叫前會先乘上 sessions 再傳進去。
  這跟 cli.py 的 --hands 語意不同 —— CLI 那個是使用者直接給總回合數，
  沒有這層轉換。兩邊不要弄混。
"""
import os
import queue
import random
import threading
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# matplotlib 與 charts 故意延遲到真的要畫圖時才載入。
# 多核模擬用 spawn 開子行程，子行程會重新 import 本檔案；
# 若在模組層 import matplotlib，每個 worker 都要多付約一秒的啟動成本。
_charts = None
_tkagg = None


def _load_charts():
    global _charts, _tkagg
    if _charts is None:
        import charts as _c                 # 它會設好 Agg 後端與中文字型
        import matplotlib
        matplotlib.use('TkAgg')             # 再換成 Tk 後端以便嵌入視窗
        from matplotlib.backends.backend_tkagg import (
            FigureCanvasTkAgg, NavigationToolbar2Tk)
        _charts = _c
        _tkagg = (FigureCanvasTkAgg, NavigationToolbar2Tk)
    return _charts, _tkagg


from core.rules import (Rules, normalize, SURRENDER_NONE, SURRENDER_LATE,
                        SURRENDER_EARLY, DOUBLE_ANY2, DOUBLE_9_11,
                        DOUBLE_10_11, LOSS_ALL, LOSS_ORIGINAL,
                        DECK_MIN, DECK_MAX)
from core import strategy as strategy_mod
from core.engine import Hand, ACT_SURRENDER
from core.runner import run, compare
from core.stats import summarize, format_summary
from core import scenario as scenario_mod
from core.solver import solve as solver_solve
from core import presets as presets_mod
from cli import SWEEPS

CARD_VALUES = ('2', '3', '4', '5', '6', '7', '8', '9', '10', 'A')

# 策略表格子的顯示顏色，跟一般策略卡的配色習慣一致。
CELL_COLORS = {
    'S': '#e2e8f0', 'H': '#93c5fd', 'D': '#fb923c', 'Ds': '#fb923c', 'Dh': '#fb923c',
    'P': '#c4b5fd', 'N': '#f8fafc',
    'R': '#fca5a5', 'Rh': '#fca5a5', 'Rs': '#fca5a5', 'Rp': '#fca5a5',
    '': '#ffffff',
}
CELL_TEXT = {'Ds': 'D', 'Dh': 'D', 'Rh': 'R', 'Rs': 'R', 'Rp': 'R'}


class Cancelled(Exception):
    pass


class _StubShoe:
    """給「問策略表現在建議什麼動作」用的假牌靴，只是要滿足 decide() 的介面。

    只在起手兩張這個時間點問一次，算牌策略此時通常還沒開始算牌
    （真數當作 0），這裡只是要拿到「不算牌時的建議動作」當比較基準，
    不是要模擬完整的算牌情境。
    """
    running_count = 0
    true_count = 0.0

    def start_round(self):
        pass


# 每次模擬要打幾手（不是總回合數——那個是「每次手數 x 次數」算出來的，
# 不用使用者自己乘除）。
HAND_PRESETS = [('1 千', 1_000), ('1 萬', 10_000), ('10 萬', 100_000),
                ('100 萬', 1_000_000), ('1000 萬', 10_000_000),
                ('1 億', 100_000_000)]

def load_strategy_list():
    """從 strategies/*.json 讀出可用策略。改完 JSON 按「重新載入」就會更新。"""
    return [(f"{'⊛ ' if counting else ''}{name}", name, desc)
            for name, desc, counting in strategy_mod.describe()]

SWEEP_LABELS = [('不比較（單一設定）', ''),
                ('比較 牌副數', 'decks'),
                ('比較 CSM 連續洗牌機', 'csm'),
                ('比較 莊家 A 可否投降', 'surrender-ace'),
                ('比較 S17 / H17', 'h17'),
                ('比較 DAS 有無', 'das'),
                ('比較 投降規則', 'surrender'),
                ('比較 peek / no-peek / OBO', 'peek'),
                ('比較 分 A 規則', 'rsa'),
                ('比較 加倍時機', 'double'),
                ('比較 BJ 賠率 3:2 / 6:5', 'bj')]


class App:
    def __init__(self, root):
        self.root = root
        root.title('Blackjack 模擬器')
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        # 依螢幕實際可用空間決定視窗大小，扣掉選單列/工作列大概的高度；
        # 不要對所有螢幕都假設同一個固定高度，筆電螢幕常常比這矮。
        root.geometry(f'{min(1360, sw - 60)}x{min(900, sh - 120)}')
        root.minsize(1000, 560)
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self._after_id = None
        self.results = None
        self.summaries = []
        self.canvases = {}

        self._build_vars()
        self._build_layout()
        self._sync_locks()
        self._sync_seed_lock()
        self._update_precision()
        self._fill_strategy_dropdowns()
        self._render_strategy_tables()

    # ---------------------------------------------------------------- 變數
    def _build_vars(self):
        v = self
        v.decks = tk.IntVar(value=6)
        v.pen = tk.DoubleVar(value=75)
        v.csm = tk.BooleanVar(value=False)
        v.h17 = tk.BooleanVar(value=False)
        v.double_rule = tk.StringVar(value=DOUBLE_ANY2)
        v.das = tk.BooleanVar(value=True)
        v.surrender = tk.StringVar(value=SURRENDER_LATE)
        v.sur_vs_ace = tk.BooleanVar(value=True)
        v.peek = tk.BooleanVar(value=True)
        v.obo = tk.BooleanVar(value=False)
        v.rsa = tk.BooleanVar(value=False)
        v.hsa = tk.BooleanVar(value=False)
        v.bj65 = tk.BooleanVar(value=False)

        v.hands = tk.StringVar(value='1000000')
        v.sessions = tk.IntVar(value=8)
        v.bet = tk.DoubleVar(value=1.0)
        v.bankroll = tk.DoubleVar(value=100.0)
        v.seed = tk.IntVar(value=20240514)
        v.fixed_seed = tk.BooleanVar(value=False)
        v.jobs = tk.IntVar(value=os.cpu_count() or 1)
        v.adaptive = tk.BooleanVar(value=True)
        v.sweep = tk.StringVar(value='')
        v.strat_vars = {}

        v.status = tk.StringVar(value='就緒')
        v.precision = tk.StringVar(value='')
        for var in (v.peek, v.surrender, v.rsa, v.csm):
            var.trace_add('write', lambda *_: self._sync_locks())
        for var in (v.hands, v.sessions):
            var.trace_add('write', lambda *_: self._update_precision())

    # ---------------------------------------------------------------- 版面
    def _build_layout(self):
        # 曾經試過用 tk.Canvas 包 ttk 控制項做左欄捲動，但 macOS Aqua 主題下
        # 那些控制項的座標／大小量起來都正常，畫面上卻整個不畫出來
        # （ttk 在 macOS 走原生繪製，跟 Canvas 的合成流程對不上）。
        # 改成不捲動、直接排版；「開始模擬」等按鈕仍然釘在左欄最下面
        # （pack 時最先排進去，永遠保留得到空間），不會被規則清單擠到看不見。
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill='both', expand=True)
        left = ttk.Frame(outer, width=330)
        left.pack(side='left', fill='y', padx=(0, 10))
        right = ttk.Frame(outer)
        right.pack(side='left', fill='both', expand=True)

        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        run_bar = ttk.Frame(left)
        run_bar.pack(side='bottom', fill='x')

        # 規則、模擬設定、策略疊在一起需要的高度加總起來超過 1000px，
        # 矮螢幕（例如 1280x720）的視窗根本塞不下，兩欄排版也不夠（最高那欄
        # 還是要 ~670px）。改用分頁籤：同時間只顯示一頁，需要的高度只看
        # 最高的那一頁（約 400px），任何螢幕都放得下，不需要捲動。
        nb = ttk.Notebook(left)
        nb.pack(side='top', fill='both', expand=True)
        tab_rules = ttk.Frame(nb, padding=(4, 6))
        tab_sim = ttk.Frame(nb, padding=(4, 6))
        tab_strategy = ttk.Frame(nb, padding=(4, 6))
        nb.add(tab_rules, text='賭場規則')
        nb.add(tab_sim, text='模擬設定')
        nb.add(tab_strategy, text='策略／對比')

        self._build_rules(tab_rules)
        self._build_sim(tab_sim)
        self._build_strategy(tab_strategy)
        self._build_run(run_bar)
        self._build_output(right)

    def _row(self, parent, r, text):
        ttk.Label(parent, text=text).grid(row=r, column=0, sticky='w', pady=2)

    def _build_rules(self, parent):
        preset_box = ttk.LabelFrame(parent, text=' 賭場預設（一鍵帶入） ', padding=8)
        preset_box.pack(fill='x', pady=(0, 8))
        row = ttk.Frame(preset_box)
        row.pack(fill='x')
        self.cb_preset = ttk.Combobox(row, width=22, state='readonly')
        self.cb_preset.pack(side='left')
        ttk.Button(row, text='套用', command=self._apply_preset).pack(side='left', padx=6)
        ttk.Button(row, text='重新載入', command=self._fill_presets).pack(side='left')
        self._fill_presets()

        f = ttk.LabelFrame(parent, text=' 賭場規則 ', padding=8)
        f.pack(fill='x')
        r = 0
        self._row(f, r, '牌副數')
        ttk.Spinbox(f, from_=DECK_MIN, to=DECK_MAX, width=8,
                    textvariable=self.decks).grid(row=r, column=1, sticky='w')
        r += 1
        self._row(f, r, 'Penetration %')
        self.sp_pen = ttk.Spinbox(f, from_=30, to=95, increment=5, width=8,
                                  textvariable=self.pen)
        self.sp_pen.grid(row=r, column=1, sticky='w')
        r += 1
        self.cb_csm = ttk.Checkbutton(f, text='連續洗牌機 (CSM)：每手都重洗，沒有切牌點',
                                      variable=self.csm)
        self.cb_csm.grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        self._row(f, r, '莊家軟 17')
        box = ttk.Frame(f); box.grid(row=r, column=1, sticky='w')
        ttk.Radiobutton(box, text='S17 停', variable=self.h17, value=False).pack(side='left')
        ttk.Radiobutton(box, text='H17 補', variable=self.h17, value=True).pack(side='left')
        r += 1
        self._row(f, r, '加倍時機')
        self.cb_double = ttk.Combobox(f, width=14, state='readonly',
                                      values=['任兩張', '只有 9/10/11', '只有 10/11'])
        self.cb_double.current(0)
        self.cb_double.bind('<<ComboboxSelected>>', self._on_double)
        self.cb_double.grid(row=r, column=1, sticky='w')
        r += 1
        ttk.Checkbutton(f, text='分牌後可加倍 (DAS)', variable=self.das
                        ).grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        # peek 排在投降前面：peek 開著的話 early surrender 這個選項根本不存在
        # （BJ 在玩家動作前就結算完了），要先知道 peek 開關才看得懂投降下拉
        # 選單裡為什麼有時候只有兩個選項、有時候有三個。
        self.cb_peek = ttk.Checkbutton(f, text='莊家發底牌並偷看 (peek)', variable=self.peek)
        self.cb_peek.grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        self.cb_obo = ttk.Checkbutton(f, text='莊家 BJ 時玩家只輸原始注 (OBO)',
                                      variable=self.obo)
        self.cb_obo.grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        self._row(f, r, '投降')
        self.cb_sur = ttk.Combobox(f, width=14, state='readonly',
                                   values=['不可投降', 'late surrender', 'early surrender'])
        self.cb_sur.current(1)
        self.cb_sur.bind('<<ComboboxSelected>>', self._on_sur)
        self.cb_sur.grid(row=r, column=1, sticky='w')
        r += 1
        self.cb_sur_ace = ttk.Checkbutton(f, text='莊家明牌 A 也可以投降',
                                          variable=self.sur_vs_ace)
        self.cb_sur_ace.grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        self.cb_rsa = ttk.Checkbutton(f, text='可以再分 A (RSA)', variable=self.rsa)
        self.cb_rsa.grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        self.cb_hsa = ttk.Checkbutton(f, text='分 A 之後還能補牌', variable=self.hsa)
        self.cb_hsa.grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        ttk.Checkbutton(f, text='BJ 只賠 6:5（否則 3:2）', variable=self.bj65
                        ).grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        self.lock_note = ttk.Label(f, text='', foreground='#b45309', wraplength=250,
                                   justify='left')
        self.lock_note.grid(row=r, column=0, columnspan=2, sticky='w', pady=(6, 0))
        ttk.Label(f, text='分牌上限固定 4 手', foreground='#64748b'
                  ).grid(row=r + 1, column=0, columnspan=2, sticky='w')

    def _build_sim(self, parent):
        f = ttk.LabelFrame(parent, text=' 模擬設定 ', padding=8)
        f.pack(fill='x', pady=(8, 0))
        r = 0
        self._row(f, r, '每次模擬手數')
        ttk.Entry(f, textvariable=self.hands, width=14).grid(row=r, column=1, sticky='w')
        r += 1
        box = ttk.Frame(f); box.grid(row=r, column=0, columnspan=2, sticky='w', pady=(0, 4))
        for name, n in HAND_PRESETS:
            ttk.Button(box, text=name, width=5,
                       command=lambda n=n: self.hands.set(str(n))).pack(side='left', padx=1)
        r += 1
        ttk.Label(f, textvariable=self.precision, foreground='#0369a1',
                  wraplength=250, justify='left').grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        for text, var, kw in (('獨立實驗次數', self.sessions, dict(from_=1, to=64)),
                              ('基本注碼', self.bet, dict(from_=1, to=1000)),
                              ('本金（單位）', self.bankroll, dict(from_=10, to=100000, increment=50)),
                              ('平行核心數', self.jobs, dict(from_=1, to=64))):
            self._row(f, r, text)
            ttk.Spinbox(f, width=12, textvariable=var, **kw).grid(row=r, column=1, sticky='w')
            r += 1
        self._row(f, r, '亂數種子')
        self.sp_seed = ttk.Spinbox(f, width=12, textvariable=self.seed,
                                   from_=0, to=10 ** 9, state='disabled')
        self.sp_seed.grid(row=r, column=1, sticky='w')
        r += 1
        self.cb_fixed_seed = ttk.Checkbutton(
            f, text='固定種子（可重現同一次結果）', variable=self.fixed_seed,
            command=self._sync_seed_lock)
        self.cb_fixed_seed.grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        ttk.Label(f, text='預設每次「開始模擬」都會換一個新種子，\n'
                          '結果自然每次不同；固定種子會一直重複同一批牌。',
                  foreground='#64748b', justify='left'
                  ).grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1

    def _build_strategy(self, parent):
        f2 = ttk.LabelFrame(parent, text=' 策略（來自 strategies/*.json） ', padding=8)
        f2.pack(fill='x')
        self.strat_box = ttk.Frame(f2)
        self.strat_box.pack(fill='x')
        self._fill_strategies()
        bar = ttk.Frame(f2)
        bar.pack(fill='x', pady=(4, 0))
        ttk.Button(bar, text='重新載入策略檔', command=self._reload_strategies
                   ).pack(side='left')
        ttk.Label(bar, text='  ⊛ = 算牌', foreground='#64748b').pack(side='left')
        ttk.Separator(f2, orient='horizontal').pack(fill='x', pady=5)
        ttk.Checkbutton(f2, text='策略表隨規則自動調整', variable=self.adaptive).pack(anchor='w')
        ttk.Label(f2, text='取消勾選＝忽略策略檔裡的 overrides，\n可看出用錯策略表的代價',
                  foreground='#64748b', justify='left').pack(anchor='w')

        f3 = ttk.LabelFrame(parent, text=' 規則對比 ', padding=8)
        f3.pack(fill='x', pady=(8, 0))
        self.cb_sweep = ttk.Combobox(f3, width=28, state='readonly',
                                     values=[n for n, _ in SWEEP_LABELS])
        self.cb_sweep.current(0)
        self.cb_sweep.bind('<<ComboboxSelected>>', self._on_sweep)
        self.cb_sweep.pack(anchor='w')
        ttk.Label(f3, text='選了就用同一組牌靴掃描該維度\n（共用亂數，差異更容易分辨）',
                  foreground='#64748b', justify='left').pack(anchor='w')

    def _build_run(self, parent):
        f = ttk.Frame(parent, padding=(0, 10))
        f.pack(fill='x')
        self.btn_run = ttk.Button(f, text='開始模擬', command=self._start)
        self.btn_run.pack(side='left')
        self.btn_cancel = ttk.Button(f, text='取消', command=self._request_cancel,
                                     state='disabled')
        self.btn_cancel.pack(side='left', padx=6)
        self.btn_save = ttk.Button(f, text='匯出圖表', command=self._export,
                                   state='disabled')
        self.btn_save.pack(side='left')
        self.pbar = ttk.Progressbar(parent, mode='determinate', maximum=1000)
        self.pbar.pack(fill='x')
        ttk.Label(parent, textvariable=self.status, foreground='#334155'
                  ).pack(anchor='w', pady=(3, 0))

    def _build_output(self, parent):
        self.nb = ttk.Notebook(parent)
        self.nb.pack(fill='both', expand=True)
        self.txt = tk.Text(self.nb, wrap='none', font=('Menlo', 11), padx=10, pady=10)
        self.nb.add(self.txt, text='摘要')
        for key, title in (('bankroll', '資金曲線'), ('dist', '結果分布'), ('cmp', '對比圖')):
            frame = ttk.Frame(self.nb)
            self.nb.add(frame, text=title)
            self.canvases[key] = {'frame': frame, 'canvas': None, 'toolbar': None,
                                  'fig': None, 'job_id': None}
        self._build_strategy_view(self._add_tab('策略表'))
        self._build_scenario_view(self._add_tab('情境試算'))

    def _add_tab(self, title):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text=title)
        return frame

    # -------------------------------------------------------- 策略表檢視器
    def _build_strategy_view(self, parent):
        bar = ttk.Frame(parent, padding=6)
        bar.pack(fill='x')
        ttk.Label(bar, text='策略：').pack(side='left')
        self.cb_view_strategy = ttk.Combobox(bar, width=24, state='readonly')
        self.cb_view_strategy.pack(side='left', padx=(0, 8))
        self.cb_view_strategy.bind('<<ComboboxSelected>>',
                                   lambda e: self._render_strategy_tables())
        ttk.Button(bar, text='重新整理（套用目前規則）',
                   command=self._render_strategy_tables).pack(side='left')
        self.strategy_view_note = ttk.Label(bar, text='', foreground='#64748b',
                                            wraplength=520, justify='left')
        self.strategy_view_note.pack(side='left', padx=10)

        legend = ttk.Frame(parent, padding=(6, 0, 6, 4))
        legend.pack(fill='x')
        for label, name in (('H', '補牌'), ('S', '停牌'), ('D', '加倍'),
                            ('P', '分牌'), ('Rh', '投降'), ('N', '不分/往下查')):
            tk.Label(legend, text='  ', bg=CELL_COLORS[label], relief='solid', bd=1
                     ).pack(side='left', padx=(0, 3))
            ttk.Label(legend, text=name).pack(side='left', padx=(0, 12))

        canvas, holder = self._make_tk_scrollable(parent)
        canvas.pack(fill='both', expand=True)
        self.strategy_view_holder = holder

    def _make_tk_scrollable(self, parent):
        """用純 tk（不是 ttk）元件做垂直捲動區域。

        macOS Aqua 主題下把 ttk 控制項塞進 tk.Canvas 會整個畫不出來——
        widget 的座標/大小量起來都正常，畫面上就是不會被畫出來（ttk 在
        macOS 走原生繪製，跟 Canvas 的合成流程對不上，之前在左側規則欄
        踩過這個坑）。純 tk 元件走 Tk 自己的跨平台繪製，沒有這個問題，
        這裡的策略表格子刻意全部用 tk.Frame/tk.Label，不要用 ttk 版本。
        """
        wrap = ttk.Frame(parent)
        canvas = tk.Canvas(wrap, highlightthickness=0, bg='white')
        vsb = ttk.Scrollbar(wrap, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side='left', fill='both', expand=True)
        vsb.pack(side='right', fill='y')

        holder = tk.Frame(canvas, bg='white')
        win = canvas.create_window((0, 0), window=holder, anchor='nw')
        holder.bind('<Configure>',
                    lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(win, width=e.width))

        def on_wheel(e):
            canvas.yview_scroll(-1 * (e.delta // 120 or (1 if e.delta > 0 else -1)), 'units')
        canvas.bind('<MouseWheel>', on_wheel)
        holder.bind('<MouseWheel>', on_wheel)
        return wrap, holder

    def _cell_widget(self, parent, label, row, col):
        text = CELL_TEXT.get(label, label)
        color = CELL_COLORS.get(label, '#fde047')
        tk.Label(parent, text=text, bg=color, fg='black', width=4, height=1,
                 relief='solid', bd=1, font=('Menlo', 10)
                 ).grid(row=row, column=col, sticky='nsew')

    def _draw_header_row(self, grid):
        tk.Label(grid, text='', bg='white', width=5).grid(row=0, column=0)
        for c, name in enumerate(CARD_VALUES):
            tk.Label(grid, text=name, bg='white', fg='black', width=4,
                     font=('Menlo', 10, 'bold')).grid(row=0, column=c + 1)

    @staticmethod
    def _col_to_up(col):
        """格子欄位索引 0..9（對應 2..10,A）換算成實際的莊家明牌點數。"""
        return 1 if col == 9 else col + 2

    def _draw_total_grid(self, holder, title, table, totals, rules, is_soft, strat):
        """totals 由小到大畫（小總點數在上、大總點數在下）。

        is_soft 決定怎麼把顯示總點數換算回 rules.double_allowed_on() 要的
        「不算軟手加值的原始點數和」——軟 18(A,7) 顯示 18，換算回去是 8。

        strat 傳給 effective_cell_label() 是為了讓 SURRENDER_EARLY 模式下
        的顯示跟真正的牌局行為一致（early_surrender() 的 es_vs_ace/es_vs_ten
        前置檢查會蓋過 hard 表本身，見 core/strategy.py 的說明）。
        """
        tk.Label(holder, text=title, bg='white', fg='black', font=('Menlo', 11, 'bold')
                 ).pack(anchor='w', padx=8, pady=(10, 2))
        grid = tk.Frame(holder, bg='white')
        grid.pack(anchor='w', padx=8)
        self._draw_header_row(grid)
        for r, total in enumerate(totals, start=1):
            row = table.get(total)
            hard_total = total - 10 if is_soft else total
            tk.Label(grid, text=str(total), bg='white', fg='black', width=5,
                     font=('Menlo', 10, 'bold')
                     ).grid(row=r, column=0, sticky='e', padx=(0, 4))
            for c in range(10):
                cell = row[c] if row else None
                label = strategy_mod.effective_cell_label(
                    cell, self._col_to_up(c), rules, hard_total, is_soft, strat)
                self._cell_widget(grid, label, r, c + 1)

    def _draw_pair_grid(self, holder, title, table, rules, strat):
        tk.Label(holder, text=title, bg='white', fg='black', font=('Menlo', 11, 'bold')
                 ).pack(anchor='w', padx=8, pady=(10, 2))
        grid = tk.Frame(holder, bg='white')
        grid.pack(anchor='w', padx=8, pady=(0, 12))
        self._draw_header_row(grid)
        pair_names = ('A', '2', '3', '4', '5', '6', '7', '8', '9', '10')
        for r, (key, name) in enumerate(zip(range(1, 11), pair_names), start=1):
            row = table.get(key)
            hard_total = 2 * key           # A,A 的原始點數和是 1+1=2
            is_soft = key == 1             # 只有 A,A 算軟手（不分牌就是軟 12）
            tk.Label(grid, text=f'{name},{name}', bg='white', fg='black', width=5,
                     font=('Menlo', 10, 'bold')
                     ).grid(row=r, column=0, sticky='e', padx=(0, 4))
            for c in range(10):
                cell = row[c] if row else None
                label = strategy_mod.effective_cell_label(
                    cell, self._col_to_up(c), rules, hard_total, is_soft, strat)
                self._cell_widget(grid, label, r, c + 1)

    def _draw_deviation_list(self, holder, strat):
        tk.Label(holder, text='算牌偏離（依真數／流水數變化的格子）', bg='white',
                 fg='black', font=('Menlo', 11, 'bold')
                 ).pack(anchor='w', padx=8, pady=(10, 2))
        if not strat.deviations:
            tk.Label(holder, text='（這個策略沒有定義偏離）', bg='white', fg='#64748b'
                     ).pack(anchor='w', padx=8, pady=(0, 12))
            return
        for (tname, row, col), rule_list in sorted(strat.deviations.items()):
            dealer = CARD_VALUES[col]
            row_label = ({1: 'A', 10: '10'}.get(row, row) if tname == 'pair' else row)
            for lo, hi, cell in rule_list:
                act = strategy_mod.cell_label(cell)
                if hi == float('inf'):
                    cond = f"計數 >= {lo:g}"
                elif lo == float('-inf'):
                    cond = f"計數 <= {hi:g}"
                else:
                    cond = f"{lo:g} <= 計數 <= {hi:g}"
                tk.Label(holder, bg='white', fg='black', anchor='w', justify='left',
                         text=f"  {tname} {row_label} 對 {dealer}：{cond} 時改成 {act}",
                         font=('Menlo', 10)).pack(anchor='w', padx=16)
        tk.Label(holder, text='', bg='white').pack(pady=4)

    def _render_strategy_tables(self):
        for w in self.strategy_view_holder.winfo_children():
            w.destroy()
        label = self.cb_view_strategy.get()
        code = getattr(self, '_strategy_view_map', {}).get(label, label)
        if not code:
            return
        rules, _notes = self._collect()
        try:
            strat = strategy_mod.make(code, rules)
        except Exception as e:
            tk.Label(self.strategy_view_holder, text=f'載入失敗：{e}', bg='white',
                     fg='#dc2626').pack(anchor='w', padx=8, pady=8)
            return
        note = rules.label()
        if strat.applied_overrides:
            note += '｜套用了：' + '；'.join(strat.applied_overrides)
        self.strategy_view_note.configure(text=note)

        self._draw_total_grid(self.strategy_view_holder, '硬牌 Hard Totals',
                              strat.hard, range(4, 21), rules, is_soft=False, strat=strat)
        self._draw_total_grid(self.strategy_view_holder, '軟牌 Soft Totals',
                              strat.soft, range(12, 21), rules, is_soft=True, strat=strat)
        self._draw_pair_grid(self.strategy_view_holder, '對子 Pairs', strat.pair, rules, strat)
        if strat.count_tags:
            self._draw_deviation_list(self.strategy_view_holder, strat)

    # -------------------------------------------------------- 情境試算
    def _build_scenario_view(self, parent):
        bar = ttk.Frame(parent, padding=8)
        bar.pack(fill='x')

        ttk.Label(bar, text='玩家第一張').grid(row=0, column=0, sticky='w')
        self.cb_p1 = ttk.Combobox(bar, width=5, state='readonly', values=CARD_VALUES)
        self.cb_p1.current(6)             # 預設 8
        self.cb_p1.grid(row=0, column=1, padx=(2, 12))

        ttk.Label(bar, text='玩家第二張').grid(row=0, column=2, sticky='w')
        self.cb_p2 = ttk.Combobox(bar, width=5, state='readonly', values=CARD_VALUES)
        self.cb_p2.current(6)             # 預設 8（8,8 是最常拿來討論的分牌情境）
        self.cb_p2.grid(row=0, column=3, padx=(2, 12))

        ttk.Label(bar, text='莊家明牌').grid(row=0, column=4, sticky='w')
        self.cb_dup = ttk.Combobox(bar, width=5, state='readonly', values=CARD_VALUES)
        self.cb_dup.current(8)            # 預設 10
        self.cb_dup.grid(row=0, column=5, padx=(2, 12))

        ttk.Label(bar, text='每個動作模擬手數').grid(row=1, column=0, sticky='w', pady=(8, 0))
        self.scen_hands = tk.StringVar(value='1000000')
        ttk.Entry(bar, textvariable=self.scen_hands, width=12
                  ).grid(row=1, column=1, columnspan=2, sticky='w', pady=(8, 0))

        ttk.Label(bar, text='延續策略').grid(row=1, column=3, sticky='w', pady=(8, 0))
        self.cb_scen_strategy = ttk.Combobox(bar, width=16, state='readonly')
        self.cb_scen_strategy.grid(row=1, column=4, columnspan=2, sticky='w', pady=(8, 0))
        ttk.Label(bar, text='（第一步之後照這個策略打）', foreground='#64748b'
                  ).grid(row=1, column=6, sticky='w', pady=(8, 0), padx=(6, 0))

        run_row = ttk.Frame(parent, padding=(8, 0, 8, 8))
        run_row.pack(fill='x')
        self.btn_scen_run = ttk.Button(run_row, text='計算', command=self._start_scenario)
        self.btn_scen_run.pack(side='left')
        self.btn_scen_cancel = ttk.Button(run_row, text='取消', command=self._cancel_scenario,
                                          state='disabled')
        self.btn_scen_cancel.pack(side='left', padx=6)
        self.scen_pbar = ttk.Progressbar(run_row, mode='determinate', maximum=1000, length=220)
        self.scen_pbar.pack(side='left', padx=6)
        self.scen_status = tk.StringVar(value='就緒')
        ttk.Label(run_row, textvariable=self.scen_status, foreground='#334155'
                  ).pack(side='left', padx=6)

        ttk.Separator(parent, orient='horizontal').pack(fill='x')
        self.scen_result_text = tk.Text(parent, wrap='word', font=('Menlo', 12),
                                        padx=10, pady=10)
        self.scen_result_text.pack(fill='both', expand=True)

        self.scen_q = queue.Queue()
        self.scen_cancel = threading.Event()
        self.scen_worker = None
        self.scen_after_id = None
        self._scen_cell = None
        self._scen_table_action = None

    @staticmethod
    def _card_name(v):
        return 'A' if v == 1 else str(v)

    def _start_scenario(self):
        if self.scen_worker and self.scen_worker.is_alive():
            return
        try:
            c1 = scenario_mod.parse_card(self.cb_p1.get())
            c2 = scenario_mod.parse_card(self.cb_p2.get())
            up = scenario_mod.parse_card(self.cb_dup.get())
        except scenario_mod.ScenarioError as e:
            messagebox.showerror('輸入錯誤', str(e))
            return
        try:
            hands = int(float(self.scen_hands.get()))
        except ValueError:
            messagebox.showerror('輸入錯誤', '模擬手數要是數字')
            return
        rules, _notes = self._collect()
        strat_label = self.cb_scen_strategy.get()
        strat_code = getattr(self, '_strategy_view_map', {}).get(strat_label, strat_label)

        legal = scenario_mod.legal_first_actions((c1, c2), up, rules)
        if legal == 0:
            messagebox.showerror('輸入錯誤', '這個起手在目前規則下沒有合法動作可比較')
            return

        try:
            strat = strategy_mod.make(strat_code, rules)
            hand = Hand([c1, c2], 1.0)
            # decide() 不知道 early surrender：那是 core.engine.play_round
            # 裡「decide() 之前」的獨立前置檢查（early_surrender()），這裡要
            # 照同樣的順序判斷，不然 SURRENDER_EARLY 桌會顯示成沒投降的動作。
            player_bj = len(hand.cards) == 2 and hand.total == 21
            if (rules.surrender == SURRENDER_EARLY and not player_bj
                    and rules.surrender_allowed_vs(up)
                    and strat.early_surrender(hand, up, _StubShoe(), rules)):
                act = ACT_SURRENDER
            else:
                act = strat.decide(hand, up, legal, _StubShoe(), rules)
            self._scen_table_action = scenario_mod.ACTION_NAMES.get(act)
        except Exception:
            self._scen_table_action = None

        # 記下這次測的是哪一格，結果出來才知道「更新策略表」按鈕要改哪個
        # 檔案的哪個位置；同時檢查目前這格是不是被某個 override 蓋過
        # （例如 H17 差異格）——如果是，更新基礎表不會真的生效，要先講清楚。
        self._scen_cell = self._resolve_scenario_cell(c1, c2, up, strat_code, rules)

        # 跟主流程共用同一個「固定種子」開關：沒勾的話這裡也要每次重新抽
        # 一個種子，不然每次按「計算」都是同一個種子、同一副牌，結果會
        # 一模一樣，容易被誤會成「這格答案很穩定」，其實只是沒有真的
        # 重新抽樣。
        if not self.fixed_seed.get():
            self.seed.set(random.randrange(1, 2 ** 31 - 1))
        actual_seed = int(self.seed.get())

        self._show_solver_reference(c1, c2, up, rules, actual_seed)

        self.scen_cancel.clear()
        self.scen_q = queue.Queue()
        self.btn_scen_run.state(['disabled'])
        self.btn_scen_cancel.state(['!disabled'])
        self.scen_pbar['value'] = 0
        self.scen_status.set('計算中…')

        args = dict(rules=rules, strat_code=strat_code, cards=(c1, c2), up=up,
                    hands=hands, jobs=int(self.jobs.get()), seed=actual_seed)
        self.scen_worker = threading.Thread(target=self._run_scenario_bg,
                                            args=(args,), daemon=True)
        self.scen_worker.start()
        self.scen_after_id = self.root.after(80, self._poll_scenario)

    @staticmethod
    def _resolve_scenario_cell(c1, c2, up, strat_code, rules):
        """算出這次情境對應策略表的哪一格（kind/row/col），順便檢查這格
        目前是不是被某個 override 蓋過（H17 差異格之類的）。

        回傳 dict 或 None（策略載入失敗、或這格根本查不到表時）。
        """
        try:
            is_pair = c1 == c2
            if is_pair:
                kind, row = 'pair', c1
            else:
                s, aces = c1 + c2, (c1 == 1) + (c2 == 1)
                soft = aces > 0 and s + 10 <= 21
                kind = 'soft' if soft else 'hard'
                row = s + 10 if soft else s
            col = 9 if up == 1 else up - 2

            with_ov = strategy_mod.make(strat_code, rules, apply_overrides=True)
            without_ov = strategy_mod.make(strat_code, rules, apply_overrides=False)
            table_with = getattr(with_ov, kind)
            table_without = getattr(without_ov, kind)
            overridden = (row in table_with and row in table_without
                         and table_with[row][col] != table_without[row][col])
            return {'kind': kind, 'row': row, 'col': col, 'strat_code': strat_code,
                    'overridden': overridden}
        except Exception:
            return None

    def _show_solver_reference(self, c1, c2, up, rules, seed):
        """先顯示無限牌組的精確解（瞬間算完，不含分牌）當快速參考，
        蒙地卡羅（含分牌、用實際副數）跑完會接在後面。"""
        lines = [f'玩家 {self._card_name(c1)},{self._card_name(c2)} 對莊家 '
                f'{self._card_name(up)}　({rules.label()})',
                f'亂數種子：{seed}' + ('（固定）' if self.fixed_seed.get() else '（本次自動產生，未固定）'),
                '']
        # 明確標出「策略表現在建議什麼」，不然使用者很容易誤把下面的「精確解」
        # 當成策略表的答案——精確解是每次都獨立算出來的數學結果，完全不查
        # 任何 JSON，改了策略檔也不會變；只有這一行才是真的在讀策略檔，
        # 按過「更新策略表」之後重新計算，這一行才會變。
        table_name = getattr(self, '_scen_table_action', None)
        lines.append(f'目前策略表（{self.cb_scen_strategy.get()}）建議：'
                     + (table_name if table_name else '（無法判斷）'))
        lines.append('')

        is_pair = c1 == c2
        try:
            s, aces = c1 + c2, (c1 == 1) + (c2 == 1)
            soft = aces > 0 and s + 10 <= 21
            total = s + 10 if soft else s
            kind = 'soft' if soft else 'hard'
            col = 9 if up == 1 else up - 2
            solved = solver_solve(rules)
            letter, evs = solved[kind][total][col]
            lines.append('精確解（無限牌組、不含分牌，瞬間算出、零誤差；'
                         '\n跟上面的策略表無關——這裡不查任何 JSON，是每次重新用數學算出來的，'
                         '\n改策略檔不會讓這段跟著變，它本來就不是從策略檔查來的）：')
            for k in sorted(evs, key=evs.get, reverse=True):
                tag = '  ← 精確解最佳（未比較分牌）' if k == letter else ''
                lines.append(f'  {k:<3} EV = {evs[k]:+.5f}{tag}')
            if is_pair:
                lines.append('  （這手是對子，精確解不含分牌選項——分牌牽扯 DAS/RSA/再分牌，'
                             '\n   精確解代價太高，分牌只能靠下面的蒙地卡羅比較）')
            lines.append('')
            lines.append('這張精確解假設無限副牌，真實副數少時邊際格子答案可能不同'
                         '\n（實測過 A,2 對 5 這種例子），下面的蒙地卡羅用的是你設定的實際副數，'
                         '\n而且會跟上面「策略表建議」那一行比較（不是跟精確解比較）：')
            lines.append('')
        except Exception as e:
            lines.append(f'（精確解計算失敗：{e}）')
        # 「計算中…」的進度已經有 self.scen_status 那個 StringVar 顯示了，
        # 這裡不用重複塞一行，不然結果跑完後會跟下面的蒙地卡羅結果黏在一起。
        self.scen_result_text.delete('1.0', 'end')
        self.scen_result_text.insert('end', '\n'.join(lines))

    def _run_scenario_bg(self, a):
        def cb(done, total):
            if self.scen_cancel.is_set():
                raise Cancelled()
            self.scen_q.put(('progress', done, total))
        try:
            results = scenario_mod.compare_actions(
                a['rules'], a['strat_code'], a['cards'], a['up'], a['hands'],
                seed=a['seed'], jobs=a['jobs'], progress=cb)
            self.scen_q.put(('done', results))
        except Cancelled:
            self.scen_q.put(('cancelled',))
        except scenario_mod.ScenarioError as e:
            self.scen_q.put(('error', str(e)))
        except Exception:
            self.scen_q.put(('error', traceback.format_exc()))

    def _cancel_scenario(self):
        self.scen_cancel.set()
        self.scen_status.set('取消中…')

    def _poll_scenario(self):
        alive = bool(self.scen_worker and self.scen_worker.is_alive())
        if self._drain_scenario():
            self.scen_after_id = None
            return
        if alive:
            self.scen_after_id = self.root.after(80, self._poll_scenario)
        else:
            self.scen_after_id = None
            self._scenario_idle()

    def _drain_scenario(self):
        try:
            while True:
                msg = self.scen_q.get_nowait()
                kind = msg[0]
                if kind == 'progress':
                    done, total = msg[1], msg[2]
                    self.scen_pbar['value'] = 1000 * done / total
                    self.scen_status.set(f'{done:,}/{total:,} ({done/total*100:.1f}%)')
                elif kind == 'done':
                    self._finish_scenario(msg[1])
                    return True
                elif kind == 'cancelled':
                    self.scen_status.set('已取消')
                    self._scenario_idle()
                    return True
                elif kind == 'error':
                    self.scen_status.set('發生錯誤')
                    self.scen_result_text.insert('end', '\n' + msg[1])
                    self._scenario_idle()
                    return True
        except queue.Empty:
            return False

    def _scenario_idle(self):
        self.btn_scen_run.state(['!disabled'])
        self.btn_scen_cancel.state(['disabled'])

    def _finish_scenario(self, results):
        self.scen_status.set('完成')
        self._scenario_idle()
        lines = ['蒙地卡羅（含分牌、用實際副數）— 由好到壞：', '']
        best = results[0]
        for act, name, ev, ci, sd in results:
            tag = '  ← 最佳' if (act, name) == (best[0], best[1]) else ''
            lines.append(f'  {name:<10} EV = {ev:+.5f} ± {ci:.5f}   SD={sd:.3f}{tag}')

        # 「跑出來的結論」跟「策略表現在建議的動作」不一樣，不代表策略表錯了——
        # 蒙地卡羅有抽樣誤差，差距要超過兩邊信賴區間合起來的範圍才算真的顯著，
        # 不然常常只是雜訊，加大手數才知道是不是真的。
        table_name = getattr(self, '_scen_table_action', None)
        lines.append('')
        if table_name is None:
            lines.append('（無法判斷策略表現在建議哪個動作，略過比較）')
        elif table_name == best[1]:
            lines.append(f'跟策略表一致：目前建議的「{table_name}」正是蒙地卡羅測出的最佳動作。')
        else:
            table_row = next((r for r in results if r[1] == table_name), None)
            if table_row is None:
                lines.append(f'策略表建議「{table_name}」，但那個動作在這個起手不合法'
                             '（表格本身可能沒套用目前的規則限制）。')
            else:
                _a2, _n2, table_ev, table_ci, _sd2 = table_row
                gap = best[2] - table_ev
                pooled = (best[3] ** 2 + table_ci ** 2) ** 0.5
                if gap > pooled:
                    lines.append(f'⚠ 策略表建議「{table_name}」，但蒙地卡羅測出「{best[1]}」'
                                 f'顯著更好：')
                    lines.append(f'   差距 {gap:+.5f}，大於兩邊誤差合計 ±{pooled:.5f}'
                                 '——這格可能真的該改。')
                    self.scen_result_text.insert('end', '\n'.join(lines) + '\n')
                    self._offer_table_update(results)
                    return
                else:
                    lines.append(f'策略表建議「{table_name}」，蒙地卡羅測出的最佳是「{best[1]}」，')
                    lines.append(f'但差距 {gap:+.5f} 沒有超過誤差範圍 ±{pooled:.5f}——')
                    lines.append('   目前手數還分不出來是真的更好還是抽樣雜訊，加大手數再看看。')
        self.scen_result_text.insert('end', '\n'.join(lines))

    def _offer_table_update(self, results):
        """顯著差異時，在結果文字後面嵌一顆按鈕，讓使用者自己決定要不要
        把這格寫回策略檔——不自動寫，統計顯著不代表使用者一定想改
        （可能只是想先看看，或手數還想再加大確認）。"""
        cell = getattr(self, '_scen_cell', None)
        if cell is None:
            self.scen_result_text.insert(
                'end', '（無法判斷這格對應策略表的哪個位置，略過更新選項）\n')
            return
        best_act = results[0][0]
        fallback_act = results[1][0] if len(results) > 1 else None
        token = strategy_mod.derive_token(best_act, fallback_act)
        target = strategy_mod.find_defining_file(cell['strat_code'], cell['kind'], cell['row'])

        if cell['overridden']:
            self.scen_result_text.insert(
                'end', f"\n注意：這格目前被某個 override（例如 H17 差異格）蓋過，"
                      f"更新 {target}.json 的基礎表不一定會改變實際結果，"
                      "可能要另外手動改那個 override。\n\n")

        btn = ttk.Button(
            self.scen_result_text, text=f'把這格更新成「{results[0][1]}」寫回 {target}.json',
            command=lambda: self._apply_table_update(cell, token, results[0][1]))
        self.scen_result_text.window_create('end', window=btn)
        self.scen_result_text.insert('end', '\n')

    def _apply_table_update(self, cell, token, action_name):
        loc = f"{cell['kind']}[{cell['row']}] 對 {CARD_VALUES[cell['col']]}"
        target = strategy_mod.find_defining_file(cell['strat_code'], cell['kind'], cell['row'])
        if not messagebox.askyesno(
                '更新策略表',
                f'把 {target}.json 的 {loc} 改成「{action_name}」？\n\n'
                '這會直接修改硬碟上的檔案，套用到所有繼承這份表的策略。'):
            return
        try:
            path = strategy_mod.write_cell(cell['strat_code'], cell['kind'], cell['row'],
                                           cell['col'], token)
        except strategy_mod.StrategyError as e:
            messagebox.showerror('更新失敗', str(e))
            return
        self._reload_strategies()
        self._render_strategy_tables()
        self.scen_result_text.insert('end', f'\n已更新 {path}，策略表已重新整理。\n')
        self.scen_result_text.see('end')

    # -------------------------------------------------------- 策略檔
    def _fill_strategies(self):
        for w in self.strat_box.winfo_children():
            w.destroy()
        try:
            items = load_strategy_list()
        except Exception as e:
            ttk.Label(self.strat_box, text=f'讀取策略檔失敗：{e}',
                      foreground='#dc2626', wraplength=250).pack(anchor='w')
            return
        if not items:
            ttk.Label(self.strat_box, text='strategies/ 底下沒有策略檔',
                      foreground='#dc2626').pack(anchor='w')
            return
        keep = {c: v.get() for c, v in self.strat_vars.items()}
        self.strat_vars = {}
        for label, code, desc in items:
            var = tk.BooleanVar(value=keep.get(code, code == 'basic'))
            self.strat_vars[code] = var
            cb = ttk.Checkbutton(self.strat_box, text=label, variable=var)
            cb.pack(anchor='w')
            if desc:
                self._tip(cb, desc)

    @staticmethod
    def _tip(widget, text):
        widget.configure(cursor='hand2')
        widget.bind('<Enter>', lambda e, t=text: widget.winfo_toplevel()
                    .title(f'Blackjack 模擬器 — {t}'))
        widget.bind('<Leave>', lambda e: widget.winfo_toplevel()
                    .title('Blackjack 模擬器'))

    def _reload_strategies(self):
        self._fill_strategies()
        self._fill_strategy_dropdowns()
        self.status.set(f'已重新載入策略檔（{len(self.strat_vars)} 個）')

    def _fill_strategy_dropdowns(self):
        """策略表檢視器／情境試算的策略下拉選單，跟主清單共用同一份策略檔。"""
        items = load_strategy_list()
        self._strategy_view_map = {label: code for label, code, _d in items}
        values = list(self._strategy_view_map)
        for cb in (self.cb_view_strategy, self.cb_scen_strategy):
            cur = cb.get()
            cb.configure(values=values)
            if cur in values:
                cb.set(cur)
            elif values:
                cb.current(0)

    # ------------------------------------------------------------ 規則連動
    def _on_double(self, _e=None):
        self.double_rule.set([DOUBLE_ANY2, DOUBLE_9_11, DOUBLE_10_11][self.cb_double.current()])

    def _on_sur(self, _e=None):
        self.surrender.set([SURRENDER_NONE, SURRENDER_LATE, SURRENDER_EARLY][self.cb_sur.current()])

    def _on_sweep(self, _e=None):
        self.sweep.set(SWEEP_LABELS[self.cb_sweep.current()][1])

    # -------------------------------------------------------- 賭場預設
    def _fill_presets(self):
        """從 presets/*.json 讀出可用的賭場規則預設。改完 JSON 按「重新載入」就會更新。"""
        try:
            items = presets_mod.describe()
        except Exception as e:
            messagebox.showerror('讀取預設檔失敗', str(e))
            return
        self._preset_map = {f'{disp}': name for name, disp, _d in items}
        cur = self.cb_preset.get()
        self.cb_preset.configure(values=list(self._preset_map))
        if cur in self._preset_map:
            self.cb_preset.set(cur)
        elif self._preset_map:
            self.cb_preset.current(0)

    def _apply_preset(self):
        label = self.cb_preset.get()
        name = self._preset_map.get(label)
        if not name:
            messagebox.showerror('套用預設失敗', '沒有選擇預設檔')
            return
        try:
            rules, _notes = presets_mod.load(name)
        except presets_mod.PresetError as e:
            messagebox.showerror('套用預設失敗', str(e))
            return

        # 順序有講究：peek 要先設好，surrender 才不會被 _sync_locks() 的
        # 「peek 開著時 early surrender 自動退回 late」邏輯中途打回去
        # ——那個檢查是靠 trace 即時觸發的，設定順序錯了會被中途攔截掉。
        self.decks.set(rules.num_decks)
        self.pen.set(rules.penetration * 100)
        self.csm.set(rules.continuous_shuffle)
        self.h17.set(rules.dealer_hits_soft_17)
        self.double_rule.set(rules.double_rule)
        self.cb_double.current({DOUBLE_ANY2: 0, DOUBLE_9_11: 1, DOUBLE_10_11: 2}[rules.double_rule])
        self.das.set(rules.double_after_split)
        self.peek.set(rules.dealer_peek)
        self.surrender.set(rules.surrender)
        self.cb_sur.current({SURRENDER_NONE: 0, SURRENDER_LATE: 1,
                             SURRENDER_EARLY: 2}[rules.surrender])
        self.sur_vs_ace.set(rules.surrender_vs_ace)
        self.obo.set(rules.dealer_bj_loss == LOSS_ORIGINAL)
        self.rsa.set(rules.resplit_aces)
        self.hsa.set(rules.hit_split_aces)
        self.bj65.set(abs(rules.blackjack_pays - 1.2) < 1e-9)

        self._sync_locks()
        self._sync_seed_lock()
        self.status.set(f'已套用預設：{label}')

    def _sync_seed_lock(self):
        # 沒勾「固定種子」的話，這個欄位只是顯示「上一次實際用了哪個種子」，
        # 不給手動編輯，避免使用者以為自己調了種子但其實下一次還是會被蓋掉。
        self.sp_seed.configure(state='normal' if self.fixed_seed.get() else 'disabled')

    def _sync_locks(self):
        notes = []
        if self.surrender.get() == SURRENDER_NONE:
            self.cb_sur_ace.state(['disabled'])
        else:
            self.cb_sur_ace.state(['!disabled'])
        if self.peek.get():
            self.cb_obo.state(['disabled'])
            if self.surrender.get() == SURRENDER_EARLY:
                self.surrender.set(SURRENDER_LATE)
                self.cb_sur.current(1)
            self.cb_sur.configure(values=['不可投降', 'late surrender'])
            notes.append('莊家 peek：BJ 在玩家動作前就結算，因此無 early surrender，'
                         'OBO 也無意義（本來就只輸原始注）。')
        else:
            self.cb_obo.state(['!disabled'])
            self.cb_sur.configure(values=['不可投降', 'late surrender', 'early surrender'])
            notes.append('no-peek：莊家動作結束才翻底牌。late surrender 碰到莊家 BJ 會輸全額，'
                         'early surrender 則不管莊家有沒有 BJ 都只輸一半。')
        if self.csm.get():
            self.sp_pen.state(['disabled'])
            notes.append('CSM：每一手都重新洗牌，沒有切牌點，penetration 被忽略；'
                         '算牌因此完全沒用（真數永遠貼近基準值，⊛ 算牌策略選了也沒意義）。')
        else:
            self.sp_pen.state(['!disabled'])
        self.lock_note.configure(text='\n'.join(notes))

    def _update_precision(self):
        # 這個欄位（self.hands）是「每次模擬要打幾手」，使用者直接輸入，
        # 不用自己乘除；總回合數 = 每次手數 x 獨立實驗次數，這裡算出來顯示，
        # 不是使用者要填的東西。
        try:
            per_session = int(float(self.hands.get()))
        except (ValueError, tk.TclError):
            return
        if per_session <= 0:
            return
        try:
            sessions = max(1, int(self.sessions.get()))
        except (ValueError, tk.TclError):
            sessions = 1
        total = per_session * sessions

        ci = 1.96 * 1.14 / (total ** 0.5) * 100
        msg = f'= 總共 {total:,} 手（{per_session:,} 手 × {sessions:,} 次獨立實驗）'
        msg += f'\n預期精度 ±{ci:.4f}%（95% CI）'
        if ci > 0.22:
            msg += '\n連 H17 這種 0.22% 的差異都分不出來'
        elif ci > 0.08:
            msg += '\n夠分辨 H17，但分不出投降/RSA 這種 0.08% 的差異'
        elif ci > 0.02:
            msg += '\n夠分辨大多數規則差異'
        else:
            msg += '\n足以分辨 0.02% 等級的細微差異'

        # 每次模擬的手數太少的話，資金曲線／分布圖幾乎只反映單手運氣，
        # 不是長期趨勢（標準差就是單手的 1.14，不是引擎有問題）。
        if per_session < 30:
            msg += (f'\n⚠ 每次模擬只有 {per_session} 手，資金曲線/分布圖幾乎'
                     '\n  只反映單手運氣，不是長期趨勢。')
        elif per_session < 1000:
            msg += f'\n⚠ 每次模擬只有 {per_session:,} 手，波動主要來自短期運氣。'
        self.precision.set(msg)

    # ------------------------------------------------------------ 執行
    def _collect(self):
        rules = Rules(
            num_decks=int(self.decks.get()),
            penetration=float(self.pen.get()) / 100.0,
            continuous_shuffle=self.csm.get(),
            dealer_hits_soft_17=self.h17.get(),
            double_rule=self.double_rule.get(),
            double_after_split=self.das.get(),
            surrender=self.surrender.get(),
            surrender_vs_ace=self.sur_vs_ace.get(),
            dealer_peek=self.peek.get(),
            resplit_aces=self.rsa.get(),
            hit_split_aces=self.hsa.get(),
            blackjack_pays=1.2 if self.bj65.get() else 1.5,
            dealer_bj_loss=LOSS_ORIGINAL if self.obo.get() else LOSS_ALL,
        )
        return normalize(rules)

    def _selected_strategies(self):
        picked = [c for c, v in self.strat_vars.items() if v.get()]
        if not self.adaptive.get():
            picked = [c + '-fixed' for c in picked]
        return picked

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            per_session = int(float(self.hands.get()))
        except ValueError:
            messagebox.showerror('輸入錯誤', '每次模擬手數要是數字')
            return
        try:
            sessions_n = int(self.sessions.get())
        except (ValueError, tk.TclError):
            messagebox.showerror('輸入錯誤', '獨立實驗次數要是數字')
            return
        hands = per_session * sessions_n     # run()/compare() 吃的是總回合數
        strategies = self._selected_strategies()
        if not strategies:
            messagebox.showerror('輸入錯誤', '至少要選一個策略')
            return
        rules, notes = self._collect()
        sweep = self.sweep.get()
        if sweep:
            configs = []
            for name, fn in SWEEPS[sweep]:
                r2, _ = normalize(fn(rules))
                configs.append((name, r2, strategies[0]))
        else:
            configs = [(c, rules, c) for c in strategies]

        # 沒勾「固定種子」就每次重新抽一個 —— 不然同樣的參數配上同一個種子
        # 會產生位元級完全相同的牌局，圖表自然每次都長一樣，容易被誤會成沒有重畫。
        # 抽完寫回欄位，讓使用者看得到這次實際用的是哪個種子（想重現就勾「固定」）。
        if not self.fixed_seed.get():
            self.seed.set(random.randrange(1, 2 ** 31 - 1))
        actual_seed = int(self.seed.get())

        self.cancel.clear()
        self.q = queue.Queue()
        self.btn_run.state(['disabled'])
        self.btn_cancel.state(['!disabled'])
        self.btn_save.state(['disabled'])
        self.pbar['value'] = 0
        self.status.set('準備中…')     # 別讓上一輪的「完成」留在畫面上
        head = [f'規則：{rules.label()}   penetration {rules.penetration:.0%}',
                f'模擬：每次 {per_session:,} 手 × {sessions_n:,} 次獨立實驗'
                f' × {len(configs)} 個設定 = {hands*len(configs):,} 回合',
                f'亂數種子：{actual_seed}'
                + ('（固定）' if self.fixed_seed.get() else '（本次自動產生，未固定）')]
        head += [f'  規則調整：{n}' for n in notes]
        self.txt.delete('1.0', 'end')
        self.txt.insert('end', '\n'.join(head) + '\n\n模擬中…\n')

        args = dict(configs=configs, hands=hands, sessions=sessions_n,
                    bet=float(self.bet.get()), seed=actual_seed,
                    jobs=int(self.jobs.get()), bankroll=float(self.bankroll.get()),
                    rules=rules)
        self.worker = threading.Thread(target=self._run_bg, args=(args,), daemon=True)
        self.worker.start()
        self._after_id = self.root.after(80, self._poll)

    def _run_bg(self, a):
        def cb(done, total):
            if self.cancel.is_set():
                raise Cancelled()
            self.q.put(('progress', done, total))
        try:
            configs = a['configs']
            if len(configs) == 1:
                label, rules, sname = configs[0]
                merged, per_s = run(rules, sname, a['hands'], a['sessions'],
                                    a['bet'], a['seed'], a['jobs'], 2000, label, cb)
                res = [(label, merged, per_s)]
            else:
                res = compare(configs, a['hands'], a['sessions'], a['bet'],
                              a['seed'], a['jobs'], 2000, cb)
            self.q.put(('done', res, a))
        except Cancelled:
            self.q.put(('cancelled',))
        except Exception:
            self.q.put(('error', traceback.format_exc()))

    def _request_cancel(self):
        self.cancel.set()
        self.status.set('取消中…（等目前的分段跑完）')

    def _drain(self):
        """把 queue 清空。收到結束訊息回傳 True。"""
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == 'progress':
                    done, total = msg[1], msg[2]
                    self.pbar['value'] = 1000 * done / total
                    self.status.set(f'{done:,} / {total:,} 回合 ({done/total*100:.1f}%)')
                elif kind == 'done':
                    self._finish(msg[1], msg[2])
                    return True
                elif kind == 'cancelled':
                    self.status.set('已取消')
                    self._idle()
                    return True
                elif kind == 'error':
                    self.status.set('發生錯誤')
                    self.txt.insert('end', '\n' + msg[1])
                    self._idle()
                    return True
        except queue.Empty:
            return False

    def _poll(self):
        # 順序很重要：先確認執行緒是否還活著，再清 queue。
        # 反過來的話，執行緒可能在「清完 queue」與「檢查存活」之間結束，
        # 最後一則訊息就再也沒人讀，狀態會永遠卡在「取消中…」。
        alive = bool(self.worker and self.worker.is_alive())
        if self._drain():
            self._after_id = None
            return
        if alive:
            self._after_id = self.root.after(80, self._poll)
        else:
            self._after_id = None
            self._idle()

    def _idle(self):
        self.btn_run.state(['!disabled'])
        self.btn_cancel.state(['disabled'])

    def _finish(self, results, a):
        self.results = results
        summaries = []
        lines = []
        for label, merged, _per in results:
            s = summarize(merged, a['bankroll'], a['bet'])
            s['label'] = label
            summaries.append(s)
            lines.append(f'── {label} ' + '─' * max(2, 60 - len(label)))
            lines.append(format_summary(s))
            lines.append('')
        if len(summaries) > 1:
            base = summaries[0]
            lines.append('=' * 70)
            lines.append('對比（共用亂數：所有設定拿到同一副洗好的牌）')
            lines.append(f"  {'設定':<26}{'賭場優勢':>13}{'95% CI':>11}{'相對差異':>12}")
            for s in summaries:
                d = (s['house_edge'] - base['house_edge']) * 100
                pooled = ((s['house_edge_ci95'] ** 2 + base['house_edge_ci95'] ** 2)
                          ** 0.5) * 100
                tag = '' if s is base else (f"{d:+.4f}%" +
                                            ('' if abs(d) > pooled else ' (分辨不出)'))
                lines.append(f"  {s['label']:<26}{s['house_edge']*100:>12.4f}%"
                             f"{s['house_edge_ci95']*100:>10.4f}%{tag:>16}")
        self.summaries = summaries
        self.txt.delete('1.0', 'end')
        self.txt.insert('end', '\n'.join(lines))

        ch, _ = _load_charts()
        groups = [(label, per) for label, _m, per in results]
        self._draw('bankroll', ch.bankroll_curves(groups))
        if a['sessions'] > 1:
            self._draw('dist', ch.result_distribution(groups))
        else:
            # 只有 1 個 session 就只有 1 個樣本點，畫不出分布，
            # 但畫面不能什麼都不做——不然使用者會以為沒有重新整理，
            # 其實是上一次跑的舊圖還留在畫面上。明講清楚並清掉舊圖。
            self._clear_chart(
                'dist', '獨立實驗次數目前是 1，只有一個樣本點，\n畫不出分布。'
                        '把「獨立實驗次數」調到 2 以上再跑一次。')
        if len(summaries) > 1:
            self._draw('cmp', ch.edge_comparison(summaries))
            self.nb.select(3)
        else:
            self.nb.select(1)
        self.status.set('完成')
        self.btn_save.state(['!disabled'])
        self._idle()

    def _draw(self, key, fig):
        _, (FigureCanvasTkAgg, NavigationToolbar2Tk) = _load_charts()
        slot = self.canvases[key]
        self._cancel_relayout(slot)
        if slot['canvas'] is not None:
            slot['canvas'].get_tk_widget().destroy()
            slot['toolbar'].destroy()
        canvas = FigureCanvasTkAgg(fig, master=slot['frame'])
        toolbar = NavigationToolbar2Tk(canvas, slot['frame'], pack_toolbar=False)
        toolbar.update()
        toolbar.pack(side='bottom', fill='x')
        canvas.get_tk_widget().pack(fill='both', expand=True)

        # tight_layout() 算的邊界是照 charts.py 建圖時的預設尺寸（例如寬 10 吋）。
        # 但這個面板通常比那個尺寸窄，圖被壓縮嵌進來時邊界沒有重算，
        # 左側的座標數字／刻度就會被擠到畫布外面看不到。resize 時要重算一次。
        # <Configure> 在拖曳視窗時會連續觸發很多次，用 after 做防手震，
        # 累積 120ms 沒有新事件才真的重畫一次，不然拖窗會頓。
        # job id 存在 slot 裡（而不是這裡的區域變數），這樣重畫圖表或關視窗時
        # 才找得到還沒觸發的排程去取消掉，不然視窗關閉後排程觸發會對著
        # 已經銷毀的 widget 操作而噴錯。
        def relayout():
            slot['job_id'] = None
            try:
                fig.tight_layout()
            except Exception:
                pass
            canvas.draw_idle()

        def on_configure(_e=None):
            self._cancel_relayout(slot)
            slot['job_id'] = self.root.after(120, relayout)

        # add='+'：matplotlib 自己在 FigureCanvasTk.__init__ 就對同一個 widget
        # 綁過一次 <Configure> -> self.resize（負責讓畫布的實際像素尺寸跟著視窗
        # 縮放）。如果這裡不加 add='+'，bind() 預設會直接取代掉那個綁定，
        # 圖形內容就停在建立當時的尺寸，視窗變寬時右側會整塊空白。
        canvas.get_tk_widget().bind('<Configure>', on_configure, add='+')
        fig.tight_layout()
        canvas.draw()
        slot.update(canvas=canvas, toolbar=toolbar, fig=fig)

    def _clear_chart(self, key, message):
        """把某個圖表分頁清空並顯示說明文字（用在畫不出圖的情況，
        避免留著上一次跑的舊圖讓人誤以為畫面沒有更新）。"""
        slot = self.canvases[key]
        self._cancel_relayout(slot)
        if slot['canvas'] is not None:
            slot['canvas'].get_tk_widget().destroy()
            slot['toolbar'].destroy()
        for w in slot['frame'].winfo_children():
            w.destroy()
        slot.update(canvas=None, toolbar=None, fig=None)
        ttk.Label(slot['frame'], text=message, foreground='#64748b',
                  justify='center').pack(expand=True)

    def _cancel_relayout(self, slot):
        if slot['job_id'] is not None:
            self.root.after_cancel(slot['job_id'])
            slot['job_id'] = None

    def _on_close(self):
        self.cancel.set()
        self.scen_cancel.set()
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)   # 別讓 callback 在視窗銷毀後才觸發
            self._after_id = None
        if self.scen_after_id is not None:
            self.root.after_cancel(self.scen_after_id)
            self.scen_after_id = None
        for slot in self.canvases.values():
            self._cancel_relayout(slot)
        self.root.destroy()

    def _export(self):
        d = filedialog.askdirectory(title='選擇輸出資料夾')
        if not d:
            return
        saved = []
        for key, name in (('bankroll', 'bankroll.png'), ('dist', 'distribution.png'),
                          ('cmp', 'comparison.png')):
            fig = self.canvases[key]['fig']
            if fig is not None:
                fig.savefig(os.path.join(d, name), dpi=140)
                saved.append(name)
        with open(os.path.join(d, 'summary.txt'), 'w') as f:
            f.write(self.txt.get('1.0', 'end'))
        saved.append('summary.txt')
        messagebox.showinfo('已匯出', '\n'.join(saved))


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use('aqua')
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
