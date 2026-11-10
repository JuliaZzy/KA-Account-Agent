"""一轮里的第二次调用：流式的人话回复。

它发生在代码检查之后，并且被告知闸门结果（缺口 / 提示 / 阶段），所以流出来的文字是准的。
草稿表由界面根据代码渲染——回复只说话，绝不打印表格。语气和篇幅规则写在这条提示词里。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ..llm import calls as model_calls
from ..memory import prompt_turns
from .extract import context_block
from .prompt import REPLY, fill


@dataclass
class ReplyInput:
    tone: str
    language: str
    user_message: str
    stage: str  # gathering | confirm | submitted
    draft_summary: str
    resolutions: list[tuple[str, str]] = field(default_factory=list)  # (said, got)
    gaps: list[str] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)
    layer_notes: list[str] = field(default_factory=list)
    image_count: int = 0
    submitted: dict | None = None  # {po_number, total, currency}
    operating_context: str | None = None
    contract_discount: str | None = None  # set on the turn the contract rate was applied
    history: list[dict] = field(default_factory=list)
    folded_through: int = 0


def _stage_block(i: ReplyInput) -> str:
    if i.stage == "gathering":
        return (
            f"还要弄清这些（自然地带进话里，不要逐条念）：{' | '.join(i.gaps)}" if i.gaps
            else "问问他们想订什么。"
        )
    if i.stage == "confirm":
        bits = [f"要提到：{' | '.join(i.advisories)}"] if i.advisories else []
        bits.append(
            "已经齐了，并且每条规则都通过。确认会下一张真订单，所以在他们按下之前，根据上面这张订单逐项说清楚："
            "每一款产品及其箱数，折扣以及它从哪来"
            "（他们的合同、他们自己要求的，或没有），付款条件、贸易术语、运输方式、预计到货和总额。"
            "然后请他们逐行核对，只有全部符合他们想要的，才按确认。"
            "不要说看起来没问题，也不要叫他们直接确认——核对是他们的事，不是你来放行。"
        )
        return "\n".join(bits)
    s = i.submitted or {}
    bits = [f"已下为 {s.get('po_number')}（{s.get('currency')} {s.get('total')}）。" if s else "已下单。"]
    if i.advisories:
        bits.append(f"需要品牌批准，因为：{' | '.join(i.advisories)}")
    bits.append("确认已经完成，以及接下来做什么。")
    return "\n".join(bits)


def build_reply_prompt(i: ReplyInput) -> str:
    length = (
        "大约六行短句——这是他们付钱之前看到的最后一段。"
        if i.stage == "confirm" else "一到三句短句。"
    )
    image_note = ""
    if i.image_count:
        n = "一张图片" if i.image_count == 1 else f"{i.image_count} 张图片"
        image_note = (
            f"他们还附了{n}，下面这张订单是从图片里读出来的。"
            "简短说明你读过了，并请他们核对草稿——读照片会出错，所以要让改正这件事显得容易。"
        )
    resolutions = ""
    if i.resolutions:
        lines = [f"他们说的是「{said}」→ 你这边对应的是「{got}」。" for said, got in i.resolutions]
        lines.append("如果这并不是他们字面上要的，就承认这一点，并把它作为最接近的匹配提出来，不要悄悄换掉。")
        resolutions = "\n".join(lines)
    contract = ""
    if i.contract_discount:
        contract = (
            f"这一轮你把他们的合同费率放上了订单：{i.contract_discount}。在这条回复里告诉他们——"
            "折扣是钱，绝不能悄悄加上。把它说成合同里已经约定的条款，"
            "绝不能说成他们可以申请、可以去要，或可以谈判的东西。"
        )
    layer = ""
    if i.stage != "submitted" and i.layer_notes:
        layer = f"可以提一句（提议，不要坚持——订单本身没有问题）：{' | '.join(i.layer_notes)}"
    text = fill(
        REPLY,
        tone=i.tone,
        language=i.language or "客户的语言",
        length_rule=length,
        context_block=context_block(i.operating_context),
        customer_said=f"客户刚说：「{i.user_message}」" if i.user_message else "",
        image_note=image_note,
        resolutions=resolutions,
        draft_summary=i.draft_summary or "还没有",
        contract_discount=contract,
        stage_block=_stage_block(i),
        layer_notes=layer,
    )
    return "\n".join(line for line in text.splitlines() if line)


def stream_reply(llm: Any, i: ReplyInput) -> Iterator[str]:
    # The user turn is required, not decorative: with a system-only array a local Llama
    # chat template completes the missing turn itself and emits a literal "assistant\n\n"
    # as the first tokens of the reply (3/3 on llama3.2:3b).
    prior = [
        {"role": turn.get("role"), "content": str(turn.get("content") or "")}
        for turn in prompt_turns(i.history, i.folded_through)
        if turn.get("role") in ("user", "assistant") and str(turn.get("content") or "").strip()
    ]
    prior.append({"role": "user", "content": i.user_message.strip() or "（他们发了一张没有说明的图片）"})
    yield from model_calls.school_text(
        model_calls.SCHOOL_CHAT, client=llm, system=build_reply_prompt(i), messages=prior,
        purpose="po.reply",
    )
