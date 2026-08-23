"""牌靴。

牌用 int 表示，1 = A，10 涵蓋 10/J/Q/K。花色對 blackjack 沒有影響，
所以不建模花色，只建模點數 —— 這不損失任何精確度。

作法是「預先洗好一整靴，然後依序發」，這在數學上等同真實牌桌，
而且是所有正確作法裡最快的（洗一次 52*n 張，攤提到約 45 手）。
切牌點只在每一手開始前檢查，絕不在一手牌中間洗牌。
"""
import random

# Hi-Lo 計數標籤（預設）。index = 牌點數 1..10，index 0 沒用到。
HI_LO = (0, -1, 1, 1, 1, 1, 1, 0, 0, 0, -1)


class Shoe:
    """牌靴。tags 是每張牌的計數標籤，由策略檔決定（見 core/strategy.py）。"""

    __slots__ = ('num_decks', 'penetration', 'total', 'cut', 'rng', 'tags',
                 'cards', 'idx', 'running_count', 'start_count', 'shuffles', 'csm')

    def __init__(self, num_decks=6, penetration=0.75, seed=None,
                 tags=HI_LO, start_count=0, csm=False):
        self.num_decks = num_decks
        self.penetration = penetration
        self.total = num_decks * 52
        self.cut = int(self.total * penetration)
        self.csm = csm            # 連續洗牌機：每一手都重洗，切牌點形同虛設
        self.rng = random.Random(seed)
        self.tags = tuple(tags)
        # 不平衡系統（如 KO）的流水數起點會隨牌副數改變
        self.start_count = start_count
        # 一整靴的牌，之後只做 in-place shuffle，不重新配置
        pool = []
        for rank in range(1, 14):
            pool.extend([rank if rank < 10 else 10] * (4 * num_decks))
        self.cards = pool
        self.idx = 0
        self.running_count = start_count
        self.shuffles = 0
        self.shuffle()

    def shuffle(self):
        # random.shuffle 是 C 實作的正確 Fisher-Yates，比手寫 Python 迴圈快得多
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
        """每一手開始前呼叫：過了切牌點就洗牌。永不在手牌中途洗牌。

        CSM（連續洗牌機）沒有切牌點，每一手都重洗——真實機器是把用過的牌
        連續送回去重洗，統計效果等同「每一手都從一副全新洗好的牌開始發」，
        這裡直接用同一個機制達成，不用另外維護一套邏輯。順帶的正確副作用：
        shuffle() 會把 running_count 重設回 start_count，所以算牌策略在
        CSM 下量到的真數永遠貼近基準值——這正是真實 CSM 讓算牌失效的原因，
        不用另外特別處理算牌邏輯。
        """
        if self.csm or self.idx >= self.cut:
            self.shuffle()

    def draw(self):
        i = self.idx
        if i >= self.total:      # 安全網：一手牌極端長時用光整靴
            self.shuffle()
            i = 0
        card = self.cards[i]
        self.idx = i + 1
        self.running_count += self.tags[card]
        return card
