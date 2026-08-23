"""Casino rule presets: apply one casino's complete rule set in one click.

Same design philosophy as core/strategy.py's strategy files — the rule
content lives in presets/*.json rather than being hardcoded, so adding a
new casino just means dropping a new JSON file into presets/, no Python
required.

Each file looks like:
    {
      "name": "Display name",
      "description": "One-line description",
      "rules": { ...fields of Rules... }
    }

Under "rules" you only need to fill in the fields you actually know;
anything omitted falls back to core.rules.Rules' defaults.
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
        raise PresetError(f"Preset file not found: {path}")
    with open(path, encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise PresetError(f"{path} is not valid JSON: {e}")


def available():
    """List every preset file under presets/ (sorted by filename)."""
    if not PRESET_DIR.exists():
        return []
    return sorted(p.stem for p in PRESET_DIR.glob('*.json'))


def describe():
    """Return [(filename, display name, description), ...] for CLI/GUI listing."""
    out = []
    for name in available():
        try:
            spec = _load_json(name)
        except PresetError:
            continue
        out.append((name, spec.get('name', name), spec.get('description', '')))
    return out


def load(name):
    """Load a preset file, returning (Rules, notes). notes are normalize()'s
    correction messages (only present when the rules genuinely conflict,
    e.g. CSM making penetration meaningless)."""
    spec = _load_json(name)
    raw = spec.get('rules', {})
    unknown = set(raw) - _VALID_FIELDS
    if unknown:
        raise PresetError(f"{name}.json has unrecognized rule field(s): {', '.join(sorted(unknown))}")
    return normalize(Rules(**raw))
