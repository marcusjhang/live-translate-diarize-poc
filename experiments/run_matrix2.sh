#!/usr/bin/env bash
# Round 2: tighten the A-vs-gated comparison and test audio_stream_end. 3 runs each.
set -u
cd "$(dirname "$0")"
RUNS=${1:-3}; PAR=${2:-3}
conds=(
  "gated_conv_g5__r2|--input gated --clips conv --gap 5"
  "routed_nothing_conv_g5__r2|--sessions routed --idle nothing --clips conv --gap 5"
  "routed_noise_conv_g5__r2|--sessions routed --idle noise --clips conv --gap 5"
  "routed_se_conv_g5|--sessions routed --stream-end 1.5 --clips conv --gap 5"
  "gated_hard_g5__r2|--input gated --clips hard --gap 5"
  "routed_hard_g5__r2|--sessions routed --idle noise --clips hard --gap 5"
  "routed_se_hard_g5|--sessions routed --stream-end 1.5 --clips hard --gap 5"
  "routed_se_conv4_g30|--sessions routed --stream-end 1.5 --clips conv4 --gap 30"
  "routed_se_conv_g2|--sessions routed --stream-end 1.0 --clips conv --gap 2"
)
run() {
  local name=${1%%|*} flags=${1#*|}
  [ -s "results/$name.json" ] && { echo "skip $name"; return; }
  uv run --quiet multiturn.py $flags --runs "$RUNS" --label "$name" --out "results/$name.json" > "results/$name.log" 2>&1
  echo "done $name (exit $?)"
}
i=0
for c in "${conds[@]}"; do run "$c" & i=$((i+1)); [ $((i % PAR)) -eq 0 ] && wait; done
wait; echo ALL DONE
