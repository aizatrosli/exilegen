# Proof-of-concept optimisers: results (2026-11-14)

These are three POCs, each run on real poe.ninja ladder builds from the corpus. All of them use the pob-redux PoB engine (PoB-PoE2 0.23.1) with a locked Pinnacle-boss config and one engine per POC. Each optimised build was exported as a PoB code. The code is in `tools/poc/`, and the per-build results and PoB codes are in the project's `poc/` folder.

## Summary

| POC | Builds | Median DPS gain | Cost per build | Verdict |
|---|---|---|---|---|
| Support gems (greedy + pairwise swap) | 20 | **+113%** strict / +176% if supports with conditions are allowed (p90 +205% / +303%) | ~5k evals, ~57 s | Works. The cheapest and most trustworthy win. |
| Passive tree, warm start (player tree + local search) | 10 | ×2.22 DPS at EHP ×1.01 | ~1.6k evals, ~14 s | Works, with caveats about the objective |
| Passive tree from scratch (budgeted PCST heuristic + local search) | 10 | ×2.15 DPS, overlap with the player's tree only 0.36 | ~8.7k evals, ~68 s | Solver works; the objective is too narrow |
| 1-button (trigger spell and support search, energy-corrected) | 8 + 4 conversions | ×1.07–×2.9 after the energy correction; 2 of 4 conversions | ~90–130 s | Promising. Energy modelling changes which builds win. |

Measured throughput in real searches was about 6–10 ms per evaluation. That is slower than the 1.2–4 ms seen in micro-benchmarks, because guardrails need full calcs.

## What we learned

1. **PoB's number is the optimiser's target, so every gap in PoB becomes a fake win.**
   - **Supports:** the "strict" gains still include supports whose drawbacks PoB doesn't model: Overextend daze, Inexorable Critical ramp-up, Atziri's Impatience life loss and Atalui's Bloodletting. 11 of the 20 builds use at least one of them.
   - **1-button:** the large wins from Hexblast and Detonate Dead rely on PoB assuming the enemy is cursed or that corpses exist, which isn't true on a Pinnacle boss.
   - **Needed:** a per-gem "condition registry" that blocks or discounts these gems under each config profile.
2. **The energy model matters.**
   - **Optimising on PoB's raw number picks the wrong spells.** Hexblast scored ×3.5 raw but ×1.67 once energy-corrected.
   - **Some baselines are already overstated:** Arc runs at 0.11–0.48× of PoB's assumed trigger rate.
   - **Comet fed by a Spark at 100% crit** is not energy-limited at Power 20.
   - **Results depend on enemy Power.** It must be a locked config parameter.
3. **The tree objective needs more terms.** Players spend points on avoidance, ailments, movement, mana and spirit, which a log(DPS)+0.3·log(EHP) objective ignores. We need to add:
   - mana sustain;
   - movement speed;
   - ailment threshold and avoidance;
   - spirit;
   - max-hit for each damage type.
4. **Structural fixes for the tree solver:**
   - count weapon-set points separately;
   - treat notables granted through jewels as fixed free nodes;
   - score keystones together with their supporting cluster (multi-node moves);
   - add restarts or perturbation;
   - handle jewel-radius effects (what-if and allocated scores differ slightly).
5. **For 1-button builds, Spirit is the binding constraint.** Conversions failed when the meta gem's 100 Spirit couldn't be freed without breaking a floor. The search needs Spirit-aware moves: drop or swap persistent skills, and add Spirit gear or tree nodes.
6. **Gains compound.** Support swaps multiply: Detonate Dead +380%, Flameblast +205%, Poisonburst Arrow +203%. Rare, less-tuned skills gain the most, which supports seeding the search from under-played archetypes.

## Recommended next steps

1. Merge the three POC drivers into one persistent engine service (`pobrepl`, which tree-solver already has), with a shared what-if API and locked profiles. That is phase 0 of the design doc.
2. Add a condition registry for gems and keystones, plus the energy model, to the scoring layer, and re-run the POCs to get honest numbers.
3. Chain the steps: tree warm start → supports → tree again, run the 1-button checker inside the loop, and add an outer loop over skeletons.
4. Validate in game: take the top 3 exported builds, play-test them, and compare real boss kill times against PoB.
