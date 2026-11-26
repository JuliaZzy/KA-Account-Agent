"""Terminal result passed into run close-out.

`reason` is optional and has no default. Callers pass the value they already
record: ``permanent``, ``dead_letter``, ``over_duration``, ``lease_expired``, and
the queue-sweep codes. Success and cancel record none today.

A duration stop writes a dead letter on both the worker and the sweeper.
A requested cancel does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Outcome:
    kind: Literal["succeeded", "failed", "cancelled"]
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    reason: str | None = None
    dead_letter: bool = False


def over_duration_outcome(cancel_requested: bool) -> Outcome:
    """One outcome for a run that has used up its duration.

    A requested cancel wins and does not write a dead letter. Otherwise the run
    fails with ``COST_BUDGET_EXCEEDED`` and one dead letter.
    """
    if cancel_requested:
        return Outcome(kind="cancelled", reason="over_duration")
    return Outcome(
        kind="failed",
        reason="over_duration",
        error={
            "kind": "permanent",
            "code": "COST_BUDGET_EXCEEDED",
            "message": "run duration exceeded",
        },
        dead_letter=True,
    )
