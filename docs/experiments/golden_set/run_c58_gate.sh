#!/usr/bin/env bash
# C knives 5–8 mixed-load: same offered load as experiment D / record 29 at Worker=5.
#
#   docs/experiments/golden_set/run_c58_gate.sh
#
# Client concurrency 10, timeout 300 s, 32 pre + 18 post. Does not overwrite c-gate-w5-* or d-w5-*.
set -u
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
E=docs/experiments/exports
G=docs/experiments/golden_set
C=$G/golden_all_v2.jsonl
N=$E/c58-gate-w5

mkdir -p "$E"
rm -f "$N.done" "$N-wall.txt"

docker compose exec -T redis sh -c '
while true; do
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  redis-cli --scan --pattern "gate:*" | while read -r k; do
    [ -n "$k" ] && echo "$ts $k $(redis-cli ZCARD "$k")"
  done
  sleep 2
done
' > "$N-gates.tsv" &
SPID=$!

cleanup() {
  kill "$SPID" 2>/dev/null || true
  wait "$SPID" 2>/dev/null || true
  date -u +%Y-%m-%dT%H:%M:%SZ > "$N.done"
}
trap cleanup EXIT

run() {
  name=$1; phase=$2; only=$3; reps=$4
  s=$(date +%s)
  ./dev py $G/run_golden.py --cases $C --phase $phase --only $only --repeats $reps --concurrency 10 --timeout 300 \
    --out $N-$name.jsonl --raw-dir $N-$name-raw > $N-$name.log 2>&1
  e=$(date +%s)
  echo "$name $s $e $(tail -1 $N-$name.log)" | tee -a $N-wall.txt
}

run pre pre t2s-01,t2s-02,t2s-03,t2s-04,t2s-05,t2s-06,t2s-07,t2s-08,t2s-09,t2s-10,t2s-11,t2s-12,rpt-01,rpt-07,plan-02,po-01 2
run post post con-qa-01,con-qa-02,con-qa-03,con-qa-04,con-qa-05,con-qa-06,po-02,po-03,po-04 2
