"""Which query implementation a call uses.

`KA_TEXT2SQL_IMPL=legacy` runs the archived pick/write/repair/reflect workflow so an experiment can
compare it with the ReAct loop behind the same planner, tools and answer stage. It is never the
default and production code does not set it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any


def answer_question_impl() -> Callable[..., Any]:
    if os.environ.get("KA_TEXT2SQL_IMPL", "react").strip().lower() == "legacy":
        from ..text2sql_legacy.text2sql import answer_question as legacy_answer

        return legacy_answer
    from .text2sql import answer_question

    return answer_question
