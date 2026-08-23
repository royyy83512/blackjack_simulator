"""Strategy loader.

All strategy content lives in strategies/*.json; the code only handles
loading, compiling, and deciding. To add or tweak a strategy, edit the
JSON — no need to touch Python.

Cell notation (matches how real strategy charts are usually written)
----------------------------------------------------------------------
    H     hit
    S     stand
    D     double; hit if not allowed          (Dh is a synonym)
    Ds    double; stand if not allowed
    P     split                               (pair table may also write Y)
    Ph    split; fall through to the hard/soft table if not allowed
          <- expresses "split only with DAS"
    N     don't split, fall through to the hard/soft table (pair table only)
    Rh    surrender; hit if not allowed
    Rs    surrender; stand if not allowed
    Rp    surrender; split if not allowed

Each row is 10 space-separated cells, in order for dealer upcard
2 3 4 5 6 7 8 9 10 A.

File layout
-----------
    name / description            strategy name and description
    extends                       inherits another strategy file's tables
                                   (counting strategies usually extend basic)
    tables.hard / soft / pair     the strategy tables themselves
    overrides[]                   rule-conditional cell overrides (e.g. the
                                   6 cells that differ under H17)
    counting                      count tags; presence means this is a
                                   counting strategy
    betting.ramp                  bet multiplier as a function of the count
    insurance.min_count           count threshold for taking insurance
    deviations[]                  count-based deviations from basic strategy
                                   (e.g. the Illustrious 18)
    early_surrender                the no-peek-only early surrender table
"""
import json
import os
from pathlib import Path

from .engine import ACT_HIT, ACT_STAND, ACT_DOUBLE, ACT_SPLIT, ACT_SURRENDER
from .rules import SURRENDER_EARLY

STRATEGY_DIR = Path(os.environ.get(
    'BJ_STRATEGY_DIR', Path(__file__).resolve().parent.parent / 'strategies'))

COLUMNS = ('2', '3', '4', '5', '6', '7', '8', '9', '10', 'A')
FALLTHROUGH = 0          # pair table only: don't split, fall through to the hard/soft table

# cell -> (primary action, fallback if illegal)
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
# Ph depends on whether the rules have DAS, resolved at load time — see _compile_row()

# Reverse lookup for TOKENS: turns a compiled (action, fallback) tuple back
# into a single display letter. 'Y'/'P' and 'D'/'Dh' compile to the same
# tuple, so only one is kept as the canonical spelling; later entries win
# when a dict is built from repeated keys, so the letters we want to display
# are deliberately placed last below.
_CELL_LABEL = {}
for _tok, _cell in TOKENS.items():
    _CELL_LABEL[_cell] = _tok
_CELL_LABEL[(ACT_SPLIT, FALLTHROUGH)] = 'P'
_CELL_LABEL[(ACT_DOUBLE, ACT_HIT)] = 'D'
del _tok, _cell


def cell_label(cell):
    """Turn a compiled (action, fallback) cell back into a letter, for the
    strategy table viewer.

    cell may be None (no such row in the table, e.g. a total out of range).

    Note: this is a purely literal translation — it doesn't check whether
    this cell is actually legal under the current rules. A JSON cell
    written as Rh (surrender, hit if not allowed) always displays as R,
    regardless of whether this table offers surrender at all, or against
    a dealer ace specifically — that check belongs to effective_cell_label().
    """
    if cell is None:
        return ''
    return _CELL_LABEL.get(cell, '?')


_ACT_LETTER = {ACT_STAND: 'S', ACT_HIT: 'H', ACT_DOUBLE: 'D',
              ACT_SPLIT: 'P', ACT_SURRENDER: 'R'}


def effective_cell_label(cell, up, rules, hard_total, is_soft, strategy=None):
    """Resolve a cell into the action that will actually happen under this
    table's rules.

    This is the version the strategy table viewer should use: a JSON cell
    written as Rh (surrender, hit if not allowed) is not a legal choice on
    the first two cards when "dealer doesn't allow surrender against an
    ace" or "this table has no surrender at all" — the fallback logic here
    has to match core.strategy.Strategy._resolve() exactly. It doesn't need
    to actually build a hand and ask core.engine.legal_actions() though:
    whether doubling is allowed on the first two cards only depends on the
    total/hard-vs-soft, and whether surrender is allowed only depends on
    the dealer's upcard, so we can just call straight into Rules' own
    methods instead of maintaining the same logic in two places and keeping
    them in sync (legal_actions() after a split also has to handle split
    count and from_split state that's irrelevant here).

    up: dealer's upcard (1..10, 1 is an ace)
    hard_total: this hand's raw card total not counting the soft-ace bonus
        (soft 18 is A,7, so pass 8 here)
    is_soft: whether this hand is soft

    strategy: optional. Under SURRENDER_EARLY, what actually decides
    whether to surrender is the Strategy.early_surrender() pre-check in
    core.engine.play_round, which runs *before* decide() is ever called
    (it consults the es_vs_ace/es_vs_ten lists in the early_surrender
    block) — not the Rh cells in the hard table, which never even get
    asked in EARLY mode. Passing strategy in is what makes this function
    match real gameplay; without it (e.g. before a strategy file is
    loaded), only the table itself is consulted, and early-surrender cells
    display whatever the table happens to say.
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
    """Normalize 'A' / '1' / 'T' / '10' and friends to 1..10."""
    k = str(k).strip().upper()
    if k in ('A', 'ACE', '1'):
        return 1
    if k in ('T', '10', 'J', 'Q', 'K'):
        return 10
    try:
        v = int(k)
    except ValueError:
        raise StrategyError(f"Unrecognized card value '{k}'")
    if not 2 <= v <= 10:
        raise StrategyError(f"Card value out of range: {k}")
    return v


def dealer_index(up):
    """Dealer upcard -> column index: 2..10 -> 0..8, A -> 9."""
    return 9 if up == 1 else up - 2


def column_index(name):
    k = str(name).strip().upper()
    if k in ('A', 'ACE', '1'):
        return 9
    return dealer_index(card_key(k))


def _token(cell, das, where):
    cell = cell.strip()
    if cell == 'Ph':
        # "split only with DAS" — the rule is known at load time, so resolve it now
        return (ACT_SPLIT, FALLTHROUGH) if das else (FALLTHROUGH, None)
    try:
        return TOKENS[cell]
    except KeyError:
        raise StrategyError(
            f"{where}: unrecognized cell '{cell}', valid values: {', '.join(sorted(TOKENS))}, Ph")


def _compile_row(row, das, where):
    cells = row.split() if isinstance(row, str) else list(row)
    if len(cells) != 10:
        raise StrategyError(f"{where}: expected 10 cells (dealer 2-10,A), got {len(cells)}: {row}")
    return tuple(_token(c, das, where) for c in cells)


def _load_json(name):
    path = STRATEGY_DIR / f"{name}.json"
    if not path.exists():
        raise StrategyError(f"Strategy file not found: {path}")
    with open(path, encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise StrategyError(f"{path} is not valid JSON: {e}")


def _merge_spec(name, seen=None):
    """Walk the extends inheritance chain and return the merged spec."""
    seen = seen or []
    if name in seen:
        raise StrategyError(f"Strategy extends form a cycle: {' -> '.join(seen + [name])}")
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
            raise StrategyError(f"Override condition '{key}' is not a rules field")
        if getattr(rules, key) != want:
            return False
    return True


class Strategy:
    """A strategy driven by a JSON spec."""

    def __init__(self, spec, rules, apply_overrides=True):
        self.name = spec.get('name', '?')
        self.description = spec.get('description', '')
        das = rules.double_after_split

        tables = spec.get('tables') or {}
        if not tables.get('hard'):
            raise StrategyError(f"Strategy '{self.name}' has no hard table")
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

        # ---- card counting ----
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
            # starting running count for unbalanced systems = constant + per-deck term
            # (KO's IRC = 4 - 4N)
            self.start_count = int(cnt.get('start_count', 0))
            if cnt.get('start_count_per_deck'):
                self.start_count += int(round(
                    cnt['start_count_per_deck'] * rules.num_decks))

        ramp = (spec.get('betting') or {}).get('ramp') or []
        self.ramp = tuple(sorted((float(t), float(m)) for t, m in ramp))
        ins = spec.get('insurance') or {}
        self.insurance_min = ins.get('min_count')

        # deviations: (table, row, col) -> [(min, max, action), ...]
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

    # ------------------------------------------------------------ loading
    def _set_cell(self, cell, das):
        tname = cell['table']
        table = getattr(self, tname, None)
        if table is None:
            raise StrategyError(f"Override references a nonexistent table '{tname}'")
        row = card_key(cell['row']) if tname == 'pair' else int(cell['row'])
        if row not in table:
            raise StrategyError(f"Override references a nonexistent row {tname}[{cell['row']}]")
        col = column_index(cell['dealer'])
        cells = list(table[row])
        cells[col] = _token(cell['action'], das, f"{self.name}.overrides")
        table[row] = tuple(cells)

    # ------------------------------------------------------------ decisions
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
        """Resolve (primary action, fallback) into an actual action; None
        means "no answer, keep looking elsewhere"."""
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
        # no row for this total (e.g. already 21) -> stand
        return ACT_STAND if (legal & ACT_STAND) else ACT_HIT


# ---------------------------------------------------------------- registry
def available():
    """List every strategy file under strategies/."""
    if not STRATEGY_DIR.exists():
        return []
    return sorted(p.stem for p in STRATEGY_DIR.glob('*.json'))


def describe():
    """Return [(name, description, is_counting), ...] for CLI/GUI display."""
    out = []
    for name in available():
        try:
            spec = _merge_spec(name)
        except StrategyError:
            continue
        out.append((name, spec.get('description', ''), bool(spec.get('counting'))))
    return out


def find_defining_file(name, kind, row):
    """Walk up the extends chain and return the name of the strategy whose
    own JSON actually defines this row.

    A child strategy that doesn't define a given row inherits it from its
    parent (see _merge_spec); to edit a cell you need to edit "whichever
    JSON is actually in effect", not just any child file (editing the
    child wouldn't do anything, since the child has no such row and
    lookups fall through to the parent). Returns None if not found.

    name may carry a -fixed suffix (accepted the same way make() does) —
    that suffix denotes a synthesized "overrides not applied" variant that
    doesn't correspond to an actual file, so it's stripped before lookup.
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
    """Given the best action found by Monte Carlo simulation and the
    runner-up action, derive the cell token to write back to the JSON.

    The runner-up serves as the fallback: a cell never falls back to split
    (splitting swaps in a whole different hand, not a fallback relationship),
    so when the best action is split we just use 'P' outright, regardless
    of what the runner-up was.
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
    """Change cell (kind, row, col) to token, writing back to whichever
    JSON file actually defines it.

    row is the total (int) for the hard/soft tables, or the card value
    (1..10, 1 is an ace) for the pair table. col is 0..9 (following the
    COLUMNS order: 2..10,A).

    Returns the path of the file actually modified; raises StrategyError
    if no file defining that row can be found.
    """
    owner = find_defining_file(name, kind, row)
    if owner is None:
        raise StrategyError(f"Could not find the strategy file defining {kind}[{row}], "
                            "can't auto-update it (you may need to add it to the JSON by hand)")
    path = STRATEGY_DIR / f"{owner}.json"
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    cells = data['tables'][kind][str(row)].split()
    if len(cells) != 10:
        raise StrategyError(f"{path}'s {kind}[{row}] is malformed, not 10 cells")
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
        raise SystemExit(f"{e}\nAvailable strategies: {', '.join(available())}")
    s = Strategy(spec, rules, apply_overrides)
    if name.endswith('-fixed'):
        s.name = name
    return s


class _Registry(dict):
    """Lets older code keep using REGISTRY[name] / 'x' in REGISTRY / sorted(REGISTRY)."""

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
