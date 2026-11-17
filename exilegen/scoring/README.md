# exilegen.scoring

The scoring layer turns engine output into a number the optimiser can trust. The optimiser maximises PoB's DPS, so every gap in PoB turns into a fake win (see `docs/poc-results.md`). This layer closes the gaps that the POCs exposed:

- **Meta-gem Energy.** PoB 0.23.1 treats spells triggered by Cast on Critical or Cast on Elemental Ailment as if they were self-cast.
- **Assumed states.** PoB assumes some states hold: the enemy is cursed, a corpse exists, a minion is alive, the player is moving, the player has Glory, and so on.
- **Ignored drawbacks.** PoB ignores some drawbacks: Overextend's daze, Atziri's Impatience life loss, and Atalui's Bloodletting life costs.
- **Missing objective terms.** The objective has to value what players spend points on: defence floors, sustain, movement speed and ailment avoidance.

```python
from exilegen.scoring import Scorer, front
s = Scorer(profile=["pinnacle_v1", "mapping_v1"], constraints=["one_button"])
r = s.score({"pinnacle_v1": stats_p, "mapping_v1": stats_m}, skills=skills_view, baseline=base)
r["feasible"], r["objective"], r["dps_honest"], r["flags"], print(r["explain"])
```

## Modules

| File | Role |
|---|---|
| `adapter.py` | **The only place that knows field names.** `FIELDS` maps canonical keys to candidate paths in the engine output, the corpus record (`BuildView.from_corpus`) or the POC output (`doc_from_poc`). A missing field returns a default and is recorded in `missing`. It never raises. |
| `conditions.yaml` | The curated registry: a condition catalogue (availability per profile), a drawback catalogue, and hand-checked entries. |
| `conditions_auto.yaml` | **Generated** draft (`gen_conditions.py`). Do not edit it; curate in `conditions.yaml`. |
| `conditions.py` | Registry loading and merging, build signals, per-entry credit, custom checks, and how credits apply to DPS parts. |
| `energy.py` | Energy-corrected trigger DPS, ported from `tools/poc/one-button/lib.lua`. |
| `floors.py` | Floors as data (`DEFAULT_FLOORS`, `LENIENT_FLOORS`), in hard and target tiers, with absolute, relative and either modes. |
| `constraints.py` | The constraint DSL: `one_button`, `no_cast[:strict\|lenient]`, `no_minions`, `max_manual_skills:N`, and `require_`/`forbid_` for gems, uniques and keystones. |
| `objective.py` | `Scorer`, the min over profiles, `explain`, and the Pareto helpers `dominates` and `front`. |
| `profiles.py` | Reads `enemy_power` and the condition flags from `exilegen/data/profiles.json` (`profiles.<name>.scoring`), which the engine owns. Any value that falls back to a default is flagged. |
| `pobdata.py` | A regex reader for the PoB Lua data (skills, gems, keystones, uniques), used by the generator. |

## How a score is computed (per profile)

1. **Energy.** For each Meta+Triggers group the engine reports:
   - `energy/s` = feeder events/s × energy per event × enemy Power (from the profile) × threshold share × (1+gen%) / (1 − ½·refund%)
   - threshold share: crits count `min(1, crit hit ÷ enemy ailment threshold)` and ignites `min(1, 0.8 × fire hit ÷ threshold)`, as the gem text says; shock and freeze are not scaled. Without the facts the share is 1 and the build is flagged `unverified:energy_threshold_unknown`. Whether a hit above the threshold gives more than 1 is untested in game.
   - `triggers/s` = energy/s ÷ max energy, where max energy = Σ socketed spells' base cast time × 100

   Each spell's DPS is multiplied by `min(1, triggers/s ÷ PoB's assumed rate)`. A half-energy factor is reported as a sensitivity check.

   **Freeze energy** counts only when the engine reports freeze events/s. Otherwise it is ignored and flagged.

   **Metas without a model** (Block, Minion Death, Dodge, Melee Kill, …) keep PoB's number × `unmodelled_energy_factor` and are flagged `unverified`.
2. **DPS parts.** The parts are the main skill plus every energy-triggered spell. A main skill inside a meta group is not counted twice. A main skill in a meta group with no energy facts is flagged.
3. **Conditions.** Every registry entry that matches a gem in an enabled group, or a keystone or unique, gets an action and a credit for the profile:
   - `allow`: no change.
   - `flag`: informational. Most auto-scan entries end here.
   - `discount`: keep `credit` of the gem's PoB contribution.
   - `adjust`: multiply the group's spells by `dps_multiplier`.
   - `block`: a violation that makes the build infeasible. Only curated entries block, and only when the gem sits in a scored group.

   **Applying a credit.** How a credit changes DPS depends on the entry's role:
   - Role `skill`: the credit multiplies that skill's DPS. Zerphi's Infamy also uses this role, because the whole skill is gated.
   - Role `support`: the formula is `m + credit·(d − m)`, where `m` is the engine's drop-one marginal for the gem (`marginals: [{group, gem_id, dps_without}]`). Without a marginal, the fallback is `1 − (1−credit)·typical_share`, which is flagged `no_marginal`.
   - Role `global` (keystones, uniques): the same fallback, applied to every part.
4. **Floors.** A failed hard floor is a violation. A failed target floor is listed in `below_target`. An unknown floor is flagged `unverified`.
5. **Constraints.** These are hard filters on the skill list.
6. **Objective.** The objective is `log(dps_honest) + 0.3·log(EHP) + 0.1·log(lowest max hit) + 0.5·log(movement × utility) + 0.2·log(min(1, cost sustain)) + 0.05·log(ailment threshold ÷ (life+ES)) [+ 0.3·log(aoe) on mapping_v1]`.

   Weights are set with `Scorer(weights=...)`, and a weight may be a per-profile dict. With several profiles, the score is the minimum objective across them, and the build is feasible only if every profile passes.

`verified` is false when any flag starts with `unverified:`. Flags of that kind include missing fields, an unknown signal, an unverified registry entry that changed a number, and an unmodelled meta. `Scorer(reject_unverified=True)` makes such builds infeasible.

## Rules for the POC offenders

| Gem | Rule (pinnacle_v1 / mapping_v1) | Source |
|---|---|---|
| Hexblast | Blocked unless the build applies a curse (a gem with AppliesCurse, Cast Curse on Block, or Rondel de Ezo). With a curse it is discounted to 0.5 / 0.7 for the re-curse cycle. | 1-button 237/197 |
| Detonate Dead | `corpses_present`: blocked on pinnacle unless the build makes corpses (Sacrifice plus minions); 0.8 on mapping. | 1-button 360/321; support-opt +380% |
| Snap | Credit = the best shock, ignite or freeze chance of the build. Blocked at 0%: in 1231, Elemental Focus on the feeder removed every ailment. | 1-button 223/1231/350 |
| Overextend | `self_daze` drawback: 0.5 / 0.7 of the gem's share (typical 0.2). | support-opt (6/20 strict) |
| Inexorable Critical I/II | `ramp_up_time`: 0.8 / 0.4. | support-opt (5/20) |
| Atziri's Impatience | Checks 25% of life+ES × uses/s against life regen and leech. Blocked when that is unsustainable, otherwise flagged. | support-opt (Flameblast) |
| Atalui's Bloodletting | Life cost/s against regen + leech + pool/window (60 s pinnacle, 20 s mapping). Unsustainable: the whole skill is credited at the usable fraction. | support-opt |
| Minion Pact I/II | `minion_present` from the build (minion skill or minion DPS). Without a minion the credit is 0. | support-opt (7/20 best case) |
| Zerphi's Infamy | Glory/s = 5% of life spent/s. The usable fraction is Glory/s ÷ (50 × uses/s), applied to the whole skill. With no life spent the credit is 0. | support-opt (6/20 best case) |
| Hit and Run | `moving`: 0.5 / 0.85 with typical share 0.6, because the movement gate limits every use. | support-opt |
| Deliberation | DPS is unchanged. It is a utility penalty: movement ×0.95 / ×0.9. | support-opt flag |
| Spell Cascade (group rule) | Applies to every spell socketed with it, so a whole Meta group. PoB applies −30% damage and ignores the cascades, so the rule multiplies by 1.43 (net neutral against no cascade). On mapping, area ×2.28. Unverified. | synergy §5, 1-button |
| Uhtred's Omen | The +2 levels count only with exactly one other support in the group, meta-only supports included (PoB ignores those). | support-opt caveat |
| Concentrated Area | DPS is allowed as is. Area ×0.5 on mapping. | support-opt (6/20) |

## Adding or changing a condition

1. **Reuse or add a catalogue state.** If the state is not in `conditions:` in `conditions.yaml`, add it with a credit per profile:
   - a number,
   - `build` (the credit comes from a `signal`), or
   - `build_or:x`.

   Add a `default` and `as_exported: 1.0`. A new signal is a method `s_<name>` on `Signals` in `conditions.py`. It returns a bool, a fraction, or `None` when unknown.
2. **Add an entry** under `entries:`. Set these fields:
   - `id`, `kind`, `name`, and `match: {ids: [<PoB granted-effect id>], names: [...]}`
   - `role`, `conditions` and/or `drawbacks`, and `typical_share` for supports
   - `verified` and `source`

   Optional fields:
   - `treatment: {profile: {action, credit}}` to override the computed credit
   - `requires` with `if_unmet`
   - `check` with `params`, for a custom computation added to `CHECKS`
   - `utility: {profile: {movement_speed_mult, aoe_mult}}`

   A curated entry replaces any auto entry with the same id, gem id or name.
3. **Add a test** in `tests/test_scoring.py`. Use `synthetic(...)` for a quick build, or a POC fixture.
4. **Regenerate the draft after a PoB data update**, then diff the high-risk list:

   ```bash
   python3 -m exilegen.scoring.gen_conditions --pob $POB_ROOT
   ```

   The scanner reads descriptions, stat ids and the `TargetsDestructibleCorpses` skill type, plus keystone text and current-variant unique lines. It sorts entries into three risk tiers:
   - **high:** a hard state and a gating phrase in the same sentence.
   - **medium:** a damage bonus behind a condition.
   - **low:** anything else.

   Auto entries never block. A high entry is discounted to no less than 0.5 and is always `unverified`. Medium and low entries are only flags. Promote an entry by copying it into `conditions.yaml` and curating it.

## Engine fields this layer reads

`adapter.FIELDS` lists every path. The main ones are:

- `stats.*` (PoB output names, including `*ResistMax` and `Req*`)
- `dps.{player,minion}`
- `offence.{hitsPerSec,speed,critChance,shockChance,igniteChance,freezeChance,...}`
- `sustain.{manaUsePerSec,lifeUsePerSec,manaRegen,lifeRegen,manaLeech,lifeLeech,lifeCost}`
- `maxHit.{Physical,...}`
- `spirit.unreserved`
- `movementSpeed`
- `ailmentThreshold.ailment`
- `requirements.{Str,Dex,Int}.need`
- `unsupported.{count,sample}`
- `main.group`
- `mainSkill`
- `energy[]`:
  - `group`, `metaSkillId`, `metaName`, `maxEnergy`, `genBonus`, `refund`, `perEvent`
  - `spells[].castTime`
  - `feeder.{hitsPerSec,critChance,...}`
  - `triggered[].{name,id,dps,pobRate}`
- `skills()` view (`groups[].gems[]` + `groups[].skills[]`)

Two inputs are still missing from the engine:

- **`keystones` and `uniques` (names).** Without them the keystone and unique entries, CI handling and the `require_`/`forbid_` constraints are unknown.
- **`marginals`.** Without drop-one values, support discounts fall back to `typical_share`.

## Tests

```bash
python3 -m unittest tests.test_scoring        # or: pytest tests/test_scoring.py
python3 tests/fixtures/scoring/make_fixtures.py --pob $POB_ROOT   # rebuild fixtures from /mnt/project-files
```
