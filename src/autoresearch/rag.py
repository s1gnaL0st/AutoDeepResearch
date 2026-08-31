"""Evidence-first retrieval augmented generation primitives.

The index is deliberately independent of any LLM vendor.  It stores immutable
paper chunks and embeddings in PostgreSQL and combines vector cosine similarity
with PostgreSQL full-text/lexical scoring.  A deterministic hashing embedder is
included for offline smoke tests; production deployments can inject a real
embedding callable (for example a local BGE model).
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable


def chunk_text(text: str, *, chunk_size: int = 900, overlap: int = 120) -> list[str]:
    if chunk_size < 64 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("chunk_size must be >=64 and overlap must be in [0, chunk_size)")
    words = text.split()
    chunks: list[str] = []
    step = max(1, chunk_size - overlap)
    # Character windows preserve readable passage boundaries while remaining
    # deterministic across Python versions.
    for start in range(0, len(text), step):
        piece = text[start:start + chunk_size].strip()
        if piece:
            chunks.append(piece)
        if start + chunk_size >= len(text):
            break
    return chunks


class HashingEmbedder:
    """Dependency-free baseline embedder for tests and offline operation."""
    dimension = 256

    def __call__(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in re.findall(r"\w+", text.casefold()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    document_id: str
    text: str
    locator: dict[str, Any]
    score: float
    lexical_score: float
    vector_score: float
    source: str = "rag"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class RAGIndex:
    """In-process index used by the workflow and PostgreSQL adapter."""

    def __init__(self, embedder: Callable[[str], list[float]] | None = None) -> None:
        self.embedder = embedder or HashingEmbedder()
        self.rows: list[dict[str, Any]] = []

    def add_document(self, document_id: str, text: str, locator: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None) -> int:
        added = 0
        for index, piece in enumerate(chunk_text(text)):
            chunk_id = hashlib.sha256(f"{document_id}:{index}:{piece}".encode()).hexdigest()[:24]
            if any(row["chunk_id"] == chunk_id for row in self.rows):
                continue
            self.rows.append({"chunk_id": chunk_id, "document_id": document_id, "text": piece,
                              "locator": {**(locator or {}), "chunk_index": index},
                              "embedding": self.embedder(piece), "metadata": metadata or {}})
            added += 1
        return added

    def search(self, query: str, top_k: int = 8) -> list[RetrievedChunk]:
        if top_k < 1:
            raise ValueError("top_k must be positive")
        qv = self.embedder(query)
        terms = set(re.findall(r"\w+", query.casefold()))
        scored = []
        for row in self.rows:
            rv = row["embedding"]
            vector_score = sum(a * b for a, b in zip(qv, rv))
            text_terms = set(re.findall(r"\w+", row["text"].casefold()))
            lexical_score = len(terms & text_terms) / max(1, len(terms))
            score = 0.7 * vector_score + 0.3 * lexical_score
            scored.append(RetrievedChunk(row["chunk_id"], row["document_id"], row["text"], row["locator"], score, lexical_score, vector_score))
        return sorted(scored, key=lambda item: (-item.score, item.chunk_id))[:top_k]


class PostgresRAGStore:
    """Persist chunks in PostgreSQL; uses JSONB vectors until pgvector exists.

    When the `vector` extension is installed, callers may migrate the JSONB
    column to `vector(n)` and add an HNSW index without changing the API.
    """

    def __init__(self, dsn: str, embedder: Callable[[str], list[float]] | None = None):
        import psycopg
        self.psycopg = psycopg
        self.dsn = dsn
        self.embedder = embedder or HashingEmbedder()
        with self.psycopg.connect(dsn) as conn:
            self.vector_available = bool(conn.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')").fetchone()[0])
            self.backend = "pgvector" if self.vector_available else "postgres_jsonb_compat"
            conn.execute("CREATE TABLE IF NOT EXISTS autoresearch_rag_chunks (chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, text TEXT NOT NULL, locator JSONB NOT NULL, embedding JSONB NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb)")
            conn.execute("CREATE INDEX IF NOT EXISTS autoresearch_rag_chunks_document_idx ON autoresearch_rag_chunks(document_id)")

    def index_documents(self, documents: Iterable[dict[str, Any]]) -> int:
        index = RAGIndex(self.embedder)
        for document in documents:
            index.add_document(str(document.get("document_id")), str(document.get("text", "")), document.get("locator"), document.get("metadata"))
        with self.psycopg.connect(self.dsn) as conn:
            for row in index.rows:
                conn.execute("INSERT INTO autoresearch_rag_chunks (chunk_id,document_id,text,locator,embedding,metadata) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (chunk_id) DO NOTHING", (row["chunk_id"], row["document_id"], row["text"], self.psycopg.types.json.Json(row["locator"]), self.psycopg.types.json.Json(row["embedding"]), self.psycopg.types.json.Json(row["metadata"])))
        return len(index.rows)

    def search(self, query: str, top_k: int = 8) -> list[RetrievedChunk]:
        index = RAGIndex(self.embedder)
        with self.psycopg.connect(self.dsn) as conn:
            rows = conn.execute("SELECT chunk_id,document_id,text,locator,embedding,metadata FROM autoresearch_rag_chunks").fetchall()
        index.rows = [{"chunk_id": r[0], "document_id": r[1], "text": r[2], "locator": r[3], "embedding": r[4], "metadata": r[5]} for r in rows]
        return index.search(query, top_k)
