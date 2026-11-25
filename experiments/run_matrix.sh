#!/usr/bin/env bash
# Phase 1 matrix for docs/starter-alignment-plan.md. Writes experiments/results/*.json.
# Usage: experiments/run_matrix.sh [RUNS] [PARALLEL]
set -u
cd "$(dirname "$0")"
RUNS=${1:-2}
PAR=${2:-3}
mkdir -p results

conds=(
  "gated_conv_g5|--input gated --clips conv --gap 5"
  "routed_noise_conv_g5|--sessions routed --idle noise --clips conv --gap 5"
  "routed_nothing_conv_g5|--sessions routed --idle nothing --clips conv --gap 5"
  "routed_zero_conv_g5|--sessions routed --idle zero --floor 0 --clips conv --gap 5"
  "both_conv_g5|--sessions both --clips conv --gap 5"
  "gated_hard_g5|--input gated --clips hard --gap 5"
  "routed_hard_g5|--sessions routed --clips hard --gap 5"
  "both_hard_g5|--sessions both --clips hard --gap 5"
  "both_conv_g1|--sessions both --clips conv --gap 1"
  "both_fb_conv_g1|--sessions both --feedback --clips conv --gap 1"
  "both_fbsup_conv_g1|--sessions both --feedback --suppress --clips conv --gap 1"
  "routed_fb_conv_g1|--sessions routed --feedback --clips conv --gap 1"
  "routed_fbsup_conv_g1|--sessions routed --feedback --suppress --clips conv --gap 1"
  "gated_conv4_g30|--input gated --clips conv4 --gap 30"
  "routed_nothing_conv4_g30|--sessions routed --idle nothing --clips conv4 --gap 30"
  "routed_noise_conv4_g30|--sessions routed --idle noise --clips conv4 --gap 30"
  "both_conv4_g30|--sessions both --clips conv4 --gap 30"
)

run() {
  local name=${1%%|*} flags=${1#*|}
  [ -s "results/$name.json" ] && { echo "skip $name"; return; }
  uv run --quiet multiturn.py $flags --runs "$RUNS" --label "$name" --out "results/$name.json" \
    > "results/$name.log" 2>&1
  echo "done $name (exit $?)"
}

i=0
for c in "${conds[@]}"; do
  run "$c" &
  i=$((i + 1))
  if [ $((i % PAR)) -eq 0 ]; then wait; fi
done
wait
echo ALL DONE
