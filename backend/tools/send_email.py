"""通过 SMTP 发邮件。有副作用的工具——只有用户要求发送时才用。

配置（环境变量，绝不展示给模型）：
  SMTP_HOST、SMTP_PORT（587 STARTTLS 或 465 SSL）、SMTP_USER、SMTP_PASSWORD、
  SMTP_FROM（默认等于 SMTP_USER）、SMTP_USE_TLS（默认 1；465 端口上忽略）。

QQ 邮箱示例：
  SMTP_HOST=smtp.qq.com
  SMTP_PORT=465
  SMTP_USER=you@qq.com
  SMTP_PASSWORD=<QQ 邮箱授权码，不是登录密码>
  SMTP_FROM=you@qq.com

SMTP_HOST 为空时，工具拒绝发送。设 EMAIL_DRY_RUN=1 则接受请求但不真正发出。
"""

from __future__ import annotations

import re
import smtplib
from email.message import EmailMessage
from typing import Any

from ..config import env

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "send_email",
        "description": (
            "用户明确要求给某人发邮件时发送。不要编造收件人。"
            "主题和正文尽量短、清楚。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "收件人邮箱",
                },
                "subject": {
                    "type": "string",
                    "description": "邮件主题",
                },
                "body": {
                    "type": "string",
                    "description": "纯文本正文",
                },
                "cc": {
                    "type": "string",
                    "description": "可选的抄送地址",
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
}


def _addr(raw: Any) -> str:
    return str(raw or "").strip()


def _valid(addr: str) -> bool:
    return bool(addr) and bool(_EMAIL.match(addr))


def send(
    *,
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    dry_run: bool | None = None,
) -> dict[str, Any]:
    to_a, subj, text = _addr(to), str(subject or "").strip(), str(body or "").strip()
    cc_a = _addr(cc)
    if not _valid(to_a):
        raise ValueError("to must be a valid email address")
    if cc_a and not _valid(cc_a):
        raise ValueError("cc must be a valid email address")
    if not subj:
        raise ValueError("subject required")
    if not text:
        raise ValueError("body required")

    dry = env("EMAIL_DRY_RUN") in ("1", "true", "yes") if dry_run is None else dry_run
    smtp_user = env("SMTP_USER")
    mail_from = (env("SMTP_FROM") or smtp_user).strip()
    if dry:
        return {
            "sent": False,
            "dry_run": True,
            "provider": "smtp",
            "to": to_a,
            "cc": cc_a or None,
            "subject": subj,
            "from": mail_from or "dry-run@localhost",
        }

    smtp_host = env("SMTP_HOST")
    if not smtp_host:
        raise RuntimeError("SMTP_HOST is not set — configure SMTP or EMAIL_DRY_RUN=1")
    if not mail_from:
        raise RuntimeError("SMTP_FROM or SMTP_USER required")

    smtp_port = int(env("SMTP_PORT") or "587")
    smtp_password = env("SMTP_PASSWORD")
    use_tls = env("SMTP_USE_TLS", "1") not in ("0", "false", "no")

    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = to_a
    if cc_a:
        msg["Cc"] = cc_a
    msg["Subject"] = subj
    msg.set_content(text)
    recipients = [to_a] + ([cc_a] if cc_a else [])

    # 465 = implicit SSL (common for QQ); 587 = STARTTLS.
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=20) as client:
            if smtp_user:
                client.login(smtp_user, smtp_password)
            client.send_message(msg, from_addr=mail_from, to_addrs=recipients)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as client:
            client.ehlo()
            if use_tls:
                client.starttls()
                client.ehlo()
            if smtp_user:
                client.login(smtp_user, smtp_password)
            client.send_message(msg, from_addr=mail_from, to_addrs=recipients)

    return {
        "sent": True,
        "dry_run": False,
        "provider": "smtp",
        "to": to_a,
        "cc": cc_a or None,
        "subject": subj,
        "from": mail_from,
    }


class MailError(Exception):
    """A refusal before SMTP accepts the message. `code` is a `ToolErrorCode` value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def preflight(arguments: dict[str, Any]) -> tuple[str, str] | None:
    """Reject a message that must not be handed to SMTP.

    None means the executor may claim the business key and call `run`.
    A bad address or missing SMTP config stays out of that key, so a later
    corrected call can still send.
    """
    to_address = _addr(arguments.get("to"))
    cc_address = _addr(arguments.get("cc"))
    subject = str(arguments.get("subject") or "").strip()
    body = str(arguments.get("body") or "").strip()
    if not _valid(to_address):
        return ("TOOL_VALIDATION_ERROR", "to must be a valid email address")
    if cc_address and not _valid(cc_address):
        return ("TOOL_VALIDATION_ERROR", "cc must be a valid email address")
    if not subject:
        return ("TOOL_VALIDATION_ERROR", "subject required")
    if not body:
        return ("TOOL_VALIDATION_ERROR", "body required")
    if env("EMAIL_DRY_RUN") in ("1", "true", "yes"):
        return None
    if not env("SMTP_HOST"):
        return ("TOOL_UPSTREAM_ERROR", "SMTP_HOST is not set")
    if not (env("SMTP_FROM") or env("SMTP_USER")).strip():
        return ("TOOL_UPSTREAM_ERROR", "SMTP_FROM or SMTP_USER required")
    return None


def run(arguments: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
    refusal = preflight(arguments)
    if refusal is not None:
        raise MailError(*refusal)
    data = send(
        to=str(arguments.get("to") or ""),
        subject=str(arguments.get("subject") or ""),
        body=str(arguments.get("body") or ""),
        cc=str(arguments.get("cc") or ""),
    )
    return {"ok": True, "data": data, "error": None}
