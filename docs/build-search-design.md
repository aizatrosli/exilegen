# Constrained build search for PoE2 on pob-redux

**Date:** 2026-11-13
**Scope:** Design research only. Nothing in either repo was changed.

## TL;DR

- **Machine learning is not the core.** PoB is fast enough that simple algorithms win:
  - On a 4-core box, a full recalculation takes about 22 ms and a what-if node eval about 4 ms.
  - That works out to roughly 45 full evals or 250 what-if evals per core-second.
  - Published ML wins (surrogates, Bayesian optimisation) apply when you can afford only 10² to 10⁴ evaluations that each take minutes. Here we can afford 10⁵ to 10⁶ per hour.
- **Recommended design: two loops.**
  - **Outer loop:** a quality-diversity archive (MAP-Elites) over discrete "skeletons" (main skill, supports, ascendancy, keystones, weapon type), filtered by a constraint language.
  - **Inner loop:** a deterministic polisher.
    1. Score every passive node with PoB's node power.
    2. Solve a budgeted prize-collecting Steiner tree to get a connected allocation.
    3. Re-verify in PoB.
    4. Run local search: add, remove, swap and path moves.
    5. Run the existing `optimise_gear`.
- **"Multiple workers cooking"** is an asynchronous island model: one island per skeleton, a shared archive, and a global evaluation cache. Each worker is a long-lived PoB engine using about 0.7–1 GB of RAM.
- **Weird interactions** come from three sources:
  - a stat-interaction graph mined from the mod data;
  - diversity pressure from MAP-Elites;
  - outlier detection, which flags builds that score far above what a simple additive model predicts.
- **The biggest risk is false positives, not search quality.** PoB has gaps, and an optimiser will exploit them. In particular, PoE2 meta-gem Energy triggers are not modelled in the pinned PoB (0.23.1), and the config toggles are free buffs unless they are locked.

---

## 1. What the evaluator gives us (measured)

These measurements come from building pob-redux `pobctl` against the pinned PathOfBuilding-PoE2 commit (PoB 0.23.1, 3 patches applied). The test build was a level 90 Deadeye using Lightning Arrow, on a 4-core box.

| Operation | Time |
|---|---|
| Engine boot | 0.5–1.2 s |
| Full recalc (`refresh`) | **~22 ms** |
| `set_gem` / `set_config` with recalc | ~22 ms |
| `alloc_node` with recalc | ~75 ms |
| `load_build_xml` | ~150–250 ms (spikes up to 1 s from garbage collection) |
| What-if node eval (`calcFunc{addNodes}`) | **~4 ms per node** |
| What-if support gem eval | ~1.2 ms per support |
| `optimise_gear` | ~5 s |

The whole-tree power scan scaled almost linearly: 1 worker took 13.9 s, 2 took 7.7 s, and 4 took 4.4 s.

**Memory:** each engine uses about 0.7–1 GB of resident memory. A 64 GB machine therefore holds about 50 engines.

**Hooks that already exist:**
- `EnginePool` / `scatter` for parallel chunks.
- `score_nodes`, `score_gems` and `list_valid_supports` (which can sort by DPS).
- `tree_power_partition`, which gives reachable nodes with their distances and paths.
- `plan_points`, a greedy planner.
- `optimise_gear`, a greedy search by affix family scored with `repItem`.
- `set_config`, `checkpoint` / `rollback`.
- The `corpus` and `golden` subcommands.

**Gaps to close before searching:**
1. **No persistent server mode.** `pobctl call` reboots the engine on every call, which costs 0.5–1 s. The search workers should link `pob_engine::EnginePool` directly from Rust. The alternative is a long-lived stdin/stdout JSON loop.
2. **No generic what-if evaluation.** PoB's `calcFunc(override)` (add/remove nodes, replace an item, swap a gem) is only used inside the scorers. Adding one bridge method of about 30 lines of Lua, `whatif({addNodes, removeNodes, repItem, gem}) -> {selected stats}`, would give a 5–20× speed-up over mutate-then-recalc.
3. **No fixed output schema.** We need a stable stats schema; reuse the curated key list in `golden_lua` (`pobctl.rs:383`).
4. **No locked config profiles.** We need a canonical config per scenario, for example `mapping_v1` and `pinnacle_v1`.
5. **No legality validator.** We need one that checks gem compatibility, Spirit, attribute requirements, weapon types and the point budget, beyond what `sanity_check` covers.
6. **Undo is coarse.** It only exists as an XML reload. Cheap undo is needed for gem and item moves, or we keep using what-if evaluation for those.

---

## 2. Encoding a build as a search space

| Layer | Representation | Searched by |
|---|---|---|
| **Skeleton** | main skill, its supports (≤5), utility/trigger skills, ascendancy, keystones, weapon class | MAP-Elites / GA (outer) |
| **Passive tree** | connected node set rooted at the class start, budget N points, plus attribute choices | PCST solve + local search (inner) |
| **Gear** | per slot: base plus affix families (rares), or a unique ID | `optimise_gear` coordinate ascent; later, swapping uniques |
| **Jewels** | slot → jewel (uniques and timeless via existing tools) | greedy using the existing suggesters |
| **Config** | **locked profile, never searched** | — |

**Constraints are hard filters applied before evaluation, not penalties.** Each skill gets an activation class from PoB's `SkillType` flags:

- **`manual`:** a skill the player presses that has no trigger source.
- **`triggered`:** a skill with the `Triggered` or `InbuiltTrigger` flag, or one socketed in a `Meta`+`Triggers` gem.
- **`persistent`:** a skill with `Persistent` or `HasReservation`.
- **`proxy`:** minions, totems, traps and mines.

Support legality uses PoB's own `canGrantedEffectSupportActiveSkill`; do not re-derive it from tags. Note that `addSkillTypes` chains, where one support adds a type that unlocks another support, are a combo source.

Proposed constraint language:

```
constraint one_button:  count(skill where class==manual and dps_source) <= 1
                        count(skill where class==manual) <= 2        # allow a movement skill
constraint no_cast:     none(skill where has(Spell) and class==manual)   # strict: also ban triggered
floor:                  res.ele >= 75, res.chaos >= 0, max_hit.min >= 8000, cost_sustainable
profiles:               [mapping_v1, pinnacle_v1]   score = min over profiles
```

Each constraint should carry a `strict` or `lenient` flag, because "no cast" can mean either "no spells at all" or "spells allowed if triggered".

---

## 3. Search methods compared

| Method | Fit here | Verdict |
|---|---|---|
| Greedy marginal gain + local search (add / remove / point-neutral swap) | Proven by poe-optimizer and PoB's own Power Report. Myopic when a payoff needs several points first. | **Use** as the polisher |
| Beam / path moves (the whole path to each notable within depth D) | Fixes the multi-point myopia | **Use** |
| Linearise → budgeted PCST/MWCS solve → re-evaluate in PoB, repeated (like sequential LP) | Best fit for the tree. Prizes are the node-power deltas. Solvers: `pcst_fast` (fast approximation) or SCIP-Jack (exact). A graph of about 1.5k nodes is tiny for them. | **Use** as the tree seed. This is the main new idea. |
| GA with graph-repair crossover | Fine for the outer layer. PoESkillTree's GA works but scores additive stats only. | Optional |
| MAP-Elites (quality-diversity) | Keeps odd niches alive, which is exactly the goal of finding weird builds. Evidence comes from Hearthstone deck search (Fontaine 2019). | **Use** as the outer loop |
| MCTS | Poor for a 120-step tree. Plausible for choosing the skeleton. | Skip for now |
| CMA-ES | Only for continuous variables, and there are almost none | Skip |
| Bayesian optimisation | Designed for 10²–10³ expensive evaluations. Model overhead would exceed PoB's cost. | Skip |
| Surrogate ML (gradient boosting / GNN) | PoB's what-if evaluation already *is* a cheap exact surrogate, and a learned model must be retrained every patch | Only as an **anomaly detector** (section 4) |
| RL | Very expensive, with no evidence it works for this problem | Skip |

**When ML would start to earn its place:**
- **(a)** A prior learned from real builds, used to rank which skeletons to try first.
- **(b)** Pre-screening, if full multi-profile evaluation of a skeleton gets slow, for example once gear optimisation is included at about 5 s per build.

Both are experiments for phase 4, not foundations.

**Prior art to borrow from:**
- **PoB:** `CalcsTab:PowerBuilder` (node power) and `TradeQueryGenerator` (linear gear weights).
- **PoESkillTree:** Steiner GA.
- **alecrivet/poe-optimizer:** a headless PoB pool doing greedy add/remove/swap, at about 0.2 s per candidate.
- **PoE2-specific projects:** PoE2-Builder and pob-mcp. Both are immature, and pob-mcp is greedy add-only.
- **Raidbots Smart Sim:** a successive-halving "fidelity ladder".

None of these searches skeletons and tree together, or searches for diversity.

---

## 4. Discovering non-obvious interactions

1. **Stat-interaction graph.** Parse every mod, node and gem stat through PoB `ModParser`. Add an edge A→B for patterns like:
   - "X per Y" and "Y equal to % of Z";
   - "gain % of A as B" and conversion;
   - condition-granting stats;
   - resource loops such as recoup feeding a resource that scales damage.

   Then mine three things:
   - **cycles**, which can mean unbounded scaling;
   - **long chains that end in damage or trigger rate**;
   - **group multipliers**, such as "bonuses from equipped rings".

   Each motif seeds a skeleton. Past exploits fit these shapes: Archmage (mana per X), Volatility (an uncapped stack), and PoE1's 0-mana reservation.
2. **MAP-Elites behaviour axes.** Use damage-type mix, conversion chain length, activation class, main defence layer, and graph motif. A plain GA collapses onto the meta; the archive keeps strange niches until they mature.
3. **Outlier detection.** Fit a cheap additive model (gradient boosting on summed mods) to PoB DPS. Flag builds whose PoB score is more than 3σ above the prediction. Each flag is either a real non-linear combo or a PoB bug, and both need a human look.
4. **Patch-diff hypotheses.** Diff the patch notes and the RePoE/PoB data between versions. An LLM proposes hypotheses of the form "X now interacts with Y", and each one becomes a seed build that PoB scores. Never trust the LLM's numbers.

---

### 4b. Unique items

The pinned PoB data has about 465 uniques, in `Data/Uniques/*.lua`.

1. **Uniques as genes.** Each gear slot has a choice of "rare (affix families)" or one of the uniques that fit the slot. Every candidate is scored with what-if `repItem` (~4 ms each). Sweeping every unique for one slot costs well under a second, so try them all on every elite during polishing.
2. **Build-around uniques as skeleton seeds.** Some uniques change the rules: they grant a skill or trigger, convert damage, invert a stat, scale "per X", or add skill types. Tag these from their mod text and the stat graph (section 4.1). Each one starts its own skeleton, with the tree and gems re-solved around it, instead of being swapped into an existing build where it looks bad.
3. **Pairs and triples.** Find the edges in the stat graph between unique A's output stat and unique B's (or a keystone's or ascendancy node's) input stat. Only those pairs are evaluated jointly, which avoids an exhaustive N² search.
4. **Unique as a MAP-Elites axis.** Make "defining unique" a behaviour axis. A build that only works with one unique then keeps its own cell and is not crowded out by the meta.
5. **Guards.**
   - **Unsupported lines:** a unique line PoB marks unsupported is worth zero, so a unique that carries the build but has unsupported lines goes to manual review.
   - **Variants:** use only the `Current` variant.
   - **Price:** must come from poe.ninja. A unique with fewer than ~10 listings counts as unpriced, not free.

### 4c. Corpus-guided search (added after a follow-up research round)

**Inputs:** a few thousand real player builds (from pob-redux `corpus` or ladder exports), plus the game text: mods, gems, uniques, patch notes.

**Key point:** PoB evals are cheap, so the corpus is not for saving evaluations. It has two uses:
- **Where to look.** It gives the search a prior over sensible starting points.
- **What counts as novel.** "Rare in the corpus" is the signal for "new".

**Caution:** any model trained on player builds reproduces the meta by construction. Novelty has to come from explicit anti-popularity terms or from mechanics-level reasoning.

**Ranked for this project**

| # | Method | Role | Novelty | Effort |
|---|---|---|---|---|
| 1 | **Corpus- and archive-conditioned EDA / cross-entropy** (PBIL/UMDA-style node and support probabilities per skeleton, refit from elites) | Proposal distribution, and a prior on the PCST prizes | Tunable: up to high with the "anti-meta" emitter | 1–2 days |
| 2 | **Produces/consumes mechanics graph minus corpus lift** (built from PoB ModParser output) | Seeds plus a "graft chain" mutation | **High**, within what PoB models | ~1 week |
| 3 | **Co-occurrence lift mining (anti-meta gaps)** | Lists the pairs that share mechanics but are rare in the corpus | High | 1–2 days |
| 4 | **LLM skeleton proposer** (QDAIF/LMX-style, grounded in retrieved patch text, with ShinkaEvolve-style novelty rejection) | Occasional macro mutation, about 1 call per 10³–10⁴ evals | Medium–high; the only method that reads mod text PoB can't parse | ~$20–150 per campaign (estimate) |
| 5 | **Linkage-Tree GOMEA optimal mixing** (clusters learned from corpus + archive) | Smarter local search: cluster swaps from a donor elite, then PCST repair | Medium | ~1 week |
| 6 | **Complete-the-build recommender** (item-item collaborative filtering or a small masked model) | Repair operator that fills slots sensibly after an off-meta graft | Low (it learns the meta) | Days |
| 7 | Online surrogate / ranker (DSA-ME-style, retrained on archive data) | Pre-screens big neighbourhoods, only if PoB becomes the bottleneck | — | Medium |
| 8 | Inverse optimisation on corpus builds | Calibrates objective weights and floors, not used for search | — | Medium |
| — | Skip: πBO, GFlowNets (a plain GA beat them on the PMO benchmark), AlphaZero / neural combinatorial optimisation, LLM as a per-step mutator, OPRO | | | |

**How these plug into the existing design**
- **Prior-weighted PCST.** Each node's prize = λ·(PoB marginal gain) + μ·log P(node | skeleton). Run three emitters side by side, and let a bandit pick between them based on how many new or improved archive cells each produces:
  - μ>0: exploits the meta;
  - μ=0: neutral;
  - μ<0: the novelty engine.
- **New archive axis: distance from corpus.** This is the negative log-likelihood of a build under the corpus model. With it, novel builds earn their own cells instead of competing with meta builds on DPS alone.
- **Graft-chain mutation.** Take a typed chain from method 2, for example *granted skill → ailment state → unique that scales that ailment*. Force it into a skeleton, fill the remaining slots with the completion model (method 6), then polish.
- **Keep plain mutations in the mix.** DDE-Elites showed that learned operators only scale when combined with ordinary ones.

**Key sources:**
- EDA and GOMEA background: hBOA with priors (arXiv 1203.5443), the GOMEA tutorial (ir.cwi.nl/pub/28834).
- Learned operators and surrogates: DDE-Elites (arXiv 2003.04389), DSA-ME (arXiv 2112.03534), Genetic GFN (arXiv 2402.05961).
- LLM-driven search: FunSearch (Nature 2023), AlphaEvolve (arXiv 2506.13131), ShinkaEvolve (arXiv 2509.19349), QDAIF (arXiv 2310.13032), the "LLMs as black-box EA operators" study (arXiv 2501.15081), and the (1+1) baseline (arXiv 2407.10873).
- LLMs judging card synergy in Slay the Spire (arXiv 2508.19484).
- Recommenders: EDHREC lift, Dota item recommendation (arXiv 2201.08724).

## 5. Guarding against the optimiser cheating

- **Unmodelled mechanics.** In the pinned PoB, `CalcTriggers.lua` has no Energy model for PoE2 meta gems. Cast on Crit still follows the PoE1 rule, and some triggers fall back to "assume cast on every hit". Mark any build whose DPS comes from a triggered or proxy skill as **unverified** until we compute the trigger rate ourselves:

  `rate = energy events/s × energy per event / Σ(base cast time × 100)`

  This matters a lot for `one_button` searches.
- **Config abuse.** There are 1,027 config vars, many of them free conditions such as `conditionEnemyShocked`. Lock the profiles, derive conditions from the build itself (shock chance × uptime), and score with the minimum across profiles.
- **Unparsed mods.** Log any mod PoB cannot parse. A mod that carries the build but is unparsed should go to human review.
- **Sanity caps.** Apply these checks:
  - trigger rate ≤ server tick (~0.033 s);
  - costs sustainable (cost/s ≤ regen + leech);
  - bounded attack and cast speed;
  - reject DPS more than 10× the best known DPS for that skill unless a graph motif explains it.
- **Version check.** Re-score elites on each new PoB release, and flag any build whose score moves more than 30% when the data relevant to it did not change.
- **Final gate.** A human tests the build on a dummy before it is called "broken".

## 6. Scoring

- **Feasibility:** the floors in section 2 come first. A build that fails one is excluded, not penalised.
- **Objectives:**
  - boss DPS (minimum across profiles);
  - mapping DPS × AoE;
  - EHP (minimum max-hit across damage types);
  - cost tier.
- **How objectives combine:** use a Pareto front (NSGA-II) inside each archive cell. Do not use a weighted sum.
- **Cost:** use tier labels (≤10 / ≤100 / ≤1000 div) from poe.ninja **economy** endpoints. The poe.ninja builds API is internal and not for third-party use. Price rares by affix tier, and treat thinly listed uniques as having no known price.

## 7. Architecture

```
                 ┌──────────── exilegen (Rust or Python orchestrator) ─────────────┐
 patch notes ──► │ Data layer: RePoE / PoB data, stat graph, constraint compiler   │
 poe.ninja $ ──► │                                                                 │
                 │ Skeleton generator ──► Archive (MAP-Elites cells, Pareto/cell)  │
                 │   (motifs, mutations,        ▲            │                     │
                 │    LLM hypotheses)           │ elites     │ pull skeleton       │
                 │                              │            ▼                     │
                 │ Job queue (async, no generations) ──► Workers ×N                │
                 │                                       each = persistent PoB     │
                 │ Eval cache (canonical build hash)     engine + polisher:        │
                 │                                       PCST seed → local search  │
                 │ Verifier: profiles, sanity caps,      → gems → gear             │
                 │   trigger-rate model, outlier flag                              │
                 └─────────────────────────────────────────────────────────────────┘
                              ▲ new methods: whatif(), batch, legality, profiles
                        pob-redux (pob-engine crate, bridge.lua)
```

- **Islands are skeletons.** Workers pull the most promising, least-explored cells, polish them, and push the elites back. Elites migrate through the archive, so there is no synchronisation between workers.
- **Fidelity ladder:** what-if eval (~4 ms) → full recalc in one profile (~22 ms) → all profiles plus gear optimisation (~5 s) → human review.
- **Rough budget (estimate):** a 16-core box runs ~12 engines. One polished build (tree + gems + gear) takes ~10–20 s, so that is roughly 2–4k fully polished builds per hour, plus millions of what-if evaluations inside them.

## 8. Phased plan

| Phase | Deliverable | Rough size |
|---|---|---|
| **0. Evaluator API** (pob-redux branch) | Bridge `whatif` + `batch_whatif`; stable stats schema; locked config profiles; legality check including Spirit; a long-lived server mode for `pobctl` (JSON lines over stdio) | 1–2 weeks |
| **1. Single-skeleton polisher** | Given a fixed skill, supports, ascendancy and gear: PCST tree seed → local search. Benchmark against `plan_points` and real builds from 5–10 public PoB exports. | 1–2 weeks |
| **2. Constraints + gems + gear** | Constraint compiler (`one_button`, `no_cast`); support-gem search using `list_valid_supports`; `optimise_gear` in the loop; Pareto scoring | 2 weeks |
| **3. Workers + archive** | Async job queue, N engines, MAP-Elites archive, eval cache, results UI or report | 2 weeks |
| **4. Discovery** | Stat-interaction graph motifs; outlier flags; patch-diff LLM hypotheses; Energy trigger-rate model | ongoing |

## 9. What to try first (this week)

1. Add `whatif` to `bridge.lua` and a stdio server loop to `pobctl`. Confirm that per-candidate cost falls from ~22 ms to ~4 ms.
2. Take one known PoE2 build, strip its tree, and run **node power → `pcst_fast` → local search**. Compare its DPS and EHP with the original tree at the same point count.
   - If it matches or beats the original, the inner loop works.
   - If not, add path moves and iterate.
3. Hand-write `one_button` for that skill and list which supports or meta gems survive the filter. This shows quickly how often PoB's trigger modelling would mislead us.

## Sources

- **PoB:** `src/Classes/CalcsTab.lua` and `TradeQueryGenerator.lua` (PathOfBuilding-PoE2).
- **Optimizers and tools:**
  - PoESkillTree `SteinerSolver.cs` / `AdvancedSolver.cs`
  - github.com/alecrivet/poe-optimizer
  - github.com/leavitodeaver/PoE2-Builder
  - github.com/juddisjudd/pob-mcp
- **Papers:**
  - Fontaine et al., MAP-Elites for Hearthstone (arXiv 1904.10656)
  - DSA-ME (arXiv 2112.03534)
  - COMBO (arXiv 1902.00448)
  - Propulate (arXiv 2301.08713)
  - Churchill, StarCraft build-order search
- **Solvers:** pcst_fast (github.com/harrymvr/pcst_fast), SCIP-Jack (scipjack.zib.de).
- **Game data:** poe2db Cast on Shock / Cast on Critical (Energy rules); poe.ninja/docs/api.
- **Patch history:** poe-vault and mobalytics patch coverage (Volatility, Archmage, Husk of Dreams).
