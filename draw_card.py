import random

def draw2():
    newCardInt = random.randrange(1,14)
    if newCardInt >= 10:
        return 10
    return newCardInt