"""Casino rule configuration.

A Rules object describes the complete rule set for one table. When rules
logically conflict, normalize() resolves them and reports which options
got overridden, so the UI can show the reason.
"""
from dataclasses import dataclass, replace, asdict

# surrender modes
SURRENDER_NONE = 'none'
SURRENDER_LATE = 'late'    # can only surrender after the dealer checks for no BJ
SURRENDER_EARLY = 'early'  # can surrender before the dealer checks for BJ (no-peek only)

# when doubling is allowed
DOUBLE_ANY2 = 'any2'      # any two starting cards
DOUBLE_9_11 = '9-11'      # hard 9/10/11 only
DOUBLE_10_11 = '10-11'    # hard 10/11 only

# how much the player loses on a dealer BJ (only meaningful under no-peek)
LOSS_ALL = 'all'           # busted bets: extra wagers from doubles/splits are all lost
LOSS_ORIGINAL = 'original' # OBO (original bets only): extra wagers are returned

DECK_MIN, DECK_MAX = 2, 8


@dataclass(frozen=True)
class Rules:
    num_decks: int = 6
    penetration: float = 0.75          # fraction dealt before the cut card (irrelevant if CSM is on)
    continuous_shuffle: bool = False   # CSM: reshuffles every hand, no cut card
    dealer_hits_soft_17: bool = False  # H17 / S17
    double_rule: str = DOUBLE_ANY2
    double_after_split: bool = True    # DAS
    surrender: str = SURRENDER_LATE
    surrender_vs_ace: bool = True      # some casinos don't offer surrender vs. dealer ace
    dealer_peek: bool = True           # dealer has a hole card and peeks at it
    resplit_aces: bool = False         # RSA
    hit_split_aces: bool = False       # can hit again after splitting aces
    blackjack_pays: float = 1.5        # 3:2 = 1.5, 6:5 = 1.2
    dealer_bj_loss: str = LOSS_ALL     # see LOSS_* (irrelevant in peek mode)
    insurance_offered: bool = True

    # fixed, not user-adjustable: consistent across casinos
    max_split_hands: int = 4           # max 4 hands from splitting (aces excepted, see resplit_aces)

    # ---- derived properties ----
    @property
    def can_double_soft(self) -> bool:
        return self.double_rule == DOUBLE_ANY2

    def surrender_allowed_vs(self, upcard: int) -> bool:
        """Whether surrender is legal against this particular upcard."""
        if self.surrender == SURRENDER_NONE:
            return False
        return self.surrender_vs_ace or upcard != 1

    def double_allowed_on(self, hard_total: int, is_soft: bool) -> bool:
        if self.double_rule == DOUBLE_ANY2:
            return True
        if is_soft:
            return False
        if self.double_rule == DOUBLE_9_11:
            return 9 <= hard_total <= 11
        return 10 <= hard_total <= 11

    def label(self) -> str:
        bits = [
            f"{self.num_decks}D",
            "H17" if self.dealer_hits_soft_17 else "S17",
            "DAS" if self.double_after_split else "noDAS",
            {DOUBLE_ANY2: "DOA", DOUBLE_9_11: "D9", DOUBLE_10_11: "D10"}[self.double_rule],
            ({SURRENDER_NONE: "noSur", SURRENDER_LATE: "LS", SURRENDER_EARLY: "ES"}[self.surrender]
             + ("" if self.surrender_vs_ace or self.surrender == SURRENDER_NONE else "(noA)")),
            "peek" if self.dealer_peek else "NHC",
            "3:2" if abs(self.blackjack_pays - 1.5) < 1e-9 else f"{self.blackjack_pays:g}x",
        ]
        if self.continuous_shuffle:
            bits.append("CSM")
        if self.resplit_aces:
            bits.append("RSA")
        if self.hit_split_aces:
            bits.append("HSA")
        if not self.dealer_peek and self.dealer_bj_loss == LOSS_ORIGINAL:
            bits.append("OBO")
        return " ".join(bits)

    def to_dict(self):
        return asdict(self)


def normalize(rules: Rules):
    """Resolve mutually contradictory rule combinations.

    Returns (fixed Rules, [note strings]). The UI can use the notes to
    disable the corresponding controls.
    """
    notes = []
    r = rules

    if not (DECK_MIN <= r.num_decks <= DECK_MAX):
        clamped = max(DECK_MIN, min(DECK_MAX, r.num_decks))
        notes.append(f"Number of decks {r.num_decks} is out of range {DECK_MIN}-{DECK_MAX}, clamped to {clamped}")
        r = replace(r, num_decks=clamped)

    if not (0.30 <= r.penetration <= 0.95):
        clamped = max(0.30, min(0.95, r.penetration))
        notes.append(f"Penetration must be between 30%-95%, clamped to {clamped:.0%}")
        r = replace(r, penetration=clamped)

    if r.continuous_shuffle:
        notes.append("Continuous shuffling machine (CSM): reshuffles every hand, no cut card, "
                     "so the penetration setting is ignored; card counting is therefore useless "
                     "(the true count always stays near its baseline)")

    if r.dealer_peek:
        # the dealer peeks at the hole card, so BJ is already settled before the player acts
        if r.surrender == SURRENDER_EARLY:
            notes.append("Early surrender isn't possible when the dealer peeks (BJ is already "
                         "settled), switched to late surrender")
            r = replace(r, surrender=SURRENDER_LATE)
        if r.dealer_bj_loss != LOSS_ALL:
            notes.append("With dealer peek, only the original bet is ever at risk anyway, "
                         "so the OBO option is meaningless")
            r = replace(r, dealer_bj_loss=LOSS_ALL)

    return r, notes
