# exg-engine API

The engine service runs one Path of Building (PoE2, pob-redux `pob-engine`) per process and serves JSON lines over stdin/stdout.

- **Binary:** `engine/`, built as `exg-engine`.
- **Ops:** `exilegen/lua/exg.lua`, loaded once at start.
- **Python client:** `exilegen/engine.py` (`Engine`, `EnginePool`).
- **Profiles:** `exilegen/data/profiles.json`.

This directory holds documentation only. The Python module is `exilegen/engine.py`; a directory without `__init__.py` does not shadow it.

## Running

```
cd engine && CARGO_TARGET_DIR=... cargo build --release
exg-engine --pob-root <PoB-PoE2 src dir> [--state DIR] [--lua exilegen/lua/exg.lua] [--profiles exilegen/data/profiles.json] [--timeout 120]
```

- **Environment fallbacks:**
  - `EXG_POB_ROOT` or `POB_ROOT`
  - `EXG_STATE` (default `<tmp>/exg-engine`, which holds the PoB user dir and the compiled Lua cache)
- **Embedded copies:** without `--lua` or `--profiles`, the binary uses the copies of `exg.lua` and `profiles.json` embedded at build time. The Python client always passes the repo files, so Lua edits need no rebuild.
- **Ready line:** the first stdout line is `{"ok":true,"r":{"ready":true,"boot_ms":..,"exg":{"version":"1","profiles":[..]},"pob":{..version..},"pid":..}}`.

### Wire format

- **Requests (one JSON object per line):**
  - `{"op": "<name>", ...params, "id"?: any, "timeout"?: seconds}`
  - `{"lua": "<chunk>"}` runs a raw chunk. A string result that holds JSON is decoded.
  - `{"m": "<bridge method>", "p": {...}}` makes a raw pob-redux `bridge.lua` call, for debugging.
  - `{"op": "quit"}`
- **Replies:**
  - `{"ok": true, "r": <result>, "id": .., "ms": <wall ms>}`
  - `{"ok": false, "err": "<message + Lua traceback>", "id": .., "ms": ..}`: the op failed and the engine is still usable.
  - `{"ok": false, "err": "timeout after Ns in <op>", "fatal": true, "restart": true}`: the engine then **exits with status 3**. A Lua call cannot be interrupted, so the process is the unit of restart. A dead engine thread exits with status 4.
- **Python client recovery:** on a fatal reply or EOF, the client restarts the engine and reloads the last build with the same profile.
  - It retries read-only ops once if nothing had been mutated.
  - Otherwise it raises `EngineCrash`, because applied changes are lost.

## Ops

All node ids are PoB passive ids (ints). All group and slot indices are 1-based, as in PoB.

- **Group index:** the position in `skillsTab.socketGroupList`. It is stable across calls. Groups are only added by a gem change to `#groups+1` and removed by `restore`.
- **Slot:** the gem's position in `group.gemList`.

| op | params | result |
|---|---|---|
| `load` | `code` \| `xml`, `profile?` (default `as_exported`), `main?` (`"auto"` default \| `"pob"` \| group index \| `{group, skill}`), `gc?` (`auto` \| `full` \| `none`) | summary (below) |
| `summary` | – | summary |
| `set_profile` | `name`, `main?` | `{profile, scoring, main}` |
| `profiles` | – | `{profiles: <profiles.json profiles>, current}` |
| `select_main` | `main` (as in `load`) | `{main: {group, skill}, selection}` |
| `stats` | `fields?` (default all), `skip_ehp?`, `marginals?` | stats record plus `scoring` (the profile's scoring block) |
| `skills` | – | `{groups: [...], main, characterLevel}` |
| `valid_supports` | `group`, `skill?`, `include_used?` | `{group, skill, activeSkill, characterLevel, slots, supports: [...]}` |
| `tree` | `frontier?` (default true), `max_dist?`, `graph?` (default false), `ascendancy?` (default true) | tree view |
| `whatif` | change (below), `fields?` (default `core`), `skip_ehp?`, `marginals?` | stats record (+ `pruned`). Nothing is kept. |
| `whatif_many` | `variants: [change...]`, `fields?`, `skip_ehp?` | list of stats records; a failed variant gives `{err}` |
| `apply` | change, `fields?` (default `core`; `false` returns `{applied, pruned}`) | stats record (+ `pruned`). The change is kept. |
| `snapshot` | – | `{token}` |
| `restore` | `token` | `{restored: true}` |
| `drop_snapshot` | `token` | – |
| `export` / `export_xml` | – | `{code}` / `{xml}`: a PoB share code of the current state |
| `lua` | `chunk` | the chunk's return value (debugging) |
| `gc`, `ping` | – | heap MB / liveness |

### Load summary

- **Fields:** `className`, `ascendancy`, `level`, `treeVersion`, `targetVersion`, `profile`, `main {group, skill}`, `mainSelection`, `pobMainGroup` (the group the export marked as main), `groups` (count), `mainLabel`, `points`.
- **`points`:**
  - `used`, `normalUsed` (used minus min(ws1, ws2)), `available` ((level−1) + quest points + `extraPoints`)
  - `ascUsed`, `ascMax`, `secondaryAscUsed`, `sockets`
  - `weaponSet1`, `weaponSet2`, `weaponSetMax`
  - `levelPoints`, `questPoints`, `extraPoints`

### Main skill selection

- **`"auto"` (default):** the main skill is the enabled skill with the highest DPS (player `CombinedDPS` or minion `CombinedDPS`), over every damaging or proxy (minion/totem/trap/mine) skill except meta gems. It excludes:
  - PoB's synthetic mechanic groups (Explode, Thorns);
  - a weapon's default attack with no supports, unless it is the exported main.
- **`"pob"`:** the main skill is the exported marker.
- **When it is chosen:** on `load`, on `set_profile` while the selection is `auto`, and on `select_main`. It does **not** change on `apply`.
- **PoB marker:** PoB's own marker is moved to the selection, so exports open on the scored skill.
- **Corpus comparison:** use `main: "pob"` to compare with `evaluated.jsonl`, where the marked main skill is scored.

### Change (whatif / whatif_many / apply)

```
{ "add_nodes":    [id | {"id": id, "ws": 1|2}],   // nodes to allocate; a path is a list of ids
  "remove_nodes": [id],
  "attr_choices": {"<id>": 1|2|3},                // attribute node option (Str/Dex/Int order of tree.options)
  "gem_changes":  [{"group": g, "slot": s, "gem": <gem id|skill id|name>|null, "level"?, "quality"?, "enabled"?, "label"?}],
  "group_enabled": {"<g>": true|false} }
```

**Tree rules** (identical in `whatif` and `apply`):

- **Pruning:** PoB prunes allocated nodes that lose their path to the class start. `whatif` resolves connectivity the same way, and both report the dropped ids as `pruned`.
- **Weapon-set mode of added nodes:** an added node without `ws` gets the first mode that connects it (0, then weapon set 1, then 2). Frontier entries carry the `ws` they need.
- **Not modelled in whatif:** Intuitive-Leap-like jewel radii and unlock constraints. Ascendancy nodes are added or removed without checks.
- **Attribute choices:** `attr_choices` applies to added nodes and to already-allocated attribute nodes.

**Gem rules:**

- **`gem`:** replaces the slot. A new gem object is created; gems are never mutated in place.
- **`gem: null`:** removes the slot. Removals run after all replacements, highest slot first, so the slot numbers in one request refer to the state before the request.
- **`level` / `quality` / `enabled` without `gem`:** edits the gem.
- **`slot` > #gems:** appends a gem.
- **`group` = #groups+1:** creates a new group.
- **Default level:** an active gem replaced by an active gem keeps its level. Otherwise the gem gets PoB's default (`ProcessGemLevel`).
- **Item- or node-granted groups:** cannot be changed.

**Method:**

- **Node changes:** evaluated through a calc override (`addNodes` / `removeNodes`).
- **Gem changes:** applied to the live skills tab, calculated, and then restored.
- **`apply`:** commits through PoB's own `PassiveSpec:RestoreUndoState` (`ImportFromNodeList`) and `ProcessSocketGroup`.

### Snapshot / restore

- **What a snapshot holds:**
  - the tree undo state (nodes, weapon sets, attribute overrides);
  - the socket group list and every group's gem list, `enabled` and `mainActiveSkill`;
  - the main selection and the active config set.
- **Cost:** restore takes about 50–120 ms and needs no XML reload.
- **Scope:** tokens live until the next `load`.

## Stats record

**Calculation:**

- Every stats-producing op calculates through one path: PoB `calcs.initEnv(build, "CALCULATOR", override)` followed by `calcs.perform`. This is the pipeline behind PoB's `calcFunc` and node power, with `mainSocketGroup` set to the selected main skill.
- `stats`, `whatif` and `apply` therefore agree exactly. On the test corpus, `stats` matches corpus-eval (MAIN mode) `CombinedDPS` and `TotalEHP` exactly.
- PoB's trigger `GlobalCache` is wiped before every calc, so variants never share stale trigger rates. This roughly doubles the cost for builds that use triggers.

**Field groups:** `fields` takes top-level keys.

- **`"core"`:** `mainSkill stats minionStats main dps offence sustain movementSpeed ailmentThreshold spirit maxHit requirements keystones uniques`. One calc.
- **`"all"`:** core plus `pobMain unsupported skills energy`. `skills` and `energy` add one calc per damaging or proxy skill.
- **`marginals`:** requested with `fields` or `marginals: true`. Never part of `all`.

**Always present:** `profile`.

| key | content |
|---|---|
| `mainSkill` | `{name, id, types[], flags[], minion}`, as corpus-eval `extract.lua` |
| `stats` | PoB output, **corpus-eval `STAT_KEYS` with the same names**, plus `HitDamageByType{Physical..}`, `NonHitDPS`. Extensions: `FireResistMax ColdResistMax LightningResistMax ChaosResistMax ReqStr ReqDex ReqInt LifeCost LifePerSecondCost ESCost ESPerSecondCost LifeLeechRate ManaLeechRate LifeLeechGainRate ManaLeechGainRate NetLifeRegen NetManaRegen AilmentThreshold StunThreshold ShockChance IgniteChance FreezeChance ChillChance ElectrocuteChance BleedChance PoisonChance EnemyShockChance FreezeBuildup FreezeDurationMod SkillTriggerRate EffectiveSourceRate TriggerRateCap ManaRegen LifeRegen ExtraPoints` (present when PoB sets them). `FullDPS` is not computed (CALCULATOR mode); `CharmLimit` is filled from the mod DB. |
| `minionStats` | the same picker on `output.Minion`, or null |
| `main` | `{group, skill, selection, name}` |
| `pobMain` | `{group, skill}` of the exported marker |
| `dps` | `{best: max(player, minion), player: CombinedDPS, minion: Minion.CombinedDPS, full}` |
| `offence` | main skill: `speed hitSpeed hitChance hitsPerSec averageHit critChance critMultiplier shockChance igniteChance freezeChance chillChance electrocuteChance bleedChance poisonChance skillTriggerRate effectiveSourceRate triggerRateCap` (chances are 0–100, capped), plus `minion{...}`. `hitsPerSec` = TotalDPS/AverageHit, else Speed×HitChance. |
| `sustain` | `manaCost lifeCost esCost manaPerSecondCost lifePerSecondCost manaUsePerSec lifeUsePerSec manaRegen lifeRegen manaLeech lifeLeech manaNet lifeNet sustainable`. Use per second is PerSecondCost if set, else cost × Speed. Net = regen + leech − use. |
| `movementSpeed` | `MovementSpeedMod` (1.0 = base) |
| `ailmentThreshold` | `{ailment, stun}` |
| `spirit` | `{total, reserved, unreserved}` |
| `maxHit` | `{Physical, Fire, Cold, Lightning, Chaos, min, minType}` |
| `requirements` | `{Str|Dex|Int: {need, have}, deficit}` |
| `keystones` | `[{id, name, granted?}]` allocated or granted keystones |
| `uniques` | `[{name, slot, base}]` equipped unique and relic items, including jewels |
| `unsupported` | `{count, sample[≤40]}`: item and passive mod lines PoB does not parse (as `extract.lua`) |
| `skills` | per enabled damaging or proxy skill: the `skills`-op facts plus `dps playerDps minionDps offence{..} minionOffence manaUsePerSec lifeUsePerSec types` (types after supports), each as if it were the main skill |
| `energy` | per enabled Meta+Triggers group (below) |
| `marginals` | `[{group, slot, gem_id (skill id), gemId, name, dps_without, dps_base, group_dps_without?, group_dps_base?}]`: each enabled support of the main group and of every enabled meta group, disabled one at a time. `dps_*` is the main skill's DPS; `group_dps_*` (meta groups only) sums the group's triggered skills. |
| `pruned` | (whatif/apply) node ids dropped by connectivity |
| `scoring` | (`stats` op only) the active profile's `scoring` block |

### Energy facts

The engine reports raw facts only and applies no correction. Each entry of `energy`:

- **Group and meta gem:** `group`, `metaGemId`, `metaSkillId`, `metaName`, `metaLevel`, `metaQuality`.
- **`spells`:** `[{slot, gemId, id, name, castTime, spell, level}]`, the socketed non-meta actives with their base cast time.
- **Energy totals:**
  - `maxEnergy` = Σ castTime×100 over the socketed spells (10 energy per 0.1 s);
  - `genBonus`: `energy_generated_+%` from the gem level, quality and supports;
  - `refund`: Σ `trigger_skills_refund_half_energy_spent_chance_%`;
  - `energySupports[]`: the supports that contribute to `genBonus` or `refund`.
- **`perEvent`:** `{crit|shock|ignite|freeze|...: energy per monster power}`, parsed from `*_centienergy_per_monster_power_on_<event>` divided by 100. `energyStats[]` lists every raw stat that contains "energy".
- **`feeder`:** the main skill if it is a manual skill outside meta groups, else the highest-DPS manual damaging skill outside meta groups.
  - Fields: `{group, index, name, id, dps, hitsPerSec, speed, critChance, shockChance, igniteChance, freezeChance, chillChance, electrocuteChance}`.
  - Event rates: `critsPerSec`, `shocksPerSec`, `ignitesPerSec`, `freezesPerSec`. Each is hits/s × PoB's per-hit chance; this is not a freeze-buildup model.
- **`triggered`:** `[{index, name, id, dps, pobRate (Speed), skillTriggerRate, effectiveSourceRate, triggerRateCap}]`, where `pobRate` is PoB's assumed trigger rate.

## skills op

`groups[]`, one entry per socket group:

- **Group facts:** `index`, `label`, `slot`, `enabled`, `main`, `pobMain`, `mainActiveSkill`, `includeInFullDPS`, `grantedBy` (item/node name or mechanic), `metaGroup`.
- **`gems[]`:** `{slot, id (gem id), skillId, name, level, quality, enabled, support, known, tags[], gemTypes[], attr, tier, reqLevel, family[], meta}`.
- **`skills[]`:** the calculated active skills.
  - Identity: `group`, `index` (PoB `mainActiveSkill` index), `name`, `id`, `gemSlot`.
  - `types[]`: SkillTypes after supports.
  - `activation`: `manual` | `triggered` | `persistent` | `proxy`, with the same rules as corpus-eval and the one-button POC.
  - Flags: `proxyKind[]`, `damaging`, `spell`, `attack`, `movement` (Travel/Blink), `utility` (not damaging and not movement), `meta`, `itemGranted`, `grantKind` (`item` \| `node` \| `mechanic`), `groupSupports`, `weaponDefault`, `enabled`, `triggeredBy`.

## valid_supports op

The op returns legal supports for the group's active skill. For a meta group, that is the first socketed non-meta skill.

- **Legality checks:**
  - PoB `calcLib.canGrantedEffectSupportActiveSkill` (the skill's types already include those added by current supports);
  - gem tier level ≤ character level;
  - not socketed in another enabled group, unless `include_used`;
  - not hidden.
- **`supports[]` fields:** `{id, skillId, name, tier, reqLevel, attr (Str/Dex/Int; each socketed support adds 5 to that colour's build-wide requirement source), attrCost: 5, socketed (already in this group), usedInGroup, conflicts: [{slot, why: same|family|lineage}], family[], lineage (name has an apostrophe), tags[]}`.
- **Replacement test:** a candidate is legal as a replacement for slot k when every conflict slot equals k.
- **`slots`:** the current enabled support slots.

## tree op

- **Class facts:** `classId`, `className`, `ascendClassId`, `ascendancy`, `startNode`, `treeVersion`.
- **Allocation:**
  - `allocated[]`: main-tree nodes;
  - `ascendancyNodes[]`;
  - `weaponSetNodes{id: 1|2}`;
  - `attributeChoices{id: option index}`;
  - `grantedNodes[]`: path-free nodes granted by jewels or anoints (`env.grantedPassives`);
  - `jewelSockets[{id, item}]`.
- **`frontier[]`:** `{id, dist (points to allocate, PoB pathDist), path [ids to allocate, target first], ws?}` for unallocated nodes with dist ≤ `max_dist`, sorted by dist.
- **`nodes{id: facts}`:** `{id, name, kind (keystone|notable|small|attribute|jewel|mastery|ascendancy|ascendancy_notable|start|ascendancy_start), type, sd[] (stat lines), asc, attribute, options[] (attribute nodes), linked[] (with graph)}` for allocated, granted and frontier nodes. With `graph: true` it covers the whole tree, with adjacency.
- **`points`:** as in the load summary, with `extraPoints` taken from the mod DB.

## Profiles (`exilegen/data/profiles.json`)

```json
{ "format": 1,
  "profiles": {
    "<name>": {
      "description": "...",
      "base": "player" | "fresh",        // the build's own active config set, or a new PoB-default set
      "carry_prefixes": ["quest"],        // fresh only: copy player inputs whose names start with these
      "config": {"<PoB config var>": value},   // ConfigOptions.lua var names, applied on top
      "scoring": { ... plain data for the scorer; the engine never applies it ... } } } }
```

| profile | config |
|---|---|
| `as_exported` | the player's own config |
| `pinnacle_v1` | fresh defaults, quest inputs carried over, `enemyIsBoss = "Pinnacle"`. Same as corpus-eval `pinnacle_locked`. |
| `mapping_v1` | fresh defaults, quest inputs carried over, `enemyIsBoss = "None"`, rare target, 8 nearby enemies, 1 rare |

- **Scoring keys:**
  - `scenario`, `enemy_power` (20 for pinnacle; the mapping pack value is a tunable estimate), `enemy_count`, `enemy_rarity`, `corpses_available`, `trusted`;
  - mapping only: `enemy_power_pack`.
- **Config sets:** a fresh profile adds a config set titled `exg_<name>` to the build once and reuses it on later switches. An export carries these sets, with the active one selected.

## Python client (`exilegen/engine.py`)

- **`Engine`:**
  - Ops: `load`, `ensure_loaded`, `set_profile`, `select_main`, `summary`, `profiles`, `stats`, `skills`, `valid_supports`, `tree`, `whatif`, `whatif_many`, `apply`, `snapshot`, `restore`, `export`, `export_xml`, `lua`, `raw`, `gc`, `close`.
  - Generic calls: `op(name, **params)` and `request(dict)`.
- **`ensure_loaded(code, profile, main)`:**
  - keeps the build cached by code hash, profile and main;
  - after mutations, restores a base snapshot instead of reloading.
- **`EnginePool(n, **engine_kwargs).map(fn, jobs)`:** runs `fn(engine, job)` on n persistent engines, one thread each, and returns results in job order. Pass `return_exceptions=True` to collect errors instead of raising.
- **Hash:** `code_hash(code)` is the corpus-eval hash: sha256 of the trimmed code, first 16 bytes as hex.

## Measured cost

These are medians from `scripts/bench_engine.py` on a 4-core box with 3 corpus builds.

| op | cost |
|---|---|
| `load` | 0.9–2.0 s (PoB `Build:Init` plus a full UI frame; auto main adds 0.1–0.2 s) |
| `stats core` | 20–33 ms |
| `stats all` | 40–220 ms |
| `whatif` node or gem (`fields: ["dps"]`) | 7–19 ms |
| `whatif_many` | about the same per variant |
| `apply` | about 80 ms |
| `restore` | 50–120 ms |
| `tree` (full frontier, about 4k nodes) | about 0.5 s |
| `export` | 0.1–0.5 s (one PoB refresh when dirty) |

## Deviations from the original request

- **Load:** `load` runs pob-redux `load_build_code` / `load_build_xml` from the Rust host, then `exg.lua`, because exg.lua cannot reach the bridge table.
- **Garbage collection between loads:** a full GC costs 0.3–0.5 s, so it is incremental by default. A full collection runs when the heap exceeds 600 MB or when `gc: "full"` is passed.
- **What-if method:** what-ifs do not call PoB's cached `calcFunc` closure. They call its non-accelerated path directly (`initEnv` + `perform`, CALCULATOR mode) with a fresh trigger cache, so variants are independent and `apply` gives identical numbers.
- **Gem what-ifs:** gem what-ifs mutate the skills tab and then restore it.
- **Main skill marker:** the main-skill selection also moves PoB's own main-skill marker; use `main: "pob"` to keep the exported one.
- **Timeouts:** a timeout ends the process, with a reply that carries `fatal` and `restart`. The client restarts the engine.
