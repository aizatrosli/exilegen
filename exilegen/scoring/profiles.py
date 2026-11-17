"""Profile parameters for scoring (enemy Power, fight windows).

The locked profiles live in exilegen/data/profiles.json (owned by the engine). This module only reads the
parameters scoring needs and falls back to DEFAULTS, reporting which values were defaulted so the scorer
can flag them.
"""
from __future__ import annotations

import json
import os

PROFILES_JSON = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "profiles.json")

# Fallbacks. Monster Power (wiki: Power) = base Power x rarity coefficient (normal 1, magic 2, rare 5);
# unique monsters are always 20. pinnacle: 20. mapping: a rare monster of average base Power (1.0) = 5.
DEFAULTS = {
    "pinnacle_v1": {"enemy_power": 20},
    "mapping_v1": {"enemy_power": 5},
    "as_exported": {"enemy_power": 20},
}
FALLBACK = "pinnacle_v1"
# corpus section names -> registry profiles
ALIASES = {"pinnacle_locked": "pinnacle_v1"}


class Profiles:
    def __init__(self, data: dict | None = None):
        self.data = data or {}

    @classmethod
    def load(cls, path: str = PROFILES_JSON) -> "Profiles":
        if not os.path.exists(path):
            return cls({})
        try:
            raw = json.load(open(path))
        except (OSError, ValueError):
            return cls({})
        if isinstance(raw, dict) and "profiles" in raw:
            raw = raw["profiles"]
        if isinstance(raw, list):
            raw = {p.get("name"): p for p in raw if isinstance(p, dict)}
        return cls(raw if isinstance(raw, dict) else {})

    def param(self, profile: str, key: str):
        """-> (value, defaulted: bool)."""
        p = self.data.get(profile) or {}
        for k in (key, key.replace("_", ""), "".join(w.capitalize() if i else w for i, w in enumerate(key.split("_")))):
            if k in p:
                return p[k], False
            for sub in ("params", "parameters", "scoring", "enemy"):
                if isinstance(p.get(sub), dict) and k in p[sub]:
                    return p[sub][k], False
        d = DEFAULTS.get(ALIASES.get(profile, profile)) or DEFAULTS[FALLBACK]
        return d.get(key), True

    def registry_profile(self, profile: str) -> str:
        """Profile name used for registry lookups (profiles may declare `scoring_as`)."""
        p = self.data.get(profile) or {}
        return p.get("scoring_as") or ALIASES.get(profile, profile)

    def condition_overrides(self, profile: str) -> dict:
        """Availability overrides from the profile's scoring block.

        `condition_availability: {condition: value}` is taken as is; the engine's flags
        `corpses_available` and `enemy_cursed_unless_build_curses` map onto the catalogue conditions."""
        p = self.data.get(profile) or {}
        sc = p.get("scoring") if isinstance(p.get("scoring"), dict) else p
        out = dict(sc.get("condition_availability") or {})
        if sc.get("corpses_available") is False:
            out.setdefault("corpses_present", "build")
        if sc.get("enemy_cursed_unless_build_curses") is False:
            out.setdefault("enemy_cursed", "build")
        elif sc.get("enemy_cursed_unless_build_curses") is True:
            out.setdefault("enemy_cursed", 1.0)
        return out
