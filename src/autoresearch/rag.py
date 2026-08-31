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


class SentenceTransformerEmbedder:
    """Optional local semantic embedder.

    It is intentionally opt-in because model downloads are large and must be
    approved by the operator.  The selected model must produce 256 dimensions
    when used with the bundled pgvector schema (or be wrapped by a project
    specific projection before indexing).
    """

    def __init__(self, model_name: str):
        from sentence_transformers import SentenceTransformer
        self.model_name = model_name
        self._model = SentenceTransformer(model_name)
        self.dimension = int(self._model.get_sentence_embedding_dimension())

    def __call__(self, text: str) -> list[float]:
        values = self._model.encode(text, normalize_embeddings=True)
        return [float(value) for value in values]


def configured_embedder() -> tuple[Callable[[str], list[float]], str, str | None]:
    """Build the explicitly configured local embedder.

    Returns ``(embedder, model_label, configuration_error)``.  No model is
    downloaded implicitly: setting ``AUTORESEARCH_EMBEDDING_MODEL`` is the
    operator's opt-in and the package must already be installed/cache-ready.
    """
    import os
    model_name = os.environ.get("AUTORESEARCH_EMBEDDING_MODEL", "").strip()
    if not model_name:
        return HashingEmbedder(), "hashing-256-offline-baseline", None
    try:
        embedder = SentenceTransformerEmbedder(model_name)
        if embedder.dimension != HashingEmbedder.dimension:
            return HashingEmbedder(), f"hashing-256-fallback (requested {model_name}; dimension={embedder.dimension})", "configured embedding dimension must be 256 for the pgvector schema"
        return embedder, model_name, None
    except Exception as exc:
        return HashingEmbedder(), f"hashing-256-fallback (requested {model_name})", f"{type(exc).__name__}: {exc}"


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
    """Persist chunks in PostgreSQL with pgvector and FTS hybrid retrieval.

    JSONB keeps an explicit portable fallback for developer machines where the
    PostgreSQL server extension is not installed. Once pgvector is present,
    this class creates a `vector(256)` column and HNSW cosine index itself.
    """

    def __init__(self, dsn: str, embedder: Callable[[str], list[float]] | None = None):
        import psycopg
        self.psycopg = psycopg
        self.dsn = dsn
        self.embedder = embedder or HashingEmbedder()
        with self.psycopg.connect(dsn) as conn:
            installed = bool(conn.execute("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector')").fetchone()[0])
            available = bool(conn.execute("SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name='vector')").fetchone()[0])
            if available and not installed:
                # Extension installation requires database ownership/superuser
                # privileges.  A failed attempt must be rolled back before
                # issuing the normal schema statements on this connection.
                try:
                    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    conn.commit()
                    installed = True
                except Exception:
                    conn.rollback()
                    installed = False
            self.vector_available = installed
            self.backend = "pgvector" if installed else "postgres_jsonb_compat"
            conn.execute("CREATE TABLE IF NOT EXISTS autoresearch_rag_chunks (chunk_id TEXT PRIMARY KEY, document_id TEXT NOT NULL, text TEXT NOT NULL, locator JSONB NOT NULL, embedding JSONB NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb)")
            conn.execute("CREATE INDEX IF NOT EXISTS autoresearch_rag_chunks_document_idx ON autoresearch_rag_chunks(document_id)")
            if installed:
                conn.execute("ALTER TABLE autoresearch_rag_chunks ADD COLUMN IF NOT EXISTS embedding_vector vector(256)")
                # Migrate chunks written by the JSONB compatibility backend.
                # The stored array is deterministic and can be cast directly
                # to pgvector without re-embedding documents.
                conn.execute("UPDATE autoresearch_rag_chunks SET embedding_vector = (embedding::text)::vector WHERE embedding_vector IS NULL")
                conn.execute("CREATE INDEX IF NOT EXISTS autoresearch_rag_chunks_hnsw_idx ON autoresearch_rag_chunks USING hnsw (embedding_vector vector_cosine_ops)")

    @staticmethod
    def _vector_literal(vector: list[float]) -> str:
        return "[" + ",".join(f"{item:.9g}" for item in vector) + "]"

    def index_documents(self, documents: Iterable[dict[str, Any]]) -> int:
        index = RAGIndex(self.embedder)
        for document in documents:
            index.add_document(str(document.get("document_id")), str(document.get("text", "")), document.get("locator"), document.get("metadata"))
        with self.psycopg.connect(self.dsn) as conn:
            for row in index.rows:
                if self.vector_available:
                    conn.execute("INSERT INTO autoresearch_rag_chunks (chunk_id,document_id,text,locator,embedding,metadata,embedding_vector) VALUES (%s,%s,%s,%s,%s,%s,%s::vector) ON CONFLICT (chunk_id) DO NOTHING", (row["chunk_id"], row["document_id"], row["text"], self.psycopg.types.json.Json(row["locator"]), self.psycopg.types.json.Json(row["embedding"]), self.psycopg.types.json.Json(row["metadata"]), self._vector_literal(row["embedding"])))
                else:
                    conn.execute("INSERT INTO autoresearch_rag_chunks (chunk_id,document_id,text,locator,embedding,metadata) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (chunk_id) DO NOTHING", (row["chunk_id"], row["document_id"], row["text"], self.psycopg.types.json.Json(row["locator"]), self.psycopg.types.json.Json(row["embedding"]), self.psycopg.types.json.Json(row["metadata"])))
        return len(index.rows)

    def search(self, query: str, top_k: int = 8) -> list[RetrievedChunk]:
        if self.vector_available:
            vector = self._vector_literal(self.embedder(query))
            with self.psycopg.connect(self.dsn) as conn:
                rows = conn.execute("""
                    WITH scored AS (
                      SELECT chunk_id, document_id, text, locator,
                        1 - (embedding_vector <=> %s::vector) AS vector_score,
                        ts_rank_cd(to_tsvector('simple', text), websearch_to_tsquery('simple', %s)) AS lexical_score
                      FROM autoresearch_rag_chunks
                      WHERE embedding_vector IS NOT NULL
                    )
                    SELECT chunk_id, document_id, text, locator, vector_score, lexical_score,
                      0.7 * vector_score + 0.3 * lexical_score AS score
                    FROM scored ORDER BY score DESC, chunk_id ASC LIMIT %s
                """, (vector, query, top_k)).fetchall()
            return [RetrievedChunk(row[0], row[1], row[2], row[3], float(row[6]), float(row[5]), float(row[4])) for row in rows]
        index = RAGIndex(self.embedder)
        with self.psycopg.connect(self.dsn) as conn:
            rows = conn.execute("SELECT chunk_id,document_id,text,locator,embedding,metadata FROM autoresearch_rag_chunks").fetchall()
        index.rows = [{"chunk_id": r[0], "document_id": r[1], "text": r[2], "locator": r[3], "embedding": r[4], "metadata": r[5]} for r in rows]
        return index.search(query, top_k)
