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

## Data

The corpus is not committed. It contains other players' PoB exports from poe.ninja, whose builds API is
documented as internal and not for third-party use. Account and character names are stored only as
SHA-256 hashes.
