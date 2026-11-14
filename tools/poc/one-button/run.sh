#!/bin/bash
# usage: run.sh jobs.jsonl out.jsonl [timeout_s]   (one PoB engine)
S=${EXG_SCRATCH:-.cache}  # holds exg-target/ (CARGO_TARGET_DIR) and engine state
here=$(cd "$(dirname "$0")" && pwd)
exec $S/exg-target/release/onebutton ${POB_ROOT:?set POB_ROOT} $S/ob-state "$here/lib.lua" "$1" "$2" "${3:-1200}"
