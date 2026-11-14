# PoE2 corpus: synergies, interactions, anti-meta gaps

**Data:** 1,488 builds from poe.ninja (Forbidden Rites; every build in `evaluated.jsonl`, 0 errors), evaluated in PoB-PoE2 0.23.1 under the **pinnacle_locked** profile. The corpus is stratified (skill × ascendancy), so lift is relative to the **sample**, not to real popularity. Global counts below are taken from `overview.json` (124,124 characters).
**Main skill:** I re-ran PoB on every build (engine scan) and set each build's main skill to the highest-DPS group among PoB's main group and poe.ninja's `main_skills`. That changed 138 builds; 1,350 kept PoB's main group. 1,325 builds belong to the 48 skills with at least 5 builds and DPS above 0. Those builds have a residual = log10 DPS − the median for their skill.
Scripts: `tools/analysis/synergy/` (`analyze.py`, `gaps.py`, `mkjobs_*.py`, `parse_engine.py`, `engine-exp/` Rust driver). The CSVs are in `analysis-synergy/`.

## 1. Co-occurrence and the DPS link (`cooc_pairs.csv`, `synergy_pairs_highdps.csv`)
Columns: lift = P(ab)/P(a)P(b), and Δres = mean residual with the pair minus without it. For skill pairs, "without" means the same skill without the other component. Each pair has at least 5 builds with it and at least 5 without.

| Pair | n | lift | Δres (log10) |
|---|---|---|---|
| Ice Shot + Garukhan's Resolve (support) | 113 | 4.3 | +1.06 |
| Flameblast + Concentrated Area | 64 | 9.0 | +0.92 |
| Dark Consequences + Impending Doom / The Vertex / Windscream | 12–23 | 28–30 | +0.66…0.88 |
| Arc + Dominus' Grasp | 40 | 23.5 | +0.69 |
| Spark / Grim Pillars + Rathpith Globe | 19 / 25 | 6 | +1.14 / +1.03 |
| Arc / Grim Pillars + Eldritch Battery | 35 / 34 | 4–6 | +0.88 / +0.97 |
| Twister + Dance with Death | 121 | 7.2 | +0.68 |
| Giant's Blood + Chober Chaber (+ Sylvan's Effigy, Eshtera's Path) | 32 | 27 | +1.11 |
| Oracle + Conduit (+ The Unseen Path) | 54 | 11 | +0.63 |
| Alpha's Howl + Svalinn | 49 | 6.0 | +0.57 |
| The Vertex + Windscream (curse kit) | 8 | 61 | +0.45 |

The PMI leaders are mostly locked packages: an ascendancy with its keystone (Giant's Blood with Idolatry, Natural Order and Catha's Balance), notable clusters (Core of the Guardian with Wide Barrier, n=99), and companion setups (Chober Chaber with "minion damage also affects you" and Giant's Blood).

## 2. Additive model and outliers (`ridge_coefs.csv`, `dps_outliers.csv`, `interaction_candidates.csv`)
I fit a ridge regression on 1,188 one-hot components that each appear at least 8 times (supports, uniques, keystones, ascendancy and other notables, other gems, class). The target is the within-skill residual. Out-of-fold R² is **0.53** (λ=30).
- **Top coefficients:** Voices +0.26, Rakiata's Flow +0.25, Breachlord's Amalgam +0.23, The Adorned +0.21, Garukhan's Resolve +0.19, Mageblood +0.17, Prism of Belief +0.16. Mageblood, Heart of the Well and From Nothing behave partly as **wealth proxies**.
- **Largest out-of-fold residuals:** Frostbolt Infernalist/Shaman/Gemling builds (+1.8 to +2.8 log10 above the model). All of them combine **Sione's Temper**, Vilenta's Propulsion and a Comet setup (The Stars Answer).
- **Interaction candidates:** pairs or triples whose mean residual beats each of their parts by more than 1 t-unit:

| Combo | n | mean model resid | t |
|---|---|---|---|
| Frostbolt + Sione's Temper (+Comet group) | 5 | +2.20 | 9.9 |
| Infernalist/Grinning Immolation + Sione's Temper | 5 | +1.14 | 5.2 |
| Loyalty + Blasphemy/Temporal Chains (companion) | 5 | +1.08 | 4.9 |
| Lord of the Wilds + Devour | 5 | +0.90 | 4.1 |
| Crystalline Phylactery + Chaos Inoculation | 5 | +0.84 | 3.8 |
| Feeding Frenzy II + Skeletal Warrior/Sniper | 6–8 | +0.6–0.7 | 3.5 |
| Boundless Energy II + Overabundance II | 5 | +0.75 | 3.4 |
| Sione's Temper + From Nothing + Heart of the Well | 16 | +0.53 | 4.3 |

Unsupported mod lines do not explain the residuals (r = 0.006).

## 3. Anti-meta gaps (`antimeta_candidates.csv`, `whatif_uniques.csv`)
I paired components that share mechanic tags (damage type, ailment, conversion, minion, trigger and similar), taken from PoB gem descriptions, skill types and unique text, and that appear together at most once in the corpus. This gave 1,919 candidates: 539 support–skill, 627 unique–skill and 753 unique–unique.

**What-if test 1, uniques (engine, 64 runs, 24 candidates):** I equipped the PoB DB unique into the matching slot of 3 builds (median, best and lower-quartile residual) and kept the main group fixed.

| Candidate | median ΔDPS | ΔEHP |
|---|---|---|
| Blackflame → Sacred Flame builds | +19% (n=2, range −20…+19) | −5% |
| Nightscale → Morior Invictus builds | +3% | −18% |
| Skin of the Loyal, Waveshaper, Crown of Eyes, Atziri's Disdain | 0% | −33…−43% |
| Eshtera's Path / Blackflame → Ice Shot, Painter's Servant → Frostbolt, The Taming → Snap | −23…−25% | −15…−31% |
| Alpha's Howl → Navira, Choir of the Storm → Morior, Forgotten Warden → Rathpith | −29…−30% | — |

A single-item swap reveals almost none of these gaps. That fits the build-around uniques, which pay off only with tree and support changes. The optimizer should test unique swaps **jointly** with re-optimizing tree and supports.

**What-if test 2, supports:** see section 5. It is the stronger gap signal.

**Shortlist to test (20):**
- **Supports with high PoB gain that are rare on the skill:** Zerphi's Infamy on Twister / Flicker Strike / Quarterstaff Strike / Falling Thunder; Rakiata's Flow on Arc / Twister / Comet / Navira / Zekoa; Hit and Run on Crossbow Shot / Ice Shot; Atziri's Impatience and Eternal Flame on Flameblast; Elemental Focus and Inexorable Critical on Ice Shot / Spark; Concentrated Area on Frostbolt / Navira; Commandment on Navira; Uul-Netol's Embrace on Zekoa / Crossbow Shot.
- **Uniques:** Blackflame + Sacred Flame (fire/ignite), Sione's Temper builds without Infernalist, Rathpith Globe on Flameblast (trigger/crit), Svalinn on Flameblast, Skin of the Loyal on Falling Thunder.

## 4. Uniques (`uniques.csv`)
Build-around score = within-skill gap in log10 DPS (with vs without the unique, weighted across skills).

| Unique | n | gap | Notes |
|---|---|---|---|
| Bones of Ullr | 12 | +1.27 | minion reservation (Twister, Wolf Pack) |
| Rathpith Globe | 92 | +0.92 | life-scaled spell crit, 7 skills |
| The Vertex | 14 | +0.88 | 86% Dark Consequences, curse kit |
| Runeseeker's Call | 232 | +0.87 | **grants The Stars Answer** (233); a second damage source |
| The Adorned | 148 | +0.72 | corrupted magic-jewel effect |
| Windscream | 14 | +0.66 | curses, Dark Consequences |
| Temporalis, Voices | 44 / 443 | +0.65 / +0.64 | |
| Sunsplinter | 15 | +0.55 | grants Parry |
| Chober Chaber | 35 | +0.41 | 77% Zekoa companion |

Item-granted main skills: The Unborn Lich (18), Choir of the Storm (12, all Lightning Bolt, lift 98, Δres +1.56), Svalinn (Raise Shield/Cast on Block, 116), plus 69 builds whose best group is the weapon Default Attack. Many of those are probably mis-assigned.
Tight unique↔unique packages: Drillneck + Widowhail (n=35, lift 39), The Vertex + Windscream, Atziri's Disdain + Nightscale, Grand Spectrum + Veil of the Night, Myris Uxor + The Flesh Poppet.

## 5. Ladder supports vs PoB's choice (`support_vs_pob.csv`)
I took 37 builds from the 15 largest skills (median, best and lower-quartile residual). For each I measured the DPS lost by removing each socketed support, and ran PoB `list_valid_supports sortByDps` on the main group.
- **Median overlap** between the player's N supports and PoB's top-N by marginal value: **0.2**.
- **Median estimated gain** from swapping the weakest support for PoB's best unused one: **+50%**. This is an additive estimate.
- On average **1.5 socketed supports per build add about 0 PoB DPS**: Efficiency II, Fork, Ice Bite II, Verglas, Sione's Temper, Rageforged II and Boundless Energy II. These are utility supports, or mechanics PoB does not model.
- Some supports **lower PoB DPS**: Spell Cascade and Breachlord's Amalgam (each twice), Cold Attunement and Searing Flame II. Breachlord's Amalgam still has the 3rd-highest ridge coefficient, so PoB probably undervalues returning projectiles.
- PoB's most frequent unused picks: Zerphi's Infamy (12), Rakiata's Flow (11), Concentrated Area (10), Hit and Run (9), Elemental Focus (7) and Minion Pact II (7). Minion Pact needs a minion and Hit and Run needs movement, so PoB scores such conditional supports at their best case. Rakiata's Flow is a lineage support (41k global users); in 5 of the 13 cases where PoB recommends it, the build already uses it on another group.
