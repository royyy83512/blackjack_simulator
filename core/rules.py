"""賭場規則設定。

一個 Rules 物件描述一張桌子的完整規則。規則之間有邏輯衝突時由
normalize() 統一修正，並回報哪些選項被鎖定，讓 UI 可以顯示原因。
"""
from dataclasses import dataclass, replace, asdict

# surrender 模式
SURRENDER_NONE = 'none'
SURRENDER_LATE = 'late'    # 莊家確認沒有 BJ 之後才能投降
SURRENDER_EARLY = 'early'  # 莊家檢查 BJ 之前就能投降（只有 no-peek 才成立）

# 可以加倍的時機
DOUBLE_ANY2 = 'any2'      # 任兩張都能加倍
DOUBLE_9_11 = '9-11'      # 只有硬 9/10/11
DOUBLE_10_11 = '10-11'    # 只有硬 10/11

# 莊家 BJ 時玩家的損失範圍（僅 no-peek 有意義）
LOSS_ALL = 'all'           # busted bets：分牌/加倍的追加注全輸
LOSS_ORIGINAL = 'original' # OBO：只輸原始注，追加注退還

DECK_MIN, DECK_MAX = 2, 8


@dataclass(frozen=True)
class Rules:
    num_decks: int = 6
    penetration: float = 0.75          # 發到幾成放切牌（CSM 開啟時無意義）
    continuous_shuffle: bool = False   # CSM：每一手都重新洗牌，沒有切牌點
    dealer_hits_soft_17: bool = False  # H17 / S17
    double_rule: str = DOUBLE_ANY2
    double_after_split: bool = True    # DAS
    surrender: str = SURRENDER_LATE
    surrender_vs_ace: bool = True      # 有些賭場莊家 A 不給投降
    dealer_peek: bool = True           # 莊家有底牌並偷看
    resplit_aces: bool = False         # RSA
    hit_split_aces: bool = False       # 分 A 之後還能補牌
    blackjack_pays: float = 1.5        # 3:2 = 1.5, 6:5 = 1.2
    dealer_bj_loss: str = LOSS_ALL     # 見 LOSS_* （peek 模式下無意義）
    insurance_offered: bool = True

    # 固定不開放調整：各家賭場一致
    max_split_hands: int = 4           # 分牌最多 4 手（A 除外，見 resplit_aces）

    # ---- 衍生性質 ----
    @property
    def can_double_soft(self) -> bool:
        return self.double_rule == DOUBLE_ANY2

    def surrender_allowed_vs(self, upcard: int) -> bool:
        """這張明牌能不能投降。"""
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
    """修掉互相矛盾的規則組合。

    回傳 (修正後的 Rules, [說明字串])。UI 可以拿說明去 disable 對應的控制項。
    """
    notes = []
    r = rules

    if not (DECK_MIN <= r.num_decks <= DECK_MAX):
        clamped = max(DECK_MIN, min(DECK_MAX, r.num_decks))
        notes.append(f"牌副數 {r.num_decks} 超出 {DECK_MIN}-{DECK_MAX}，改為 {clamped}")
        r = replace(r, num_decks=clamped)

    if not (0.30 <= r.penetration <= 0.95):
        clamped = max(0.30, min(0.95, r.penetration))
        notes.append(f"penetration 需在 30%-95%，改為 {clamped:.0%}")
        r = replace(r, penetration=clamped)

    if r.continuous_shuffle:
        notes.append("連續洗牌機 (CSM)：每一手都重新洗牌，沒有切牌點，"
                     "penetration 設定被忽略；算牌因此完全沒用（真數永遠貼近基準值）")

    if r.dealer_peek:
        # 莊家會偷看底牌，玩家在下決定時 BJ 早就結算完了
        if r.surrender == SURRENDER_EARLY:
            notes.append("莊家 peek 時無法 early surrender（BJ 已先結算），改為 late surrender")
            r = replace(r, surrender=SURRENDER_LATE)
        if r.dealer_bj_loss != LOSS_ALL:
            notes.append("莊家 peek 時本來就只輸原始注，OBO 選項無意義")
            r = replace(r, dealer_bj_loss=LOSS_ALL)

    return r, notes
