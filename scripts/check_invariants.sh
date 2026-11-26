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

# Paths under one [section] in the allowlist. Comment lines are dropped.
section_paths() {
  local section=$1
  awk -v section="$section" '
    /^#/ { next }
    /^\[/ {
      current = $0
      gsub(/[][]/, "", current)
      next
    }
    NF && current == section { print }
  ' "$allowlist" | sort
}

# Each allowlisted file has exactly one hit. No file outside the section.
check_file_set() {
  local label=$1
  local section=$2
  local hits=$3
  local counts bad paths allow extra missing detail
  counts=$(printf '%s\n' "$hits" | awk -F: 'NF {print $1}' | sort | uniq -c)
  bad=$(printf '%s\n' "$counts" | awk 'NF && $1 != 1 {print}')
  paths=$(printf '%s\n' "$counts" | awk 'NF {print $2}' | sort)
  allow=$(section_paths "$section")
  extra=$(comm -23 <(printf '%s\n' "$paths") <(printf '%s\n' "$allow") || true)
  missing=$(comm -13 <(printf '%s\n' "$paths") <(printf '%s\n' "$allow") || true)
  if [[ -n "$bad" || -n "$extra" || -n "$missing" ]]; then
    detail=""
    if [[ -n "$bad" ]]; then
      detail="${detail}计数不是 1:
${bad}
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
${hits}"
    fail "$label" "$detail"
  else
    pass "$label"
  fi
}

asyncio_hits=$(grep -rn "asyncio.run(" backend/ | strip_comments || true)
check_file_set "I-1 asyncio.run( 每个入口文件一处" "asyncio.run" "$asyncio_hits"

loop_hits=$(grep -rnw "new_event_loop" backend/ | strip_comments || true)
check_file_set "I-1 new_event_loop 只在专用线程" "new_event_loop" "$loop_hits"

check_empty "I-1" "run_until_complete"
check_empty "I-1" "_await"
check_empty "I-1" "_background_loop"

# I-3. The name fail_run is gone. Terminal status writes go through _write_terminal.
check_empty "I-3" "fail_run"
write_defs=$(grep -rn "def _write_terminal" backend/ | strip_comments || true)
write_files=$(printf '%s\n' "$write_defs" | awk -F: 'NF {print $1}' | sort -u)
if [[ "$write_files" == "backend/db/database/run.py" ]]; then
  pass "I-3 _write_terminal 只在 backend/db/database/run.py"
else
  fail "I-3 _write_terminal 只允许定义在 backend/db/database/run.py" "$write_defs"
fi

# A call that names a terminal status within eight lines of _transition( or transition_run(.
terminal_hits=$(python3 - <<'PY'
import re
from pathlib import Path
pat = re.compile(r"RunStatus\.(SUCCEEDED|FAILED|CANCELLED)")
call = re.compile(r"_transition\(|transition_run\(")
root = Path("backend")
for path in root.rglob("*.py"):
    lines = path.read_text().splitlines()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if not call.search(line):
            continue
        window = "\n".join(lines[index:index + 8])
        if pat.search(window):
            print(f"{path}:{index + 1}:{line.strip()}")
PY
)
if [[ -n "$terminal_hits" ]]; then
  check_file_set "I-3 直接写成终态的调用只在白名单" "terminal_transition" "$terminal_hits"
else
  pass "I-3 没有直接写成终态的调用"
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

named_hits=$(grep -rnE 'except .*(JsonRepairExhausted|ModelBudgetExceeded)' backend/ | strip_comments || true)
named_files=$(printf '%s\n' "$named_hits" | awk -F: 'NF {print $1}' | sort -u)
named_outside=$(printf '%s\n' "$named_files" | grep -vxF 'backend/llm/errors.py' || true)
if [[ -n "$named_outside" ]]; then
  fail "I-4 except JsonRepairExhausted / ModelBudgetExceeded 只允许在 backend/llm/errors.py" "$named_hits"
elif [[ -n "$named_hits" ]]; then
  pass "I-4 这两个 except 只在 backend/llm/errors.py"
else
  pass "I-4 没有 JsonRepairExhausted / ModelBudgetExceeded 的 except"
fi

# Route labels. The old names exist only as comments or constants in errors.py.
literal_hits=$(grep -rnE '"provider_error"|"model_capacity"' backend/ | strip_comments || true)
literal_files=$(printf '%s\n' "$literal_hits" | awk -F: 'NF {print $1}' | sort -u)
literal_outside=$(printf '%s\n' "$literal_files" | grep -vxF 'backend/llm/errors.py' || true)
if [[ -n "$literal_outside" ]]; then
  fail "I-4 provider_error / model_capacity 字面量只允许在 backend/llm/errors.py" "$literal_hits"
elif [[ -n "$literal_hits" ]]; then
  pass "I-4 路由字面量只在 backend/llm/errors.py"
else
  pass "I-4 没有旧的路由字面量"
fi

# I-5. Optional chart and insight work must not join a TaskGroup.
# The one allowed use is the required body sections, and that file may contain it once.
task_hits=$(grep -rn "TaskGroup" backend/report | strip_comments || true)
task_count=$(printf '%s\n' "$task_hits" | awk 'NF' | wc -l | tr -d ' ')
if [[ -z "$task_hits" ]]; then
  fail "I-5 必选正文的 TaskGroup 不见了" ""
elif [[ "$task_count" != "1" ]]; then
  fail "I-5 backend/report 里的 TaskGroup 只能有一处" "$task_hits"
else
  check_file_set "I-5 TaskGroup 只允许必选正文" "report_task_group" "$task_hits"
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
