from character import *
from basic_strategy import *
from bj_lib import *
from log import *
from config import *
import draw_card as dr

class SingleGame:
    def __init__(self, bet, dek, countcards):
        self.bet = bet
        self.dek = dek
        self.result = 0
        self.dealer = Dealer()
        self.player = Player()
        self.on = False
        self.real1 = False
        self.countcards = countcards
        

    def run(self):
        log('================', self.on)
        if self.countcards:
            if self.dek.real > 2 and self.dek.real <= 3:
                self.bet *= 2
            elif self.dek.real > 3 and self.dek.real <= 4:
                self.bet *= 4
            elif self.dek.real > 4:
                self.bet *= 8

        d1 = self.dek.draw(self.countcards)
        d2 = self.dek.draw(self.countcards)
        #d1 = 4
        #d2 = 6
        self.dealer.cards.append(d1)
        self.dealer.cards.append(d2)
        
        self.dealer.total = d1 + d2
        log('dealer:' + str(self.dealer.cards[0]), self.on)

        p1 = self.dek.draw(self.countcards)
        p2 = self.dek.draw(self.countcards)
        #p1 = 7
        #p2 = 5
        
        self.player.card1 = p1
        self.player.card2 = p2
        self.player.total = p1 + p2
        self.player.cards.append(p1)
        self.player.cards.append(p2)
        log('player:' + str(p1) + ', ' + str(p2) + ' total: ' + str(self.player.total), self.on)

        if([self.player.total, d1] in surrender_table):
            self.result = self.bet * -0.5
            return

        # anyone is BJ, game ended
        if((p1 == 1 and p2 == 10) or (p1 == 10 and p2 == 1)):
            self.player.isBJ = True
        if((d1 == 1 and d2 == 10) or (d1 == 10 and d2 == 1)):
            self.dealer.isBJ = True
        if self.player.isBJ and self.dealer.isBJ:
            self.result = 0
            return
        if self.player.isBJ:
            self.result = self.bet * 1.5
            return
        if self.dealer.isBJ:
            self.result = -self.bet
            return
        
        if(p1 == p2 and split_table[p1][d1] == 'Y'):
            split(self.player, d1, self.dek, self.countcards)
            dealer_get_cards(self.dealer, self.dek, self.countcards)
            log('dealer total is ' + str(self.dealer.total), self.on)
            for sp in self.player.splits:
                points = sp[0]
                w = sp[1]
                log('sp_points: ' + str(points) + ' weight: ' + str(w), self.on)
                if(points > 21):
                    self.result -= self.bet
                    continue
                if(self.dealer.total > 21):
                    self.result += self.bet * w
                    continue
                
                if(points > self.dealer.total):
                    self.result += self.bet * w
                elif(points < self.dealer.total):
                    self.result -= self.bet * w
            return

        if(self.player.total < 17):
            player_get_cards(self.player, d1, self.dek, self.countcards)

        if(self.player.total > 21):
            log('burst', self.on)
            self.result = -self.bet * self.player.weight
            return
        
        dealer_get_cards(self.dealer, self.dek, self.countcards)
        log('all player cards:', self.on)
        for i in range(0, len(self.player.cards)):
            log(self.player.cards[i], self.on)
        log('player total = ' + str(self.player.total), self.on)

        log('all dealer cards:', self.on)
        for i in range(0, len(self.dealer.cards)):
            log(self.dealer.cards[i], self.on)
        log('dealer total = ' + str(self.dealer.total), self.on)

        if(self.dealer.total > 21):
            self.result = self.bet * self.player.weight
            return
        
        if(self.player.total > self.dealer.total):
            self.result = self.bet * self.player.weight
            return
        elif(self.player.total < self.dealer.total):
            self.result = -self.bet * self.player.weight
            return
        else:
            self.result = 0
            return
