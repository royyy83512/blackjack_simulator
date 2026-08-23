"""The card shoe.

Cards are represented as ints, 1 = ace, 10 covers 10/J/Q/K. Suit has no
effect on blackjack, so it isn't modeled — only rank is — and this loses
no precision at all.

The approach is "pre-shuffle a whole shoe, then deal in order," which is
mathematically equivalent to a real table and, among all the correct
approaches, the fastest one (one shuffle of 52*n cards amortized over
roughly 45 hands). The cut card is only checked before each new hand
starts — the shoe is never reshuffled mid-hand.
"""
import random

# Hi-Lo count tags (default). index = card rank 1..10, index 0 is unused.
HI_LO = (0, -1, 1, 1, 1, 1, 1, 0, 0, 0, -1)


class Shoe:
    """The shoe. tags are the per-card count values, set by the strategy
    file (see core/strategy.py)."""

    __slots__ = ('num_decks', 'penetration', 'total', 'cut', 'rng', 'tags',
                 'cards', 'idx', 'running_count', 'start_count', 'shuffles', 'csm')

    def __init__(self, num_decks=6, penetration=0.75, seed=None,
                 tags=HI_LO, start_count=0, csm=False):
        self.num_decks = num_decks
        self.penetration = penetration
        self.total = num_decks * 52
        self.cut = int(self.total * penetration)
        self.csm = csm            # continuous shuffling machine: reshuffles every hand, no real cut card
        self.rng = random.Random(seed)
        self.tags = tuple(tags)
        # unbalanced systems (like KO) start the running count at a value that scales with deck count
        self.start_count = start_count
        # the whole shoe's cards; shuffled in place afterward, never reallocated
        pool = []
        for rank in range(1, 14):
            pool.extend([rank if rank < 10 else 10] * (4 * num_decks))
        self.cards = pool
        self.idx = 0
        self.running_count = start_count
        self.shuffles = 0
        self.shuffle()

    def shuffle(self):
        # random.shuffle is a correct, C-implemented Fisher-Yates, much
        # faster than a hand-rolled Python loop
        self.rng.shuffle(self.cards)
        self.idx = 0
        self.running_count = self.start_count
        self.shuffles += 1

    @property
    def cards_left(self):
        return self.total - self.idx

    @property
    def decks_left(self):
        return max(self.cards_left / 52.0, 0.25)

    @property
    def true_count(self):
        return self.running_count / self.decks_left

    def start_round(self):
        """Called before each hand: reshuffle if past the cut card. Never
        reshuffles mid-hand.

        A CSM (continuous shuffling machine) has no cut card and reshuffles
        every hand — a real machine continuously feeds used cards back in
        and reshuffles, which statistically is equivalent to "every hand
        starts from a freshly shuffled deck." We reuse the same shuffle()
        mechanism to get that effect instead of maintaining separate logic.
        A useful side effect: shuffle() resets running_count back to
        start_count, so a counting strategy under CSM always measures a
        true count near its baseline — which is exactly why a real CSM
        defeats card counting, with no special-casing needed in the
        counting logic itself.
        """
        if self.csm or self.idx >= self.cut:
            self.shuffle()

    def draw(self):
        i = self.idx
        if i >= self.total:      # safety net: an extremely long hand that exhausts the whole shoe
            self.shuffle()
            i = 0
        card = self.cards[i]
        self.idx = i + 1
        self.running_count += self.tags[card]
        return card
