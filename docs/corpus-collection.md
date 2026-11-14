# PoE2 ladder build corpus (poe.ninja)

- **League:** Forbidden Rites (softcore trade, the current league), experience ladder
- **Snapshot version:** `1430-20261113-58818`, overview `forbidden-rites`, passive tree `PassiveTree-0.5`
- **Collected:** 2026-11-13, 15:58 to 16:33 UTC
- **Characters in snapshot:** 124,124. **Builds sampled:** 1,488 (1,473 distinct accounts), 0 fetch failures, no 429/5xx/blocks.
- **Client:** sequential, at most about 1 request/s, User-Agent `PoB Redux research client`. Backs off on 403/429/5xx.

poe.ninja says its builds API is internal and undocumented. It may change without notice.

## Files
- `overview.json`: the aggregate poe.ninja computes for the whole snapshot:
  - `dimensions`: popularity counts, sorted descending, for
    - `class` (ascendancy or base class)
    - `skills` ("Main Skills"; one character can count toward several)
    - `allskills` (includes supports)
    - `spiritgems`
    - `items` (uniques plus "Rare X" buckets)
    - `keypassives` (keystones)
    - `anointed`
    - `traits` (main-skill traits)
    - `weaponmode`
  - `integerRanges`: min/max of the stat filters.
  - `skillByAscendancy`: per-ascendancy counts for the top 75 main skills.
- `builds.jsonl`: one build per line. Fields:
  - `account_sha256` (sha256 of the poe.ninja account string `name-1234`; no raw names are stored)
  - `character_sha256` (sha256 of the character name)
  - `class` (ascendancy), `baseClass` (poe.ninja int), `level`
  - `main_skill`: the stratum skill, or poe.ninja's DPS skill for the top-of-ladder strata
  - `main_skills`: poe.ninja's main-skill list
  - `keystones`, `stratum`, `rank_in_query`, `updatedUtc`
  - `pob`: className, ascendClassName and level, parsed from the code
  - `code`: the Path of Building share code
- `progress.json`: `{fetched, failed, target, state}`.

## Endpoints (all on https://poe.ninja)
- `GET /poe2/api/data/index-state`: JSON list of leagues and snapshot versions.
- `GET /poe2/api/builds/{version}/search?overview={snapshotName}&type=exp[&class=X][&skills=Y]`
  - Returns `application/x-protobuf`.
  - Message field 1 holds:
    - 1 = total
    - 2 = dimensions (name, dictionary kind, then repeated {index, count})
    - 3 = integer ranges
    - 6 = dictionary hashes per kind
    - 12 = columns for the top 100 matching characters. Strings are in field 7. Dictionary indexes are packed varints in field 6. Lists are in field 9.
  - Filters match the web URL query parameters. Dimension counts reflect the filters.
- `GET /poe2/api/builds/dictionary/{hash}`: binary "NDIC" v2 string table.
  - Header: magic, version, 0, count, 8-byte hash, blockSize, nBlocks, count.
  - Then nBlocks × (u32 offset, u32 firstIndex), then `count` varint lengths, then concatenated UTF-8 strings. Index i corresponds to string i.
- `GET /poe2/api/builds/{version}/character?account={acct}&name={char}&overview={snapshotName}&timeMachine=`: JSON whose `pathOfBuildingExport` is the PoB code.

## Sampling
All search results sort as poe.ninja sorts them by default, which is roughly the top of the ladder. Where a stratum takes N rows, they are spread evenly across the 100 rows returned. Duplicates are removed by (account, character).

1. `top-overall`: the unfiltered top 100 characters.
2. `skill:{S}|asc:{A}`: for the top 40 main skills, the 6 most common ascendancies with at least 10 characters each. 6 characters per combination, 1,112 in total. Because each skill uses its top 6 ascendancies, rarer archetypes are included.
3. `skill-tail:{S}|asc:{A}`: main skills ranked 41 to 75, the top 2 ascendancies each, 4 characters per combination, 200 in total.
4. `top-asc:{A}`: the top 5 characters of each of the 31 classes and ascendancies, 76 new after removing duplicates.

The sample contains 93 distinct main skills, 31 classes and 352 skill × class combinations. Levels range from 80 to 100. Because of the stratification, the sample does **not** reflect real popularity. Use `overview.json` for that.
