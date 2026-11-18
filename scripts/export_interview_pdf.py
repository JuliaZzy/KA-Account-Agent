#!/usr/bin/env python3
"""Render Markdown to PDF via Chrome headless (no extra Python deps).

Examples:
  python3 scripts/export_interview_pdf.py
  python3 scripts/export_interview_pdf.py -i docs/cv/interview.md -o docs/cv/my.pdf
  python3 scripts/export_interview_pdf.py --input ./notes.md --output ~/Desktop/notes.pdf
"""

from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "docs/cv/interview.md"
DEFAULT_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

INLINE_CODE = re.compile(r"`([^`]+)`")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = LINK.sub(r'<a href="\2">\1</a>', text)
    text = BOLD.sub(r"<strong>\1</strong>", text)
    text = INLINE_CODE.sub(r"<code>\1</code>", text)
    return text


def md_to_html(md: str) -> str:
    lines = md.splitlines()
    parts: list[str] = []
    in_pre = False
    pre_buf: list[str] = []
    in_ul = False
    in_table = False
    table_rows: list[list[str]] = []

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            parts.append("</ul>")
            in_ul = False

    def flush_table() -> None:
        nonlocal in_table, table_rows
        if not in_table:
            return
        if table_rows:
            parts.append("<table>")
            for i, row in enumerate(table_rows):
                tag = "th" if i == 0 else "td"
                parts.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in row) + "</tr>")
            parts.append("</table>")
        table_rows = []
        in_table = False

    for raw in lines:
        line = raw.rstrip()

        if line.strip().startswith("```"):
            if in_pre:
                parts.append("<pre><code>" + html.escape("\n".join(pre_buf)) + "</code></pre>")
                pre_buf = []
                in_pre = False
            else:
                close_ul()
                flush_table()
                in_pre = True
            continue
        if in_pre:
            pre_buf.append(raw)
            continue

        if "|" in line and line.strip().startswith("|"):
            close_ul()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cells):
                continue
            in_table = True
            table_rows.append(cells)
            continue
        flush_table()

        if not line.strip():
            close_ul()
            parts.append("<p></p>")
            continue

        if line.startswith("# "):
            close_ul()
            parts.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("## "):
            close_ul()
            parts.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("### "):
            close_ul()
            parts.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("> "):
            close_ul()
            parts.append(f"<blockquote><p>{inline(line[2:])}</p></blockquote>")
        elif line.startswith("- "):
            if not in_ul:
                parts.append("<ul>")
                in_ul = True
            parts.append(f"<li>{inline(line[2:])}</li>")
        elif line.strip() == "---":
            close_ul()
            parts.append("<hr/>")
        else:
            close_ul()
            parts.append(f"<p>{inline(line)}</p>")

    if in_pre and pre_buf:
        parts.append("<pre><code>" + html.escape("\n".join(pre_buf)) + "</code></pre>")
    close_ul()
    flush_table()
    return "\n".join(parts)


def wrap(body: str, *, title: str) -> str:
    safe_title = html.escape(title, quote=True)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>{safe_title}</title>
<style>
  @page {{ size: A4; margin: 18mm 16mm; }}
  body {{
    font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
    font-size: 10.5pt; line-height: 1.55; color: #222; max-width: 210mm; margin: 0 auto;
    padding: 12mm 14mm;
  }}
  h1 {{ font-size: 18pt; border-bottom: 2px solid #333; padding-bottom: 6px; }}
  h2 {{ font-size: 13pt; margin-top: 1.1em; color: #111; }}
  h3 {{ font-size: 11.5pt; margin-top: 0.9em; }}
  code, pre {{ font-family: Menlo, "SF Mono", monospace; font-size: 9pt; }}
  code {{ background: #f3f3f3; padding: 1px 4px; border-radius: 2px; }}
  pre {{ background: #f5f5f5; padding: 10px; white-space: pre-wrap; word-break: break-word; }}
  table {{ border-collapse: collapse; width: 100%; margin: 0.6em 0; font-size: 9.5pt; }}
  th, td {{ border: 1px solid #bbb; padding: 5px 8px; vertical-align: top; }}
  th {{ background: #f0f0f0; }}
  blockquote {{ margin: 0.4em 0; padding: 4px 0 4px 12px; border-left: 3px solid #888; color: #444; }}
  ul {{ margin: 0.3em 0; padding-left: 1.4em; }}
  li {{ margin: 0.15em 0; }}
  a {{ color: #0b57d0; text-decoration: none; }}
  hr {{ border: none; border-top: 1px solid #ccc; margin: 1em 0; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _resolve_path(raw: str, *, base: Path | None = None) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute() and base is not None:
        path = (base / path).resolve()
    else:
        path = path.resolve()
    return path


def _title_from_md(md_text: str, fallback: str) -> str:
    for line in md_text.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert a Markdown file to PDF (Chrome headless).")
    parser.add_argument(
        "-i", "--input",
        default=str(DEFAULT_INPUT),
        metavar="PATH",
        help=f"Input .md file (default: {DEFAULT_INPUT.relative_to(ROOT)})",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        metavar="PATH",
        help="Output .pdf path (default: same directory as input, stem + .pdf)",
    )
    parser.add_argument(
        "--html",
        default=None,
        metavar="PATH",
        help="Intermediate .html path (default: same as output with .html extension)",
    )
    parser.add_argument(
        "--chrome",
        default=str(DEFAULT_CHROME),
        help="Chrome/Chromium binary for headless print",
    )
    args = parser.parse_args(argv)
    args.input = _resolve_path(args.input, base=Path.cwd())
    if args.output is None:
        args.output = args.input.with_suffix(".pdf")
    else:
        args.output = _resolve_path(args.output, base=Path.cwd())
    if args.html is None:
        args.html = args.output.with_suffix(".html")
    else:
        args.html = _resolve_path(args.html, base=Path.cwd())
    args.chrome = Path(args.chrome).expanduser().resolve()
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    md_path: Path = args.input
    pdf_path: Path = args.output
    html_path: Path = args.html
    chrome: Path = args.chrome

    if not md_path.is_file():
        print(f"missing {md_path}", file=sys.stderr)
        return 1
    if not chrome.is_file():
        print(f"Chrome not found at {chrome}", file=sys.stderr)
        return 1

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.parent.mkdir(parents=True, exist_ok=True)

    md_text = md_path.read_text(encoding="utf-8")
    title = _title_from_md(md_text, md_path.stem)
    body = md_to_html(md_text)
    html_path.write_text(wrap(body, title=title), encoding="utf-8")

    url = html_path.resolve().as_uri()
    cmd = [
        str(chrome),
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        f"--print-to-pdf={pdf_path.resolve()}",
        url,
    ]
    subprocess.run(cmd, check=True)
    print(f"Wrote {pdf_path} ({pdf_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())