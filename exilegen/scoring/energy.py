"""Energy-corrected DPS for spells triggered by PoE2 meta gems (Cast on Critical, Cast on Elemental Ailment).

PoB-PoE2 0.23.1 has no Energy model: it treats meta-triggered spells as if cast at their own cast speed.
Port of tools/poc/one-button/lib.lua (metaEnergy / energyTriggerRate):

    energy/s   = Σ_events  feeder events/s × energy per event per monster Power
                 × enemy Power × (1 + gen%/100) / (1 − ½·min(refund%,100)/100)
    triggers/s = energy/s / max energy          (max energy = Σ socketed spells' base cast time × 100)
    corrected  = PoB DPS × min(1, triggers/s ÷ PoB's assumed triggers/s)

Feeder events/s = feeder hits/s × chance (crit, shock, ignite). Freeze needs buildup on bosses and PoB gives
no usable freeze-event rate, so freeze energy is only counted when the engine reports freeze events/s
explicitly; otherwise it is ignored (conservative) and flagged when freeze chance > 0.
Triggered spells are assumed not to generate energy themselves.
Metas without a model (Cast on Block, Cast on Minion Death, Cast on Dodge, ...) are "unmodelled": their
spells keep PoB's number × `unmodelled_factor` and the result is flagged unverified.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .adapter import EnergyGroup

# meta gem id -> which per-event energies apply ("kind")
MODELS = {
    "MetaCastOnCritPlayer": "crit",
    "MetaCastOnElementalAilmentPlayer": "ailment",
}
MODEL_BY_NAME = {"cast on critical": "MetaCastOnCritPlayer", "cast on elemental ailment": "MetaCastOnElementalAilmentPlayer"}
# PoB 0.23.1 gem data defaults (centienergy per monster power / 100)
DEFAULT_PER_EVENT = {
    "MetaCastOnCritPlayer": {"crit": 1.0},
    "MetaCastOnElementalAilmentPlayer": {"shock": 1.0, "ignite": 1.0, "freeze": 10.0},
}
SERVER_TICK = 0.033   # s; sanity cap on trigger rate


@dataclass
class SpellResult:
    group: object
    name: str
    id: str | None
    meta: str | None
    kind: str
    pob_dps: float
    pob_rate: float | None
    energy_rate: float | None
    factor: float
    corrected: float
    factor_half_energy: float
    flags: list[str] = field(default_factory=list)


@dataclass
class EnergyResult:
    spells: list[SpellResult]
    dps_raw: float
    dps_corrected: float
    dps_half_energy: float
    flags: list[str]

    @property
    def factor(self) -> float:
        return self.dps_corrected / self.dps_raw if self.dps_raw > 0 else 1.0

    @property
    def sensitivity(self) -> float:
        """corrected DPS at half energy / corrected DPS (1.0 = not energy-limited)."""
        return self.dps_half_energy / self.dps_corrected if self.dps_corrected > 0 else 1.0

    def as_dict(self) -> dict:
        return {"dps_raw": self.dps_raw, "dps_corrected": self.dps_corrected, "factor": self.factor,
                "dps_half_energy": self.dps_half_energy, "half_energy_ratio": self.sensitivity, "flags": self.flags,
                "spells": [s.__dict__ for s in self.spells]}


def resolve_meta(g: EnergyGroup) -> str | None:
    if g.meta_id in MODELS:
        return g.meta_id
    if g.meta_name and g.meta_name.lower() in MODEL_BY_NAME:
        return MODEL_BY_NAME[g.meta_name.lower()]
    return None


def max_energy(g: EnergyGroup) -> float | None:
    if g.max_energy:
        return g.max_energy
    cts = [s.cast_time for s in g.spells if s.cast_time]
    return sum(ct * 100.0 for ct in cts) if cts else None


def energy_per_sec(g: EnergyGroup, power: float, flags: list[str] | None = None) -> float | None:
    """Energy/s for a modelled meta group, or None if unmodelled / feeder unknown."""
    flags = flags if flags is not None else []
    meta = resolve_meta(g)
    if meta is None:
        return None
    per = dict(DEFAULT_PER_EVENT[meta])
    per.update({k: v for k, v in g.per_event.items() if k in per})
    f = g.feeder
    hps = f.get("hits_per_sec")
    if MODELS[meta] == "crit":
        ev = f.get("crit_events_per_sec")
        if ev is None:
            if hps is None or f.get("crit") is None:
                flags.append(f"energy:feeder_missing:{g.group}")
                return None
            ev = hps * f["crit"]
        e = ev * per["crit"]
    else:
        e = 0.0
        known = False
        for k in ("shock", "ignite"):
            ev = f.get(f"{k}_events_per_sec")
            if ev is None and hps is not None and f.get(k) is not None:
                ev = hps * f[k]
            if ev is not None:
                known = True
                e += ev * per[k]
        fev = f.get("freeze_events_per_sec")
        if fev is not None:
            known = True
            e += fev * per["freeze"]
        elif (f.get("freeze") or 0) > 0:
            flags.append(f"energy:freeze_ignored:{g.group}")
        if not known:
            flags.append(f"energy:feeder_missing:{g.group}")
            return None
    refund = min(g.refund_pct, 100.0)
    return e * power * (1.0 + g.gen_pct / 100.0) / (1.0 - 0.5 * refund / 100.0)


def correct(groups: list[EnergyGroup] | None, power: float, unmodelled_factor: float = 1.0) -> EnergyResult:
    flags: list[str] = []
    out: list[SpellResult] = []
    if not groups:
        return EnergyResult([], 0.0, 0.0, 0.0, flags)
    for g in groups:
        if not g.spells:                 # a meta gem with nothing socketed (a wand's own Spellslinger): nothing
            continue                     # is triggered, so there is nothing to correct and nothing unverified
        meta = resolve_meta(g)
        gflags: list[str] = []
        eps = energy_per_sec(g, power, gflags)
        mx = max_energy(g)
        rate = eps / mx if (eps is not None and mx) else None
        if meta is None:
            gflags.append(f"unverified:energy_unmodelled:{g.meta_name or g.meta_id}")
        elif rate is None:
            gflags.append(f"unverified:energy_inputs_missing:{g.meta_name or g.meta_id}")
        if meta is not None and rate == 0:      # the feeder makes none of the gem's events: the reaction never fires
            gflags.append(f"unverified:energy_no_events:{g.meta_name or g.meta_id}")
        if rate is not None and rate > 1.0 / SERVER_TICK:
            rate = 1.0 / SERVER_TICK
            gflags.append(f"energy:server_tick_cap:{g.group}")
        for s in g.spells:
            d = s.pob_dps or 0.0
            pr = s.pob_rate if s.pob_rate is not None else g.pob_assumed_rate
            if meta is None:
                f = fh = unmodelled_factor
                kind = "unmodelled"
            elif rate is None or not pr:
                f = fh = 1.0
                kind = MODELS[meta]
                if not pr:
                    gflags.append(f"unverified:pob_rate_missing:{s.name}")
            else:
                f = min(1.0, rate / pr)
                fh = min(1.0, 0.5 * rate / pr)
                kind = MODELS[meta]
            out.append(SpellResult(g.group, s.name, s.id, g.meta_name or g.meta_id, kind, d, pr, rate, f, d * f, fh, list(gflags)))
        flags.extend(x for x in gflags if x not in flags)
    raw = sum(s.pob_dps for s in out)
    cor = sum(s.corrected for s in out)
    half = sum(s.pob_dps * s.factor_half_energy for s in out)
    return EnergyResult(out, raw, cor, half, flags)
