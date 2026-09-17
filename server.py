"""
Flask API for the RAG chatbot.

Flow:
  1. Frontend uploads a document  -> /upload  (text is extracted + stored in Chroma)
  2. User asks a question         -> /chat
       - with docs: retrieve chunks (RAG) + LLM
       - current affairs / use_web: live web search + LLM (not model memory)
"""

from pathlib import Path
import re

from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import requests
from dotenv import load_dotenv

from pdf_extract import extract_pdf_text
from rag import add_document, clear_documents, has_documents, list_documents, retrieve
from web_search import (
    format_web_context,
    needs_live_info,
    search_web,
    source_labels,
)

# Load OPENROUTER_API_KEY and PORT from the project .env file
load_dotenv(Path(__file__).resolve().parent / ".env")

app = Flask(__name__)
CORS(app)  # Allow the React frontend (port 5173) to call this API

# Prefer a stronger OpenRouter model; override with OPENROUTER_MODEL in .env
DEFAULT_MODEL = "openai/gpt-4o-mini"


def chat_id_from(raw):
    """Sanitize chat_id so each conversation keeps its own documents."""
    value = (raw or "default").strip()
    # Only allow safe characters; fall back to "default" if invalid
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        return "default"
    return value


def extract_file_text(filename, raw_bytes):
    """
    Turn uploaded file bytes into plain text.
    PDFs use pdf_extract (text/OCR/vision). TXT/MD are decoded as UTF-8.
    """
    name = (filename or "document.txt").lower()
    if name.endswith(".pdf"):
        return extract_pdf_text(raw_bytes)
    if name.endswith(".txt") or name.endswith(".md"):
        text = raw_bytes.decode("utf-8", errors="replace")
        return {
            "text": text,
            "method": "text",
            "words": len(re.findall(r"[A-Za-z]{3,}", text)),
            "preview": " ".join(text.split())[:240],
        }
    raise ValueError("Only .txt and .pdf files are supported.")


@app.route("/")
def home():
    """Health check — confirms the backend is up."""
    return jsonify({"message": "Backend is running!", "rag": True})


@app.route("/documents", methods=["GET"])
def documents():
    """List document names stored for this chat."""
    chat_id = chat_id_from(request.args.get("chat_id"))
    return jsonify({"documents": list_documents(chat_id)})


@app.route("/documents", methods=["DELETE"])
def reset_documents():
    """Remove all indexed chunks for this chat (e.g. when a chat is deleted)."""
    chat_id = chat_id_from(request.args.get("chat_id"))
    return jsonify({"documents": clear_documents(chat_id)})


@app.route("/upload", methods=["POST"])
def upload():
    """
    Accept a file upload OR pasted text, extract content, then store it in Chroma.
    replace=True means a new upload replaces the previous document for that chat.
    """
    try:
        # Multipart file upload from the frontend
        if "file" in request.files:
            uploaded = request.files["file"]
            name = uploaded.filename or "document.txt"
            extracted = extract_file_text(name, uploaded.read())
            chat_id = chat_id_from(request.form.get("chat_id"))
        else:
            # JSON body with pasted text (optional path)
            data = request.get_json(silent=True) or {}
            name = data.get("name") or "pasted.txt"
            pasted = data.get("text") or ""
            extracted = {
                "text": pasted,
                "method": "text",
                "words": len(re.findall(r"[A-Za-z]{3,}", pasted)),
                "preview": " ".join(pasted.split())[:240],
            }
            chat_id = chat_id_from(data.get("chat_id"))

        # Chunk + embed + save into the vector DB for this chat
        result = add_document(name, extracted["text"], chat_id, replace=True)
        result.update({
            "method": extracted.get("method"),
            "words": extracted.get("words"),
            "preview": extracted.get("preview"),
        })
        return jsonify(result)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/chat", methods=["POST"])
def chat():
    """
    Answer a user question.
    Priority:
      1) Uploaded documents → RAG over Chroma chunks
      2) use_web / current-affairs intent → live web search, then LLM
      3) Otherwise → normal LLM chat
    """
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    chat_id = chat_id_from(data.get("chat_id"))
    # Frontend toggle; also auto-enable for news/market style questions
    use_web = bool(data.get("use_web")) or needs_live_info(user_message)

    if not user_message:
        return jsonify({"error": "Message is required"}), 400

    api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()

    if not api_key:
        return jsonify({"error": "OPENROUTER_API_KEY is missing from the .env file"}), 500

    # Default: plain chat with no external context
    messages = [{"role": "user", "content": user_message}]
    sources = []
    mode = "chat"
    web_provider = None

    # RAG path: pull matching document chunks and put them in a system prompt
    if has_documents(chat_id):
        chunks, sources = retrieve(user_message, chat_id)
        context = "\n\n".join(chunks) if chunks else "No matching text was found in the uploaded documents."
        messages = [
            {
                "role": "system",
                "content": (
                    "You are analyzing an uploaded document. Use the context below. "
                    "If a detail is missing from the context, say you don't know.\n\n"
                    f"Context:\n{context}"
                ),
            },
            {"role": "user", "content": user_message},
        ]
        mode = "rag"
    elif use_web:
        # Live web path: search sites first, then answer only from those results
        try:
            results, web_provider = search_web(user_message, max_results=5)
        except Exception as e:
            return jsonify({"error": f"Web search failed: {e}"}), 502

        context = format_web_context(results)
        sources = source_labels(results)
        messages = [
            {
                "role": "system",
                "content": (
                    "You answer using ONLY the live web search results below. "
                    "Do not rely on your training memory for facts. "
                    "If the results are missing or insufficient, say you could not find "
                    "reliable live information. Mention dates when present. "
                    "Cite source titles/URLs briefly.\n\n"
                    f"Web search results:\n{context}"
                ),
            },
            {"role": "user", "content": user_message},
        ]
        mode = "web"

    model = (os.getenv("OPENROUTER_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:5173",
        "X-Title": "RAG LLM Chatbot",
    }

    payload = {
        "model": model,
        "messages": messages,
    }

    try:
        # Call OpenRouter (OpenAI-compatible chat completions API)
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )
        result = response.json()

        if response.ok and result.get("choices"):
            bot_reply = result["choices"][0]["message"]["content"]
            return jsonify({
                "reply": bot_reply,
                "sources": sources,
                "mode": mode,
                "web_provider": web_provider,
            })

        error = result.get("error", "OpenRouter request failed")
        if isinstance(error, dict):
            error = error.get("message") or str(error)
        return jsonify({"error": error}), response.status_code or 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    # Start local API on http://127.0.0.1:5000 by default
    app.run(
        debug=True,
        use_reloader=False,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
    )
