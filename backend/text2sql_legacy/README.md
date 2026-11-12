# Archived multi-agent text-to-SQL

This directory is an experiment-only copy of the pre-ReAct workflow recovered from
Git history (`ea23afc`). Production code does not import it.

The workflow is deliberately kept as:

1. A planning role selects tables.
2. DeepSeek writes one SQL statement.
3. DeepSeek rewrites a failed or incomplete statement, up to three SQL runs.
4. A reflection role scores the successful result and may request a new question, up to three reflections.

All roles use the current DeepSeek JSON route, temperature, and model-call budget. The
historical commit used Qwen for the planning and reflection roles; that provider choice
is intentionally normalized here so the experiment isolates orchestration.

The current SQL safety checks, scoped read execution, result shape, row-cap handling,
required-column handling, logging, and query rules are shared with the ReAct experiment.
Only the orchestration is archived. Import `backend.text2sql_legacy.text2sql` directly
from an experiment; do not wire it into the application agent.
