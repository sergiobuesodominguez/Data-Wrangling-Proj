#!/bin/bash
# Drive stage 2 unattended on one machine: rerun each shard until COMPLETE.
#
#   src/run_shards.sh 1/4 2/4 3/4 4/4      # all shards, in sequence
#   src/run_shards.sh 3/4                  # just one
#
# Each collect_published.py run stops itself at its request budget; this loop
# relaunches it after 30 s until it exits 0 (COMPLETE). If any run log shows a
# 403 or a cool-off, the driver stops the whole chain rather than grinding a
# blocked address -- a block only clears with time. One log per run is kept
# in data/raw/ as shardNofM.runK.log.
cd "$(dirname "$0")/.." || exit 1
for shard in "$@"; do
  while :; do
    n=$(ls data/raw/shard${shard/\//of}.run*.log 2>/dev/null | wc -l | tr -d ' ')
    log="data/raw/shard${shard/\//of}.run$((n + 1)).log"
    echo "$(date '+%H:%M:%S') shard $shard -> $log"
    python3 src/collect_published.py --shard "$shard" > "$log" 2>&1
    rc=$?
    if grep -qE 'blocked|403|host still returning' "$log"; then
      echo "$(date '+%H:%M:%S') shard $shard: 403 seen in $log -- stopping, do not grind"
      exit 2
    fi
    if [ $rc -eq 0 ]; then
      echo "$(date '+%H:%M:%S') shard $shard COMPLETE"
      break
    fi
    echo "$(date '+%H:%M:%S') shard $shard: budget stop (rc=$rc), relaunching in 30s"
    sleep 30
  done
done
echo "$(date '+%H:%M:%S') all requested shards complete"
