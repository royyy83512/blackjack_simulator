# Blackjack Simulator

Quantifies blackjack's house edge with Monte Carlo methods (repeated
random trials), supporting configurable rules, multi-strategy comparison,
statistical metrics, and chart output.

```bash
python3 ui.py                                   # GUI
python3 cli.py --hands 100000000 --sessions 8   # CLI: run one hundred million hands
python3 cli.py --list-strategies                 # list all strategies
python3 test_engine.py && python3 test_strategy.py \
        && python3 test_solver.py && python3 test_scenario.py   # unit tests (~1 minute)
python3 verify.py                               # statistical verification (~10 minutes)
```

---

## File Structure

| File | Contents |
|---|---|
| `core/rules.py` | Rule configuration; `normalize()` resolves mutually contradictory combinations and reports why |
| `core/shoe.py` | The card shoe: correct shuffling, cut card checked only between hands |
| `core/engine.py` | The full playthrough of one hand (split / double / surrender / peek / settlement) |
| `core/strategy.py` | Strategy loader (loading and decision logic only, no strategy content) |
| `strategies/*.json` | **All strategy content lives here -- just edit the JSON, no Python needed** |
| `core/stats.py` | EV, standard deviation, confidence intervals, N0, risk of ruin, max drawdown |
| `core/runner.py` | The tight simulation loop, multi-core parallelism, Common Random Numbers |
| `core/exact.py` | Exact infinite-deck DP, the engine's gold standard |
| `core/solver.py` | Exact exhaustive optimal strategy at an infinite deck (splits excluded), used to check whether any strategy table is correct |
| `core/scenario.py` | Single-scenario simulation: fixed opening two cards + dealer upcard, comparing every action's EV (splits included) |
| `core/presets.py` | Casino rule preset loader (loading logic only, content lives in presets/*.json) |
| `presets/*.json` | **Apply a casino's rules in one click -- just edit the JSON to add one, no Python needed** |
| `charts.py` | Bankroll curves, result distribution, rule comparison charts |
| `cli.py` / `ui.py` | CLI / Tkinter GUI |
| `verify.py` | Three-layer statistical correctness verification |
| `test_engine.py` | Deterministic unit tests for the engine (fixed deal order, hand-by-hand assertions) |
| `test_strategy.py` | Tests for the strategy loader (cell syntax, rule-conditional differences, counting deviations) |
| `test_solver.py` | Tests for the exact solver (including cross-checks around the infinite-deck limitation) |
| `test_scenario.py` | Tests for scenario simulation (cross-checked against known standard answers) |

`core/` has no dependency on matplotlib or tkinter, and can be run standalone under PyPy.

---

## Supported Rules

| Rule | Options | Effect on house edge |
|---|---|---|
| Number of decks | 2-8 | 2 decks is about 0.2% lower than 8 |
| Penetration | 30-95% | Only affects card counting, not basic-strategy EV |
| Dealer soft 17 | S17 / H17 | H17 is about +0.22% |
| When doubling is allowed | any two cards / 9,10,11 / 10,11 | Restricting to 10,11 is about +0.21% |
| Double after split | DAS on/off | Turning it off is about +0.14% |
| Surrender | none / late / early | Late is about -0.08%, early is more |
| Surrender vs. dealer ace | on/off | S17 about 0.006%, H17 about 0.022% (see below) |
| Dealer peek | on/off | Turning it off (lose all on a dealer BJ) is about +0.11% |
| Loss on dealer BJ | lose all / original bet only (OBO) | Only meaningful under no-peek |
| Resplit aces (RSA) | on/off | About -0.08% |
| Hit after splitting aces | on/off | About -0.19% |
| BJ payout | 3:2 / 6:5 | **6:5 is about +1.39%, the single biggest-impact rule** |
| Continuous shuffling machine (CSM) | on/off | No cut card, reshuffles every hand, completely defeats card counting |
| Split cap | fixed at 4 hands | Not adjustable |

### Surrender vs. dealer ace: a rule small enough that it needs an exact calculation to even measure

Some casinos don't offer surrender when the dealer's upcard is an ace.
This rule's effect **depends on whether it's S17 or H17**:

| | Surrender vs. ace allowed | Not allowed | Difference |
|---|---|---|---|
| S17 (120M-hand simulation) | +0.3428% | +0.3466% | +0.0038% +/-0.0289 |
| H17 (120M-hand simulation) | +0.5465% | +0.5719% | +0.0254% +/-0.0290 |

Even 120 million hands can't resolve it (the CI is bigger than the effect
itself). This is when you shouldn't just brute-force more hands -- switch
to computing it exactly with `core/exact.py` instead. Only the handful of
hands where "the player would surrender against an ace" are affected;
just take their `EV(surrender) = -0.5` minus `EV(playing it out)`, weighted
by probability:

**S17: 0.0059%   H17: 0.0221%** (zero sampling error, and it matches the
simulation's point estimates above)

The reason for the difference is that S17 only has "hard 16 vs. ace" as a
surrender cell against an ace, while H17 adds three more: 15, 17, and 8-8:

| H17 hands affected | Probability | EV playing it out | Surrender wins by |
|---|---|---|---|
| Hard 16 vs. ace | 5.92% | -0.5421 | 0.0421 |
| Hard 17 vs. ace | 5.92% | -0.5156 | 0.0156 |
| Hard 15 vs. ace | 7.10% | -0.5069 | 0.0069 |

(The 8,8 row is an approximation: the exact DP doesn't handle splits, so
it's treated as a hard 16.)

**Conclusion: this is the smallest-impact rule on the list, more than 60x
smaller than 6:5.**

### Continuous shuffling machine (CSM)

A real CSM continuously feeds each hand's used cards back into the
machine and reshuffles, with no "deal until some number of cards are
left, then cut and reshuffle." With it enabled:

* The `penetration` setting is ignored (the GUI grays out that field)
* Every hand starts from a freshly shuffled deck (`core/shoe.py`'s
  `Shoe.start_round()` unconditionally reshuffles when `csm=True`,
  without checking the cut card at all)
* **Card counting is completely useless**: `running_count` resets to
  baseline every reshuffle, so the true count always stays near 0 (or
  near an unbalanced system's IRC) -- this is a correct result that falls
  naturally out of the reshuffling mechanism itself, with no special-
  casing needed for counting strategies

```bash
python3 cli.py --hands 20000000 --csm            # enable CSM
python3 cli.py --hands 20000000 --sweep csm       # compare with/without CSM
```

### The exact solution for no-peek: `core/solver.py` now handles it correctly

`core/solver.py` originally only had the peek algorithm (BJ settles
before any decision, using the conditional probability of "already
confirmed no BJ" directly). no-peek is different: the player hits/stands/
doubles with no idea whether the dealer has BJ, so every action's real EV
has to add in the branch where the dealer really does have BJ:

    EV(action) = p_bj x EV_dealer_has_BJ(however much this action ultimately wagered)
               + (1-p_bj) x EV_confirmed_no_BJ(this action)

A few key results fall out of the derivation (`core/solver.py`'s
`solve()` docstring has the full version):

* **Standing/hitting decisions are unaffected** -- at a fixed wager
  multiplier, the BJ-branch penalty is the same constant regardless of
  which option is better, so the continuation phase (`best_continue`)
  needs no changes at all.
* **Doubling is affected**: doubling pulls the wager to 2x, and under
  `LOSS_ALL` (lose all extra wagers) that's an extra unit of risk taken
  on to bet against a dealer BJ that's "actually already certain to have
  happened"; `OBO` (loses only the original bet) carries no such extra
  risk, so OBO's doubling **decision** (not its absolute EV value) is
  identical to peek's -- this equivalence has already been independently
  verified against Monte Carlo, and is captured as a permanent regression
  test in `test_solver.py`.
* **Surrender has three distinct semantics**: peek's late, and early
  under any mode, are both a flat -0.5; no-peek's late is worth less,
  because if the dealer reveals a BJ after the surrender, the full bet is
  lost (`p_bj x (-1) + (1-p_bj) x (-0.5)`, worse than a flat -0.5).

`compare_to_table()` also got two false-positive fixes for cases it was
misjudging as "table errors": not accounting for rule legality (e.g. "no
surrender vs. dealer ace" should fall back to a hit, but was flagged as a
table mistake), and not accounting for the fact that under
`SURRENDER_EARLY`, what actually decides whether to surrender is the
`early_surrender()` pre-check, not the hard table itself.

**Conclusion**: exhaustively checking the `wynn_macau` and
`walkerhill_seoul` presets' complete rule sets against the new exact
solution, `strategies/basic.json` needs zero changes -- the
`early_surrender` list, hand-transcribed early on, turned out to be
correct, and OBO's doubling threshold is mathematically proven identical
to peek's. This also answers "does applying a casino preset need a
customized strategy table": **no need to clone a table per casino** -- the
same table plus conditional overrides (the same mechanism used for the
H17 differences) is enough; it just so happens neither of these two
casinos required any new overrides.

**But that doesn't mean the table "looks" correct**: the GUI's strategy
table viewer and scenario tester tab both originally only called
`effective_cell_label()`/`Strategy.decide()`, and neither knew about the
separate `es_vs_ace`/`es_vs_ten` list that early surrender uses -- in real
gameplay (`core/engine.py`), surrender is decided by this list *before*
`decide()` is even asked, and the hard table's Rh cells are never queried
at all under EARLY mode. After applying Wynn Macau, hard 14 vs. 10
displayed as hit on screen, but real gameplay actually surrenders there --
the display just hadn't caught up, not a `basic.json` error. Now fixed:
`effective_cell_label()` can take an extra `strategy` argument, and when
given one, checks the es list before resolving the display letter;
`core/solver.py`'s `compare_to_table()` and `ui.py`'s table viewer/
scenario tester now both go through this same unified path instead of
each maintaining a duplicate copy of the logic. The regression test lives
in `test_strategy.py`.

(Side note: after applying Wynn Macau, most of 5/6/7/12/13/14/17 vs. ace
not being marked surrender on the table isn't a bug -- this casino simply
doesn't allow surrender vs. an ace, so those cells never trigger early
surrender in real gameplay; not matching a generic early-surrender chart
found elsewhere is a genuine rule difference, not a table error.)

### How the rules interact

* With **dealer peek on**, BJ is fully settled before the player acts, so
  there's no early surrender, and OBO is meaningless (only the original
  bet was ever at risk anyway). `normalize()` corrects this automatically
  and reports why; the GUI locks the corresponding controls.
* **No-peek's late surrender** loses the **full bet** on a dealer BJ;
  **early surrender** always loses only **half**, regardless of whether
  the dealer has BJ. This difference is exactly why early surrender is
  worth more.

---

## Casino Rule Presets: `presets/*.json`

Know a specific casino's actual rules and don't want to click through a
pile of options every time -- at the top of the GUI's "Casino Rules" tab
there's a "Casino Presets (One-Click)" box: pick one, press "Apply," and
every rule field below gets set automatically. The CLI equivalent is
`--preset NAME` (this ignores all the other individual `--decks`/`--h17`/
etc. rule flags).

Adding a new casino just means dropping a new JSON into `presets/`:

```jsonc
{
  "name": "Display name",
  "description": "One-line description",
  "rules": {                     // only fill in the fields you know; the rest fall back to Rules' defaults
    "continuous_shuffle": true,
    "num_decks": 6,
    "dealer_hits_soft_17": false,
    "double_rule": "any2",
    "surrender": "early",
    "surrender_vs_ace": false,
    "dealer_peek": false,
    "dealer_bj_loss": "original",
    "blackjack_pays": 1.5
  }
}
```

Pressing "Apply" shows no text explanation or notice on screen at all --
it just sets every rule field to the value written in the JSON, exactly
equivalent to checking each box by hand yourself, with no extra message
to read. Every field in the bundled presets is a rule the user has
confirmed; if you add your own preset with some fields guessed, it's
worth noting that in `description` as a reminder to yourself, or just
verifying it before writing it in (`description` only shows up in the
dropdown, it never pops up after applying).

Bundled presets:

| Filename | Casino | Rules |
|---|---|---|
| `wynn_macau` | Wynn Macau | CSM / S17 / double on any two cards / no-peek / early surrender (no surrender vs. dealer ace) / OBO / BJ 3:2 / aces split only once, one card each |
| `walkerhill_seoul` | Walkerhill Seoul | peek / S17 / double on any two cards / DAS / late surrender (surrender vs. dealer ace allowed) / dealer BJ crushes all bets / 6 decks, 75% penetration / BJ 3:2 / aces resplit up to 4 hands, one card each |

```bash
python3 cli.py --list-presets
python3 cli.py --preset wynn_macau --hands 20000000
```

---

## Strategy Files: `strategies/*.json`

All strategy content lives in JSON -- just edit and save (the GUI has a
"reload strategy files" button, no restart needed). Adding a strategy
just means dropping a new `.json` into `strategies/`; it automatically
appears in both the CLI's and GUI's lists.

### Cell notation

Each row is **10 space-separated cells**, in order for dealer upcard
`2 3 4 5 6 7 8 9 10 A`. It follows the same notation as real strategy charts:

| Cell | Meaning |
|---|---|
| `H` | Hit |
| `S` | Stand |
| `D` (or `Dh`) | Double; **hit if doubling isn't allowed** |
| `Ds` | Double; **stand if doubling isn't allowed** |
| `Y` or `P` | Split |
| `Ph` | Split; **fall through to the hard/soft table if splitting isn't allowed** <- expresses "split only with DAS" |
| `N` | Don't split, fall through to the hard/soft table (pair table only) |
| `Rh` / `Rs` / `Rp` | Surrender; **hit/stand/split** if surrender isn't allowed |

The fallback notation matters: when a rule disables some action (this
table doesn't offer surrender, or doesn't allow doubling after a split),
the engine automatically falls back, with no need to prepare a separate table.

### File layout

```jsonc
{
  "name": "hi-lo",
  "description": "Basic strategy + Hi-Lo card counting",
  "extends": "basic",                    // inherits another strategy file's tables

  "tables": {                            // the strategy tables themselves
    "hard": { "16": "S  S  S  S  S  H  H  Rh Rh Rh" },
    "soft": { "18": "S  Ds Ds Ds Ds S  S  H  H  H" },
    "pair": { "8":  "Y  Y  Y  Y  Y  Y  Y  Y  Y  Y" }
  },

  "overrides": [                         // rule-conditional cell differences
    { "when": {"dealer_hits_soft_17": true},
      "description": "the 6 cells that differ under H17",
      "cells": [
        {"table": "hard", "row": "11", "dealer": "A", "action": "D"}
      ] }
  ],

  "counting": {                          // presence of this block means it's a counting strategy
    "balanced": true,                    // true=uses the true count, false=uses the running count (like KO)
    "tags": {"2": 1, "10": -1, "A": -1},
    "start_count": 4,                    // starting running count for unbalanced systems (IRC)
    "start_count_per_deck": -4           //   IRC = start_count + this x number of decks
  },
  "betting": { "ramp": [[1,1],[2,2],[3,4],[4,8],[5,12]] },   // [count threshold, bet multiplier]
  "insurance": { "min_count": 3 },

  "deviations": [                        // count-based deviations from basic strategy
    {"table":"hard","row":"16","dealer":"10","min_count":0,"action":"Rs"},
    {"table":"hard","row":"12","dealer":"4","max_count":0,"action":"H"}
  ],

  "early_surrender": {                   // only active under no-peek + early surrender
    "vs_ace": [5,6,7,12,13,14,15,16,17],
    "vs_ten": [14,15,16]
  }
}
```

`when` can use any rules field (`dealer_hits_soft_17`, `num_decks`,
`double_after_split`, `surrender_vs_ace`, ...) -- it applies whenever the
value matches.

Any strategy name with a **`-fixed`** suffix skips `overrides` entirely --
e.g. `basic-fixed` always uses the S17/DAS table -- used to measure "what
does it cost to use the wrong strategy table."

### Bundled strategies

| Name | Description |
|---|---|
| `basic` | Basic strategy (baseline S17 / DAS / late surrender, includes H17 difference cells) |
| `hi-lo` | Hi-Lo card counting, bet spread only, no deviations |
| `hi-lo-i18` | Hi-Lo + Illustrious 18 deviations |
| `ko` | Knock-Out, an unbalanced system, reads the running count directly |
| `mimic-dealer` | Fully mimics the dealer, hits to 17 |
| `never-bust` | Stands on any hard 12+ |
| `no-split-no-double` | Measures how much splitting and doubling are worth |
| `basic-nosplit` | Used for exact-DP cross-checking |

### Measured: card-counting strategy comparison

6 decks / 75% penetration / 60 million hands each, bet spread 1-12 units:

| Strategy | EV per 100 hands | +/-95% CI | SD per round | Avg. bet | Edge on action |
|---|---|---|---|---|---|
| `basic` | -0.342 u | 0.029 | 1.14 | 1.00 | -0.302% |
| `hi-lo` | +1.153 u | 0.073 | 2.88 | 1.60 | +0.632% |
| **`hi-lo-i18`** | **+1.427 u** | 0.076 | 2.99 | 1.60 | **+0.772%** |
| `ko` | +1.237 u | 0.078 | 3.09 | 1.60 | +0.679% |

The Illustrious 18 deviations earn about **24%** more than bet-spreading
alone (+0.274 u/100 hands), without wagering an extra cent -- all three
have the same 1.60 average bet.

Note that a counting strategy's per-round standard deviation is more than
2.5x a flat-bet strategy's (caused by the bet spread), so achieving the
same statistical precision needs more hands.

Chart: `results/counting.png`

### Adding a "counting strategy 2"

Copy `hi-lo.json`, rename it, adjust `tags` (swap the counting system),
`ramp` (swap the bet spread), `deviations` (swap the deviation table), and
save it into `strategies/`. Then:

```bash
python3 cli.py --hands 50000000 --strategy hi-lo hi-lo-i18 your-new-strategy
```

The same shoe is fed to every strategy (Common Random Numbers), making
differences easiest to resolve.

---

## Is My Strategy Correct? -- Strategy Table Viewer + Scenario Tester

The GUI has two tools dedicated to answering "under this table's rules,
how should this hand actually be played" -- instead of making you copy a
chart from somewhere that may not even apply to your table's rules:

### Strategy table viewer (the "Strategy Table" tab)

Takes any strategy's hard/soft/pair tables from `strategies/*.json`,
applies the rules currently set on the left (H17 difference cells, DAS's
`Ph` branches, etc. are compiled in automatically), and renders them as a
color-coded grid. A counting strategy also gets an extra "counting
deviations" section (cells that change with true/running count -- these
are conditional and can't be drawn into a static grid). Edit the JSON and
press "reload strategy files," and this view updates too.

### Scenario tester (the "Scenario Tester" tab)

Specify the player's two cards plus the dealer's upcard, press "Compute,"
and get two layers of answers:

1. **Exact solution** (`core/solver.py`, computed instantly, zero
   sampling error): the best of hit/stand/double/surrender, splits
   excluded. Exists to give you an immediate reference with no wait.
2. **Monte Carlo** (`core/scenario.py`, uses your actual configured deck
   count, split option included): each legal action is run for however
   many hands you specify, reporting EV +/- 95% CI, sorted best to worst.

Once it finishes, it's automatically compared against what the strategy
table currently recommends, checking whether the gap exceeds the combined
95% CI of both sides -- a Monte Carlo result differing from the strategy
table **doesn't mean the table is wrong**, it might just be sampling
noise. A gap within the margin of error gets a "not resolvable yet, try
more hands" note; only a gap that genuinely exceeds it gets flagged "this
cell might really need to change." Just like the main flow, a fresh seed
is drawn every time you press "compute" unless "fixed seed" is checked,
so you can see that results naturally fluctuate.

### Writing a finding back to the strategy table

When the gap is significant (not noise -- genuinely statistically
resolvable), a button appears below the results: "Write this cell back to
xxx.json as 'X'." Clicking it asks for confirmation once, and on accepting:

* It walks up the strategy file's `extends` inheritance chain to find the
  file that **actually defines this cell** before editing it (e.g.
  testing with `hi-lo` actually edits `basic.json`, which it inherits
  from, not `hi-lo.json` -- `hi-lo.json` never had its own table to edit
  in the first place)
* The new cell's "fallback" (where to fall back to when this action isn't
  allowed) is determined by the **runner-up action**, not chosen
  arbitrarily: e.g. if the best action is double and the runner-up is
  stand, it writes back `Ds` (double, else stand), not `D` (double, else
  hit) -- the fallback is also measured by Monte Carlo, not guessed
* If this cell is currently overridden by something (e.g. an H17
  difference cell), you're warned up front that "editing the base table
  may not take effect," because the override takes priority -- that case
  needs manually editing the JSON's `overrides` section; the tool won't
  do that for you automatically
* Strategy files are reloaded immediately after editing, and the strategy
  table viewer shows the new result right away

This button only appears for **statistically significant** gaps -- a gap
within the margin of error gets no update option, avoiding baking
sampling noise into the strategy table permanently. To update one of
those marginal cells, run more hands first until the gap becomes
consistently significant.

### Why two layers -- why not just the exact solution?

Because splits bring in a combinatorial explosion of DAS, RSA, and
resplitting, making an exact solution too expensive -- only Monte Carlo
can do a complete comparison "including splits." But the exact solution
has an advantage Monte Carlo can't match: zero error, computed instantly,
making it a fast, trustworthy anchor point.

**This design accidentally turned up a genuinely interesting finding,
worth recording**: cross-checking the solver's exact solution against
`basic.json`, only one cell disagrees under S17 rules -- `A,2 vs. 5`. The
solver says hitting beats doubling (EV 0.1334 vs. 0.1260), but
`basic.json` (transcribed from most published references) says double.
Digging in:

* The solver walks an **infinite deck** (the same assumption
  `core/exact.py` always makes)
* Running Monte Carlo at 200 decks (approximating an infinite deck)
  agrees with the solver: hitting wins
* Running Monte Carlo at **your actually configured 6 decks** flips it:
  doubling wins (EV 0.1386 vs. 0.1379, very close either way)

In other words, **neither side is wrong** -- they're just answering
different questions. The solver gives you the "theoretical limit,"
Monte Carlo gives you the answer for "this table's actual rules," and
this cell happens to be a marginal case where deck count flips the
answer. `solve_cell`/`compare_to_table` therefore flags mismatches within
1% as `deck_sensitive`, meaning "the answer may depend on deck count,
this doesn't necessarily mean the table is wrong" -- for a cell like
that, trust the scenario tester's Monte Carlo (at your actual deck count),
not the exact solution alone.

Side note: this cross-check **did genuinely catch a real table error**:
`basic.json` originally had hard 14 vs. dealer 10 marked as surrender
(`Rh`) too, but under a standard surrender table, hard 14 should never
surrender -- only hard 15 vs. 10, and hard 16 vs. 9/10/A, do. The exact
solution measured this cell as costing 3.37% in EV (far above the 1%
deck-margin threshold), and after fixing it with `write_cell()`,
`python3 test_solver.py` went fully green. It looks like a copy-paste
slip when the file was originally written, copying the format from row
15 and going one row too far (the two row strings were originally
identical, which was itself suspicious).

This proves the cross-check mechanism is genuinely useful, not just for
show. Side note: `core/solver.py` doesn't handle splits (see its header),
so this layer of cross-checking **can't see errors in the pair table** --
later, during another full regression pass, `test_strategy.py`
additionally caught the same class of error in the pair table too (`8,8
vs. 10` was marked surrender, when it should be split, costing about 3%
in EV), fixed the same way with `write_cell()`. This is also why
`test_engine.py` (tests the split path directly with a stacked deck),
`test_strategy.py` (tests the compiled table contents), and
`test_solver.py` (exact-solution cross-checking, but blind to splits) all
need to run together, none dispensable -- the three layers' coverage
doesn't fully overlap, and only together do they leave no blind spots.

`python3 test_engine.py && python3 test_strategy.py && python3 test_solver.py`
all pass now, and `strategies/basic.json` is currently known to have no
genuine table errors (the only remaining discrepancy is that deck-margin effect).

---

## How Many Hands Are Enough?

The standard deviation of per-hand results is about **1.14 units**, so

$$\text{95\% CI} = \pm 1.96 \times \frac{1.14}{\sqrt{N}}$$

| Hands | 95% CI | What it can resolve |
|---|---|---|
| 100K | +/-0.71% | Only enough to see a huge difference like 6:5 |
| 1M | +/-0.22% | Even H17 is barely at the edge of resolvable |
| 10M | +/-0.071% | Enough to resolve H17, DAS, doubling restrictions |
| **100M** | **+/-0.022%** | Enough to resolve a 0.08%-scale rule like surrender or RSA |

**Key point: one million hands isn't enough to compare rules.** The house
edge itself is only about 0.4%, and the rule differences you're likely
trying to measure are mostly in the 0.08-0.22% range.

Both the CLI and GUI tell you the expected precision for a given run before it starts.

### Common Random Numbers (CRN)

When comparing multiple configurations, all of them use the **same
seed** -- that is, the same shuffled shoe. Most hands make the same
decision and draw the same cards on both sides, so the difference is 0,
meaning the variance of "A minus B" is far smaller than either side's own
variance, letting the same number of hands resolve much smaller differences.

Caveat: once one hand's decision diverges, the card sequences after it
drift apart, so the pairing isn't perfect from that point on.

---

## Performance

Measured (8-core macOS):

| Hands | Wall time |
|---|---|
| 1M | ~1 second |
| 10M | ~8 seconds |
| 100M | ~75 seconds |
| 1B | ~12 minutes |

Monte Carlo is inherently O(n), which is already optimal -- fewer samples
just means less precision. So optimization focuses on the constant factor:

* The unit of parallelism is a chunk (250K rounds each), load-balanced
  across cores, which also lets cancellation take effect within a few seconds
* Hand totals are updated incrementally, never re-summed with `sum()`
* Actions are int bitmasks, avoiding set allocation on the decision hot path
* Statistics only accumulate scalars (`sum` / `sumsq`), never keeping
  every hand's result -- storing all of them for a hundred million hands
  would take 0.8 GB
* Bankroll curves are downsampled to about 2000 points during the
  simulation itself; the screen is only about 2000 px wide, and plotting
  a million points would stack 99.8% of them on the same pixel

For another 10-30x, run `core/` directly under PyPy (it has no dependency on matplotlib).

---

## Correctness Verification

`python3 verify.py [rounds per test]` has three layers, from strongest to weakest:

**Layer 1 -- exact DP cross-check (strongest)**
`core/exact.py` sums over every card sequence weighted by probability at
an infinite deck, with zero error and zero randomness. The simulation
side disables splits and uses 200 decks to approximate an infinite deck;
the two must agree.

Measured gap of **0.0008% (S17) and 0.0079% (H17)**, proving hit / stand
/ double / dealer logic / settlement are all completely correct.

**Layer 1.5 -- deterministic unit tests** (`test_engine.py`, 35 checks +
`test_strategy.py`, 60 checks)

Statistical tests can tell you whether the overall EV is right, but when
a branch like splitting is broken, it usually only skews the numbers
slightly, hard to pin down. `test_engine.py` uses a stacked deck with a
fixed deal order for hand-by-hand assertions, covering the split cap,
splitting aces, RSA/HSA, DAS, surrender timing, peek/no-peek/OBO, and S17/H17.

`test_strategy.py` separately covers the strategy file mechanics: cell
syntax and fallbacks, `Ph` compiling based on DAS, `overrides`'
activation conditions, the `-fixed` suffix, `extends` inheritance,
counting tags, the bet ramp, the Illustrious 18's deviation thresholds,
KO's IRC scaling with deck count, and error messages.

This layer has caught two bugs that the statistical tests couldn't see:

* Breaking out of the decision loop right after splitting aces meant RSA
  never triggered when the **first** hand drew a second ace, while it did
  for the second hand -- asymmetric behavior.
* Two surrender checks in `decide()` that should have been mutually
  exclusive were written as sequential checks instead, causing **8,8 vs.
  9/10/A to surrender instead of split** (the 16-point total hit the
  generic hard-total surrender entry).

**Layer 2 -- known constants**

| Metric | Exact value | Simulated value |
|---|---|---|
| Dealer final-outcome distribution, S17 (17/18/19/20/21/bust) | 14.51 / 13.95 / 13.35 / 18.03 / 12.01 / 28.16% | All within +/-0.25% |
| Dealer bust rate, H17 | 28.54% | 28.56% |
| Player natural frequency | 4.749% | 4.747% |
| Per-round standard deviation | ~1.14 | 1.137 |

**Layer 3 -- comparison against published house-edge figures** (100M hands each)

| Rules | Simulated | Literature | Diff |
|---|---|---|---|
| 6D S17 DAS no-surrender 3:2 | +0.4133% +/-0.0226 | 0.40% | +0.013% |
| 6D S17 DAS LS 3:2 | +0.3430% +/-0.0224 | 0.33% | +0.013% |
| 6D H17 DAS LS 3:2 | +0.5367% +/-0.0224 | 0.51% | +0.027% |

This simulator uses a **total-based** basic strategy table (only looking
at the hand's total), while published figures usually assume a
**composition-based** strategy (which also looks at exactly which cards
make up the total), earning roughly 0.02-0.04% more. So the simulation
consistently landing 0.013-0.027% above the literature is expected --
layer 1 has already proven the engine itself is correct.

---

## Common Commands

```bash
# single configuration, one hundred million hands, eight bankroll curves
python3 cli.py --hands 100000000 --sessions 8

# strategy comparison (Common Random Numbers)
python3 cli.py --hands 20000000 --strategy basic hi-lo mimic never-bust no-sp-dbl

# rule sweeps
python3 cli.py --hands 20000000 --sweep decks
python3 cli.py --hands 20000000 --sweep peek

# custom rules
python3 cli.py --hands 20000000 --decks 8 --h17 --no-das --surrender none --bj-pays 1.2

# European no-hole-card + early surrender + OBO
python3 cli.py --hands 20000000 --no-peek --surrender early --obo
```

`--sweep` accepts: `decks` `h17` `das` `surrender` `peek` `rsa` `double` `bj`

`--strategy` accepts:

| Name | Description |
|---|---|
| `basic` | Basic strategy (table adapts automatically to the rules) |
| `basic-fixed` | Always uses the standard 6D S17 DAS table, showing the cost of using the wrong table |
| `hi-lo` | Basic strategy + Hi-Lo true-count bet ramp + insurance at true count >=3 |
| `no-sp-dbl` | Basic strategy but never splits or doubles, measuring how much those two actions are worth |
| `mimic` | Fully mimics the dealer, hits to 17 |
| `never-bust` | Stands on any hard 12+ |

---

## Measured Results (100M hands each, 95% CI +/-0.022%)

Baseline: 6D S17 DAS double-on-any-two LS peek 3:2 -> house edge **+0.356%**

| Rule change | House edge | Relative to baseline | Literature |
|---|---|---|---|
| BJ pays only 6:5 | +1.714% | **+1.358%** | +1.39% |
| H17 | +0.562% | +0.206% | +0.22% |
| Double only on 10/11 | +0.545% | +0.189% | +0.21% |
| No DAS | +0.495% | +0.139% | +0.14% |
| No surrender | +0.434% | +0.078% | +0.08% |
| 8 decks | +0.363% | +0.007% | +0.02% |
| 2 decks | +0.221% | -0.135% | -0.19% |
| Resplit aces (RSA) | +0.288% | -0.068% | -0.08% |
| no-peek + early surrender | **-0.045%** | -0.401% | -- |

The last row is worth noting: European no-hole-card combined with early
surrender actually gives the player a 0.045% edge under this rule combination.

Chart: `results/rule_impact.png`

---

## Statistical Metrics Explained

| Metric | Meaning |
|---|---|
| House edge | -net result / total original wagers. Positive means the house wins |
| Action / initial bet | Includes extra wagers from doubling and splitting, about 1.13 |
| N0 | How many rounds until expected profit catches up to one standard deviation |
| Risk of ruin | `exp(-2 x bankroll x EV / variance)`, an approximation over an unbounded number of hands |
| Max drawdown | The largest drop from a bankroll peak (computed exactly across chunk boundaries) |
