import base64
import os
import re
from io import BytesIO

import pymupdf
import requests
from pypdf import PdfReader

SKIP_WORDS = {
    "cid", "obj", "endobj", "stream", "endstream", "xref", "trailer",
    "startxref", "type", "font", "page", "pdf",
}


def _clean(text):
    return " ".join((text or "").split())


def _real_words(text):
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
    return len(_real_words(text)) >= 12


def _extract_pymupdf(raw_bytes):
    doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
    pages = [page.get_text("text") or "" for page in doc]
    doc.close()
    return "\n".join(pages).strip()


def _extract_pypdf(raw_bytes):
    reader = PdfReader(BytesIO(raw_bytes))
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages).strip()


def _extract_ocr(raw_bytes):
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    doc = pymupdf.open(stream=raw_bytes, filetype="pdf")
    page_texts = []
    for page in doc:
        pix = page.get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False)
        result, _ = ocr(pix.tobytes("png"))
        if not result:
            continue
        lines = [item[1] for item in result if len(item) > 1 and item[1]]
        page_texts.append("\n".join(lines))
    doc.close()
    return "\n".join(page_texts).strip()


def _extract_vision(raw_bytes):
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
    for method, extractor in (
        ("text", _extract_pymupdf),
        ("text", _extract_pypdf),
        ("ocr", _extract_ocr),
        ("vision", _extract_vision),
    ):
        try:
            text = extractor(raw_bytes)
        except Exception:
            if method == "vision":
                raise
            continue
        if _usable(text):
            return {
                "text": text,
                "method": method,
                "words": len(_real_words(text)),
                "preview": _clean(text)[:240],
            }

    raise ValueError("No readable text found in this PDF.")
