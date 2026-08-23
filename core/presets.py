"""賭場規則預設檔：一鍵帶入某家賭場的完整規則。

跟 core/strategy.py 的策略檔是同一種設計哲學——規則內容放在
presets/*.json，不寫死在程式碼裡，新增一家賭場就是丟一個新的 JSON 檔案
進 presets/，不用碰 Python。

每個檔案結構：
    {
      "name": "顯示名稱",
      "description": "一行說明",
      "rules": { ...Rules 的欄位... }
    }

"rules" 底下只要填你知道的欄位，沒填的欄位沿用 core.rules.Rules 的預設值。
"""
import json
import os
from dataclasses import fields
from pathlib import Path

from .rules import Rules, normalize

PRESET_DIR = Path(os.environ.get(
    'BJ_PRESET_DIR', Path(__file__).resolve().parent.parent / 'presets'))

_VALID_FIELDS = {f.name for f in fields(Rules)}


class PresetError(Exception):
    pass


def _load_json(name):
    path = PRESET_DIR / f"{name}.json"
    if not path.exists():
        raise PresetError(f"找不到預設檔 {path}")
    with open(path, encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise PresetError(f"{path} 不是合法的 JSON：{e}")


def available():
    """列出 presets/ 底下所有預設檔（依檔名排序）。"""
    if not PRESET_DIR.exists():
        return []
    return sorted(p.stem for p in PRESET_DIR.glob('*.json'))


def describe():
    """回傳 [(檔名, 顯示名稱, 說明), ...]，給 CLI/GUI 列表用。"""
    out = []
    for name in available():
        try:
            spec = _load_json(name)
        except PresetError:
            continue
        out.append((name, spec.get('name', name), spec.get('description', '')))
    return out


def load(name):
    """讀一個預設檔，回傳 (Rules, notes)。notes 是 normalize() 的修正訊息
    （規則本身互相矛盾時才會有，例如 CSM 讓 penetration 失效）。"""
    spec = _load_json(name)
    raw = spec.get('rules', {})
    unknown = set(raw) - _VALID_FIELDS
    if unknown:
        raise PresetError(f"{name}.json 有不認得的規則欄位：{', '.join(sorted(unknown))}")
    return normalize(Rules(**raw))
