"""單一情境模擬：固定「玩家起手兩張＋莊家明牌」，比較每個第一步動作的 EV。

存在的意義：想知道「這桌規則下，這手牌真正該怎麼打」不該去查網路上的表——
那些表通常是針對某一組特定規則（例如 6D S17 DAS）算出來的，規則一變就可能不對。
這裡直接在你設定的規則下模擬，量出補/停/加倍/分牌/投降各自的 EV，答案是
「這桌規則的正確答案」，不是抄來的。

不直接複製 core/engine.py 的手牌流程（風險：容易跟主引擎的行為產生分歧，
peek/no-peek/OBO/投降這些語意都很細）。改用兩個技巧複用已驗證過的 play_round：

1. 把指定的起手兩張牌「安排」到牌靴接下來會發出的位置——play_round 內部
   的發牌順序固定是 p1, up, p2，只要把這三張牌換到牌靴目前發牌位置的
   最前面三格，shoe.draw() 自然就會照順序發出來，完全不用碰 play_round。
2. 包一層策略（_ForceFirstAction），只在第一次被問到「這手要幹嘛」時
   回傳指定動作，之後（包含分牌後的第二手）全部照真正的策略表打。

分牌沒有精確解（core/exact.py 只算不分牌的情況——分牌牽扯 DAS/RSA/
再分牌的組合爆炸，精確解代價太高），所以這裡的分牌 EV 一定帶抽樣誤差，
跑的手數要夠多。不分牌的動作可以用 core/solver.py 的精確解交叉驗證。
"""
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from .engine import (ACT_HIT, ACT_STAND, ACT_DOUBLE, ACT_SPLIT, ACT_SURRENDER,
                     Hand, legal_actions, play_round)
from . import strategy as strategy_mod
from .shoe import HI_LO

ACTION_NAMES = {
    ACT_HIT: '補牌 (H)', ACT_STAND: '停牌 (S)', ACT_DOUBLE: '加倍 (D)',
    ACT_SPLIT: '分牌 (P)', ACT_SURRENDER: '投降 (R)',
}
ALL_ACTIONS = (ACT_STAND, ACT_HIT, ACT_DOUBLE, ACT_SPLIT, ACT_SURRENDER)


class ScenarioError(Exception):
    pass


def parse_card(x):
    """'A'/'10'/'T'/8 都接受，統一成 1..10。"""
    s = str(x).strip().upper()
    if s in ('A', 'ACE', '1'):
        return 1
    if s in ('T', '10', 'J', 'Q', 'K'):
        return 10
    v = int(s)
    if not 1 <= v <= 10:
        raise ScenarioError(f"牌面超出範圍：{x}")
    return v


def legal_first_actions(player_cards, dealer_up, rules):
    """這個起手（兩張固定的牌）在這桌規則下，第一步有哪些動作合法。"""
    c1, c2 = player_cards
    h = Hand([c1, c2], 1.0)
    return legal_actions(h, dealer_up, 1, rules)


class _ForceFirstAction:
    """包住真正的策略：只攔截第一次決策，之後全部照真正策略打。

    early_surrender 是 no-peek+early 專屬的獨立詢問（在 decide 迴圈之前），
    所以要分開攔截：測的是投降就直接回 True（等同玩家選擇投降），
    測的是別的動作就強制回 False，讓手牌一定能走到 decide() 迴圈，
    不然真正策略可能會搶先幫這手牌投降掉，測不到我們要的那個動作。
    """
    __slots__ = ('real', 'action', 'used_decide')

    def __init__(self, real, action):
        self.real = real
        self.action = action
        self.used_decide = False

    def bet(self, shoe, rules, base_bet):
        return self.real.bet(shoe, rules, base_bet)

    def take_insurance(self, shoe, rules):
        return self.real.take_insurance(shoe, rules)

    def early_surrender(self, hand, up, shoe, rules):
        return self.action == ACT_SURRENDER

    def decide(self, hand, up, legal, shoe, rules):
        if not self.used_decide:
            self.used_decide = True
            return self.action
        return self.real.decide(hand, up, legal, shoe, rules)


class _ScenarioShoe:
    """給情境模擬用的輕量牌堆：不是牌陣列，是「每個點數還剩幾張」的配額。

    正常的 core.shoe.Shoe 用陣列＋洗牌，模擬真實連續牌靴的 penetration 效應；
    這裡不需要那個——每手都是獨立同分布的抽樣（要回答的是「這個固定起手
    的期望值」，不是連續牌靴的算牌動態），而且情境模擬每手都會強制塞相同
    的稀有點數（例如「8,8 對 10」每手扣 2 張 8），用真實牌靴陣列的話沒打
    到切牌點就會被榨乾，只能被迫每手重洗一整副 —— 洗一次 312 張的陣列
    （Python 層的 Fisher-Yates）實測只有 5,800 次/秒，會變成全流程的瓶頸。

    改用配額陣列 + 加權隨機抽取（機率上跟「不放回抽樣」完全等價，只是
    不用真的排列一整副牌），reset 一次只要動 10 個數字，draw() 一次也只要
    掃 10 個數字，比整副洗牌快了兩個數量級。

    只實作 engine.py／strategy.py 實際會用到的介面：draw()、start_round()、
    true_count、running_count（已經核對過，僅此四項）。
    """
    __slots__ = ('num_decks', 'tags', 'rng', 'counts', 'running_count',
                 'forced', '_forced_i')

    def __init__(self, num_decks, tags, rng):
        self.num_decks = num_decks
        self.tags = tags
        self.rng = rng
        self.counts = [0] * 11
        self.running_count = 0
        self.forced = ()
        self._forced_i = 0

    def new_hand(self, forced):
        """重置成一副全新的牌，且接下來 len(forced) 次 draw() 依序照 forced 發。"""
        n = self.num_decks
        c = self.counts
        for r in range(1, 10):
            c[r] = 4 * n
        c[10] = 16 * n
        self.running_count = 0
        self.forced = forced
        self._forced_i = 0

    def start_round(self):
        pass   # 每手都是全新的牌，沒有切牌點需要檢查

    @property
    def decks_left(self):
        return max(sum(self.counts) / 52.0, 0.25)

    @property
    def true_count(self):
        return self.running_count / self.decks_left

    def draw(self):
        if self._forced_i < len(self.forced):
            rank = self.forced[self._forced_i]
            self._forced_i += 1
            if self.counts[rank] <= 0:
                raise ScenarioError(f"這副牌裡已經沒有點數 {rank} 的牌了（指定的起手不合理）")
            self.counts[rank] -= 1
            self.running_count += self.tags[rank]
            return rank

        total = sum(self.counts)
        pick = self.rng.randrange(total)
        acc = 0
        for rank in range(1, 11):
            acc += self.counts[rank]
            if pick < acc:
                self.counts[rank] -= 1
                self.running_count += self.tags[rank]
                return rank
        raise AssertionError('配額加總與逐一累加不一致，不該發生')


def _run_chunk(args):
    (rules, strat_name, player_cards, dealer_up, action, rounds,
     base_bet, seed) = args
    real = strategy_mod.make(strat_name, rules)
    rng = random.Random(seed)
    shoe = _ScenarioShoe(rules.num_decks, real.count_tags or HI_LO, rng)
    wrapped = _ForceFirstAction(real, action)
    c1, c2 = player_cards
    forced = (c1, dealer_up, c2)

    net = 0.0
    sumsq = 0.0
    t0 = time.perf_counter()
    for _ in range(rounds):
        shoe.new_hand(forced)
        wrapped.used_decide = False
        v = play_round(shoe, wrapped, rules, base_bet)[0]
        net += v
        sumsq += v * v
    return net, sumsq, rounds, time.perf_counter() - t0


# 情境模擬的吞吐量（配額陣列 + 加權隨機抽取，約 13 萬手/秒/核）比主模擬引擎
# 慢，所以 chunk 上限抓得比 core/runner.py 的 250_000 更小一點，讓取消／
# 進度回報的延遲維持在同一個量級（大約 1 秒），不然「計算中…」時按取消
# 會卡住很久才生效。
MAX_CHUNK = 150_000


def _plan_chunks(rounds, jobs, target_per_job=4, min_chunk=20_000, max_chunk=MAX_CHUNK):
    n = max(1, min(jobs * target_per_job, max(1, rounds // min_chunk)))
    n = max(n, -(-rounds // max_chunk))          # 無條件進位，確保每段不超過上限
    base, extra = divmod(rounds, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def run_action(rules, strat_name, player_cards, dealer_up, action, rounds,
               base_bet=1.0, seed=20240514, jobs=None, progress=None, executor=None):
    """跑一個動作，回傳 (ev, ev_ci95, sd, rounds)。

    progress 拋出例外（GUI 用來實作取消）時，要確保還沒開始跑的 chunk
    不會繼續佔用 process pool —— 用 with 語法離開時預設會等所有已經
    submit 出去的任務跑完才真的關閉，取消會等到卡住的那個 chunk跑完
    才生效；改成手動 shutdown(cancel_futures=True) 才能立刻生效。
    """
    jobs = jobs or os.cpu_count() or 1
    sizes = _plan_chunks(rounds, jobs)
    tasks = [(rules, strat_name, player_cards, dealer_up, action, sz,
              base_bet, seed + 7919 * k) for k, sz in enumerate(sizes)]

    net = sumsq = 0.0
    n = 0
    if jobs == 1 or len(tasks) == 1:
        for t in tasks:
            r_net, r_sumsq, r_n, _ = _run_chunk(t)
            net += r_net; sumsq += r_sumsq; n += r_n
            if progress:
                progress(n, rounds)
    else:
        own = executor is None
        ex = executor or ProcessPoolExecutor(max_workers=jobs)
        try:
            futures = {ex.submit(_run_chunk, t): sz for t, sz in zip(tasks, sizes)}
            for fut in as_completed(futures):
                r_net, r_sumsq, r_n, _ = fut.result()
                net += r_net; sumsq += r_sumsq; n += r_n
                if progress:
                    progress(n, rounds)
        finally:
            if own:
                ex.shutdown(cancel_futures=True)

    mean = net / n
    var = max((sumsq - net * net / n) / (n - 1), 0.0) if n > 1 else 0.0
    sd = var ** 0.5
    ci95 = 1.96 * sd / (n ** 0.5) if n > 1 else float('nan')
    return mean, ci95, sd, n


def compare_actions(rules, strat_name, player_cards, dealer_up, rounds,
                    actions=None, base_bet=1.0, seed=20240514, jobs=None,
                    progress=None):
    """比較這個起手在每個合法動作下的 EV（同一組種子，跑一個動作更新一次進度）。

    回傳 [(action, name, ev, ci95, sd), ...]，依 EV 由高到低排序。
    非法動作（例如這桌規則不給投降、牌不是對子卻要求分牌）直接跳過。
    """
    legal = legal_first_actions(player_cards, dealer_up, rules)
    candidates = [a for a in (actions or ALL_ACTIONS) if a & legal]
    if not candidates:
        raise ScenarioError("沒有合法動作可以比較（檢查一下規則設定）")

    jobs = jobs or os.cpu_count() or 1
    out = []
    done_before = [0]
    total_work = rounds * len(candidates)

    def sub_progress(done, _tot):
        if progress:
            progress(done_before[0] + done, total_work)

    ex = ProcessPoolExecutor(max_workers=jobs) if jobs > 1 and rounds >= 20_000 else None
    try:
        for act in candidates:
            ev, ci95, sd, n = run_action(rules, strat_name, player_cards, dealer_up,
                                         act, rounds, base_bet, seed, jobs,
                                         sub_progress, executor=ex)
            out.append((act, ACTION_NAMES[act], ev, ci95, sd))
            done_before[0] += rounds
    finally:
        if ex is not None:
            ex.shutdown(cancel_futures=True)
    out.sort(key=lambda x: -x[2])
    return out
