"""
Extract readable text from PDF bytes.

Tries methods in order (fastest / cheapest first):
  1. PyMuPDF text layer
  2. pypdf text layer
  3. Local OCR (RapidOCR) for scanned pages
  4. OpenRouter vision model for hard image PDFs
"""

import base64
import os
import re
from io import BytesIO

import pymupdf
import requests
from pypdf import PdfReader

# PDF internals that look like words but are not real document content
SKIP_WORDS = {
    "cid", "obj", "endobj", "stream", "endstream", "xref", "trailer",
    "startxref", "type", "font", "page", "pdf",
}


def _clean(text):
    """Collapse whitespace so previews stay tidy."""
    return " ".join((text or "").split())


def _real_words(text):
    """
    Count words that look like real English (letters + a vowel).
    Filters out PDF garbage so we know if extraction actually worked.
    """
    words = re.findall(r"[A-Za-z]{3,}", text or "")
    good = []
    for word in words:
        lower = word.lower()
        if lower in SKIP_WORDS:
            continue
        if not any(ch in "aeiou" for ch in lower):
            continue
        good.append(word)
    return good


def _usable(text):
    """True when extracted text has enough real words to be useful."""
    return len(_real_words(text)) >= 12


def _extract_pymupdf(raw_bytes):
    """Fast native PDF text extraction (best for normal text PDFs)."""
    doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
    pages = [page.get_text("text") or "" for page in doc]
    doc.close()
    return "\n".join(pages).strip()


def _extract_pypdf(raw_bytes):
    """Fallback text extractor if PyMuPDF finds little/nothing."""
    reader = PdfReader(BytesIO(raw_bytes))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


def _extract_ocr(raw_bytes):
    """
    Render each page as an image and run local OCR.
    Used for scanned / image-only PDFs when no text layer exists.
    """
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
    page_texts = []
    for page in doc:
        # Scale up (3x) so OCR can read small fonts more clearly
        pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False)
        result, _ = ocr(pix.tobytes("png"))
        if not result:
            continue
        lines = [item[1] for item in result if len(item) > 1 and item[1]]
        page_texts.append("\n".join(lines))
    doc.close()
    return "\n".join(page_texts).strip()


def _extract_vision(raw_bytes):
    """
    Last resort: send page images to a vision LLM via OpenRouter.
    Limited to first 5 pages to control cost/time.
    """
    api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is required to read image PDFs.")

    doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
    parts = []
    for index, page in enumerate(doc):
        if index >= 5:
            break
        pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        image_b64 = base64.standard_b64encode(pix.tobytes("png")).decode("ascii")
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Extract ALL visible text from this document page. "
                                    "Keep names, skills, education, jobs, and dates. "
                                    "Return plain text only."
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                            },
                        ],
                    }
                ],
            },
            timeout=90,
        )
        result = response.json()
        if not response.ok:
            error = result.get("error", {})
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ValueError(message or "Vision extract failed.")
        parts.append(result["choices"][0]["message"]["content"] or "")
    doc.close()
    return "\n\n".join(parts).strip()


def extract_pdf_text(raw_bytes):
    """
    Try extractors in order; return the first usable result.
    Raises ValueError if none of the methods produce readable text.
    """
    for method, extractor in (
        ("text", _extract_pymupdf),
        ("text", _extract_pypdf),
        ("ocr", _extract_ocr),
        ("vision", _extract_vision),
    ):
        try:
            text = extractor(raw_bytes)
        except Exception:
            # Vision is the last step — surface its error; otherwise try next method
            if method == "vision":
                raise
            continue
        if _usable(text):
            return {
                "text": text,
                "method": method,  # "text" | "ocr" | "vision" — shown in UI metadata
                "words": len(_real_words(text)),
                "preview": _clean(text)[:240],
            }

    raise ValueError("No readable text found in this PDF.")
