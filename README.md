# exilegen

Tools and research for an automated Path of Exile 2 build optimiser. The optimiser uses
[pob-redux](https://github.com/aizatrosli/pob-redux)'s headless Path of Building engine as its evaluator.

## Layout

| Path | What |
|---|---|
| `docs/build-search-design.md` | Search design: encoding, constraints (1-button, no-cast), search methods, ML vs simple algorithms, corpus-guided search, phased plan |
| `docs/corpus-findings.md` | Main findings from 1,488 poe.ninja ladder builds scored in PoB |
| `docs/analysis-meta.md`, `docs/analysis-synergy.md` | Detailed corpus analyses |
| `docs/poc-results.md` | Results of the support, tree and 1-button proof-of-concept optimisers |
| `docs/corpus-collection.md` | How the corpus was sampled: endpoints, strata, snapshot |
| `tools/ninja-collect/` | Polite (~1 req/s) poe.ninja build sampler. Decodes the protobuf search API and fetches PoB export codes |
| `tools/corpus-eval/` | Rust batch evaluator that links `pob_engine`. N worker engines; locked Pinnacle profile plus the exported config; tags each skill's activation class |
| `tools/poc/` | Proof-of-concept optimisers: `support-opt`, `tree-solver` (with the `pobrepl` engine REPL), `one-button` (with the Energy model) |
| `tools/analysis/meta/` | Meta, power vs popularity, constraint builds, defence floors, PoB gap report |
| `tools/analysis/synergy/` | Co-occurrence lift, ridge outliers, anti-meta gaps, unique and support what-ifs (`engine-exp/` is the PoB driver) |

## Setup

Clone `pob-redux` next to this repo (`../pob-redux`). The Rust tools depend on
`../pob-redux/crates/pob-engine` by path. Then sync PoB-PoE2 data with pob-redux's `pob-sync --dest <dir>` and set `POB_ROOT=<dir>`.

## Running

```bash
# 1. collect (edit VERSION/OVERVIEW in collect.py for the current league snapshot)
python3 tools/ninja-collect/collect.py          # writes data/corpus/poe2/{overview.json,builds.jsonl}
# 2. evaluate (resumable)
POB_ROOT=... WORKERS=4 tools/corpus-eval/run.sh  # writes evaluated.jsonl, errors.jsonl
# 3. analyse
python3 -I tools/analysis/meta/analyse.py data/corpus/poe2 out/meta
# synergy scripts expect cwd with data/ (corpus files) and out/
```

Each PoB engine needs about 1 GB of RAM. 4 engines evaluate about 50 builds/min.

## Optimiser

`exilegen/` is the optimiser package: `engine.py` (one PoB engine per process, see `engine/API.md`),
`scoring/` (honest DPS, floors, constraints, see `exilegen/scoring/README.md`) and `optimise/` (steps and
pipeline). Set `EXG_ENGINE_BIN` (the built `exg-engine`) and `EXG_POB_ROOT` (PoB-PoE2 data).

```bash
python3 -m exilegen optimise --code CODE --constraint one_button [--profile pinnacle_v1 --profile mapping_v1] \
        [--budget 6000] [--steps tree,supports,triggers] [--out result.json]
python3 -m exilegen score --file build.txt          # score only (honest DPS, EHP, max hit, flags)
python3 -m exilegen check --code CODE --constraint one_button --constraint no_cast:strict   # exit 1 if infeasible
```

`optimise` prints a before/after table per profile (honest and raw DPS, EHP, lowest max hit, movement
speed, Spirit), violations and `unverified` reasons, each step's gain, a readable diff (gems, groups,
passive notables) and the exported PoB code.

| Module | Role |
|---|---|
| `optimise/base.py` | Step contract: `Context` (evaluate / `evaluate_many` / commit / `try_commit` with snapshot rollback, budget in engine evaluations, multi-profile scoring with one profile switch per batch), `StepResult`, `patch_skills` (skills view for a what-if gem change, so constraints and the condition registry judge the candidate's gems) |
| `optimise/gems.py` | Gem catalogue from the PoB data: triggerable spells, meta gems, energy supports, id/name lookup |
| `optimise/tree.py`, `optimise/supports.py` | `TreeStep(mode="warm"\|"scratch")`, `SupportStep` |
| `optimise/triggers.py` | `TriggerStep`: trigger spell, meta gem (Cast on Critical / Cast on Elemental Ailment), meta-group supports (incl. energy supports), feeder skill and feeder supports; Spirit-aware (a candidate failing only the Spirit floor is retried with the cheapest persistent group disabled). `ConvertStep`: manual caster to 1-button (meta gem + feeder group, other manual damaging groups disabled, Spirit freed, feeder supports filled), kept only if constraints and floors pass |
| `optimise/pipeline.py` | `optimise(code, constraints, profiles, steps="auto", budget, seed, engine)` and `optimise_many(codes, workers=N)` (EnginePool, independent jobs) |

Pipeline `auto`: convert (only when a `one_button` constraint is violated), then cycles of tree (warm) →
supports → triggers (if the build has Meta+Triggers groups) until a cycle gains < 1%. The budget is
shared: each step gets an even share of what is left for the planned steps.

Optimiser-side scoring choices (the Scorer itself is unchanged):

- **Mana sustain floor in `either` mode** (absolute ≥ 1 *or* no worse than the start), instead of purely relative.
- **Energy-aware cost** (`triggers.energy_cost_filter`): a main skill triggered by a modelled meta gem has
  its cost per second scaled by its Energy factor; PoB prices it at its own, energy-unaware trigger rate.
  Without it every faster-casting trigger spell fails the mana floor.
- **Replaced gems are new gems:** a corrupted or over-quality gem that the search replaces loses its corruption,
  so re-socketing the same spell can score lower than the original.

`scripts/run_pipeline_v0.py` runs the pipeline on the POC 1-button builds and a mixed corpus sample.

## Data

The corpus is not committed. It contains other players' PoB exports from poe.ninja, whose builds API is
documented as internal and not for third-party use. Account and character names are stored only as
SHA-256 hashes.
