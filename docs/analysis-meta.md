# PoE2 ladder meta vs PoB power: Forbidden Rites, n = 1488

Data: poe.ninja snapshot 1430-20261113 (124,124 characters). The 1488 builds were evaluated in PoB-PoE2 0.23.1 and all stats come from the `pinnacle_locked` profile. CSVs and `analyse.py` are in `analysis-meta/`.

**Read this first.** The sample is stratified on purpose (README): 1112 builds from the top 40 skills × their top 6 ascendancies, 200 from skills ranked 41–75, 100 from the overall top and 76 from the per-class top. It leans toward rare archetypes, so **every play-share figure comes from overview.json, never from sample counts**. DPS is the skill PoB marks as main, or the minion's DPS for 113 minion builds. That skill matches poe.ninja's main skill in only 331 builds. There is no per-group DPS, so "skill" below means the skill PoB actually computed. Groups with n<5 stay in the CSVs as `small_n=True` and get no flag.

## 1. Meta map (shares from poe.ninja)

| Ascendancy | play % | | Ascendancy | play % |
|---|---|---|---|---|
| Gemling Legionnaire | **32.4** | | Infernalist | 5.7 |
| Martial Artist | 10.6 | | Stormweaver | 3.5 |
| Oracle | 7.3 | | Titan | 2.8 |
| Disciple of Varashta | 7.2 | | Blood Mage | 2.7 |
| Spirit Walker | 6.8 | | each of the other 15 | ≤1.7 |
| Deadeye | 6.0 | | | |

The main skills used most on poe.ninja (% of characters) are Herald of Ice 17.9, Frost Bomb 16.9, Barrage 15.8, Spark 15.8, Comet 14.7, Elemental Weakness 13.2, Whirling Slash 12.8, Herald of Thunder 12.7, Arc 11.1, Wolf Pack 11.0 and Twister 10.9. Many of these are setup or buff skills. The skills PoB actually computes are mostly Ice Shot, Comet, Twister, Grim Pillars, Flameblast, Living Bomb, Spark, Zekoa, Dark Consequences and Arc.

The sample mix below is shown for context only and is **not** play share:

| Archetype (sample %) | | Defence pool | | Layer threshold | |
|---|---|---|---|---|---|
| attack | 41.9 | life | 37.1 | evade ≥40% | 38.2 |
| self-cast spell | 23.4 | hybrid life+ES | 21.8 | deflect ≥20% | 34.5 |
| triggered spell | 16.2 | CI / ES-only | 21.3 | MoM | 20.6 |
| DoT (>50% non-hit) | 8.2 | ES-primary | 19.8 | armour PDR ≥40% | 17.9 |
| minion | 8.1 | | | EB | 12.8 |
| non-damaging | 1.9 | | | block ≥30% | 6.5 |

43% of builds reach none of the armour, evade, block or deflect thresholds. Defence choices in the sample match poe.ninja well (poe.ninja % / sample %): CI 21.9/21.2, MoM 16.4/20.6, EB 12.1/12.8 and Dance with Death 12.9/11.8. Pain Attunement is over-represented at 13.8/21.1. On poe.ninja's own traits, 36% of characters use Triggered skills, 11% use Totems, and Crit vs no-crit is about 52/48.

## 2. Power vs popularity (n≥5)

Across the sample, median DPS is 331k (p90 3.34M), median EHP is 26.5k and the median lowest max-hit is 6.3k.

| Skill | n | med DPS | p90 DPS | med EHP | med min-hit | play % | flag |
|---|---|---|---|---|---|---|---|
| Arc | 41 | 4.09M | 22.7M | 18.4k | 8.5k | 11.1 | strongest and already popular |
| Flicker Strike | 28 | 2.45M | 15.8M | 40.1k | 5.2k | 2.3 | **STRONG-RARE** |
| Plasma Blast | 13 | 2.67M | 10.7M | 11.9k | 5.3k | 0.4 | strong-rare but glass |
| Lightning Bolt (trig.) | 12 | 2.51M | 5.5M | 34.4k | 9.8k | 1.2 | **STRONG-RARE** |
| His Scattering Calamity | 18 | 1.74M | 3.4M | 14.9k | 6.9k | 0.5 | STRONG-RARE |
| Ember Fusillade | 9 | 1.32M | 1.9M | 23.2k | 6.7k | 1.5 | STRONG-RARE |
| Skyfall / Poisonburst / Bone Cage | 7/5/8 | 0.46–0.57M | 2.3–7.6M | 21–31k | 5–12k | <1 | STRONG-RARE |
| Companion: Zekoa | 45 | 0.91M | 4.0M | **103k** | **25.3k** | 3.6 | safest strong |
| Dark Consequences | 44 | 0.91M | 5.1M | 21.6k | 7.5k | 2.8 | strong, under-played |
| Comet | 150 | 0.85M | 5.7M | 17.5k | 6.4k | 14.7 | popular, fragile |
| Spark | 46 | 0.75M | 3.4M | 17.4k | 7.6k | 15.8 | popular, fragile |
| Ice Shot | 164 | 0.57M | 4.1M | 25.7k | 4.0k | 7.1 | |
| Twister | 144 | 0.34M | 1.6M | 31.1k | 5.2k | 10.9 | |
| Living Bomb | 59 | 0.10M | 1.05M | 16.5k | 5.8k | 6.2 | **POPULAR-WEAK** and fragile |
| Ice Nova / Orb of Storms | 13/6 | 0.36M/65k | 1.1M | 11–13k | 5.5–5.9k | 6.5/5.0 | popular, fragile |
| Crossbow Shot / Rend / Spear Throw | 31/10/5 | 17–47k | 0.04–0.38M | 18–32k | 4–6k | 4–5 | popular, low DPS |

Four ascendancies are STRONG-RARE: Pathfinder (median 849k, p90 5.7M, 1.4% play), Shaman (p90 2.7M, min-hit 10.3k, 1.5%), Invoker (p90 2.3M, 0.3%) and **Blood Mage** (median 912k, p90 4.8M, EHP 78k, 2.7%), which has the best balance of damage and defence. Gemling, at 32% of play, is upper-mid on power (528k / 4.8M, EHP 49k). Infernalist (EHP 13.9k) and Stormweaver (15.3k) are popular but fragile. Witchhunter, Tactician, Warbringer, Acolyte and Smith all have median DPS under 60k.

**Artefacts:** Frostbolt, Wolf Pack, Oil Grenade and Raise Shield show median DPS under 10k because PoB marks a setup skill as main. These are mislabelled, not weak.

## 3. Constraint builds

| Segment | n | med DPS | p90 DPS | med EHP | med min-hit |
|---|---|---|---|---|---|
| oneButton | 245 (16.5%) | **722k** | 4.12M | 33.9k | 8.6k |
| noCast | 362 (24.3%) | 217k | 2.53M | 35.3k | 6.1k |
| both | 114 (7.7%) | 653k | 3.87M | **51.6k** | **12.7k** |
| neither | 995 | 331k | 3.16M | 25.3k | 6.4k |

One-button builds give up no damage and are tankier than the rest. 113 of them press no damaging skill at all: 106 are minion builds, 83 spell or triggered spell, and 33 attack. In 32 builds, a skill granted by an item adds a second button: Whirling Assault, His Scattering Calamity, Devour or Oil Barrage.

The enablers below show the count in one-button builds and the lift, which is how much more often they appear there than in other builds.
- **The one button pressed:** Spark (39) as a crit or ailment engine, then Pounce, Shattering Palm, Parry and Ice Nova.
- **Meta gems:** Cast on Critical (50), Cast on Elemental Ailment (33), Cast on Minion Death (23, ×3.8) and Cast on Block (23).
- **Triggered damage:** Comet (50), Arc (30), Ice Nova (30, ×2.7) and Lightning Bolt (10, ×3.9).
- **Uniques:** Sylvan's Effigy (46, ×78), Chober Chaber (35) and Eshtera's Path (17), which appear only in one-button builds, Forgotten Warden (26, ×7), Evergrasping Ring (19, ×48), The Unborn Lich (16, ×20), The Hammer of Faith, Windscream, The Vertex and Choir of the Storm.
- **Granted or persistent skills:** Wolf Pack (×60), Navira (×71), Kelari (×54), Azmerian Wolf, Skeletal Warrior, Discipline (×9) and Wild Protector. The common persistent damage source is Arctic Armour.

Top 10 one-button builds by DPS, counting only builds with EHP ≥ 15.8k and min-hit ≥ 4.4k (sample p25):

| line | Asc | Skill | DPS | EHP | min-hit | Setup |
|---|---|---|---|---|---|---|
| 1309 | Gemling | Doom Blast | 28.0M | 18k | 9.6k | presses Frost Wall only |
| 822 | Spirit Walker | Zekoa | 27.9M | 57k | 26.6k | companion only |
| 237 | Oracle | Comet | 19.6M | 22k | 9.9k | Spark → Cast on Critical Comet |
| 197 | Gemling | Spark | 11.4M | 22k | 10.5k | Spark + Cast on Critical (Comet, Frost Wall) |
| 350 | Infernalist | Comet | 11.2M | 38k | 4.9k | Spark → Cast on Critical + Cast on Elemental Ailment |
| 276 | Blood Mage | Dark Consequences | 10.7M | 22k | 9.9k | no button; Windscream, The Vertex |
| 1213 | Spirit Walker | Quadrilla | 7.9M | 425k | 333k | companion only |
| 321 | Martial Artist | Quarterstaff Strike | 7.9M | 50k | 5.9k | Shattering Palm + Cast on Critical |
| 1335 / 1410 | Spirit Walker | Zekoa | 7.7M / 7.4M | 24k / 349k | 9k / 191k | companion only |

Every one of these builds runs Mageblood or Morior Invictus, so they are high-budget examples.

## 4. Defensive floors in use

| Metric | p05 | p10 | p25 | p50 |
|---|---|---|---|---|
| Fire/Cold/Lightning res | 36–40 | 54–58 | 74 | 75 |
| Chaos res (non-CI, n=1172) | 0 | 0 | 31 | 65 |
| Life+ES (all builds) | 1.26k | 1.58k | 2.20k | 4.11k |
| Life (life-based builds) | 1.36k | 1.48k | 1.66k | 2.02k |
| Life+ES (hybrid builds) | 2.57k | 2.86k | 3.41k | 4.06k |
| Total EHP | 8.0k | 10.0k | 15.8k | 26.5k |
| Lowest max-hit | 2.69k | 3.26k | 4.36k | 6.34k |
| Physical max-hit | 2.89k | 3.47k | 4.57k | 6.46k |
| Elemental max-hit | 6.6–7.1k | 9.3–9.7k | 13.2–13.7k | ~20k |
| Spirit | 100 | 120 | 150 | 215 |

57% of builds are capped on all three elemental resistances, and 99.5% of non-CI builds keep chaos res at 0 or above. Physical max-hit is the limiting defence.

**Suggested optimiser floors:**

| Floor | Elemental res | Chaos res (non-CI) | Life+ES | EHP | Lowest max-hit | Spirit |
|---|---|---|---|---|---|---|
| Hard (≈p10) | ≥ 55 | ≥ 0 | ≥ 1.6k | ≥ 10k | ≥ 3.3k | ≥ 120 |
| Target (≈p25) | 75 | ≥ 30 | ≥ 2.2k (hybrid ≥ 3.4k) | ≥ 15.8k | ≥ 4.4k (physical ≥ 4.6k) | — |

## 5. Reliability: unsupported lines

A build has a median of 14 lines PoB cannot model (p10 10, p90 30). That count correlates (Spearman) **+0.23** with DPS, **+0.19** with DPS inside the same skill, and −0.06 with EHP. Median DPS by tercile of unsupported lines (0–11 / 11–18 / 18–54) is 155k, 362k and 485k. Unsupported mods cluster in the best-geared builds, so PoB understates the top end the most, and does so in one direction.

Share of builds hit by each group of mechanics: Bonded rune/idol mods 82%, item-meta lines 78% (cosmetic), Archon/Invocation/Energy 51%, recoup/leech/recovery 41%, Headhunter/enemy Power 40%, charges/charms 39%, Remnants/Infusions 34% and Hinder/Maim/Daze 32%.

| Top unsupported lines | % builds | | Top unsupported lines | % builds |
|---|---|---|---|---|
| Sinister Jewel Socket | 38.6 | | Archon Buffs grant #% all Ele Res | 22.1 |
| Bonded: Archon recovery period #% faster | 38.4 | | Archon Buffs grant #% Movement Speed | 22.1 |
| Can roll Marksman modifiers | 35.1 | | #% increased Archon Buff duration | 21.9 |
| Remnants collected #% further away | 28.8 | | Bonded: Leech #% Life on Spell cast | 21.0 |
| #% reduced Slowing Potency of Debuffs | 25.3 | | Grants Thaumaturgical Dynamism | 20.7 |
| Kill Rare → gain its Modifiers (Headhunter) | 25.2 | | #% chance for additional Remnant | 20.4 |
| Bonded: chance for extra random Charge | 24.7 | | Faster ES Recharge start while Archon | 19.0 |
| Augment Sockets → Jewel Socket | 24.1 | | Can roll Chronomancy modifiers | 17.6 |
| Bonded: #% Gold Quantity | 23.3 | | Archon Buffs no recovery / reduced effect | 17.4 |
| Bonded: reduced Slowing Potency | 17.3 | | Meta Skills gain #% increased Energy | 13.6 |

**Patch priority:** (1) Archon buffs: duration, resistances, ES recharge and recovery. (2) Remnants and Infusions. (3) Headhunter. (4) Thaumaturgical Dynamism. (5) Bonded leech and charge mods. (6) Meta-gem Energy gain, which sets the trigger rate of one-button builds. Socket, "can roll", gold and slow-potency lines are cosmetic.
