"""Read an uploaded file into pages the rest of the agent can use.

PDFs go to Zhipu GLM-OCR (`GLM_API_KEY`). A text layer misses scans and tables, and
evidence quotes are checked against the text we actually return, so each page has to
come back as its own string. Word and plain text stay local. A PDF page can still be
rendered to JPEG for a vision model; that image does not replace the OCR text.
"""

from __future__ import annotations

import io
import re
import time

from .config import env
from .db import Database
from .services.run_names import StepStatus, StepType, ToolCallStatus, ToolErrorCode, step_name, tool_error

PSEUDO_PAGE_CHARS = 3500
# GLM-OCR本身有 PDF 大约 50MB、最多 100 页的限制
MAX_FILE_BYTES = 20 * 1024 * 1024
JPEG_QUALITY = 72
MAX_IMAGE_WIDTH = 1600

SUPPORTED_EXTS = (".pdf", ".txt", ".md", ".docx")

_TAG = re.compile(r"<[^>]+>")


def _chunk(text: str, size: int = PSEUDO_PAGE_CHARS) -> list[str]:
    text = text.strip()
    if not text:
        return []
    out, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        if buf and len(buf) + len(para) + 2 > size:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        out.append(buf)
    return out


def _element_text(label: str, content: str) -> str:
    """One layout block as plain text. Table cells arrive as HTML; image blocks are URLs."""
    content = content.strip()
    if not content or label == "image" or content.startswith(("http://", "https://")):
        return ""
    if label == "table" or "<" in content:
        content = re.sub(r"<br\s*/?>", "\n", content, flags=re.I)
        content = re.sub(r"</(p|tr|h\d|div|table|li)>", "\n", content, flags=re.I)
        content = re.sub(r"</(td|th)>", " | ", content, flags=re.I)
        content = _TAG.sub("", content)
        content = re.sub(r"[ \t]*\|[ \t]*$", "", content, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", content).strip()


def _page_text(elements: list) -> str:
    rows = [e for e in elements if isinstance(e, dict)]
    rows.sort(key=lambda e: e.get("index") if isinstance(e.get("index"), int) else 0)
    parts = [_element_text(str(e.get("label") or ""), str(e.get("content") or "")) for e in rows]
    return "\n\n".join(p for p in parts if p)


def _pages_from_payload(payload: dict) -> list[tuple[int, str]]:
    """GLM-OCR returns one markdown string for the whole file. Page text is `layout_details`,
    an array per page, so a quote can still be tied to a page number."""
    details = payload.get("layout_details")
    if isinstance(details, list) and details:
        if all(isinstance(page, list) for page in details):
            return [(i, _page_text(page)) for i, page in enumerate(details, start=1)]
        if all(isinstance(item, dict) for item in details):
            return [(1, _page_text(details))]
    markdown = str(payload.get("md_results") or "").strip()
    if markdown:
        return [(1, markdown)]
    raise ValueError("GLM-OCR returned no pages.")

def _open_ocr_step(db: Database, run_id: str, file_name: str) -> str | None:
    if db is None or not run_id:
        return None 
    step = db.insert_step(run_id, StepType.TOOL, step_name(StepType.TOOL, "document.ocr"), {"file_name": file_name})
    if step is None:
        return None
    return step["id"]

def _close_ocr(
    db: Database, run_id: str | None, step_id: str | None, *,
    file_name: str, pages: int, latency_ms: int, error: Exception | None,
) -> None:
    if db is None or not run_id or not step_id:
        return 
    tool_status = ToolCallStatus.FAILED if error is not None else ToolCallStatus.SUCCEEDED
    step_status = StepStatus.FAILED if error is not None else StepStatus.SUCCEEDED
    result = {"file_name": file_name, "pages": pages}
    failure = None if error is None else tool_error(ToolErrorCode.UPSTREAM, str(error))
    db.insert_tool_call(
        run_id, step_id,
        tool_name="document.ocr",
        args={"file_name": file_name},
        result={} if error is not None else result,
        status=tool_status,
        idempotency_key=f"document.ocr:{run_id}:{file_name}",
        latency_ms=latency_ms,
        error=failure,
    )
    db.finish_step(
        step_id, step_status,
        None if error is not None else result,
        failure,
    )

def ocr_pdf(
    data: bytes, *, db: Database | None = None, run_id: str | None = None, file_name: str = "",
) -> list[tuple[int, str]]:
    """Pages of one PDF from GLM-OCR. Raises ValueError when the key is missing or the call fails."""
    from .llm.client import GLMOCRClient

    key = env("GLM_API_KEY")
    url = env("GLM_BASE_URL")
    if not key or not url:
        raise ValueError("GLM_API_KEY and GLM_BASE_URL must both be set in .env.")
    step_id = _open_ocr_step(db, run_id, file_name)
    started = time.perf_counter()
    try:
        pages = _pages_from_payload(GLMOCRClient(api_key=key, url=url).parse(data))
    except Exception as e:
        _close_ocr(
            db, run_id, step_id, file_name=file_name, pages=0, 
            latency_ms=int((time.perf_counter() - started) * 1000), error=e,
        )
        raise 
    _close_ocr(
        db, run_id, step_id, file_name=file_name, pages=len(pages), 
        latency_ms=int((time.perf_counter() - started) * 1000), error=None,
    )
    return pages

def load_pages(
    file_name: str, data: bytes, *, db: Database | None = None, run_id: str | None = None,
) -> list[tuple[int, str]]:
    """(page_number, text) per page. PDF via GLM-OCR; docx / txt / md stay local.
    
    Pages are coordinates for a later citation. Contract heading splits live in
    `contract.chunks` - this function does not cut on clause numbers.
    """
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"{file_name} is over 10MB")
    name = file_name.lower()
    if name.endswith(".pdf"):
        return ocr_pdf(data, db=db, run_id=run_id, file_name=file_name)
    if name.endswith(".docx"):
        import docx

        d = docx.Document(io.BytesIO(data))
        text = "\n\n".join(p.text for p in d.paragraphs if p.text.strip())
        for t in d.tables:
            text += "\n\n" + "\n".join(" | ".join(c.text.strip() for c in row.cells) for row in t.rows)
        return list(enumerate(_chunk(text), start=1))
    if name.endswith((".txt", ".md")):
        text = data.decode("utf-8", errors="replace")
        parts = [p for p in text.split("\f") if p.strip()] if "\f" in text else _chunk(text)
        return list(enumerate(parts, start=1))
    raise ValueError(f"unsupported document: {file_name} (use {', '.join(SUPPORTED_EXTS)})")


def render_pdf_images(data: bytes, page_numbers: list[int] | None = None) -> dict[int, bytes]:
    """JPEG per PDF page, downscaled so a chart still reads and the request stays small.

    `page_numbers` limits which 1-based pages are rendered. Page text comes from GLM-OCR;
    PyMuPDF is only this picture. Missing it is a hard error.
    """
    try:
        import pymupdf
    except ImportError as err:
        raise ValueError('Reading PDFs needs PyMuPDF: pip install -e ".[contract]"') from err
    wanted = set(page_numbers) if page_numbers else None
    out: dict[int, bytes] = {}
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for i, page in enumerate(doc, start=1):
            if wanted is not None and i not in wanted:
                continue
            wide = page.rect.width or MAX_IMAGE_WIDTH
            dpi = min(150, int(MAX_IMAGE_WIDTH * 72.0 / wide))
            pix = page.get_pixmap(dpi=max(36, dpi))
            out[i] = pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY)
    return out
