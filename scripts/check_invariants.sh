#!/usr/bin/env bash
# Check backend/ against docs/INVARIANTS.md.
#
# Comment lines are skipped: a note that says "do not call asyncio.run(" does
# not count. A string literal with the same text still counts; confirm those
# by hand.
#
# The I-6 name list only stops those sync defs from coming back. It does not
# prove every other sync model function is gone.

set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

allowlist=docs/invariants_allowlist.txt
violations=0

say() {
  printf '%s\n' "$*"
}

# Drop hits whose code (the part after file:line:) is a comment.
strip_comments() {
  grep -vE ':[0-9]+:[[:space:]]*#' || true
}

fail() {
  violations=$((violations + 1))
  say ""
  say "违反 $1"
  if [[ -n "${2:-}" ]]; then
    printf '%s\n' "$2"
  fi
}

pass() {
  say "通过 $1"
}

check_empty() {
  local id=$1 pattern=$2
  local hits
  hits=$(grep -rnw "$pattern" backend/ | strip_comments || true)
  if [[ -n "$hits" ]]; then
    fail "$id $pattern 必须为空" "$hits"
  else
    pass "$id $pattern 为空"
  fi
}

# I-1. Each allowlisted file has exactly one asyncio.run(. No other file has one.
i1_hits=$(grep -rn "asyncio.run(" backend/ | strip_comments || true)
i1_counts=$(printf '%s\n' "$i1_hits" | awk -F: 'NF {print $1}' | sort | uniq -c)
i1_bad=$(printf '%s\n' "$i1_counts" | awk 'NF && $1 != 1 {print}')
i1_paths=$(printf '%s\n' "$i1_counts" | awk 'NF {print $2}' | sort)
allow_paths=$(grep -v '^#' "$allowlist" | grep -v '^[[:space:]]*$' | sort)
extra=$(comm -23 <(printf '%s\n' "$i1_paths") <(printf '%s\n' "$allow_paths") || true)
missing=$(comm -13 <(printf '%s\n' "$i1_paths") <(printf '%s\n' "$allow_paths") || true)
if [[ -n "$i1_bad" || -n "$extra" || -n "$missing" ]]; then
  detail=""
  if [[ -n "$i1_bad" ]]; then
    detail="${detail}计数不是 1:
${i1_bad}
"
  fi
  if [[ -n "$extra" ]]; then
    detail="${detail}不在白名单:
${extra}
"
  fi
  if [[ -n "$missing" ]]; then
    detail="${detail}白名单里有、代码里没有:
${missing}
"
  fi
  detail="${detail}命中:
${i1_hits}"
  fail "I-1 asyncio.run( 每个白名单文件一处" "$detail"
else
  pass "I-1 asyncio.run( 每个白名单文件一处"
fi

check_empty "I-1" "run_until_complete"
check_empty "I-1" "new_event_loop"
check_empty "I-1" "_await"
check_empty "I-1" "_background_loop"

# I-3. Public fail_run is gone. _fail_run stays in the file that defines finalize_run.
check_empty "I-3" "fail_run"
fail_hits=$(grep -rnw "_fail_run" backend/ | strip_comments || true)
finalize_file=$(grep -rl "def finalize_run" backend/ || true)
if [[ -z "$fail_hits" ]]; then
  pass "I-3 _fail_run 尚未出现"
elif [[ -z "$finalize_file" ]]; then
  fail "I-3 _fail_run 出现时 finalize_run 还不存在" "$fail_hits"
else
  outside=$(printf '%s\n' "$fail_hits" | awk -F: 'NF {print $1}' | sort -u | grep -vxF "$finalize_file" || true)
  if [[ -n "$outside" ]]; then
    fail "I-3 _fail_run 只允许在 ${finalize_file}" "$fail_hits"
  else
    pass "I-3 _fail_run 只在 ${finalize_file}"
  fi
fi

# I-4. These except clauses live only in the classify module.
except_hits=$(grep -rnE 'except [(]ModelCapacityExceeded, ProviderError[)]|except ProviderError|except ModelCapacityExceeded' backend/ | strip_comments || true)
except_files=$(printf '%s\n' "$except_hits" | awk -F: 'NF {print $1}' | sort -u)
except_outside=$(printf '%s\n' "$except_files" | grep -vxF 'backend/llm/errors.py' || true)
if [[ -n "$except_outside" ]]; then
  fail "I-4 except ProviderError / ModelCapacityExceeded 只允许在 backend/llm/errors.py" "$except_hits"
elif [[ -n "$except_hits" ]]; then
  pass "I-4 except 只在 backend/llm/errors.py"
else
  pass "I-4 没有这些 except"
fi

# I-6. Sync defs of these names, including methods indented inside a class.
# async def does not match: the line would start with "async", not "def".
sync_hits=$(grep -rnE '^[[:space:]]*def (call_model|_ask_ds|deepseek_json|_call_deepseek)[(]' backend/ | strip_comments || true)
if [[ -n "$sync_hits" ]]; then
  fail "I-6 同步模型函数必须为空（只防这几个名字回潮）" "$sync_hits"
else
  pass "I-6 这几个同步模型函数不存在"
fi

# I-7.
original_hits=$(grep -rn "_ORIGINAL_" backend/ | strip_comments || true)
if [[ -n "$original_hits" ]]; then
  fail "I-7 _ORIGINAL_ 必须为空" "$original_hits"
else
  pass "I-7 _ORIGINAL_ 为空"
fi

say ""
if [[ "$violations" -eq 0 ]]; then
  say "没有违反。"
  exit 0
fi
say "共 ${violations} 条违反。"
exit 1
