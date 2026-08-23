"""Exact exhaustive optimal strategy (excluding splits).

Like core/exact.py, this walks an exact infinite-deck DP, but where
exact.py asks "play according to this strategy table, what's the house
edge," this asks "for every state, exhaustively compare hit/stand/double/
(on the first two cards) surrender and compute the mathematically best
action" — which is exactly how basic strategy tables should be derived in
the first place: zero sampling error, computed instantly, and usable to
check whether any table in core/strategy.py is actually correct, or to
generate a brand-new table for a rule combination nobody has worked out
by hand.

Splits excluded: splits bring in a combinatorial explosion of DAS, RSA,
and resplitting, making the exact solution too expensive. Split decisions
are instead compared via Monte Carlo in core/scenario.py (see that
module's header).

Known limitation: infinite deck, not your configured deck count
------------------------------------------------------------------
This walks an infinite deck (the probability of each draw is fixed,
regardless of what's been dealt already), not the actual configured
`rules.num_decks`. For the overwhelming majority of cells this doesn't
matter — the answer is the same at 2 decks and at infinite decks. But a
handful of already-close marginal cells really do flip with deck count;
one measured example:

    A,2 vs. 5 (S17, DAS):
      infinite-deck exact solution: hit EV=0.1334 > double EV=0.1260 -> hit
      real 6-deck (Monte Carlo): double EV=0.1386 > hit EV=0.1379 -> double
      (but very close either way)

    Running the Monte Carlo at 200 decks matches the infinite-deck exact
    solution, confirming the difference comes from deck count, not an
    error on either side.

So: treat this module's answers as a fast, zero-error reference for most
cells, but for cells with a small margin (e.g. compare_to_table reporting
an ev_loss of only a few thousandths), the answer may flip depending on
your actual deck count — use core/scenario.py's Monte Carlo at your real
deck count to get a trustworthy answer there.
"""
from .engine import ACT_HIT, ACT_STAND, ACT_DOUBLE, ACT_SURRENDER
from .exact import P, dealer_given_up, p_dealer_bj
from .rules import SURRENDER_NONE, SURRENDER_EARLY, LOSS_ORIGINAL

ACTION_LETTERS = {ACT_STAND: 'S', ACT_HIT: 'H', ACT_DOUBLE: 'D', ACT_SURRENDER: 'R'}
COLUMNS = ('2', '3', '4', '5', '6', '7', '8', '9', '10', 'A')


def _ev_stand(total, up, h17):
    d = dealer_given_up(up, h17)
    ev = d[5]
    for k in range(5):
        dt = 17 + k
        ev += d[k] * (1 if total > dt else (-1 if total < dt else 0))
    return ev


def solve(rules):
    """Return {'hard': {total: [(letter, evs_dict), ...10 columns]}, 'soft': {...}}.

    letter already accounts for whether surrender beats everything else
    (surrender only applies on the first two cards, the only state this
    function ever queries, so every cell has already been compared).
    evs_dict holds the EV of every option at this cell: S/H are always
    present, D is present when doubling is allowed, R when surrender is.

    The correct no-peek algorithm (derived here, not guessed)
    ------------------------------------------------------------
    Under peek, dealer BJ is already settled before the player makes any
    decision, so solve_cell only needs the EV conditional on "dealer
    confirmed no BJ" — exactly what _ev_stand/best_continue already
    compute.

    no-peek is different: the player hits/stands/doubles with no idea
    whether the dealer has BJ, so the "real" EV of each action has to add
    in the branch where the dealer really does have BJ:

        EV(action) = p_bj * EV_dealer_has_BJ(however much this action
                       ultimately has wagered)
                   + (1 - p_bj) * EV_confirmed_no_BJ(this action)

    Key simplification: EV_dealer_has_BJ only depends on the final wager
    multiplier (LOSS_ALL loses the entire wager, OBO always loses exactly
    the original 1 unit) — completely independent of how many more cards
    the player draws or what total they end up with. That means at a
    **fixed wager multiplier**, the best choice between hitting and
    standing is the same decision under peek and no-peek — the p_bj term
    just adds a constant that doesn't change which option is larger, so
    best_continue() (the continuation decision after a hit, where the
    wager multiplier is fixed at 1) needs no changes at all; it's still
    correct to keep using the "confirmed no BJ" conditional probabilities.

    The only two "opening" decisions actually affected by no-peek, and
    that need separate handling, are:

    1. Doubling — because doubling pulls the wager multiplier from 1 to 2,
       under LOSS_ALL that means taking on an extra unit of risk to bet
       against a dealer BJ that has, in fact, already happened. OBO carries
       no such extra risk (the extra wager is returned), so the threshold
       for doubling under OBO is actually the same as under peek.
    2. Surrender — three genuinely different semantics:
         peek's late / early under any mode: surrender is a flat -0.5 from
           the start, regardless of whether the dealer has BJ (early is
           even decided before the dealer's cards are revealed at all).
         no-peek's late: the player surrenders first, and if the dealer
           later reveals a BJ, the full bet is lost — so surrender itself
           has to be p_bj-weighted too: p_bj*(-1) + (1-p_bj)*(-0.5), worse
           than a flat -0.5. This is exactly why late surrender is worth
           less under no-peek (core/engine.py's F_SURRENDER branch and the
           existing tests in test_engine.py already verify this semantics).

    When peek=True, p_bj is simply set to 0 and the formula degenerates
    back to the original algorithm, with no behavior change at all (the
    regression tests confirm this).
    """
    h17 = rules.dealer_hits_soft_17
    peek = rules.dealer_peek
    loss_original = rules.dealer_bj_loss == LOSS_ORIGINAL
    surrender_mode = rules.surrender
    memo = {}

    def best_continue(s, aces, up):
        """Best EV starting from a post-hit state (no longer the first two cards).

        No doubling option — by the rules, doubling can only happen on
        the original two cards, never after a hit, so this only compares
        standing against continuing to hit, with no need to check
        can_double again. The wager multiplier is fixed at 1 unit, and
        peek/no-peek make the same optimal decision here (see the module
        docstring's derivation above), so it's correct to keep using the
        "confirmed no BJ" conditional probabilities.
        """
        key = (s, aces, up)
        if key in memo:
            return memo[key]
        soft = aces > 0 and s + 10 <= 21
        total = s + 10 if soft else s
        if total > 21:
            memo[key] = -1.0
            return -1.0
        best = _ev_stand(total, up, h17)
        if total < 21:
            ev = 0.0
            for c, p in P:
                ev += p * best_continue(s + c, aces + (c == 1), up)
            if ev > best:
                best = ev
        memo[key] = best
        return best

    def solve_cell(s, aces, up):
        """Full comparison for the first two cards (surrender and doubling
        both possible). Returns (letter, evs)."""
        soft = aces > 0 and s + 10 <= 21
        total = s + 10 if soft else s
        ev_stand_c = _ev_stand(total, up, h17)          # conditional on "no BJ"
        ev_hit_c = 0.0
        for c, p in P:
            ev_hit_c += p * best_continue(s + c, aces + (c == 1), up)

        can_double = rules.double_allowed_on(s, soft)
        ev_double_c = None
        if can_double:
            ev_double_c = 0.0
            for c, p in P:
                s2, a2 = s + c, aces + (c == 1)
                t2 = s2 + 10 if (a2 and s2 + 10 <= 21) else s2
                ev_double_c += p * (-1.0 if t2 > 21 else _ev_stand(t2, up, h17))
            ev_double_c *= 2.0

        p_bj = 0.0 if peek else p_dealer_bj(up)   # when peek=True the formula degenerates automatically

        def bj_penalty(wager):
            return -1.0 if loss_original else -float(wager)

        evs = {
            'S': p_bj * bj_penalty(1) + (1 - p_bj) * ev_stand_c,
            'H': p_bj * bj_penalty(1) + (1 - p_bj) * ev_hit_c,
        }
        if can_double:
            evs['D'] = p_bj * bj_penalty(2) + (1 - p_bj) * ev_double_c

        if surrender_mode != SURRENDER_NONE and rules.surrender_allowed_vs(up):
            if peek or surrender_mode == SURRENDER_EARLY:
                evs['R'] = -0.5
            else:   # no-peek + late: full bet lost if the dealer later reveals BJ
                evs['R'] = p_bj * (-1.0) + (1 - p_bj) * (-0.5)

        letter = max(evs, key=evs.get)
        return letter, evs

    hard = {}
    for total in range(4, 22):
        row = []
        for up in range(1, 11):
            letter, evs = solve_cell(total, 0, dealer_col_to_up(up))
            row.append((letter, evs))
        hard[total] = row

    soft = {}
    for total in range(12, 22):
        row = []
        for up in range(1, 11):
            # soft total: aces=1, s = total - 10 (e.g. soft 18 = A,7 => s=8, aces=1)
            s = total - 10
            letter, evs = solve_cell(s, 1, dealer_col_to_up(up))
            row.append((letter, evs))
        soft[total] = row

    return {'hard': hard, 'soft': soft}


def dealer_col_to_up(col_1_to_10):
    """Column order is 2..10,A (matching core.strategy.COLUMNS); col=10 is an ace."""
    return 1 if col_1_to_10 == 10 else col_1_to_10 + 1


def compare_to_table(rules, strategy):
    """Compare the exact solution against a compiled strategy table,
    returning the list of mismatched cells (splits excluded).

    strategy is an object returned by core.strategy.make(...); its tables
    hold compiled (action_bitmask, fallback) tuples. Before comparing, run
    them through strategy_mod.effective_cell_label() to resolve "is this
    cell actually legal under this table's rules" into a final letter —
    comparing the cell's raw "primary action" directly would misjudge a
    rule like "the dealer doesn't allow surrender against an ace" as a
    table error (the table says Rh, but legality resolution correctly
    falls back to hit; this has to match the strategy table viewer's
    behavior, not be computed separately).

    The fact that the early-surrender pre-check takes priority over the
    hard table itself is also built into effective_cell_label() (pass
    strategy in and it checks es_vs_ace/es_vs_ten) — no need to duplicate
    that check here.
    """
    from . import strategy as strategy_mod   # deferred import: avoids a circular import with strategy.py

    solved = solve(rules)
    mismatches = []

    for kind, totals in (('hard', range(4, 22)), ('soft', range(12, 22))):
        table = getattr(strategy, kind)
        is_soft = kind == 'soft'
        for total in totals:
            if total not in table:
                continue
            row = table[total]
            hard_total = total - 10 if is_soft else total
            for col in range(10):
                solved_letter, evs = solved[kind][total][col]
                up = dealer_col_to_up(col + 1)

                table_letter = strategy_mod.effective_cell_label(
                    row[col], up, rules, hard_total, is_soft, strategy)
                # cells where the exact solution and the table are within
                # 0.001 EV of each other don't count as a mismatch — either
                # choice is essentially equivalent there, it's floating-
                # point noise, not a table error.
                best_ev = evs[solved_letter]
                table_ev = evs.get(table_letter)
                if table_ev is not None and abs(best_ev - table_ev) < 0.001:
                    continue
                if table_letter != solved_letter:
                    loss = best_ev - table_ev if table_ev is not None else None
                    mismatches.append({
                        'kind': kind, 'total': total, 'dealer': COLUMNS[col],
                        'table_says': table_letter, 'optimal': solved_letter,
                        'ev_table': table_ev, 'ev_optimal': best_ev, 'ev_loss': loss,
                        # cells within 1% often flip with deck count (a real
                        # table is a finite deck; this module computes an
                        # infinite deck) and don't necessarily mean the
                        # table is wrong — use core/scenario.py's Monte
                        # Carlo at the actual deck count for a trustworthy
                        # answer there. Note: a gap caused by no-peek can
                        # also fall in this range, so this flag only means
                        # "small gap," not "definitely caused by deck
                        # count" — the actual cause still depends on the
                        # rule combination.
                        'deck_sensitive': loss is not None and abs(loss) < 0.01,
                    })
    return mismatches
