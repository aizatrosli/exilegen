#!/bin/bash
# Evaluate every build in builds.jsonl with PoB (resumable). With --follow it tails the
# file until the collector's progress.json says done or goes quiet for --idle-min minutes.
# Env: POB_ROOT (synced PoB-PoE2, from pob-redux's pob-sync --dest), CORPUS, STATE, WORKERS.
set -e
here=$(cd "$(dirname "$0")" && pwd)
CORPUS=${CORPUS:-data/corpus/poe2/}
STATE=${STATE:-.cache/corpus-eval}
bin=$here/target/release/corpus-eval
[ -x "$bin" ] || cargo build --release --manifest-path "$here/Cargo.toml"
exec "$bin" --pob-root "${POB_ROOT:?set POB_ROOT}" \
  --input "$CORPUS/builds.jsonl" --out-dir "$CORPUS" \
  --state-dir "$STATE" --workers ${WORKERS:-3} --follow --idle-min 10 "$@"
