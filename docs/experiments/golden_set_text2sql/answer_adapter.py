"""One ANSWER call on the golden user turn. No plan, no post-hoc rewrite.

`materials` is the user content `compose_answer` would send. An empty string means
there were no results, so the model sees only the user message. The returned text
is the model reply before `ensure_empty_period_opening`.
"""
from __future__ import annotations

from backend.llm.calls import qwen_text
from backend.orchestrator.prompt import ANSWER


def answer(user_message: str, materials: str, case: dict) -> dict[str, str]:
    del case
    sent = materials if materials.strip() else user_message
    parts = qwen_text(
        system=ANSWER,
        messages=[{"role": "user", "content": sent}],
        on_text=lambda *_args, **_kwargs: None,
        purpose="orchestrator.answer",
    )
    return {"answer": "".join(parts).strip(), "materials": sent}
