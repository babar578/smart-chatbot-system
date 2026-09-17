"""
RAG helpers: chunk text, store embeddings in ChromaDB, retrieve relevant pieces.

Each chat_id has its own documents so conversations stay separate.
Data is saved under ./chroma_db (persistent on disk).
"""

from pathlib import Path
from uuid import uuid4

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

ROOT = Path(__file__).resolve().parent
CHROMA_DIR = ROOT / "chroma_db"  # Folder where Chroma stores vectors
COLLECTION_NAME = "user_docs"

# Lazy singleton — created once on first use
_collection = None


def chunk_text(text, size=800, overlap=120):
    """
    Split long text into overlapping pieces.
    Overlap helps keep meaning when a sentence sits near a chunk boundary.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return []

    chunks = []
    start = 0
    while start < len(cleaned):
        end = start + size
        chunks.append(cleaned[start:end])
        if end >= len(cleaned):
            break
        # Move forward but keep some overlap with the previous chunk
        start = max(end - overlap, start + 1)
    return chunks


def get_collection():
    """Open (or create) the Chroma collection that holds all document chunks."""
    global _collection
    if _collection is None:
        CHROMA_DIR.mkdir(exist_ok=True)
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        _collection = client.get_or_create_collection(
            name=COLLECTION_NAME,
            # DefaultEmbeddingFunction turns text into vectors for similarity search
            embedding_function=DefaultEmbeddingFunction(),
        )
    return _collection


def _ids_for_chat(chat_id):
    """Return Chroma row IDs that belong to this chat."""
    result = get_collection().get(where={"chat_id": chat_id})
    return result.get("ids") or []


def has_documents(chat_id):
    """True if this chat already has indexed document chunks."""
    return len(_ids_for_chat(chat_id)) > 0


def list_documents(chat_id):
    """Unique source file names stored for this chat."""
    if not has_documents(chat_id):
        return []

    result = get_collection().get(where={"chat_id": chat_id}, include=["metadatas"])
    sources = []
    seen = set()
    for meta in result.get("metadatas") or []:
        source = (meta or {}).get("source")
        if source and source not in seen:
            seen.add(source)
            sources.append(source)
    return sources


def add_document(name, text, chat_id, replace=True):
    """
    Chunk the document text and save embeddings into Chroma for this chat.
    If replace=True, old chunks for the chat are removed first.
    """
    source = Path(name or "document.txt").name or "document.txt"
    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("Document is empty.")

    if replace:
        clear_documents(chat_id)

    get_collection().add(
        ids=[f"{chat_id}-{source}-{uuid4()}" for _ in chunks],
        documents=chunks,
        metadatas=[{"source": source, "chat_id": chat_id} for _ in chunks],
    )
    return {"name": source, "chunks": len(chunks), "documents": list_documents(chat_id)}


def clear_documents(chat_id):
    """Delete every chunk belonging to this chat."""
    ids = _ids_for_chat(chat_id)
    if ids:
        get_collection().delete(ids=ids)
    return []


def retrieve(question, chat_id, n_results=8):
    """
    Find document chunks that best match the user's question.

    Small docs (<= 12 chunks): return everything (no need to search).
    Larger docs: semantic similarity search via Chroma.query().
    """
    collection = get_collection()
    if not has_documents(chat_id):
        return [], []

    stored = collection.get(where={"chat_id": chat_id}, include=["documents", "metadatas"])
    stored_docs = stored.get("documents") or []
    if len(stored_docs) <= 12:
        # Short document — send all chunks as context
        documents = stored_docs
        metadatas = stored.get("metadatas") or []
    else:
        # Long document — keep only the most relevant chunks
        result = collection.query(
            query_texts=[question],
            n_results=min(n_results, len(stored_docs)),
            where={"chat_id": chat_id},
        )
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]

    # Collect unique source file names for the UI "Sources:" line
    sources = []
    seen = set()
    for meta in metadatas:
        source = (meta or {}).get("source")
        if source and source not in seen:
            seen.add(source)
            sources.append(source)

    return documents, sources
