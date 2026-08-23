import random
import math

class Dealer:
    def __init__(self):
        self.cards = []
        self.total = 0
        self.isBJ = False

class Player:
    def __init__(self):
        self.card1 = 0
        self.card2 = 0
        self.cards = []
        self.total = 0
        self.isBJ = False
        self.weight = 1
        self.splits = []

class Deck:
    def __init__(self, num_deck, penertration):
        self.num_deck = num_deck
        self.total = num_deck * 52
        self.deck = self.generate()
        self.pen = math.floor((penertration / 100) * (num_deck * 52))
        self.curidx = 0
        self.count = 0
        self.real = 0

    def generate(self):
        new_deck = []
        for i in range (1, 14):
            for j in range (4 * self.num_deck):
                if i >= 10:
                    new_deck.append(10)
                else:
                    new_deck.append(i)

        return self.shuffle(new_deck)
    
    def shuffle(self, deck):
        self.count = 0
        self.real = 0
        self.curidx = 0
        n = len(deck)
        for idx in range(n-1, 0, -1):
            newidx = random.randrange(0, idx)
            tmp = deck[idx]
            deck[idx] = deck[newidx]
            deck[newidx] = tmp
        return deck
    
    def draw(self, countcards):
        if not countcards:
            newCardInt = random.randrange(1,14)
            if newCardInt >= 10:
                return 10
            return newCardInt
        else:
            if(self.curidx > self.pen):
                self.deck = self.shuffle(self.deck)
            card = self.deck[self.curidx]
            self.curidx += 1

            if card == 10 or card == 1:
                self.count -= 1
            elif card >= 2 and card <= 6:
                self.count += 1
            
            remain = self.total - self.curidx
            self.real = self.count / (remain / 52)
            return card