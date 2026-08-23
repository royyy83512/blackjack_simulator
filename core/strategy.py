"""策略載入器。

策略內容全部放在 strategies/*.json，程式碼只負責讀檔、編譯、決策。
要新增或調整策略就改 JSON，不用碰 Python。

格子的寫法（跟真實策略表的標註方式一致）
------------------------------------------
    H     補牌
    S     停牌
    D     加倍；不能加倍就補牌      （Dh 是同義寫法）
    Ds    加倍；不能加倍就停牌
    P     分牌                     （對子表也可寫 Y）
    Ph    分牌；不能分就往下查硬牌／軟牌表   ← 用來表達「有 DAS 才分」
    N     不分牌，往下查硬牌／軟牌表   （只用於對子表）
    Rh    投降；不能投降就補牌
    Rs    投降；不能投降就停牌
    Rp    投降；不能投降就分牌

每一列是 10 個以空白分隔的格子，依序對應莊家明牌 2 3 4 5 6 7 8 9 10 A。

檔案結構
--------
    name / description            策略名稱與說明
    extends                       繼承另一個策略檔的表（算牌策略通常繼承 basic）
    tables.hard / soft / pair     策略表本體
    overrides[]                   依規則生效的差異格（例如 H17 的 6 格）
    counting                      計數標籤；有這段就是算牌策略
    betting.ramp                  依計數決定注碼倍數
    insurance.min_count           計數到多少開始買保險
    deviations[]                  依計數偏離基本策略的格（例如 Illustrious 18）
    early_surrender               no-peek 專用的 early surrender 表
"""
import json
import os
from pathlib import Path

from .engine import ACT_HIT, ACT_STAND, ACT_DOUBLE, ACT_SPLIT, ACT_SURRENDER
from .rules import SURRENDER_EARLY

STRATEGY_DIR = Path(os.environ.get(
    'BJ_STRATEGY_DIR', Path(__file__).resolve().parent.parent / 'strategies'))

COLUMNS = ('2', '3', '4', '5', '6', '7', '8', '9', '10', 'A')
FALLTHROUGH = 0          # 對子表用：這格不分牌，往下查硬牌／軟牌表

# 格子 -> (主要動作, 不合法時的退路)
TOKENS = {
    'H':  (ACT_HIT, None),
    'S':  (ACT_STAND, None),
    'D':  (ACT_DOUBLE, ACT_HIT),
    'Dh': (ACT_DOUBLE, ACT_HIT),
    'Ds': (ACT_DOUBLE, ACT_STAND),
    'P':  (ACT_SPLIT, FALLTHROUGH),
    'Y':  (ACT_SPLIT, FALLTHROUGH),
    'N':  (FALLTHROUGH, None),
    'Rh': (ACT_SURRENDER, ACT_HIT),
    'Rs': (ACT_SURRENDER, ACT_STAND),
    'Rp': (ACT_SURRENDER, ACT_SPLIT),
}
# Ph 要看規則有沒有 DAS，載入時才決定，見 _compile_row()

# TOKENS 的反查表：拿編譯過的 (動作, 退路) 元組還原成一個字母顯示。
# 'Y'/'P' 跟 'D'/'Dh' 編譯結果相同，只留一個當標準寫法；
# dict 後蓋前，寫在後面的會贏，所以下面刻意把想要的顯示字母放最後。
_CELL_LABEL = {}
for _tok, _cell in TOKENS.items():
    _CELL_LABEL[_cell] = _tok
_CELL_LABEL[(ACT_SPLIT, FALLTHROUGH)] = 'P'
_CELL_LABEL[(ACT_DOUBLE, ACT_HIT)] = 'D'
del _tok, _cell


def cell_label(cell):
    """把編譯過的 (動作, 退路) 格子還原成字母，給策略表檢視器用。

    cell 可能是 None（表裡沒有這一列，例如總點數超出範圍）。

    注意：這個函式只是「字面上翻譯」，不會管這格在目前的規則下合不合法。
    JSON 裡寫 Rh（投降，不能投降就補牌）永遠顯示 R，不管這桌到底給不給
    投降、給不給對莊家 A 投降——那個判斷要用 effective_cell_label()。
    """
    if cell is None:
        return ''
    return _CELL_LABEL.get(cell, '?')


_ACT_LETTER = {ACT_STAND: 'S', ACT_HIT: 'H', ACT_DOUBLE: 'D',
              ACT_SPLIT: 'P', ACT_SURRENDER: 'R'}


def effective_cell_label(cell, up, rules, hard_total, is_soft, strategy=None):
    """把格子依「這桌規則下這一格實際合不合法」解析成最終會發生的動作。

    這是策略表檢視器該用的版本：JSON 裡寫的 Rh（投降，不能投降就補牌）
    在「莊家 A 不給投降」或「這桌根本不給投降」的規則下，投降這個選項
    在起手兩張就不合法，退路邏輯要跟 core.strategy.Strategy._resolve()
    做一樣的事，只是這裡不需要真的建一手牌去問 core.engine.legal_actions()
    ——起手兩張要不要加倍只看點數/軟硬，要不要投降只看莊家明牌，直接用
    Rules 的方法算就好，不用兩邊維護同一段邏輯還要保持同步（分牌後的
    legal_actions() 還要處理已分牌次數、from_split 這些跟這裡無關的狀態）。

    up: 莊家明牌（1..10，1 是 A）
    hard_total: 這手牌不算軟手加值的原始點數和（軟 18 是 A,7，這裡填 8）
    is_soft: 這手牌算不算軟手

    strategy: 選填。SURRENDER_EARLY 模式下，真正決定要不要投降的是
    core.engine.play_round 裡「decide() 之前」的 Strategy.early_surrender()
    前置檢查（查 early_surrender 區塊的 es_vs_ace/es_vs_ten 清單），不是
    hard 表裡的 Rh 格子——那些格子在 EARLY 模式下根本不會被問到。傳入
    strategy 才能讓這個函式跟真正的牌局行為一致；不傳的話（例如還沒載入
    策略檔時）就只看表格本身，早期投降的格子會顯示成表格原本寫的動作。
    """
    if (strategy is not None and not is_soft and rules.surrender == SURRENDER_EARLY
            and rules.surrender_allowed_vs(up)
            and hard_total in (strategy.es_vs_ace if up == 1 else
                               strategy.es_vs_ten if up == 10 else ())):
        return 'R'
    if cell is None:
        return ''
    legal = ACT_HIT | ACT_STAND | ACT_SPLIT
    if rules.double_allowed_on(hard_total, is_soft):
        legal |= ACT_DOUBLE
    if rules.surrender_allowed_vs(up):
        legal |= ACT_SURRENDER
    act, fallback = cell
    if act and (act & legal):
        return _ACT_LETTER.get(act, '?')
    if fallback and (fallback & legal):
        return _ACT_LETTER.get(fallback, '?')
    return 'N' if not act else ''


class StrategyError(Exception):
    pass


def card_key(k):
    """把 'A' / '1' / 'T' / '10' 這些寫法統一成 1..10。"""
    k = str(k).strip().upper()
    if k in ('A', 'ACE', '1'):
        return 1
    if k in ('T', '10', 'J', 'Q', 'K'):
        return 10
    try:
        v = int(k)
    except ValueError:
        raise StrategyError(f"看不懂的牌面 '{k}'")
    if not 2 <= v <= 10:
        raise StrategyError(f"牌面超出範圍：{k}")
    return v


def dealer_index(up):
    """莊家明牌 -> 欄位索引：2..10 -> 0..8，A -> 9。"""
    return 9 if up == 1 else up - 2


def column_index(name):
    k = str(name).strip().upper()
    if k in ('A', 'ACE', '1'):
        return 9
    return dealer_index(card_key(k))


def _token(cell, das, where):
    cell = cell.strip()
    if cell == 'Ph':
        # 「有 DAS 才分牌」——規則在載入時就確定了，直接編譯掉
        return (ACT_SPLIT, FALLTHROUGH) if das else (FALLTHROUGH, None)
    try:
        return TOKENS[cell]
    except KeyError:
        raise StrategyError(
            f"{where}：看不懂的格子 '{cell}'，可用：{', '.join(sorted(TOKENS))}, Ph")


def _compile_row(row, das, where):
    cells = row.split() if isinstance(row, str) else list(row)
    if len(cells) != 10:
        raise StrategyError(f"{where}：需要 10 格（莊家 2-10,A），實際 {len(cells)} 格：{row}")
    return tuple(_token(c, das, where) for c in cells)


def _load_json(name):
    path = STRATEGY_DIR / f"{name}.json"
    if not path.exists():
        raise StrategyError(f"找不到策略檔 {path}")
    with open(path, encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise StrategyError(f"{path} 不是合法的 JSON：{e}")


def _merge_spec(name, seen=None):
    """處理 extends 繼承鏈，回傳合併後的設定。"""
    seen = seen or []
    if name in seen:
        raise StrategyError(f"策略 extends 形成循環：{' -> '.join(seen + [name])}")
    spec = _load_json(name)
    parent = spec.get('extends')
    if not parent:
        return spec
    base = _merge_spec(parent, seen + [name])
    merged = dict(base)
    for k, v in spec.items():
        if k == 'tables' and 'tables' in base:
            t = {kk: dict(vv) for kk, vv in base['tables'].items()}
            for tname, rows in v.items():
                t.setdefault(tname, {}).update(rows)
            merged['tables'] = t
        elif k == 'overrides':
            merged['overrides'] = base.get('overrides', []) + v
        else:
            merged[k] = v
    merged.pop('extends', None)
    return merged


def _matches(when, rules):
    for key, want in (when or {}).items():
        if not hasattr(rules, key):
            raise StrategyError(f"override 的條件 '{key}' 不是規則欄位")
        if getattr(rules, key) != want:
            return False
    return True


class Strategy:
    """由 JSON 檔驅動的策略。"""

    def __init__(self, spec, rules, apply_overrides=True):
        self.name = spec.get('name', '?')
        self.description = spec.get('description', '')
        das = rules.double_after_split

        tables = spec.get('tables') or {}
        if not tables.get('hard'):
            raise StrategyError(f"策略 '{self.name}' 沒有 hard 表")
        self.hard = {int(k): _compile_row(v, das, f"{self.name}.hard[{k}]")
                     for k, v in tables.get('hard', {}).items()}
        self.soft = {int(k): _compile_row(v, das, f"{self.name}.soft[{k}]")
                     for k, v in tables.get('soft', {}).items()}
        self.pair = {card_key(k): _compile_row(v, das, f"{self.name}.pair[{k}]")
                     for k, v in tables.get('pair', {}).items()}

        self.applied_overrides = []
        if apply_overrides:
            for ov in spec.get('overrides', []):
                if _matches(ov.get('when'), rules):
                    self.applied_overrides.append(ov.get('description', ov.get('when')))
                    for cell in ov.get('cells', []):
                        self._set_cell(cell, das)

        # ---- 算牌相關 ----
        cnt = spec.get('counting')
        self.count_tags = None
        self.start_count = 0
        self.use_true_count = True
        if cnt:
            tags = [0] * 11
            for k, v in cnt.get('tags', {}).items():
                tags[card_key(k)] = v
            self.count_tags = tuple(tags)
            self.use_true_count = bool(cnt.get('balanced', True))
            # 不平衡系統的起始流水數 = 常數 + 每副牌的量（KO 的 IRC = 4 - 4N）
            self.start_count = int(cnt.get('start_count', 0))
            if cnt.get('start_count_per_deck'):
                self.start_count += int(round(
                    cnt['start_count_per_deck'] * rules.num_decks))

        ramp = (spec.get('betting') or {}).get('ramp') or []
        self.ramp = tuple(sorted((float(t), float(m)) for t, m in ramp))
        ins = spec.get('insurance') or {}
        self.insurance_min = ins.get('min_count')

        # 偏離：(表, 列, 欄) -> [(下限, 上限, 動作), ...]
        self.deviations = {}
        for dv in spec.get('deviations', []):
            key = (dv['table'],
                   card_key(dv['row']) if dv['table'] == 'pair' else int(dv['row']),
                   column_index(dv['dealer']))
            self.deviations.setdefault(key, []).append((
                float(dv.get('min_count', float('-inf'))),
                float(dv.get('max_count', float('inf'))),
                _token(dv['action'], das, f"{self.name}.deviations")))

        es = spec.get('early_surrender') or {}
        self.es_vs_ace = frozenset(es.get('vs_ace', []))
        self.es_vs_ten = frozenset(es.get('vs_ten', []))

    # ------------------------------------------------------------ 載入
    def _set_cell(self, cell, das):
        tname = cell['table']
        table = getattr(self, tname, None)
        if table is None:
            raise StrategyError(f"override 指到不存在的表 '{tname}'")
        row = card_key(cell['row']) if tname == 'pair' else int(cell['row'])
        if row not in table:
            raise StrategyError(f"override 指到不存在的列 {tname}[{cell['row']}]")
        col = column_index(cell['dealer'])
        cells = list(table[row])
        cells[col] = _token(cell['action'], das, f"{self.name}.overrides")
        table[row] = tuple(cells)

    # ------------------------------------------------------------ 決策
    def count(self, shoe):
        return shoe.true_count if self.use_true_count else shoe.running_count

    def bet(self, shoe, rules, base_bet):
        if not self.ramp:
            return base_bet
        c = self.count(shoe)
        mult = 1.0
        for threshold, m in self.ramp:
            if c >= threshold:
                mult = m
            else:
                break
        return base_bet * mult

    def take_insurance(self, shoe, rules):
        return self.insurance_min is not None and self.count(shoe) >= self.insurance_min

    def early_surrender(self, hand, up, shoe, rules):
        if hand.soft:
            return False
        t = hand.total
        if up == 1:
            return t in self.es_vs_ace
        if up == 10:
            return t in self.es_vs_ten
        return False

    def _lookup(self, table, row, col, tname, c):
        if self.deviations:
            for lo, hi, act in self.deviations.get((tname, row, col), ()):
                if lo <= c <= hi:
                    return act
        entry = table.get(row)
        return entry[col] if entry else None

    @staticmethod
    def _resolve(cell, legal):
        """把 (主要動作, 退路) 解成實際動作；回不出來就傳 None 代表往下查。"""
        if cell is None:
            return None
        act, fallback = cell
        if act and (act & legal):
            return act
        if fallback is None:
            return None
        if fallback and (fallback & legal):
            return fallback
        return None

    def decide(self, hand, up, legal, shoe, rules):
        col = 9 if up == 1 else up - 2
        cards = hand.cards
        c = self.count(shoe) if self.count_tags else 0.0

        if len(cards) == 2 and cards[0] == cards[1]:
            act = self._resolve(self._lookup(self.pair, cards[0], col, 'pair', c), legal)
            if act:
                return act

        soft = hand.soft
        total = hand.total
        table, tname = (self.soft, 'soft') if soft else (self.hard, 'hard')
        act = self._resolve(self._lookup(table, total, col, tname, c), legal)
        if act:
            return act
        # 表上沒有這一列（例如已經 21 點）就停牌
        return ACT_STAND if (legal & ACT_STAND) else ACT_HIT


# ---------------------------------------------------------------- 註冊表
def available():
    """列出 strategies/ 底下所有策略檔。"""
    if not STRATEGY_DIR.exists():
        return []
    return sorted(p.stem for p in STRATEGY_DIR.glob('*.json'))


def describe():
    """回傳 [(名稱, 說明, 是否算牌)]，給 CLI 與 GUI 顯示用。"""
    out = []
    for name in available():
        try:
            spec = _merge_spec(name)
        except StrategyError:
            continue
        out.append((name, spec.get('description', ''), bool(spec.get('counting'))))
    return out


def find_defining_file(name, kind, row):
    """順著 extends 鏈往上找，回傳「誰的原始 JSON 真的定義了這一列」的策略名稱。

    子策略沒定義某一列時會繼承父策略的（見 _merge_spec）；改動一格要改到
    「實際生效的那份 JSON」，不是隨便改子策略檔（改了也不會生效，因為
    子策略根本沒有這一列，查表時繼承回父策略）。找不到就回傳 None。

    name 可以帶 -fixed 後綴（跟 make() 一樣接受）——那個後綴是合成出來的
    「不套用 overrides」變體，本身不對應實際檔案，要先去掉才找得到。
    """
    if name.endswith('-fixed'):
        name = name[:-6]
    seen = []
    cur = name
    while cur and cur not in seen:
        seen.append(cur)
        try:
            spec = _load_json(cur)
        except StrategyError:
            return None
        tables = spec.get('tables') or {}
        if str(row) in (tables.get(kind) or {}):
            return cur
        cur = spec.get('extends')
    return None


def derive_token(best_act, fallback_act):
    """依「蒙地卡羅測出的最佳動作」與「次佳動作」，推導出要寫回 JSON 的格子。

    次佳動作當退路：這格本來就沒有分牌選項可以退（分牌只能整手换，不是
    退路關係），所以最佳動作是分牌時直接用 'P'，不用管次佳是什麼。
    """
    if best_act == ACT_STAND:
        return 'S'
    if best_act == ACT_HIT:
        return 'H'
    if best_act == ACT_SPLIT:
        return 'P'
    if best_act == ACT_DOUBLE:
        return 'Ds' if fallback_act == ACT_STAND else 'D'
    if best_act == ACT_SURRENDER:
        if fallback_act == ACT_SPLIT:
            return 'Rp'
        if fallback_act == ACT_STAND:
            return 'Rs'
        return 'Rh'
    return 'H'


def write_cell(name, kind, row, col, token):
    """把 (kind, row, col) 這一格改成 token，寫回實際定義它的那份 JSON 檔。

    row 對硬牌/軟牌表是總點數（int），對對子表是牌面（1..10，1 是 A）。
    col 是 0..9（依 COLUMNS 順序：2..10,A）。

    回傳實際被改到的檔案路徑；找不到定義該列的檔案時丟出 StrategyError。
    """
    owner = find_defining_file(name, kind, row)
    if owner is None:
        raise StrategyError(f"找不到定義 {kind}[{row}] 這一列的策略檔，"
                            "沒辦法自動更新（可能要手動加進 JSON）")
    path = STRATEGY_DIR / f"{owner}.json"
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    cells = data['tables'][kind][str(row)].split()
    if len(cells) != 10:
        raise StrategyError(f"{path} 的 {kind}[{row}] 格式不對，不是 10 格")
    cells[col] = token
    data['tables'][kind][str(row)] = '  '.join(c.ljust(2) for c in cells).rstrip()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return path


def make(name, rules, apply_overrides=True):
    base = name[:-6] if name.endswith('-fixed') else name
    if name.endswith('-fixed'):
        apply_overrides = False
    try:
        spec = _merge_spec(base)
    except StrategyError as e:
        raise SystemExit(f"{e}\n可用策略：{', '.join(available())}")
    s = Strategy(spec, rules, apply_overrides)
    if name.endswith('-fixed'):
        s.name = name
    return s


class _Registry(dict):
    """讓舊程式碼還能用 REGISTRY[name] / 'x' in REGISTRY / sorted(REGISTRY)。"""

    def __iter__(self):
        names = available()
        return iter(names + [n + '-fixed' for n in names])

    def keys(self):
        return list(self)

    def __len__(self):
        return len(list(self))

    def __contains__(self, k):
        return k in list(self)

    def __getitem__(self, k):
        if k not in self:
            raise KeyError(k)
        return lambda rules: make(k, rules)


REGISTRY = _Registry()
