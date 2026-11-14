# exilegen v0 pipeline run (2026-11-14)

The run used `exilegen.optimise.pipeline.optimise_many` with 2 engines, budget 4000 engine evaluations per build, and `steps="auto"`:

- convert, only if `one_button` is violated;
- then cycles of tree warm → supports → triggers, until a cycle gains less than 1%.

Every build was scored on `pinnacle_v1` and `mapping_v1`, and the objective is the minimum over the two profiles.

- **1-button builds:** the 8 POC 1-button builds and the 2 POC conversions (corpus lines), run with `--constraint one_button`.
- **Mixed builds:** 6 corpus builds with mixed skills, run without constraints.

Script: `scripts/run_pipeline_v0.py --convert`.

| File | Contents |
|---|---|
| `pipeline.csv` | One row per build: honest DPS before/after per profile, EHP, max hit, movement, gain, evals, time, step changes, the diff |
| `builds.jsonl` | Exported PoB codes and the readable diff |
| `results.jsonl` | Full results: step results, history, explain |

**Throughput:** 16 builds took 2,682 s on 2 engines, which is about 5.6 min per build per engine. Builds with Meta+Triggers groups cost about 0.2 s per evaluation per profile, because energy facts need one calc per damaging skill. Other builds cost about 25 ms per evaluation.

## Honest results vs the POC

All 16 builds end feasible: floors pass, and constraints pass where given. Both conversions succeeded. The POC converted 2 of 4. Here the conversions disabled Arctic Armour and Blink for Spirit, as in the POC.

| Build | POC energy-corrected ×  | v0 honest × pinnacle | v0 honest × mapping | What v0 did |
|---|---|---|---|---|
| 237 Comet | 1.67 (Hexblast) | 1.00 | 1.13 | Hexblast is blocked (no curse). Switched to CoEA + Boundless Energy, Siphon Elements disabled |
| 523 Comet / Sigil | 1.07 | 1.41 | 1.17 | supports + meta supports |
| 197 Spark/Comet | 2.93 (Hexblast) | 1.00 | 1.00 | Hexblast blocked; nothing honest found within the budget (one step was rolled back) |
| 350 Arc/Comet | 1.20 (Snap) | 1.14 | 1.26 | spell and meta-support swaps |
| 360 Arc | 7.14 (Detonate Dead) | 1.40 | 1.60 | Detonate Dead is blocked on pinnacle (no corpses) |
| 321 Herald of Thunder | 1.00 | 1.23 | 1.23 | supports + triggers |
| 223 Spark/Comet | 2.13 (Snap) | 1.73 | 1.34 | trigger moves |
| 1231 Comet | 2.87 (Snap) | 3.75 | 2.39 | feeder Spark → Lightning Warp; meta supports |
| convert 1134 | 1.17 | 1.59 | 1.40 | CoEA + new Spark feeder |
| convert 74 | 1.37 | 2.13 | 3.12 | converted, then trigger tuning |
| mixed 185 Barrage | | 1.62 | 1.62 | supports |
| mixed 296 Whirling Slash | | 2.29 | 2.32 | supports |
| mixed 407 Wolf Pack | | 2.15 | 2.23 | tree + supports, EHP 19k → 24k |
| mixed 555 Flameblast | | (338×) | (523×) | artefact: the start scored a near-zero main skill (Fireball, 998 DPS). See below |
| mixed 777 Tornado Shot | | 1.14 | 1.20 | supports |
| mixed 962 Grim Pillars | | 4.10 | 1.24 | start main was Frostbolt (1.7k DPS); same artefact |

How to read the comparison:

- **The POC column** is PoB DPS with the energy correction only. It has no condition registry and includes the feeder's DPS.
- **The v0 columns** are the Scorer's honest DPS. They include the conditions and the Spell Cascade adjustment. They count the main skill and the triggered spells, but not the feeder.
- **Where the POC won through gaps, v0 does not reproduce the win:** Hexblast without a curse (237 and 197), and Detonate Dead on pinnacle (360).
- **Elsewhere v0 matches or beats the POC,** because it also searches meta and feeder choices, supports and Spirit.

## Caveats

- **Tree step did nothing on most builds.** `TreeStep` reports that the engine prunes path-free nodes on any tree change (jewel- and Sinister-socket nodes), so it declines the move. Only 407 and 555 got tree changes.
- **Main skill selection is auto (the highest raw PoB DPS skill).** On some corpus builds that is not the build's real skill: 296 starts on Oil Barrage, 555 and 962 have near-zero starts. The main skill can also change during the search (74: Comet → Fiery Death). Their ×-gains are not meaningful.
- **The optimiser makes two scoring choices** (see the repo README):
  - the mana-sustain floor is in `either` mode;
  - a triggered main skill's cost is scaled by its Energy factor.
- **A step that ends worse than its start is rolled back.** SupportStep's internal scoring sometimes disagrees with a fresh re-score. This happened in 197, 350, 360, 74, 555 and 962.
- **Energy facts are flagged `unverified:energy_inputs_missing`** when a build has an item-granted meta gem with no spells (Soul Locket). That makes most results "unverified".

## Step validation (run separately)

| Step | Builds | Result |
|---|---|---|
| SupportStep | 20 support POC builds, pinnacle_v1 | Median honest gain +96.5%, against +60.9% for the POC's picks re-scored honestly. 11 of the POC's 20 strict picks are now discounted or blocked. About 6k evals and 97 s per build |
| TreeStep warm | 10 tree POC builds, pinnacle_v1 + mapping_v1 | Beats the player's tree on 9 of 10. Honest DPS ×1.41 (geometric mean), EHP ×0.98, movement ×1.00–1.04, Jaccard overlap 0.74 |
| TreeStep scratch | 6 builds without path-free nodes | 4 of 6 beat the player's tree (×1.96 and ×1.77 honest DPS on the two largest gains) |

TreeStep was fixed after the pipeline run above to handle path-free (jewel-granted) nodes, so the pipeline's "tree step did nothing" caveat is out of date. The pipeline has not been re-run yet.

## Known issues / next fixes

1. **exg.lua:** `resolveAlloc` prunes path-free nodes (Intuitive-Leap-like, From Nothing, Sinister sockets) on tree what-if and apply. Keep nodes whose leap source is kept, and expose those sources in the `tree` op. TreeStep currently works around this.
2. **Main skill selection:** "auto" picks the highest raw-DPS skill, which is sometimes a utility skill, and can change during the search. Fix: pin the main skill to poe.ninja's main skill or to a user choice.
3. **SupportStep:** its internal screening sometimes disagrees with a fresh rescore, so its result gets rolled back. Reconcile the screening estimates (marginals, energy) with the full score.
4. **Scorer:** feeder DPS and mana cost are ignored. Snap's condition reads the main skill's ailment chance instead of the feeder's. An empty item-granted meta group (Soul Locket) adds `unverified:energy_inputs_missing`.
5. **Gem swaps:** a swap creates a new, uncorrupted gem, so it is compared against a corrupted original.
6. **base.py:** `evaluate_many` drops the engine's `pruned` list.
7. **mapping_v1:** the profile is untuned.
