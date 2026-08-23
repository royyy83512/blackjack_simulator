import draw_card as dr
from single_game import *
from config import *
from character import *
import matplotlib.pyplot as plt

# config
run_chart = RUNCHART
run_stat = RUNSTAT
hands = HANDS
rounds = ROUNDS
bet = BET
countcards = COUNT

print('=== start ===')
win = 0
loss = 0
push = 0
dbj = 0
current = 0
cur_arr = []
global_arr = []
global_res = []
real1 = 0

for i in range(rounds):
    current = 0
    cur_arr = []
    dek = Deck(6, 80)

    for j in range(hands):
        single_game = SingleGame(bet, dek, countcards)
        single_game.run()
        res = single_game.result

        if(res > 0):
            win += 1
        elif(res < 0):
            loss += 1
        else:
            push += 1
        current += single_game.result

        if single_game.real1:
            real1 += 1

        if run_chart:
            cur_arr.append(current)
    if run_chart:
        global_arr.append(cur_arr)
    global_res.append(current)
    
print('win: ' + str(win))
print('loss:' + str(loss))
print('push: ' + str(push))
#print('real1: ' + str(real1))
if not run_stat:
    print('res = ' + str(global_res))

if run_chart:
    x = []
    for i in range(hands):
        x.append(i)
    for i in range(rounds):
        plt.plot(x, global_arr[i], linestyle="-", linewidth="0.5") 
    plt.show()

if run_stat:
    if rounds < 20:
        print('to run statistic, rounds should >= 20')
    else:    
        minimum = 100000
        maximum = -100000
        stat_arr = [0] * 20
        x = []
        for i in range(20):
            x.append(i+1)
        
        for res in global_res:
            if res < minimum:
                minimum = res
            if res > maximum:
                maximum = res
        rang = (maximum - minimum) / 20
        for i in range (20):
            mid_in_range = minimum + (i+0.5) * rang
        total = 0
        for i in range(rounds):
            data = global_res[i]
            total += data
            level = (data - minimum) // rang
            level = int (level) - 1
            if(level == 0):
                level = 1
            #print(data)
            stat_arr[level] += 1
        avg = total / rounds
        standard = standard_d(global_res)
        print('avg = ' + str(avg))
        print('stand = ' + str(standard))
        plt.bar(x,stat_arr,width=0.5)
        plt.show()
    


