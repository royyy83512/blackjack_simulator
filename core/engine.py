"""一手 blackjack 的完整流程。

設計重點
--------
* 動作用 int bitmask（ACT_*）表示，避免在決策熱路徑上配置 set。
* Hand 快取點數，補牌是 O(1) 更新，不用每次 sum()。
* peek / no-peek 的發牌張數不同：peek 一開始就發底牌（4 張），
  no-peek 只發 3 張，底牌等玩家動作結束才補。這會影響牌靴消耗，
  所以必須照實模擬。
* 分牌後的第二張牌等輪到那一手才發，跟真實牌桌一致。

投降的語意（重要，兩者差別就是 early surrender 值錢的原因）
* peek + late  ：莊家確認沒 BJ 後才投降，穩定輸一半。
* no-peek+late ：玩家先投降，之後莊家翻出 BJ 的話 —— 輸全額。
* no-peek+early：莊家翻牌前投降，不管莊家有沒有 BJ 都只輸一半。
"""
from .rules import SURRENDER_EARLY, LOSS_ORIGINAL

# 玩家動作
ACT_HIT, ACT_STAND, ACT_DOUBLE, ACT_SPLIT, ACT_SURRENDER = 1, 2, 4, 8, 16
ACT_NAME = {ACT_HIT: 'H', ACT_STAND: 'S', ACT_DOUBLE: 'D',
            ACT_SPLIT: 'P', ACT_SURRENDER: 'R'}

# 一手的事件旗標
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
    """回傳這一手當下合法動作的 bitmask。"""
    cards = h.cards
    ncards = len(cards)

    # 分到的 A：一張定生死（除非規則允許補牌）
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
    """莊家補到 17（或軟 17 也補），回傳最終點數，爆牌則 > 21。"""
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
    """打完一手（含分牌）。

    回傳 (net, initial_wager, total_wager, n_hands, dealer_total, flags)
    net 是這一手的淨損益（單位：注碼），dealer_total 為 0 表示莊家沒動作。
    """
    shoe.start_round()
    bet = strategy.bet(shoe, rules, base_bet)
    net = 0.0
    flags = 0

    # 發牌：玩家、莊家明牌、玩家、（peek 才發底牌）
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

    # ---- peek 模式：莊家先看底牌 ----
    if rules.dealer_peek:
        hole = shoe.draw()
        dealer_cards.append(hole)
        dealer_bj = _is_bj(up, hole)
        if dealer_bj:
            flags |= F_DEALER_BJ
            if ins_bet:
                net += 2 * ins_bet
            if player_bj:
                flags |= F_PLAYER_BJ            # 和局
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
        # ---- no-peek：early surrender 在莊家補牌前決定 ----
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

    # ---- 玩家行動 ----
    hands = [player]
    i = 0
    while i < len(hands):
        h = hands[i]
        if len(h.cards) == 1:            # 分牌後補的第二張
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
                # 不能在這裡 break：分到 A 之後若再拿到 A，開了 RSA 是可以再分的。
                # legal_actions() 已經處理「分到的 A 只能停牌」，交給它判斷即可，
                # 否則第一手不會有 RSA 機會、第二手卻會，行為不對稱。
                continue
            h.add(shoe.draw())           # ACT_HIT
        i += 1

    # ---- no-peek：現在才翻底牌 ----
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
                return (net, bet, bet + ins_bet, 1, 21, flags)   # 和局
            if rules.dealer_bj_loss == LOSS_ORIGINAL:
                net -= bet                                       # OBO：只輸原始注
                return (net, bet, bet + ins_bet, len(hands), 21, flags)
        elif player_bj:
            flags |= F_PLAYER_BJ
            net += bet * rules.blackjack_pays
            return (net, bet, bet + ins_bet, 1, 0, flags)

    # ---- 莊家行動 ----
    dealer_total = 21 if dealer_bj else 0
    if not dealer_bj:
        for h in hands:
            if not h.surrendered and not h.busted:
                dealer_total = dealer_play(dealer_cards, shoe,
                                           rules.dealer_hits_soft_17)
                break
    if dealer_total > 21:
        flags |= F_DEALER_BUST

    # ---- 結算 ----
    total_wager = ins_bet
    for h in hands:
        total_wager += h.bet
        if h.surrendered:
            # no-peek 的 late surrender 碰到莊家 BJ 要輸全額
            net -= h.bet if dealer_bj else h.bet * 0.5
            continue
        if h.busted:
            flags |= F_PLAYER_BUST
            net -= h.bet
            continue
        if dealer_bj:
            # 莊家 natural 通殺：連玩家三張湊成的 21 也照輸不誤
            # （玩家自己的 natural 在前面已經先判成和局了）
            net -= h.bet
            continue
        ht = h.total
        if dealer_total > 21 or ht > dealer_total:
            net += h.bet
        elif ht < dealer_total:
            net -= h.bet

    return (net, bet, total_wager, len(hands), dealer_total, flags)
