#!/usr/bin/env python3
"""Blackjack simulator -- Tkinter GUI.

Launch: python3 ui.py

Design notes
* The simulation runs on a background thread (which itself drives a
  multi-core process pool); the main thread only handles drawing, so the
  window stays responsive and cancellable even while running a hundred
  million hands.
* Progress is relayed back to the main thread through a queue, polled via
  after() -- Tk objects are never touched from another thread.
* Rule conflicts (peek / surrender / OBO) lock the corresponding controls
  in real time.
* The "hands per run" field (self.hands) here is the per-session hand
  count, so the user can type it directly without doing mental math;
  core.runner.run()/compare() take a total round count (summed across all
  sessions), so it gets multiplied by sessions before being passed in.
  This differs from cli.py's --hands semantics -- there, the user gives
  the total round count directly, with no such conversion. Don't confuse
  the two.
"""
import os
import queue
import random
import threading
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# matplotlib and charts are deliberately loaded lazily, only once a chart
# actually needs to be drawn. The multi-core simulation spawns child
# processes, which re-import this file; importing matplotlib at module
# level would add roughly a second of startup cost to every worker.
_charts = None
_tkagg = None


def _load_charts():
    global _charts, _tkagg
    if _charts is None:
        import charts as _c                 # sets up the Agg backend
        import matplotlib
        matplotlib.use('TkAgg')             # switch to the Tk backend so it can embed in the window
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

# Strategy table cell display colors, matching common strategy-card conventions.
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
    """A fake shoe for "what does the strategy table currently recommend,"
    just enough to satisfy decide()'s interface.

    Only ever queried once, at the moment of the first two cards, when a
    counting strategy usually hasn't started counting yet (true count is
    treated as 0) -- this is only meant to get "the recommended action
    when not counting" as a comparison baseline, not to simulate a full
    counting scenario.
    """
    running_count = 0
    true_count = 0.0

    def start_round(self):
        pass


# how many hands to play per run (not the total round count -- that's
# computed as "hands per run x number of runs," the user never has to
# multiply it themselves).
HAND_PRESETS = [('1K', 1_000), ('10K', 10_000), ('100K', 100_000),
                ('1M', 1_000_000), ('10M', 10_000_000),
                ('100M', 100_000_000)]

def load_strategy_list():
    """Read the available strategies from strategies/*.json. Edit the JSON
    and press "reload" to pick up changes."""
    return [(f"{'* ' if counting else ''}{name}", name, desc)
            for name, desc, counting in strategy_mod.describe()]

SWEEP_LABELS = [('None (single config)', ''),
                ('Deck count', 'decks'),
                ('CSM (continuous shuffler)', 'csm'),
                ('Surrender vs. dealer ace', 'surrender-ace'),
                ('S17 / H17', 'h17'),
                ('DAS on/off', 'das'),
                ('Surrender rules', 'surrender'),
                ('Peek / no-peek / OBO', 'peek'),
                ('Splitting aces rules', 'rsa'),
                ('When doubling is allowed', 'double'),
                ('BJ payout 3:2 / 6:5', 'bj')]


class App:
    def __init__(self, root):
        self.root = root
        root.title('Blackjack Simulator')
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        # size the window to the screen's usable space, minus a rough
        # allowance for the menu bar/dock; don't assume the same fixed
        # height on every screen -- laptop screens are often shorter than this.
        root.geometry(f'{min(1360, sw - 60)}x{min(960, sh - 120)}')
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

    # ---------------------------------------------------------------- vars
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

        v.status = tk.StringVar(value='Ready')
        v.precision = tk.StringVar(value='')
        for var in (v.peek, v.surrender, v.rsa, v.csm):
            var.trace_add('write', lambda *_: self._sync_locks())
        for var in (v.hands, v.sessions):
            var.trace_add('write', lambda *_: self._update_precision())

    # ---------------------------------------------------------------- layout
    def _build_layout(self):
        # Once tried wrapping ttk widgets in a tk.Canvas to make the left
        # column scrollable, but under macOS's Aqua theme those widgets'
        # coordinates/sizes measured correctly while nothing was actually
        # drawn on screen (ttk renders natively on macOS, which doesn't
        # mesh with Canvas's compositing pipeline). Switched to a
        # non-scrolling, direct layout instead; buttons like "start
        # simulation" are still pinned to the bottom of the left column
        # (packed in first, so they always keep their space) and never get
        # pushed out of view by a long rule list.
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill='both', expand=True)
        left = ttk.Frame(outer, width=380)
        left.pack(side='left', fill='y', padx=(0, 10))
        right = ttk.Frame(outer)
        right.pack(side='left', fill='both', expand=True)

        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        run_bar = ttk.Frame(left)
        run_bar.pack(side='bottom', fill='x')

        # Rules, simulation settings, and strategy stacked together add up
        # to well over 1000px of height -- a short screen (e.g. 1280x720)
        # simply can't fit it, and even a two-column layout wouldn't help
        # (the tallest column alone still needs ~670px). Switched to
        # tabs instead: only one page is visible at a time, so the needed
        # height is just the tallest single page (about 400px), which fits
        # on any screen with no scrolling required.
        nb = ttk.Notebook(left)
        nb.pack(side='top', fill='both', expand=True)
        tab_rules = ttk.Frame(nb, padding=(4, 3))
        tab_sim = ttk.Frame(nb, padding=(4, 3))
        tab_strategy = ttk.Frame(nb, padding=(4, 3))
        nb.add(tab_rules, text='Casino Rules')
        nb.add(tab_sim, text='Simulation')
        nb.add(tab_strategy, text='Strategy / Compare')

        self._build_rules(tab_rules)
        self._build_sim(tab_sim)
        self._build_strategy(tab_strategy)
        self._build_run(run_bar)
        self._build_output(right)

    def _row(self, parent, r, text):
        ttk.Label(parent, text=text).grid(row=r, column=0, sticky='w', pady=2)

    def _build_rules(self, parent):
        preset_box = ttk.LabelFrame(parent, text=' Casino Presets (One-Click) ', padding=5)
        preset_box.pack(fill='x', pady=(0, 4))
        row = ttk.Frame(preset_box)
        row.pack(fill='x')
        self.cb_preset = ttk.Combobox(row, width=24, state='readonly')
        self.cb_preset.pack(side='left')
        ttk.Button(row, text='Apply', command=self._apply_preset).pack(side='left', padx=6)
        ttk.Button(row, text='Reload', command=self._fill_presets).pack(side='left')
        self._fill_presets()

        f = ttk.LabelFrame(parent, text=' Casino Rules ', padding=5)
        f.pack(fill='x')
        r = 0
        self._row(f, r, 'Number of decks')
        ttk.Spinbox(f, from_=DECK_MIN, to=DECK_MAX, width=8,
                    textvariable=self.decks).grid(row=r, column=1, sticky='w')
        r += 1
        self._row(f, r, 'Penetration %')
        self.sp_pen = ttk.Spinbox(f, from_=30, to=95, increment=5, width=8,
                                  textvariable=self.pen)
        self.sp_pen.grid(row=r, column=1, sticky='w')
        r += 1
        self.cb_csm = ttk.Checkbutton(f, text='Continuous shuffling machine (CSM)',
                                      variable=self.csm)
        self.cb_csm.grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        self._row(f, r, 'Dealer soft 17')
        box = ttk.Frame(f); box.grid(row=r, column=1, sticky='w')
        ttk.Radiobutton(box, text='S17 (stand)', variable=self.h17, value=False).pack(side='left')
        ttk.Radiobutton(box, text='H17 (hit)', variable=self.h17, value=True).pack(side='left')
        r += 1
        self._row(f, r, 'When doubling is allowed')
        self.cb_double = ttk.Combobox(f, width=16, state='readonly',
                                      values=['Any two cards', 'Only 9/10/11', 'Only 10/11'])
        self.cb_double.current(0)
        self.cb_double.bind('<<ComboboxSelected>>', self._on_double)
        self.cb_double.grid(row=r, column=1, sticky='w')
        r += 1
        ttk.Checkbutton(f, text='Double after split (DAS)', variable=self.das
                        ).grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        # peek is placed before surrender: with peek on, early surrender
        # isn't even an option (BJ is already settled before the player
        # acts) -- the peek toggle needs to be understood first, or the
        # surrender dropdown's varying number of options (two vs. three)
        # won't make sense.
        self.cb_peek = ttk.Checkbutton(f, text='Dealer checks hole card (peek)', variable=self.peek)
        self.cb_peek.grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        self.cb_obo = ttk.Checkbutton(f, text='OBO (lose original bet only on dealer BJ)',
                                      variable=self.obo)
        self.cb_obo.grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        self._row(f, r, 'Surrender')
        self.cb_sur = ttk.Combobox(f, width=18, state='readonly',
                                   values=['No surrender', 'Late surrender', 'Early surrender'])
        self.cb_sur.current(1)
        self.cb_sur.bind('<<ComboboxSelected>>', self._on_sur)
        self.cb_sur.grid(row=r, column=1, sticky='w')
        r += 1
        self.cb_sur_ace = ttk.Checkbutton(f, text='Surrender also allowed vs. dealer ace',
                                          variable=self.sur_vs_ace)
        self.cb_sur_ace.grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        aces_box = ttk.Frame(f)
        aces_box.grid(row=r, column=0, columnspan=2, sticky='w')
        self.cb_rsa = ttk.Checkbutton(aces_box, text='Resplit aces (RSA)', variable=self.rsa)
        self.cb_rsa.pack(side='left')
        self.cb_hsa = ttk.Checkbutton(aces_box, text='Hit split aces', variable=self.hsa)
        self.cb_hsa.pack(side='left', padx=(14, 0))
        r += 1
        ttk.Checkbutton(f, text='BJ pays only 6:5 (otherwise 3:2)', variable=self.bj65
                        ).grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        self.lock_note = ttk.Label(f, text='', foreground='#b45309', wraplength=340,
                                   justify='left')
        self.lock_note.grid(row=r, column=0, columnspan=2, sticky='w', pady=(2, 0))

    def _build_sim(self, parent):
        f = ttk.LabelFrame(parent, text=' Simulation Settings ', padding=8)
        f.pack(fill='x', pady=(8, 0))
        r = 0
        self._row(f, r, 'Hands per run')
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
        for text, var, kw in (('Independent runs', self.sessions, dict(from_=1, to=64)),
                              ('Base bet', self.bet, dict(from_=1, to=1000)),
                              ('Bankroll (units)', self.bankroll, dict(from_=10, to=100000, increment=50)),
                              ('Parallel cores', self.jobs, dict(from_=1, to=64))):
            self._row(f, r, text)
            ttk.Spinbox(f, width=12, textvariable=var, **kw).grid(row=r, column=1, sticky='w')
            r += 1
        self._row(f, r, 'Random seed')
        self.sp_seed = ttk.Spinbox(f, width=12, textvariable=self.seed,
                                   from_=0, to=10 ** 9, state='disabled')
        self.sp_seed.grid(row=r, column=1, sticky='w')
        r += 1
        self.cb_fixed_seed = ttk.Checkbutton(
            f, text='Fixed seed (reproducible)', variable=self.fixed_seed,
            command=self._sync_seed_lock)
        self.cb_fixed_seed.grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1
        ttk.Label(f, text='By default a new seed is drawn every time you click\n'
                          '"start simulation," so results differ each run; a fixed\n'
                          'seed replays the same shoe every time.',
                  foreground='#64748b', justify='left'
                  ).grid(row=r, column=0, columnspan=2, sticky='w')
        r += 1

    def _build_strategy(self, parent):
        f2 = ttk.LabelFrame(parent, text=' Strategies (from strategies/*.json) ', padding=8)
        f2.pack(fill='x')
        self.strat_box = ttk.Frame(f2)
        self.strat_box.pack(fill='x')
        self._fill_strategies()
        bar = ttk.Frame(f2)
        bar.pack(fill='x', pady=(4, 0))
        ttk.Button(bar, text='Reload strategy files', command=self._reload_strategies
                   ).pack(side='left')
        ttk.Label(bar, text='  * = card counting', foreground='#64748b').pack(side='left')
        ttk.Separator(f2, orient='horizontal').pack(fill='x', pady=5)
        ttk.Checkbutton(f2, text='Strategy table adapts to the rules', variable=self.adaptive).pack(anchor='w')
        ttk.Label(f2, text='Unchecked = ignore the overrides in the strategy file,\nto see the cost of using the wrong strategy table',
                  foreground='#64748b', justify='left').pack(anchor='w')

        f3 = ttk.LabelFrame(parent, text=' Rule Comparison ', padding=8)
        f3.pack(fill='x', pady=(8, 0))
        self.cb_sweep = ttk.Combobox(f3, width=26, state='readonly',
                                     values=[n for n, _ in SWEEP_LABELS])
        self.cb_sweep.current(0)
        self.cb_sweep.bind('<<ComboboxSelected>>', self._on_sweep)
        self.cb_sweep.pack(anchor='w')
        ttk.Label(f3, text='When selected, sweeps that dimension using the same shoe\n(Common Random Numbers, making differences easier to resolve)',
                  foreground='#64748b', justify='left').pack(anchor='w')

    def _build_run(self, parent):
        f = ttk.Frame(parent, padding=(0, 10))
        f.pack(fill='x')
        # plain tk.Button with default='active' (not ttk.Button) renders as
        # macOS's native prominent blue button, making the primary action
        # visually stand out from Cancel/Export -- ttk's Aqua theme has no
        # supported way to recolor a ttk.Button.
        self.btn_run = tk.Button(f, text='Start Simulation', command=self._start,
                                 default='active', highlightthickness=0)
        self.btn_run.pack(side='left')
        self.btn_cancel = ttk.Button(f, text='Cancel', command=self._request_cancel,
                                     state='disabled')
        self.btn_cancel.pack(side='left', padx=6)
        self.btn_save = ttk.Button(f, text='Export Charts', command=self._export,
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
        self.nb.add(self.txt, text='Summary')
        for key, title in (('bankroll', 'Bankroll'), ('dist', 'Distribution'), ('cmp', 'Compare')):
            frame = ttk.Frame(self.nb)
            self.nb.add(frame, text=title)
            self.canvases[key] = {'frame': frame, 'canvas': None, 'toolbar': None,
                                  'fig': None, 'job_id': None}
        self._build_strategy_view(self._add_tab('Strategy'))
        self._build_scenario_view(self._add_tab('Scenario'))

    def _add_tab(self, title):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text=title)
        return frame

    # -------------------------------------------------------- strategy table viewer
    def _build_strategy_view(self, parent):
        bar = ttk.Frame(parent, padding=6)
        bar.pack(fill='x')
        ttk.Label(bar, text='Strategy:').pack(side='left')
        self.cb_view_strategy = ttk.Combobox(bar, width=26, state='readonly')
        self.cb_view_strategy.pack(side='left', padx=(0, 8))
        self.cb_view_strategy.bind('<<ComboboxSelected>>',
                                   lambda e: self._render_strategy_tables())
        ttk.Button(bar, text='Refresh (apply current rules)',
                   command=self._render_strategy_tables).pack(side='left')
        self.strategy_view_note = ttk.Label(bar, text='', foreground='#64748b',
                                            wraplength=520, justify='left')
        self.strategy_view_note.pack(side='left', padx=10)

        legend = ttk.Frame(parent, padding=(6, 0, 6, 4))
        legend.pack(fill='x')
        for label, name in (('H', 'Hit'), ('S', 'Stand'), ('D', 'Double'),
                            ('P', 'Split'), ('Rh', 'Surrender'), ('N', "Don't split / fall through")):
            tk.Label(legend, text='  ', bg=CELL_COLORS[label], relief='solid', bd=1
                     ).pack(side='left', padx=(0, 3))
            ttk.Label(legend, text=name).pack(side='left', padx=(0, 12))

        canvas, holder = self._make_tk_scrollable(parent)
        canvas.pack(fill='both', expand=True)
        self.strategy_view_holder = holder

    def _make_tk_scrollable(self, parent):
        """Build a vertically scrollable area using pure tk (not ttk) widgets.

        Under macOS's Aqua theme, ttk widgets embedded in a tk.Canvas
        don't render at all -- their coordinates/sizes measure correctly,
        but nothing appears on screen (ttk renders natively on macOS,
        which doesn't mesh with Canvas's compositing pipeline; ran into
        this same issue earlier with the left rules column). Pure tk
        widgets use Tk's own cross-platform rendering and don't have this
        problem, so the strategy table cells here are deliberately all
        tk.Frame/tk.Label, not the ttk equivalents.
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
        """Convert a cell's column index 0..9 (mapping to 2..10,A) into the
        actual dealer upcard value."""
        return 1 if col == 9 else col + 2

    def _draw_total_grid(self, holder, title, table, totals, rules, is_soft, strat):
        """totals are drawn from low to high (smaller totals on top, larger below).

        is_soft determines how the displayed total is converted back to
        the "raw total without the soft-ace bonus" that
        rules.double_allowed_on() needs -- soft 18 (A,7) displays as 18,
        which converts back to 8.

        strat is passed to effective_cell_label() so the display matches
        real gameplay under SURRENDER_EARLY (early_surrender()'s
        es_vs_ace/es_vs_ten pre-check overrides the hard table itself --
        see core/strategy.py for details).
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
            hard_total = 2 * key           # A,A's raw total is 1+1=2
            is_soft = key == 1             # only A,A counts as soft (a soft 12 if not split)
            tk.Label(grid, text=f'{name},{name}', bg='white', fg='black', width=5,
                     font=('Menlo', 10, 'bold')
                     ).grid(row=r, column=0, sticky='e', padx=(0, 4))
            for c in range(10):
                cell = row[c] if row else None
                label = strategy_mod.effective_cell_label(
                    cell, self._col_to_up(c), rules, hard_total, is_soft, strat)
                self._cell_widget(grid, label, r, c + 1)

    def _draw_deviation_list(self, holder, strat):
        tk.Label(holder, text='Counting deviations (cells that change with true/running count)', bg='white',
                 fg='black', font=('Menlo', 11, 'bold')
                 ).pack(anchor='w', padx=8, pady=(10, 2))
        if not strat.deviations:
            tk.Label(holder, text='(this strategy defines no deviations)', bg='white', fg='#64748b'
                     ).pack(anchor='w', padx=8, pady=(0, 12))
            return
        for (tname, row, col), rule_list in sorted(strat.deviations.items()):
            dealer = CARD_VALUES[col]
            row_label = ({1: 'A', 10: '10'}.get(row, row) if tname == 'pair' else row)
            for lo, hi, cell in rule_list:
                act = strategy_mod.cell_label(cell)
                if hi == float('inf'):
                    cond = f"count >= {lo:g}"
                elif lo == float('-inf'):
                    cond = f"count <= {hi:g}"
                else:
                    cond = f"{lo:g} <= count <= {hi:g}"
                tk.Label(holder, bg='white', fg='black', anchor='w', justify='left',
                         text=f"  {tname} {row_label} vs. {dealer}: becomes {act} when {cond}",
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
            tk.Label(self.strategy_view_holder, text=f'Load failed: {e}', bg='white',
                     fg='#dc2626').pack(anchor='w', padx=8, pady=8)
            return
        note = rules.label()
        if strat.applied_overrides:
            note += ' | applied: ' + '; '.join(strat.applied_overrides)
        self.strategy_view_note.configure(text=note)

        self._draw_total_grid(self.strategy_view_holder, 'Hard Totals',
                              strat.hard, range(4, 21), rules, is_soft=False, strat=strat)
        self._draw_total_grid(self.strategy_view_holder, 'Soft Totals',
                              strat.soft, range(12, 21), rules, is_soft=True, strat=strat)
        self._draw_pair_grid(self.strategy_view_holder, 'Pairs', strat.pair, rules, strat)
        if strat.count_tags:
            self._draw_deviation_list(self.strategy_view_holder, strat)

    # -------------------------------------------------------- scenario tester
    def _build_scenario_view(self, parent):
        bar = ttk.Frame(parent, padding=8)
        bar.pack(fill='x')

        ttk.Label(bar, text="Player's first card").grid(row=0, column=0, sticky='w')
        self.cb_p1 = ttk.Combobox(bar, width=5, state='readonly', values=CARD_VALUES)
        self.cb_p1.current(6)             # default: 8
        self.cb_p1.grid(row=0, column=1, padx=(2, 12))

        ttk.Label(bar, text="Player's second card").grid(row=0, column=2, sticky='w')
        self.cb_p2 = ttk.Combobox(bar, width=5, state='readonly', values=CARD_VALUES)
        self.cb_p2.current(6)             # default: 8 (8,8 is the most commonly discussed split scenario)
        self.cb_p2.grid(row=0, column=3, padx=(2, 12))

        ttk.Label(bar, text="Dealer's upcard").grid(row=0, column=4, sticky='w')
        self.cb_dup = ttk.Combobox(bar, width=5, state='readonly', values=CARD_VALUES)
        self.cb_dup.current(8)            # default: 10
        self.cb_dup.grid(row=0, column=5, padx=(2, 12))

        ttk.Label(bar, text='Hands per action').grid(row=1, column=0, sticky='w', pady=(8, 0))
        self.scen_hands = tk.StringVar(value='1000000')
        ttk.Entry(bar, textvariable=self.scen_hands, width=12
                  ).grid(row=1, column=1, columnspan=2, sticky='w', pady=(8, 0))

        ttk.Label(bar, text='Follow-up strategy').grid(row=1, column=3, sticky='w', pady=(8, 0))
        self.cb_scen_strategy = ttk.Combobox(bar, width=24, state='readonly')
        self.cb_scen_strategy.grid(row=1, column=4, columnspan=2, sticky='w', pady=(8, 0))
        ttk.Label(bar, text='(played per this strategy after the first move)', foreground='#64748b'
                  ).grid(row=1, column=6, sticky='w', pady=(8, 0), padx=(6, 0))

        run_row = ttk.Frame(parent, padding=(8, 0, 8, 8))
        run_row.pack(fill='x')
        self.btn_scen_run = tk.Button(run_row, text='Compute', command=self._start_scenario,
                                      default='active', highlightthickness=0)
        self.btn_scen_run.pack(side='left')
        self.btn_scen_cancel = ttk.Button(run_row, text='Cancel', command=self._cancel_scenario,
                                          state='disabled')
        self.btn_scen_cancel.pack(side='left', padx=6)
        self.scen_pbar = ttk.Progressbar(run_row, mode='determinate', maximum=1000, length=220)
        self.scen_pbar.pack(side='left', padx=6)
        self.scen_status = tk.StringVar(value='Ready')
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
            messagebox.showerror('Input Error', str(e))
            return
        try:
            hands = int(float(self.scen_hands.get()))
        except ValueError:
            messagebox.showerror('Input Error', 'Hands must be a number')
            return
        rules, _notes = self._collect()
        strat_label = self.cb_scen_strategy.get()
        strat_code = getattr(self, '_strategy_view_map', {}).get(strat_label, strat_label)

        legal = scenario_mod.legal_first_actions((c1, c2), up, rules)
        if legal == 0:
            messagebox.showerror('Input Error', 'This opening has no legal actions to compare under the current rules')
            return

        try:
            strat = strategy_mod.make(strat_code, rules)
            hand = Hand([c1, c2], 1.0)
            # decide() doesn't know about early surrender: that's a
            # separate pre-check (early_surrender()) run "before decide()"
            # in core.engine.play_round -- this has to follow the same
            # order, or a SURRENDER_EARLY table would display an action
            # that isn't actually a surrender.
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

        # remember which cell this run corresponds to, so once results are
        # in, the "update strategy table" button knows which file/position
        # to edit; also check whether this cell is currently overridden by
        # something (e.g. an H17 difference cell) -- if so, updating the
        # base table won't actually take effect, and that needs to be
        # flagged up front.
        self._scen_cell = self._resolve_scenario_cell(c1, c2, up, strat_code, rules)

        # shares the same "fixed seed" toggle as the main flow: if
        # unchecked, a fresh seed needs to be drawn here too, or every
        # "compute" click would reuse the same seed and shoe, producing
        # identical results every time -- easy to mistake for "this cell's
        # answer is very stable" when it's really just not resampling at all.
        if not self.fixed_seed.get():
            self.seed.set(random.randrange(1, 2 ** 31 - 1))
        actual_seed = int(self.seed.get())

        self._show_solver_reference(c1, c2, up, rules, actual_seed)

        self.scen_cancel.clear()
        self.scen_q = queue.Queue()
        self.btn_scen_run.configure(state='disabled')
        self.btn_scen_cancel.state(['!disabled'])
        self.scen_pbar['value'] = 0
        self.scen_status.set('Computing...')

        args = dict(rules=rules, strat_code=strat_code, cards=(c1, c2), up=up,
                    hands=hands, jobs=int(self.jobs.get()), seed=actual_seed)
        self.scen_worker = threading.Thread(target=self._run_scenario_bg,
                                            args=(args,), daemon=True)
        self.scen_worker.start()
        self.scen_after_id = self.root.after(80, self._poll_scenario)

    @staticmethod
    def _resolve_scenario_cell(c1, c2, up, strat_code, rules):
        """Work out which strategy table cell (kind/row/col) this scenario
        corresponds to, and check whether it's currently overridden by
        something (an H17 difference cell, etc.).

        Returns a dict, or None (strategy load failed, or the cell simply
        isn't in the table at all).
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
        """Show the infinite-deck exact solution first (computed instantly,
        splits excluded) as a quick reference; the Monte Carlo result
        (includes splits, uses the actual deck count) is appended once it
        finishes."""
        lines = [f'Player {self._card_name(c1)},{self._card_name(c2)} vs. dealer '
                f'{self._card_name(up)}   ({rules.label()})',
                f'Random seed: {seed}' + (' (fixed)' if self.fixed_seed.get() else ' (auto-generated this run, not fixed)'),
                '']
        # explicitly call out "what the strategy table currently
        # recommends," or users can easily mistake the "exact solution"
        # below for the strategy table's answer -- the exact solution is
        # an independently computed mathematical result every time,
        # consulting no JSON at all, and won't change if the strategy file
        # is edited; only this line actually reads the strategy file, and
        # only updates after "update strategy table" is pressed and this
        # is recomputed.
        table_name = getattr(self, '_scen_table_action', None)
        lines.append(f'The current strategy table ({self.cb_scen_strategy.get()}) recommends: '
                     + (table_name if table_name else '(unable to determine)'))
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
            lines.append('Exact solution (infinite deck, splits excluded, computed instantly, zero error;'
                         '\nunrelated to the strategy table above -- this consults no JSON at all, it\'s'
                         '\nrecomputed mathematically every time, and won\'t change if the strategy file'
                         '\ndoes, since it was never derived from the strategy file to begin with):')
            for k in sorted(evs, key=evs.get, reverse=True):
                tag = '  <- exact-solution best (splits not compared)' if k == letter else ''
                lines.append(f'  {k:<3} EV = {evs[k]:+.5f}{tag}')
            if is_pair:
                lines.append('  (this hand is a pair, and the exact solution has no split option --'
                             '\n   splits bring in DAS/RSA/resplitting complexity that makes an exact'
                             '\n   solution too expensive, so splits can only be compared via the Monte'
                             '\n   Carlo below)')
            lines.append('')
            lines.append('This exact solution assumes an infinite deck; marginal cells can have a'
                         '\ndifferent answer at your real (smaller) deck count (measured, e.g., for A,2'
                         '\nvs. 5). The Monte Carlo below uses your actual configured deck count, and'
                         '\nwill be compared against the "strategy table recommends" line above'
                         '\n(not against the exact solution):')
            lines.append('')
        except Exception as e:
            lines.append(f'(exact solution computation failed: {e})')
        # progress is already shown via the self.scen_status StringVar as
        # "Computing...", no need to repeat that here, or it would run
        # together with the Monte Carlo results once they land.
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
        self.scen_status.set('Cancelling...')

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
                    self.scen_status.set('Cancelled')
                    self._scenario_idle()
                    return True
                elif kind == 'error':
                    self.scen_status.set('Error')
                    self.scen_result_text.insert('end', '\n' + msg[1])
                    self._scenario_idle()
                    return True
        except queue.Empty:
            return False

    def _scenario_idle(self):
        self.btn_scen_run.configure(state='normal')
        self.btn_scen_cancel.state(['disabled'])

    def _finish_scenario(self, results):
        self.scen_status.set('Done')
        self._scenario_idle()
        lines = ['Monte Carlo (splits included, actual deck count) -- best to worst:', '']
        best = results[0]
        for act, name, ev, ci, sd in results:
            tag = '  <- best' if (act, name) == (best[0], best[1]) else ''
            lines.append(f'  {name:<10} EV = {ev:+.5f} +/- {ci:.5f}   SD={sd:.3f}{tag}')

        # "what the simulation concluded" differing from "what the
        # strategy table currently recommends" doesn't necessarily mean
        # the table is wrong -- Monte Carlo carries sampling error, and
        # the gap needs to exceed both sides' combined confidence
        # intervals to count as genuinely significant; otherwise it's
        # often just noise, and more hands are needed to tell.
        table_name = getattr(self, '_scen_table_action', None)
        lines.append('')
        if table_name is None:
            lines.append('(unable to determine what the strategy table currently recommends, skipping comparison)')
        elif table_name == best[1]:
            lines.append(f'Matches the strategy table: the current recommendation of "{table_name}" is exactly what Monte Carlo found best.')
        else:
            table_row = next((r for r in results if r[1] == table_name), None)
            if table_row is None:
                lines.append(f'The strategy table recommends "{table_name}," but that action isn\'t legal for this opening'
                             ' (the table itself may not account for the current rule restrictions).')
            else:
                _a2, _n2, table_ev, table_ci, _sd2 = table_row
                gap = best[2] - table_ev
                pooled = (best[3] ** 2 + table_ci ** 2) ** 0.5
                if gap > pooled:
                    lines.append(f'! The strategy table recommends "{table_name}," but Monte Carlo found "{best[1]}"'
                                 f' significantly better:')
                    lines.append(f'   Gap of {gap:+.5f}, larger than the combined margin of +/-{pooled:.5f}'
                                 ' -- this cell might genuinely need to change.')
                    self.scen_result_text.insert('end', '\n'.join(lines) + '\n')
                    self._offer_table_update(results)
                    return
                else:
                    lines.append(f'The strategy table recommends "{table_name}," and Monte Carlo found "{best[1]}" as the best,')
                    lines.append(f'but the gap of {gap:+.5f} doesn\'t exceed the margin of +/-{pooled:.5f} --')
                    lines.append('   not enough hands yet to tell whether it\'s a genuine improvement or sampling noise. Try more hands.')
        self.scen_result_text.insert('end', '\n'.join(lines))

    def _offer_table_update(self, results):
        """When the difference is significant, embed a button after the
        result text letting the user decide whether to write this cell
        back to the strategy file -- never automatic, since statistical
        significance doesn't necessarily mean the user wants to change it
        (they might just want to look first, or want more hands to confirm)."""
        cell = getattr(self, '_scen_cell', None)
        if cell is None:
            self.scen_result_text.insert(
                'end', '(unable to determine which strategy table position this corresponds to, skipping the update option)\n')
            return
        best_act = results[0][0]
        fallback_act = results[1][0] if len(results) > 1 else None
        token = strategy_mod.derive_token(best_act, fallback_act)
        target = strategy_mod.find_defining_file(cell['strat_code'], cell['kind'], cell['row'])

        if cell['overridden']:
            self.scen_result_text.insert(
                'end', f"\nNote: this cell is currently overridden by something (e.g. an H17 difference cell), "
                      f"so updating {target}.json's base table won't necessarily change the actual result -- "
                      "you may need to edit that override by hand as well.\n\n")

        btn = ttk.Button(
            self.scen_result_text, text=f'Write this cell back to {target}.json as "{results[0][1]}"',
            command=lambda: self._apply_table_update(cell, token, results[0][1]))
        self.scen_result_text.window_create('end', window=btn)
        self.scen_result_text.insert('end', '\n')

    def _apply_table_update(self, cell, token, action_name):
        loc = f"{cell['kind']}[{cell['row']}] vs. {CARD_VALUES[cell['col']]}"
        target = strategy_mod.find_defining_file(cell['strat_code'], cell['kind'], cell['row'])
        if not messagebox.askyesno(
                'Update Strategy Table',
                f'Change {target}.json\'s {loc} to "{action_name}"?\n\n'
                'This directly modifies the file on disk, affecting every strategy that inherits this table.'):
            return
        try:
            path = strategy_mod.write_cell(cell['strat_code'], cell['kind'], cell['row'],
                                           cell['col'], token)
        except strategy_mod.StrategyError as e:
            messagebox.showerror('Update Failed', str(e))
            return
        self._reload_strategies()
        self._render_strategy_tables()
        self.scen_result_text.insert('end', f'\nUpdated {path}, strategy table refreshed.\n')
        self.scen_result_text.see('end')

    # -------------------------------------------------------- strategy files
    def _fill_strategies(self):
        for w in self.strat_box.winfo_children():
            w.destroy()
        try:
            items = load_strategy_list()
        except Exception as e:
            ttk.Label(self.strat_box, text=f'Failed to load strategy files: {e}',
                      foreground='#dc2626', wraplength=250).pack(anchor='w')
            return
        if not items:
            ttk.Label(self.strat_box, text='No strategy files under strategies/',
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
                    .title(f'Blackjack Simulator -- {t}'))
        widget.bind('<Leave>', lambda e: widget.winfo_toplevel()
                    .title('Blackjack Simulator'))

    def _reload_strategies(self):
        self._fill_strategies()
        self._fill_strategy_dropdowns()
        self.status.set(f'Reloaded strategy files ({len(self.strat_vars)})')

    def _fill_strategy_dropdowns(self):
        """The strategy table viewer / scenario tester's strategy dropdowns
        share the same strategy files as the main list."""
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

    # ------------------------------------------------------------ rule interactions
    def _on_double(self, _e=None):
        self.double_rule.set([DOUBLE_ANY2, DOUBLE_9_11, DOUBLE_10_11][self.cb_double.current()])

    def _on_sur(self, _e=None):
        self.surrender.set([SURRENDER_NONE, SURRENDER_LATE, SURRENDER_EARLY][self.cb_sur.current()])

    def _on_sweep(self, _e=None):
        self.sweep.set(SWEEP_LABELS[self.cb_sweep.current()][1])

    # -------------------------------------------------------- casino presets
    def _fill_presets(self):
        """Read the available casino rule presets from presets/*.json. Edit
        the JSON and press "reload" to pick up changes."""
        try:
            items = presets_mod.describe()
        except Exception as e:
            messagebox.showerror('Failed to Load Presets', str(e))
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
            messagebox.showerror('Failed to Apply Preset', 'No preset selected')
            return
        try:
            rules, _notes = presets_mod.load(name)
        except presets_mod.PresetError as e:
            messagebox.showerror('Failed to Apply Preset', str(e))
            return

        # order matters: peek has to be set first, or surrender can get
        # overwritten mid-way by _sync_locks()'s "early surrender falls
        # back to late while peek is on" logic -- that check fires
        # immediately via a trace, so setting things in the wrong order
        # gets intercepted partway through.
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
        self.status.set(f'Applied preset: {label}')

    def _sync_seed_lock(self):
        # with "fixed seed" unchecked, this field only displays "the seed
        # actually used last run" and isn't editable, so the user can't be
        # fooled into thinking they've set a seed that will just get
        # overwritten next run anyway.
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
            self.cb_sur.configure(values=['No surrender', 'Late surrender'])
            notes.append('Peek: BJ settles before you act, so no early surrender; OBO is moot.')
        else:
            self.cb_obo.state(['!disabled'])
            self.cb_sur.configure(values=['No surrender', 'Late surrender', 'Early surrender'])
            notes.append('No-peek: late surrender loses the full bet on dealer BJ; '
                         'early surrender always loses only half.')
        if self.csm.get():
            self.sp_pen.state(['disabled'])
            notes.append('CSM: reshuffles every hand, no cut card; card counting is useless.')
        else:
            self.sp_pen.state(['!disabled'])
        self.lock_note.configure(text='\n'.join(notes))

    def _update_precision(self):
        # this field (self.hands) is "how many hands to play per run,"
        # entered directly by the user with no math required; the total
        # round count = hands per run x independent runs, computed and
        # displayed here -- it isn't something the user fills in themselves.
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
        msg = f'= {total:,} total hands ({per_session:,} hands x {sessions:,} independent runs)'
        msg += f'\nExpected precision +/-{ci:.4f}% (95% CI)'
        if ci > 0.22:
            msg += '\nNot even enough to resolve an H17-scale difference of 0.22%'
        elif ci > 0.08:
            msg += '\nEnough to resolve H17, but not a surrender/RSA-scale difference of 0.08%'
        elif ci > 0.02:
            msg += '\nEnough to resolve most rule differences'
        else:
            msg += '\nEnough to resolve differences as fine as 0.02%'

        # too few hands per run and the bankroll curve/distribution charts
        # mostly just reflect single-hand luck, not a long-term trend (the
        # standard deviation is simply the per-hand 1.14, not an engine problem).
        if per_session < 30:
            msg += (f'\n! Only {per_session} hands per run -- the bankroll curve/distribution'
                     '\n  chart mostly reflects single-hand luck, not a long-term trend.')
        elif per_session < 1000:
            msg += f'\n! Only {per_session:,} hands per run -- variance is mostly short-term luck.'
        self.precision.set(msg)

    # ------------------------------------------------------------ execution
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
            messagebox.showerror('Input Error', 'Hands per run must be a number')
            return
        try:
            sessions_n = int(self.sessions.get())
        except (ValueError, tk.TclError):
            messagebox.showerror('Input Error', 'Independent runs must be a number')
            return
        hands = per_session * sessions_n     # run()/compare() take the total round count
        strategies = self._selected_strategies()
        if not strategies:
            messagebox.showerror('Input Error', 'Select at least one strategy')
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

        # a fresh seed is drawn every run unless "fixed seed" is checked --
        # otherwise the same parameters plus the same seed would produce a
        # bit-for-bit identical shoe every time, so the charts would
        # naturally look the same each run, easily mistaken for "nothing
        # redrew." Written back to the field afterward so the user can see
        # which seed this run actually used (check "fixed" to reproduce it).
        if not self.fixed_seed.get():
            self.seed.set(random.randrange(1, 2 ** 31 - 1))
        actual_seed = int(self.seed.get())

        self.cancel.clear()
        self.q = queue.Queue()
        self.btn_run.configure(state='disabled')
        self.btn_cancel.state(['!disabled'])
        self.btn_save.state(['disabled'])
        self.pbar['value'] = 0
        self.status.set('Preparing...')     # don't leave the previous run's "done" on screen
        head = [f'Rules: {rules.label()}   penetration {rules.penetration:.0%}',
                f'Simulating: {per_session:,} hands x {sessions_n:,} independent runs'
                f' x {len(configs)} configuration(s) = {hands*len(configs):,} rounds',
                f'Random seed: {actual_seed}'
                + (' (fixed)' if self.fixed_seed.get() else ' (auto-generated this run, not fixed)')]
        head += [f'  Rule adjustment: {n}' for n in notes]
        self.txt.delete('1.0', 'end')
        self.txt.insert('end', '\n'.join(head) + '\n\nSimulating...\n')

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
        self.status.set('Cancelling... (waiting for the current chunk to finish)')

    def _drain(self):
        """Drain the queue. Returns True once a "finished" message is received."""
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == 'progress':
                    done, total = msg[1], msg[2]
                    self.pbar['value'] = 1000 * done / total
                    self.status.set(f'{done:,} / {total:,} rounds ({done/total*100:.1f}%)')
                elif kind == 'done':
                    self._finish(msg[1], msg[2])
                    return True
                elif kind == 'cancelled':
                    self.status.set('Cancelled')
                    self._idle()
                    return True
                elif kind == 'error':
                    self.status.set('Error')
                    self.txt.insert('end', '\n' + msg[1])
                    self._idle()
                    return True
        except queue.Empty:
            return False

    def _poll(self):
        # order matters: check whether the thread is still alive before
        # draining the queue. Reversed, the thread could finish in the gap
        # between "drain the queue" and "check alive," and the final
        # message would never be read, leaving the status stuck on
        # "Cancelling..." forever.
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
        self.btn_run.configure(state='normal')
        self.btn_cancel.state(['disabled'])

    def _finish(self, results, a):
        self.results = results
        summaries = []
        lines = []
        for label, merged, _per in results:
            s = summarize(merged, a['bankroll'], a['bet'])
            s['label'] = label
            summaries.append(s)
            lines.append(f'-- {label} ' + '-' * max(2, 60 - len(label)))
            lines.append(format_summary(s))
            lines.append('')
        if len(summaries) > 1:
            base = summaries[0]
            lines.append('=' * 70)
            lines.append('Comparison (Common Random Numbers: every configuration draws the same shuffled shoe)')
            lines.append(f"  {'Configuration':<26}{'House edge':>13}{'95% CI':>11}{'Relative diff':>14}")
            for s in summaries:
                d = (s['house_edge'] - base['house_edge']) * 100
                pooled = ((s['house_edge_ci95'] ** 2 + base['house_edge_ci95'] ** 2)
                          ** 0.5) * 100
                tag = '' if s is base else (f"{d:+.4f}%" +
                                            ('' if abs(d) > pooled else ' (not resolvable)'))
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
            # with only 1 session there's only 1 sample point, so no
            # distribution can be drawn -- but the panel can't just do
            # nothing, or the user might think it failed to refresh when
            # it's really just an old chart left over from last time.
            # Say so explicitly and clear the old chart.
            self._clear_chart(
                'dist', 'Independent runs is currently 1, giving only a single sample\n'
                        'point -- can\'t draw a distribution. Set "independent runs" to\n'
                        '2 or more and run again.')
        if len(summaries) > 1:
            self._draw('cmp', ch.edge_comparison(summaries))
            self.nb.select(3)
        else:
            self.nb.select(1)
        self.status.set('Done')
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

        # tight_layout()'s computed margins are based on charts.py's
        # default figure size when it was created (e.g. 10 inches wide).
        # But this panel is usually narrower than that, so when the figure
        # is squeezed in without recomputing margins, the left-side axis
        # labels/ticks get pushed off the edge of the canvas and clipped.
        # Needs a recompute on resize. <Configure> fires many times in a
        # row while dragging the window, so this debounces with after:
        # only actually redraws once 120ms have passed with no new event,
        # or dragging the window would stutter. The job id is stored in
        # slot (not a local variable here) so a redraw or window close can
        # still find and cancel a not-yet-fired job -- otherwise a fired
        # job after the window closes would operate on an already-
        # destroyed widget and raise an error.
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

        # add='+': matplotlib itself already binds <Configure> -> self.resize
        # on this same widget inside FigureCanvasTk.__init__ (responsible
        # for keeping the canvas's actual pixel size in sync with the
        # window as it's resized). Without add='+' here, bind() would
        # simply replace that binding, and the figure content would stay
        # frozen at its creation-time size, leaving a blank strip on the
        # right when the window widens.
        canvas.get_tk_widget().bind('<Configure>', on_configure, add='+')
        fig.tight_layout()
        canvas.draw()
        slot.update(canvas=canvas, toolbar=toolbar, fig=fig)

    def _clear_chart(self, key, message):
        """Clear a chart tab and show an explanatory message (used when a
        chart can't be drawn, so a stale chart from last run doesn't sit
        there looking like the display just didn't update)."""
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
            self.root.after_cancel(self._after_id)   # don't let the callback fire after the window is destroyed
            self._after_id = None
        if self.scen_after_id is not None:
            self.root.after_cancel(self.scen_after_id)
            self.scen_after_id = None
        for slot in self.canvases.values():
            self._cancel_relayout(slot)
        self.root.destroy()

    def _export(self):
        d = filedialog.askdirectory(title='Choose Output Folder')
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
        messagebox.showinfo('Exported', '\n'.join(saved))


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
