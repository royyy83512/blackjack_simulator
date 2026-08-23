from basic_strategy import *
import draw_card as dr
import math

def player_get_cards(player, d1, dek, countcards):
    if(len(player.cards) < 2):
        return
    p1 = player.cards[0]
    p2 = player.cards[1]
    player.total = p1 + p2
    soft = False
    if(p1 == 1 or p2 == 1):
        soft = True

    if(soft):
        if(soft_table[player.total][d1] == 'D'):
            return double_bet(player, dek, countcards)
        if(soft_table[player.total][d1] == 'S'):
            player.total += 10
            return
    
    if(table[player.total][d1] == 'D'):
        double_bet(player, dek, countcards)
        return
    
    while(player.total < 17):
        if(soft):
            if(player.total + 10 <= 21 and player.total + 10 >= 18):
                if(soft_table[player.total][d1] == 'S'):
                    player.total += 10
                    return

        st = table[player.total][d1]
        #print('strategy is: ' + st)
        if(st == 'S'):
            return

        if(st == 'H' or st == 'D'):
            hit = dek.draw(countcards)
            player.cards.append(hit)
            #print('add' + str(hit))
            if(hit == 1):
                soft = True
            player.total += hit
            if(player.total > 21):
                return
    return

def double_bet(player, dek, countcards):
    #print('[double] ')
    player.weight = 2
    p1 = player.cards[0]
    p2 = player.cards[1]
    p3 = dek.draw(countcards)
    player.cards.append(p3)
    pTotal = p1 + p2 + p3
    if(p1 == 1 or p2 == 1 or p3 == 1):
        if(pTotal + 10 <= 21):
            pTotal += 10
    player.total = pTotal
    return pTotal

def split(player, d1, dek, countcards):
    # [[total, w], [total, w], ...] maximum 4 hands
    if(player.cards[0] == 1):
        split_aces(player, dek, countcards)
        return
    hands = 2
    card = player.cards[0]
    queue = [card, card]
    while(len(queue) > 0):
        newCard = dek.draw(countcards)
        #print('split and get a new card: ' + str(newCard))
        if(newCard == card and hands < 4):
            queue.append(card)
            hands += 1
            continue
        player.weight = 1
        player.cards[0] = card
        player.cards[1] = newCard
        player_get_cards(player, d1, dek, countcards)
        player.splits.append([player.total, player.weight])
        queue.pop()

def split_aces(player, dek, countcards):
    newCard = dek.draw(countcards)
    player.splits.append([newCard + 11, 1])
    newCard = dek.draw(countcards)
    player.splits.append([newCard + 11, 1])
 

def dealer_get_cards(dealer, dek, countcards):
    d1 = dealer.cards[0]
    d2 = dealer.cards[1]
    dealer.total = d1 + d2
    if(d1 + d2 >= 17):
        return
    soft = False
    if(d1 == 1 or d2 == 1):
        soft = True

    while(dealer.total < 17):
        if(soft and dealer.total + 10 >= 17 and dealer.total + 10 <= 21):
            dealer.total += 10
            return
        hit = dek.draw(countcards)
        if(hit == 1):
            soft = True
        dealer.cards.append(hit)
        dealer.total += hit

def standard_d(data):
    length = len(data)
    sd = 0
    sum = 0
    for i in range(length):
        sum += data[i]
    avg = sum / length
    sum = 0

    for i in range(length):
        sum += (data[i] - avg) * (data[i] - avg)
        sd = math.sqrt(sum / length)
    return sd

def log(msg, on):
    if(on):
        print(msg)
