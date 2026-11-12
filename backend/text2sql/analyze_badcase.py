"""Group stored text-to-SQL attempts so a person can see what to fix.

Classification is deterministic. It does not call a model, and it does not copy a
corrected statement back into the prompt — that stays a review step.
"""

from __future__ import annotations

from typing import Any

CATEGORIES = ("rejected", "schema_error", "syntax_error", "semantic_error", "ambiguity", "complex_query")

_VALIDATION = (
    "empty sql", "multiple statements", "only select", "could not parse", "not allowed",
    "schema-qualified", "too deeply nested", "too long", "integer constants",
)


def classify_error(log: dict[str, Any]) -> str:
    """One category for a failed attempt or a row a person marked bad."""
    error_type = (log.get("error_type") or "").lower()
    message = (log.get("error_message") or "").lower()
    if error_type == "validation" or any(token in message for token in _VALIDATION):
        return "rejected"
    if log.get("user_feedback") == "bad" and log.get("execution_success"):
        return "semantic_error" if log.get("corrected_sql") else "ambiguity"
    if any(token in message for token in ("does not exist", "undefined column", "undefinedtable", "column")):
        return "schema_error"
    sql = log.get("generated_sql") or ""
    if sql.upper().count(" JOIN ") >= 3 or sql.upper().count("SELECT") >= 3:
        return "complex_query"
    return "syntax_error"


def analyze(logs: list[dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], str, list[dict[str, str]]]:
    """Return (categories, report, few-shot candidates).

    A candidate is a bad rating that has a hand-written replacement. Nothing here
    decides that the replacement is right.
    """
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name in CATEGORIES}
    interesting = [
        log for log in logs
        if not log.get("execution_success") or log.get("user_feedback") == "bad"
    ]
    for log in interesting:
        grouped[classify_error(log)].append(log)

    lines = [f"{len(logs)} attempts, {len(interesting)} failed or rated bad."]
    for name in CATEGORIES:
        lines.append(f"- {name}: {len(grouped[name])}")
    few_shot = [
        {
            "question": str(log.get("question") or ""),
            "generated_sql": str(log.get("generated_sql") or ""),
            "corrected_sql": str(log.get("corrected_sql") or ""),
        }
        for log in interesting
        if log.get("user_feedback") == "bad" and log.get("corrected_sql")
    ]
    lines.append(f"{len(few_shot)} corrected statements are waiting for review before they can be used as examples.")
    return grouped, "\n".join(lines), few_shot


def main() -> None:
    from ..db import Database

    categories, report, _few_shot = analyze(Database().list_text2sql_logs(admin=True))
    print(report)
    del categories


if __name__ == "__main__":
    main()
