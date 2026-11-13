#!/usr/bin/env bash
# Same 50-run mix as records 26/29/30, but Worker=12 and three 联采 users.
# GATE_ORG=10 / GATE_USER=5 stay at compose defaults. Restores worker=5 at the end.
#
#   docs/experiments/golden_set/run_c_org_w12.sh
set -u
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$ROOT"
E=docs/experiments/exports
G=docs/experiments/golden_set
C=$G/golden_all_v2.jsonl
N=$E/c-org-w12
W=12
POOL=4
USERS=user-liancai-chen,user-liancai-lin,user-liancai-zhou

mkdir -p "$E"
rm -f "$N.done" "$N-wall.txt"

wait_worker() {
  for i in $(seq 1 40); do
    st=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' ka_account-worker-1 2>/dev/null || echo missing)
    echo "worker $st ($i)"
    [ "$st" = "healthy" ] && return 0
    sleep 3
  done
  return 1
}

echo "recreate worker concurrency=$W pool=$POOL"
KA_WORKER_CONCURRENCY=$W KA_WORKER_POOL=$POOL docker compose up -d --force-recreate worker
wait_worker || { echo "worker did not become healthy"; exit 1; }
cmd=$(docker inspect --format '{{json .Config.Cmd}}' ka_account-worker-1)
echo "worker cmd $cmd"
echo "$cmd" | rg -q '"--concurrency=12"' || { echo "chat worker is not concurrency=12"; exit 1; }
docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' ka_account-worker-1 | rg 'KA_ACCOUNT_POOL' || true

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

restore() {
  kill "$SPID" 2>/dev/null || true
  wait "$SPID" 2>/dev/null || true
  date -u +%Y-%m-%dT%H:%M:%SZ > "$N.done"
  echo "restore worker concurrency=5 pool=6"
  KA_WORKER_CONCURRENCY=5 KA_WORKER_POOL=6 docker compose up -d --force-recreate worker
  wait_worker || true
  docker inspect --format '{{json .Config.Cmd}}' ka_account-worker-1
}
trap restore EXIT

run() {
  name=$1; phase=$2; only=$3; reps=$4
  s=$(date +%s)
  ./dev py $G/run_golden.py --cases $C --phase $phase --only $only --repeats $reps --concurrency 18 --timeout 300 \
    --liancai-users $USERS \
    --out $N-$name.jsonl --raw-dir $N-$name-raw > $N-$name.log 2>&1
  e=$(date +%s)
  echo "$name $s $e $(tail -1 $N-$name.log)" | tee -a $N-wall.txt
}

run pre pre t2s-01,t2s-02,t2s-03,t2s-04,t2s-05,t2s-06,t2s-07,t2s-08,t2s-09,t2s-10,t2s-11,t2s-12,rpt-01,rpt-07,plan-02,po-01 2
run post post con-qa-01,con-qa-02,con-qa-03,con-qa-04,con-qa-05,con-qa-06,po-02,po-03,po-04 2
