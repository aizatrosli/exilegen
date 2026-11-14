# Building new meta: what worked, and how to put it in pob-redux

Written 2026-11-14, after the corpus mining, three proofs of concept, the v0 optimiser and the first from-scratch builder runs.

## 1. The method that works

### Scoring honesty matters more than the search algorithm

Every early "big win" was the optimiser exploiting a gap in PoB. Each lesson below is now part of the scoring layer:

| Gap | Example | Fix |
|---|---|---|
| Gems whose value assumes a state | Hexblast with no curse ×3.5; Detonate Dead on a boss ×7; Overextend daze; Minion Pact without a minion | **Condition registry** (505 entries): block, discount or flag per profile |
| Meta-gem Energy not modelled | Arc triggers ran at 0.11–0.48× of PoB's assumed rate | **Energy model**: enemy Power is a profile parameter, with a half-energy sensitivity check |
| Free config toggles | "enemy shocked" etc. | **Locked profiles** (`pinnacle_v1`, `mapping_v1`); score = minimum over profiles |
| Objective too narrow | The tree solver cut movement, mana and avoidance | Objective includes honest DPS, EHP, max hit, movement and sustain. **Floors come from the ladder**: ele res 75, EHP ≥16k, lowest max hit ≥4.4k |
| Mods PoB can't parse | Median 14 lines per build (Archon, Bonded, Energy) | Counted and flagged as "unverified", ranked below verified builds |

### Two loops

```
OUTER (what to build)   MAP-Elites archive over build "styles"
                        cells = damage type × activation × defence × defining unique × ascendancy × ladder rarity
                        a bandit picks the idea source by how often it yields a new or better cell
   ↓ skeleton (class, ascendancy, main skill / trigger setup, 0–3 uniques, keystones)
SYNTH (make it real)    new build → uniques → skill groups → template rares + gear optimiser → ascendancy → tree from scratch
   ↓
INNER (polish)          tree (warm) → supports → triggers → tree → supports, repeated until a cycle gains <1%
                        every candidate is scored by the honest Scorer; a step that ends worse is rolled back
```

### Inner-loop algorithms, measured

| Part | Algorithm | Result |
|---|---|---|
| Supports | Greedy replace-weakest (ranked by drop-one marginals), then pairwise swaps over the top-k; candidates batched in one engine call | +96.5% median honest DPS on ladder builds |
| Passive tree | Budgeted prize-collecting Steiner growth (path gain per point, 12-point rounds), then local search: path moves, branch swaps, keystone + cluster moves, restarts | Warm start ×1.41 honest DPS on 9 of 10 builds; from scratch ×1.8–2.0 on its two biggest wins |
| Triggers / 1-button | Search the spell, meta gem, feeder and feeder supports, judged on energy-corrected DPS; Spirit-aware (disables the cheapest persistent skill to make room) | 8 of 8 builds improved honestly; 2 of 2 conversions to 1-button worked |
| Gear | PoB's per-slot affix optimiser, with weights taken from the objective | Works, but sets a low ceiling (see gaps) |

### Outer loop: which idea sources paid off (1-button run, 121 ideas)

| Source | Valid build rate | Note |
|---|---|---|
| Mutation of the best builds | 77% | The best producer of improvements |
| Mechanics graph ("X produces a state, Y scales off it") | 83% | Found the top 1-button build: Liminal Coil + His Foul Emergence Lich, played by 0.03% of the ladder |
| Build-around uniques | 71% | Found Sacred Flame Ember Fusillade |
| Corpus prior | 76% | Lowest share of verified builds; mostly re-finds the meta |

**Verdict on ML.** No machine learning is needed in the core. Evaluations are cheap (5–25 ms), deterministic and exact, so simple local search plus quality-diversity search wins. ML is worth adding later in only two places, both optional:

- a ranker that pre-screens large candidate sets;
- an LLM that proposes ideas from patch notes and from mod text PoB can't parse.

### Gaps that still matter most

1. **From-scratch gear has a low ceiling.** Builds use template rares and no jewels or runes, so some archetypes reach only 2–5% of ladder DPS while others reach about 1×. Next: jewels and runes, budget tiers priced from poe.ninja, and gear targets for EHP and max hit.
2. **Item-granted triggers have no model.** Choir of the Storm, Cast on Block/Dodge and Cast on Minion Death need trigger-rate models.
3. **Throughput.** About 80 builds per hour on 3 engines. Builds load in 1–2 s, and items have no snapshot/undo. Fix by caching loaded skeleton states and adding item snapshots.
4. **Unparsed PoE2 mechanics** (Archon, Bonded, Energy) need Lua patches.
5. **The final gate is still human.** Play-test the top idea of each cell; PoB is not the game.

## 2. Putting it into pob-redux

The work splits cleanly into three layers. Each one moves into pob-redux at a different time.

| Layer | Today (exilegen) | In pob-redux |
|---|---|---|
| **Engine ops**: whatif/whatif_many, locked profiles, stats with energy facts, marginals, skills with activation classes, tree frontier, snapshot/restore | `exilegen/lua/exg.lua` + `exg-engine` (Rust stdio server) | **Upstream into `crates/pob-engine/lua/bridge.lua`** as bridge methods, and add a long-running `pobctl serve` (JSON lines). This also fixes the jewel-node pruning in the bridge's tree code. Low risk, and the app's existing tools gain from it |
| **Scoring data**: condition registry, profiles, floors, constraint DSL | YAML/JSON + Python | Ship the **data files as-is** (language-neutral). Port the scorer to Rust in a new crate `crates/pob-search` (~1.5k lines of Python today) |
| **Search**: optimiser steps, skeleton generator, synth, archive, runner | Python | Two options below |

**Option A: sidecar (fast, ~1 week).**
- pob-redux bundles exilegen as an optional sidecar and talks to it over JSON lines.
- The app's existing MCP server (`src-tauri/src/tools.rs`) gets a few tools that drive it: `optimise_build`, `build_from_idea`, `meta_search_start/status/results/stop`.
- The cost is a Python runtime in the installer.

**Option B: native Rust crate `pob-search` (recommended, ~3–5 weeks).**
- Port the steps and the archive onto the existing `pool.rs` `EnginePool`/`scatter`. Workers stay in-process with no Python dependency.
- Keep exilegen as the **research lab**: try new algorithms in Python, and port the ones that work.

**What the user sees, in both options:**
- A **"Meta Lab" view** in the Svelte app (`src/lib/views`). Pick constraints (1-button, no-cast, budget tier, profile), start or stop workers, and watch the archive grid fill.
- Clicking a cell opens the elite build in the normal editor.
- **"Optimise this build"** button on any loaded build, running tree → supports → triggers with a diff preview, applied as one undoable change.
- **"Why"** panel showing honest vs raw DPS, condition discounts, energy factor and unverified flags.
- MCP tools (the app already has about 75), so Claude or other agents can run searches.
- `pobctl optimise` and `pobctl meta-search` for headless and overnight runs. That also removes the 2-hour cloud limit, because runs happen on your PC.

### Suggested order

1. Upstream the engine ops into bridge.lua, plus `pobctl serve`; exilegen switches to calling pob-redux directly.
2. Add the MCP tools and `pobctl optimise`, backed by the exilegen sidecar, so it is usable right away.
3. Build the Meta Lab UI against those tools.
4. Port the hot paths to `pob-search` (Rust): supports, tree, then the archive and runner. Drop the sidecar once at parity.
5. Ongoing in exilegen: jewel/rune gear synthesis, trigger models for item-granted skills, PoE2 Lua patches, LLM idea proposals from patch notes.
