"""Single-scenario simulation: fix "player's first two cards + dealer
upcard" and compare the EV of every possible first move.

Why this exists: if you want to know "under this table's rules, how
should this specific hand actually be played," you shouldn't look it up
in some chart online — those charts are usually computed for one specific
rule combination (e.g. 6D S17 DAS), and can be wrong the moment any rule
changes. This simulates directly under whatever rules you've configured,
measuring the EV of hit/stand/double/split/surrender individually, so the
answer is "the correct answer for this table's rules," not something
copied from elsewhere.

This doesn't directly duplicate core/engine.py's hand-playing flow (that
risks drifting out of sync with the main engine's behavior — peek/
no-peek/OBO/surrender semantics are all quite subtle). Instead it reuses
the already-validated play_round via two tricks:

1. "Arrange" the specified opening two cards into the shoe's upcoming
   draw positions — play_round's internal deal order is fixed as
   p1, up, p2, so swapping those three cards into the shoe's next three
   draw slots makes shoe.draw() naturally deal them in order, with no
   need to touch play_round at all.
2. Wrap a strategy (_ForceFirstAction) that only overrides the very first
   decision it's asked for, returning the specified action; everything
   after that (including the second hand after a split) plays out
   according to the real strategy table.

Splits have no exact solution (core/exact.py only computes the no-split
case — splits bring in a combinatorial explosion of DAS/RSA/resplitting
that makes an exact solution too expensive), so split EVs computed here
always carry sampling error and need enough rounds to be trustworthy.
Non-split actions can be cross-checked against core/solver.py's exact
solution.
"""
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from .engine import (ACT_HIT, ACT_STAND, ACT_DOUBLE, ACT_SPLIT, ACT_SURRENDER,
                     Hand, legal_actions, play_round)
from . import strategy as strategy_mod
from .shoe import HI_LO

ACTION_NAMES = {
    ACT_HIT: 'Hit (H)', ACT_STAND: 'Stand (S)', ACT_DOUBLE: 'Double (D)',
    ACT_SPLIT: 'Split (P)', ACT_SURRENDER: 'Surrender (R)',
}
ALL_ACTIONS = (ACT_STAND, ACT_HIT, ACT_DOUBLE, ACT_SPLIT, ACT_SURRENDER)


class ScenarioError(Exception):
    pass


def parse_card(x):
    """Accepts 'A'/'10'/'T'/8 and friends, normalizing to 1..10."""
    s = str(x).strip().upper()
    if s in ('A', 'ACE', '1'):
        return 1
    if s in ('T', '10', 'J', 'Q', 'K'):
        return 10
    v = int(s)
    if not 1 <= v <= 10:
        raise ScenarioError(f"Card value out of range: {x}")
    return v


def legal_first_actions(player_cards, dealer_up, rules):
    """Which first-move actions are legal for this opening (a fixed pair of
    cards) under this table's rules."""
    c1, c2 = player_cards
    h = Hand([c1, c2], 1.0)
    return legal_actions(h, dealer_up, 1, rules)


class _ForceFirstAction:
    """Wraps the real strategy: intercepts only the very first decision,
    then defers to the real strategy for everything after.

    early_surrender is a separate, no-peek+early-only query (asked before
    the decide() loop even starts), so it needs its own interception: if
    the action under test is surrender, return True (equivalent to the
    player choosing to surrender); if it's testing anything else, force
    False so the hand is guaranteed to reach the decide() loop — otherwise
    the real strategy might surrender the hand out from under us before we
    ever get to test the action we actually wanted to measure.
    """
    __slots__ = ('real', 'action', 'used_decide')

    def __init__(self, real, action):
        self.real = real
        self.action = action
        self.used_decide = False

    def bet(self, shoe, rules, base_bet):
        return self.real.bet(shoe, rules, base_bet)

    def take_insurance(self, shoe, rules):
        return self.real.take_insurance(shoe, rules)

    def early_surrender(self, hand, up, shoe, rules):
        return self.action == ACT_SURRENDER

    def decide(self, hand, up, legal, shoe, rules):
        if not self.used_decide:
            self.used_decide = True
            return self.action
        return self.real.decide(hand, up, legal, shoe, rules)


class _ScenarioShoe:
    """A lightweight deck for scenario simulation: not a card array, but a
    quota of "how many of each rank are left."

    The normal core.shoe.Shoe uses an array plus a shuffle, modeling a
    real continuous shoe's penetration effects; that's not needed here —
    every hand is an independent, identically-distributed draw (we want to
    answer "what's the expected value of this specific fixed opening," not
    model continuous-shoe counting dynamics), and scenario simulation
    forces the same rare rank into every single hand (e.g. "8,8 vs. 10"
    removes 2 eights every hand). With a real shoe array, that would drain
    those ranks before the cut card and force a full reshuffle every hand
    anyway — and shuffling a 312-card array (a Python-level Fisher-Yates)
    measured only 5,800 shuffles/sec, which would become the whole
    pipeline's bottleneck.

    Using a quota array plus weighted random sampling instead (probability-
    wise exactly equivalent to sampling without replacement, just without
    ever actually arranging a full deck) means reset touches only 10
    numbers and draw() only scans 10 numbers — about two orders of
    magnitude faster than a full reshuffle.

    Only implements the interface engine.py/strategy.py actually use:
    draw(), start_round(), true_count, running_count (checked, exactly
    these four and nothing else).
    """
    __slots__ = ('num_decks', 'tags', 'rng', 'counts', 'running_count',
                 'forced', '_forced_i')

    def __init__(self, num_decks, tags, rng):
        self.num_decks = num_decks
        self.tags = tags
        self.rng = rng
        self.counts = [0] * 11
        self.running_count = 0
        self.forced = ()
        self._forced_i = 0

    def new_hand(self, forced):
        """Reset to a brand-new deck, and make the next len(forced) draw()
        calls return `forced`'s ranks in order."""
        n = self.num_decks
        c = self.counts
        for r in range(1, 10):
            c[r] = 4 * n
        c[10] = 16 * n
        self.running_count = 0
        self.forced = forced
        self._forced_i = 0

    def start_round(self):
        pass   # every hand is a brand-new deck, no cut card to check

    @property
    def decks_left(self):
        return max(sum(self.counts) / 52.0, 0.25)

    @property
    def true_count(self):
        return self.running_count / self.decks_left

    def draw(self):
        if self._forced_i < len(self.forced):
            rank = self.forced[self._forced_i]
            self._forced_i += 1
            if self.counts[rank] <= 0:
                raise ScenarioError(f"This deck has no cards of rank {rank} left "
                                    "(the requested opening isn't reachable)")
            self.counts[rank] -= 1
            self.running_count += self.tags[rank]
            return rank

        total = sum(self.counts)
        pick = self.rng.randrange(total)
        acc = 0
        for rank in range(1, 11):
            acc += self.counts[rank]
            if pick < acc:
                self.counts[rank] -= 1
                self.running_count += self.tags[rank]
                return rank
        raise AssertionError('Quota total and cumulative sum disagree, should never happen')


def _run_chunk(args):
    (rules, strat_name, player_cards, dealer_up, action, rounds,
     base_bet, seed) = args
    real = strategy_mod.make(strat_name, rules)
    rng = random.Random(seed)
    shoe = _ScenarioShoe(rules.num_decks, real.count_tags or HI_LO, rng)
    wrapped = _ForceFirstAction(real, action)
    c1, c2 = player_cards
    forced = (c1, dealer_up, c2)

    net = 0.0
    sumsq = 0.0
    t0 = time.perf_counter()
    for _ in range(rounds):
        shoe.new_hand(forced)
        wrapped.used_decide = False
        v = play_round(shoe, wrapped, rules, base_bet)[0]
        net += v
        sumsq += v * v
    return net, sumsq, rounds, time.perf_counter() - t0


# Scenario simulation's throughput (quota array + weighted random draws,
# roughly 130k hands/sec/core) is slower than the main simulation engine,
# so the chunk size cap here is set smaller than core/runner.py's 250_000,
# keeping cancel/progress latency in the same ballpark (roughly 1 second) —
# otherwise pressing cancel while "computing..." is showing could take a
# long time to actually take effect.
MAX_CHUNK = 150_000


def _plan_chunks(rounds, jobs, target_per_job=4, min_chunk=20_000, max_chunk=MAX_CHUNK):
    n = max(1, min(jobs * target_per_job, max(1, rounds // min_chunk)))
    n = max(n, -(-rounds // max_chunk))          # ceiling division, guarantees no chunk exceeds the cap
    base, extra = divmod(rounds, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def run_action(rules, strat_name, player_cards, dealer_up, action, rounds,
               base_bet=1.0, seed=20240514, jobs=None, progress=None, executor=None):
    """Run one action, returning (ev, ev_ci95, sd, rounds).

    If progress raises an exception (how the GUI implements cancellation),
    make sure chunks that haven't started yet don't keep occupying the
    process pool — the default behavior on leaving a `with` block is to
    wait for every already-submitted task to finish before actually
    closing, so cancellation would wait for whatever chunk is currently
    stuck to finish first; using a manual shutdown(cancel_futures=True)
    instead makes it take effect immediately.
    """
    jobs = jobs or os.cpu_count() or 1
    sizes = _plan_chunks(rounds, jobs)
    tasks = [(rules, strat_name, player_cards, dealer_up, action, sz,
              base_bet, seed + 7919 * k) for k, sz in enumerate(sizes)]

    net = sumsq = 0.0
    n = 0
    if jobs == 1 or len(tasks) == 1:
        for t in tasks:
            r_net, r_sumsq, r_n, _ = _run_chunk(t)
            net += r_net; sumsq += r_sumsq; n += r_n
            if progress:
                progress(n, rounds)
    else:
        own = executor is None
        ex = executor or ProcessPoolExecutor(max_workers=jobs)
        try:
            futures = {ex.submit(_run_chunk, t): sz for t, sz in zip(tasks, sizes)}
            for fut in as_completed(futures):
                r_net, r_sumsq, r_n, _ = fut.result()
                net += r_net; sumsq += r_sumsq; n += r_n
                if progress:
                    progress(n, rounds)
        finally:
            if own:
                ex.shutdown(cancel_futures=True)

    mean = net / n
    var = max((sumsq - net * net / n) / (n - 1), 0.0) if n > 1 else 0.0
    sd = var ** 0.5
    ci95 = 1.96 * sd / (n ** 0.5) if n > 1 else float('nan')
    return mean, ci95, sd, n


def compare_actions(rules, strat_name, player_cards, dealer_up, rounds,
                    actions=None, base_bet=1.0, seed=20240514, jobs=None,
                    progress=None):
    """Compare the EV of every legal action for this opening (same seed
    across all of them, progress updates once per action run).

    Returns [(action, name, ev, ci95, sd), ...], sorted by EV descending.
    Illegal actions (e.g. this table doesn't allow surrender, or the cards
    aren't a pair but split was requested) are simply skipped.
    """
    legal = legal_first_actions(player_cards, dealer_up, rules)
    candidates = [a for a in (actions or ALL_ACTIONS) if a & legal]
    if not candidates:
        raise ScenarioError("No legal actions to compare (check your rule settings)")

    jobs = jobs or os.cpu_count() or 1
    out = []
    done_before = [0]
    total_work = rounds * len(candidates)

    def sub_progress(done, _tot):
        if progress:
            progress(done_before[0] + done, total_work)

    ex = ProcessPoolExecutor(max_workers=jobs) if jobs > 1 and rounds >= 20_000 else None
    try:
        for act in candidates:
            ev, ci95, sd, n = run_action(rules, strat_name, player_cards, dealer_up,
                                         act, rounds, base_bet, seed, jobs,
                                         sub_progress, executor=ex)
            out.append((act, ACTION_NAMES[act], ev, ci95, sd))
            done_before[0] += rounds
    finally:
        if ex is not None:
            ex.shutdown(cancel_futures=True)
    out.sort(key=lambda x: -x[2])
    return out
