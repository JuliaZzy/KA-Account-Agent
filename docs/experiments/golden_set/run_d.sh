#!/usr/bin/env bash
# Experiment D: same offered load, different Celery worker counts.
#
#   docs/experiments/golden_set/run_d.sh <workers> [pool]
#
# Offered load is fixed (client concurrency 10, 300 s timeout) so that W=3 and W=5 build a queue and W=10 can
# use all its slots. The mix has 32 runs in one pool (12 text2sql x2, one po draft x2, and three full monthly
# reports x2) followed by 18 contract/po runs. Only the worker count (and the DB pool that has to follow it) changes.
set -u
W=${1:?workers}
POOL=${2:-6}
E=docs/experiments/exports
G=docs/experiments/golden_set
C=$G/golden_all_v2.jsonl
N=$E/d-w$W

KA_WORKER_CONCURRENCY=$W KA_WORKER_POOL=$POOL docker compose up -d --force-recreate worker >/dev/null 2>&1
sleep 25
docker compose exec -T worker celery -A backend.celery.worker:celery inspect stats 2>/dev/null | rg -o '"max-concurrency": [0-9]+' | head -1

rm -f $N-wall.txt
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
