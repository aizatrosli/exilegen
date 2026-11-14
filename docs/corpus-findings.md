# PoE2 ladder build mining: the bigger picture

**Data:** 1,488 builds from poe.ninja, Forbidden Rites softcore exp ladder (snapshot `1430-20261113-58818`, 124,124 characters in the snapshot). Collected 2026-11-13.

**Sample design:** the sample is stratified toward rare archetypes:
- top 100 of the ladder;
- top 40 main skills × their top 6 ascendancies;
- skills ranked 41–75;
- the top 5 of every ascendancy.

Play-share figures below come from poe.ninja's own aggregates. Every power figure comes from PoB.

**Evaluation:** pob-redux engine running PoB-PoE2 0.23.1, 4 engines at about 50 builds/min, 0 errors. Each build was scored under two settings:
- the exported config;
- a locked Pinnacle-boss config, used for every comparison below.

**Details:**
- [analysis-meta.md](analysis-meta.md): meta, power vs popularity, constraint builds, floors, PoB gaps
- [analysis-synergy.md](analysis-synergy.md): co-occurrence, outliers, gaps, uniques, supports vs PoB
- [README.md](README.md): endpoints and sampling

## Seven takeaways

1. **The meta is concentrated.**
   - Gemling Legionnaire is 32% of all characters, followed by Martial Artist (11%) and Oracle (7%).
   - In the sample, median boss DPS is 331k, median EHP 26.5k, and median lowest max-hit 6.3k.
   - 43% of builds reach none of the armour, evasion, block or deflect thresholds. Physical max-hit is the most common weak point.

2. **Strong but under-played options exist.** These have high PoB DPS and EHP but a low play share:
   - **Skills:** Flicker Strike, triggered Lightning Bolt, His Scattering Calamity, Ember Fusillade, Skyfall, Bone Cage.
   - **Companion:** Zekoa, at 103k EHP.
   - **Ascendancies:** Pathfinder, Shaman, Invoker, and Blood Mage (912k DPS, 78k EHP, 2.7% play).

   These are the first places to point the search.

3. **1-button and no-cast builds are viable, not niche.**
   - 245 builds are 1-button, 362 are no-cast, and 114 are both.
   - 1-button builds have the highest median DPS of any group: 722k, vs 331k for builds with neither.
   - Builds that are both are the tankiest, at 52k EHP.
   - **What makes them work:**
     - Spark feeding Cast on Critical or Cast on Elemental Ailment into Comet, Arc or Ice Nova
     - Choir of the Storm (Lightning Bolt)
     - Cast on Minion Death
     - Companions such as Zekoa
     - Uniques: Sylvan's Effigy, Chober Chaber, Eshtera's Path, The Unborn Lich
   - **Caution:** PoB doesn't model meta-gem Energy, so trigger rates are assumed rather than calculated.

4. **Players leave PoB-measurable DPS on the table.** In 37 ladder builds, the supports players use match PoB's top picks only 20% of the time.
   - Swapping the weakest support for PoB's best unused one gives about +50% median DPS.
   - About 1.5 supports per build add roughly nothing.
   - **Most-missed picks:** Zerphi's Infamy, Rakiata's Flow, Concentrated Area, Hit and Run, Elemental Focus.
   - Some of these are conditional and PoB scores them at their best case, so treat the +50% as an upper bound.

5. **Build-around uniques are real, but single swaps don't reveal them.**
   - Within-skill DPS gaps for builds that use the unique: Bones of Ullr is about 19× higher, Rathpith Globe about 8×, The Vertex about 8×, Runeseeker's Call about 7× (it grants The Stars Answer), and Choir of the Storm about 36× for Lightning Bolt.
   - Yet dropping a candidate unique into an existing build almost never helped: 0% to −30% in 64 tests.
   - **For the optimiser:** unique swaps must re-solve the tree and supports around the item. This confirms the skeleton-seed design.

6. **Interaction candidates to verify.** These beat an additive model, so the effect is more than the parts:
   - Frostbolt + Sione's Temper feeding Comet (n=5, about 160×; could also be PoB over-rewarding the trigger)
   - Infernalist + Sione's Temper
   - Loyalty + companion curses
   - Lord of the Wilds + Devour
   - Crystalline Phylactery + CI
   - Feeding Frenzy II + skeletons

   A list of 20 untested pairs is in analysis-synergy.md, section 3.

7. **PoB's gaps all push the same way.** The median build has 14 mod lines PoB can't model, and the more of them, the higher the build's real DPS (r = +0.23). So PoB understates the best-geared builds most.

   **Patch first:**
   - Archon buffs (about 51% of builds)
   - Bonded rune and idol mods
   - Remnants and Infusions
   - Headhunter
   - Thaumaturgical Dynamism
   - Meta-gem Energy

   Breachlord's Amalgam and Sione's Temper also look under-modelled.

## What this changes in the optimiser plan

- **Seed the search** from the under-played strong archetypes and the 1-button trigger setups above.
- **Hard floors from real play:**
  - elemental res ≥ 75
  - chaos res ≥ 0 (≥ 30 target)
  - EHP ≥ 16k
  - lowest max-hit ≥ 4.4k
  - Spirit ≥ 120
- **Support search alone** is worth about +50%. It's the cheapest first win, and a good test that the inner loop works.
- **Priority Lua patches** in pob-redux, to reduce false negatives: Archon, Bonded, Energy.
- **The corpus is ready** for the corpus-guided methods: per-node priors, lift, and anti-meta seeds.
