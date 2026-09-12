from pathlib import Path
import re

from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import requests
from dotenv import load_dotenv

from pdf_extract import extract_pdf_text
from rag import add_document, clear_documents, has_documents, list_documents, retrieve

load_dotenv(Path(__file__).resolve().parent / ".env")

app = Flask(__name__)
CORS(app)


def chat_id_from(raw):
    value = (raw or "default").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        return "default"
    return value


def extract_file_text(filename, raw_bytes):
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
    return jsonify({"message": "Backend is running!", "rag": True})


@app.route("/documents", methods=["GET"])
def documents():
    chat_id = chat_id_from(request.args.get("chat_id"))
    return jsonify({"documents": list_documents(chat_id)})


@app.route("/documents", methods=["DELETE"])
def reset_documents():
    chat_id = chat_id_from(request.args.get("chat_id"))
    return jsonify({"documents": clear_documents(chat_id)})


@app.route("/upload", methods=["POST"])
def upload():
    try:
        if "file" in request.files:
            uploaded = request.files["file"]
            name = uploaded.filename or "document.txt"
            extracted = extract_file_text(name, uploaded.read())
            chat_id = chat_id_from(request.form.get("chat_id"))
        else:
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
    data = request.get_json(silent=True) or {}
    user_message = (data.get("message") or "").strip()
    chat_id = chat_id_from(data.get("chat_id"))

    if not user_message:
        return jsonify({"error": "Message is required"}), 400

    api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()

    if not api_key:
        return jsonify({"error": "OPENROUTER_API_KEY is missing from the .env file"}), 500

    messages = [{"role": "user", "content": user_message}]
    sources = []

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

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:5173",
        "X-Title": "RAG LLM Chatbot",
    }

    payload = {
        "model": "openai/gpt-3.5-turbo",
        "messages": messages,
    }

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )
        result = response.json()

        if response.ok and result.get("choices"):
            bot_reply = result["choices"][0]["message"]["content"]
            return jsonify({"reply": bot_reply, "sources": sources})

        error = result.get("error", "OpenRouter request failed")
        if isinstance(error, dict):
            error = error.get("message") or str(error)
        return jsonify({"error": error}), response.status_code or 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(
        debug=True,
        use_reloader=False,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5000")),
    )
