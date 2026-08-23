"""The full playthrough of a single blackjack hand.

Design notes
------------
* Actions are int bitmasks (ACT_*), avoiding set allocation on the hot
  decision path.
* Hand caches its total, so hitting is an O(1) update instead of summing
  every time.
* peek and no-peek deal a different number of cards up front: peek deals
  the hole card immediately (4 cards total), no-peek only deals 3, and
  the hole card is drawn after the player finishes acting. This changes
  shoe consumption, so it has to be simulated faithfully.
* The second card of a split hand isn't dealt until that hand comes up,
  matching a real table.

Surrender semantics (important — this difference is exactly why early
surrender is worth more)
* peek + late:     surrender only after the dealer confirms no BJ, a
                    guaranteed half-loss.
* no-peek + late:  the player surrenders first; if the dealer then flips
                    a BJ, the full bet is lost.
* no-peek + early: the player surrenders before the dealer's hole card is
                    even revealed, so it's always a flat half-loss
                    regardless of whether the dealer has BJ.
"""
from .rules import SURRENDER_EARLY, LOSS_ORIGINAL

# player actions
ACT_HIT, ACT_STAND, ACT_DOUBLE, ACT_SPLIT, ACT_SURRENDER = 1, 2, 4, 8, 16
ACT_NAME = {ACT_HIT: 'H', ACT_STAND: 'S', ACT_DOUBLE: 'D',
            ACT_SPLIT: 'P', ACT_SURRENDER: 'R'}

# per-hand event flags
F_PLAYER_BJ, F_DEALER_BJ, F_DOUBLED, F_SPLIT = 1, 2, 4, 8
F_SURRENDER, F_PLAYER_BUST, F_DEALER_BUST, F_INSURANCE = 16, 32, 64, 128


class Hand:
    __slots__ = ('cards', 'bet', 'from_split', 'split_ace',
                 'doubled', 'surrendered', '_sum', '_aces')

    def __init__(self, cards, bet, from_split=False, split_ace=False):
        self.cards = cards
        self.bet = bet
        self.from_split = from_split
        self.split_ace = split_ace
        self.doubled = False
        self.surrendered = False
        self._sum = sum(cards)
        self._aces = cards.count(1)

    def add(self, card):
        self.cards.append(card)
        self._sum += card
        if card == 1:
            self._aces += 1

    @property
    def total(self):
        s = self._sum
        return s + 10 if (self._aces and s + 10 <= 21) else s

    @property
    def soft(self):
        return self._aces > 0 and self._sum + 10 <= 21

    @property
    def hard_total(self):
        return self._sum

    @property
    def is_pair(self):
        c = self.cards
        return len(c) == 2 and c[0] == c[1]

    @property
    def busted(self):
        return self._sum > 21


def legal_actions(h, up, n_hands, rules):
    """Return the bitmask of actions legal for this hand right now."""
    cards = h.cards
    ncards = len(cards)

    # a split ace: one card decides everything (unless the rules allow hitting it)
    if h.split_ace and not rules.hit_split_aces:
        legal = ACT_STAND
        if (rules.resplit_aces and ncards == 2 and cards[0] == 1 and cards[1] == 1
                and n_hands < rules.max_split_hands):
            legal |= ACT_SPLIT
        return legal

    legal = ACT_HIT | ACT_STAND
    if ncards == 2:
        if ((not h.from_split or rules.double_after_split)
                and rules.double_allowed_on(h.hard_total, h.soft)):
            legal |= ACT_DOUBLE
        if cards[0] == cards[1] and n_hands < rules.max_split_hands:
            if cards[0] != 1 or not h.from_split or rules.resplit_aces:
                legal |= ACT_SPLIT
        if (not h.from_split and n_hands == 1
                and rules.surrender_allowed_vs(up)):
            legal |= ACT_SURRENDER
    return legal


def dealer_play(cards, shoe, hits_soft_17):
    """Draw for the dealer up to 17 (or past a soft 17 too, if applicable).
    Returns the final total; > 21 means the dealer busted."""
    s = sum(cards)
    aces = cards.count(1)
    while True:
        soft = aces > 0 and s + 10 <= 21
        t = s + 10 if soft else s
        if t < 17 or (t == 17 and soft and hits_soft_17):
            c = shoe.draw()
            cards.append(c)
            s += c
            if c == 1:
                aces += 1
        else:
            return t


def _is_bj(a, b):
    return (a == 1 and b == 10) or (a == 10 and b == 1)


def play_round(shoe, strategy, rules, base_bet=1.0):
    """Play out one hand (including any splits).

    Returns (net, initial_wager, total_wager, n_hands, dealer_total, flags).
    net is this hand's net result in bet units; dealer_total of 0 means
    the dealer never had to act.
    """
    shoe.start_round()
    bet = strategy.bet(shoe, rules, base_bet)
    net = 0.0
    flags = 0

    # deal: player, dealer upcard, player, (hole card only if peek)
    p1 = shoe.draw()
    up = shoe.draw()
    p2 = shoe.draw()
    dealer_cards = [up]
    player = Hand([p1, p2], bet)
    player_bj = _is_bj(p1, p2)

    ins_bet = 0.0
    if up == 1 and rules.insurance_offered and strategy.take_insurance(shoe, rules):
        ins_bet = bet * 0.5
        flags |= F_INSURANCE

    # ---- peek mode: the dealer checks the hole card first ----
    if rules.dealer_peek:
        hole = shoe.draw()
        dealer_cards.append(hole)
        dealer_bj = _is_bj(up, hole)
        if dealer_bj:
            flags |= F_DEALER_BJ
            if ins_bet:
                net += 2 * ins_bet
            if player_bj:
                flags |= F_PLAYER_BJ            # push
            else:
                net -= bet
            return (net, bet, bet + ins_bet, 1, 21, flags)
        if ins_bet:
            net -= ins_bet
        if player_bj:
            flags |= F_PLAYER_BJ
            net += bet * rules.blackjack_pays
            return (net, bet, bet + ins_bet, 1, 0, flags)
    else:
        # ---- no-peek: early surrender is decided before the dealer draws ----
        if (rules.surrender == SURRENDER_EARLY and not player_bj
                and rules.surrender_allowed_vs(up)
                and strategy.early_surrender(player, up, shoe, rules)):
            flags |= F_SURRENDER
            net -= bet * 0.5
            hole = shoe.draw()
            dealer_cards.append(hole)
            if _is_bj(up, hole):
                flags |= F_DEALER_BJ
                if ins_bet:
                    net += 2 * ins_bet
            elif ins_bet:
                net -= ins_bet
            return (net, bet, bet + ins_bet, 1, 0, flags)

    # ---- player acts ----
    hands = [player]
    i = 0
    while i < len(hands):
        h = hands[i]
        if len(h.cards) == 1:            # the second card dealt after a split
            h.add(shoe.draw())
        while True:
            if h.total >= 21:
                break
            legal = legal_actions(h, up, len(hands), rules)
            act = strategy.decide(h, up, legal, shoe, rules)
            if not (act & legal):
                act = ACT_STAND if (legal & ACT_STAND) else ACT_HIT
            if act == ACT_STAND:
                break
            if act == ACT_SURRENDER:
                h.surrendered = True
                flags |= F_SURRENDER
                break
            if act == ACT_DOUBLE:
                h.bet *= 2
                h.doubled = True
                flags |= F_DOUBLED
                h.add(shoe.draw())
                break
            if act == ACT_SPLIT:
                flags |= F_SPLIT
                card = h.cards.pop()
                h._sum -= card
                if card == 1:
                    h._aces -= 1
                is_ace = (card == 1)
                h.from_split = True
                h.split_ace = is_ace
                h.add(shoe.draw())
                hands.insert(i + 1, Hand([card], bet, from_split=True, split_ace=is_ace))
                # can't break here: if this hand was a split ace and draws
                # another ace, RSA (if enabled) allows splitting again.
                # legal_actions() already handles "a split ace can only
                # stand", so just let it decide — otherwise the first hand
                # would never get an RSA chance while the second one would,
                # which is asymmetric and wrong.
                continue
            h.add(shoe.draw())           # ACT_HIT
        i += 1

    # ---- no-peek: the hole card is revealed only now ----
    dealer_bj = False
    if not rules.dealer_peek:
        hole = shoe.draw()
        dealer_cards.append(hole)
        dealer_bj = _is_bj(up, hole)
        if ins_bet:
            net += 2 * ins_bet if dealer_bj else -ins_bet
        if dealer_bj:
            flags |= F_DEALER_BJ
            if player_bj:
                flags |= F_PLAYER_BJ
                return (net, bet, bet + ins_bet, 1, 21, flags)   # push
            if rules.dealer_bj_loss == LOSS_ORIGINAL:
                net -= bet                                       # OBO: only the original bet is at risk
                return (net, bet, bet + ins_bet, len(hands), 21, flags)
        elif player_bj:
            flags |= F_PLAYER_BJ
            net += bet * rules.blackjack_pays
            return (net, bet, bet + ins_bet, 1, 0, flags)

    # ---- dealer acts ----
    dealer_total = 21 if dealer_bj else 0
    if not dealer_bj:
        for h in hands:
            if not h.surrendered and not h.busted:
                dealer_total = dealer_play(dealer_cards, shoe,
                                           rules.dealer_hits_soft_17)
                break
    if dealer_total > 21:
        flags |= F_DEALER_BUST

    # ---- settle ----
    total_wager = ins_bet
    for h in hands:
        total_wager += h.bet
        if h.surrendered:
            # under no-peek, late surrender loses the full bet if the dealer has BJ
            net -= h.bet if dealer_bj else h.bet * 0.5
            continue
        if h.busted:
            flags |= F_PLAYER_BUST
            net -= h.bet
            continue
        if dealer_bj:
            # dealer natural crushes everything, even a 3-card 21 the
            # player drew into (the player's own natural was already
            # settled as a push above)
            net -= h.bet
            continue
        ht = h.total
        if dealer_total > 21 or ht > dealer_total:
            net += h.bet
        elif ht < dealer_total:
            net -= h.bet

    return (net, bet, total_wager, len(hands), dealer_total, flags)
