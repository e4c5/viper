"""PostgreSQL + pgvector persistence for context documents and RAG chunks."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from code_review.context.errors import ContextAwareFatalError
from code_review.context.fetchers import FetchedDocument

logger = logging.getLogger(__name__)

# Namespaced table names for dedicated or shared databases.
T_SOURCES = "review_context_sources"
T_DOCUMENTS = "review_context_documents"
T_CHUNKS = "review_context_chunks"

_CACHE_TTL_SECONDS = 3600


def _require_psycopg():
    try:
        import psycopg  # noqa: F401
    except ImportError as e:
        raise ContextAwareFatalError(
            "CONTEXT_AWARE_REVIEW_DB_URL is set but psycopg is not installed. "
            'Install with: pip install -e ".[context]"'
        ) from e


class ContextStore:
    """Cache-backed store; requires PostgreSQL with pgvector extension."""

    def __init__(self, dsn: str, embedding_dimensions: int) -> None:
        _require_psycopg()
        self.dsn = dsn
        self.embedding_dimensions = embedding_dimensions
        self._schema_ok = False

    def connect(self):
        import psycopg

        return psycopg.connect(self.dsn)

    def ensure_schema(self, conn) -> None:
        if self._schema_ok:
            return
        import psycopg

        dim = int(self.embedding_dimensions)
        ddl_sources = f"""
        CREATE TABLE IF NOT EXISTS {T_SOURCES} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name varchar(255) NOT NULL,
            base_url text NOT NULL,
            created_at timestamptz DEFAULT now(),
            UNIQUE (name, base_url)
        );
        """
        ddl_documents = f"""
        CREATE TABLE IF NOT EXISTS {T_DOCUMENTS} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            source_id uuid NOT NULL REFERENCES {T_SOURCES}(id) ON DELETE CASCADE,
            external_id varchar(1024) NOT NULL,
            content text NOT NULL,
            metadata jsonb DEFAULT '{{}}',
            version varchar(255),
            external_updated_at timestamptz,
            last_fetched_at timestamptz DEFAULT now(),
            UNIQUE (source_id, external_id)
        );
        """
        ddl_chunks = f"""
        CREATE TABLE IF NOT EXISTS {T_CHUNKS} (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id uuid NOT NULL REFERENCES {T_DOCUMENTS}(id) ON DELETE CASCADE,
            chunk_index integer NOT NULL,
            content text NOT NULL,
            embedding vector({dim}) NOT NULL,
            metadata jsonb DEFAULT '{{}}',
            UNIQUE (document_id, chunk_index)
        );
        """
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                # pgcrypto provides gen_random_uuid() on PostgreSQL < 13;
                # on PG 13+ it is built-in. Enabling it is harmless either way.
                cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
                cur.execute(ddl_sources)
                cur.execute(ddl_documents)
                cur.execute(ddl_chunks)
                cur.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{T_CHUNKS}_document ON {T_CHUNKS}(document_id)"
                )
            conn.commit()
        except psycopg.Error as e:
            conn.rollback()
            raise ContextAwareFatalError(
                f"Failed to initialize context database schema (pgvector required): {e}"
            ) from e
        # HNSW is optional (may fail on older pgvector); queries still work without it.
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{T_CHUNKS}_embedding_hnsw
                    ON {T_CHUNKS} USING hnsw (embedding vector_cosine_ops)
                    """
                )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(
                "Could not create HNSW index on context chunks (sequential scan): %s",
                e,
            )
        self._schema_ok = True

    def get_or_create_source(self, conn, name: str, base_url: str) -> uuid.UUID:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {T_SOURCES} (name, base_url)
                VALUES (%s, %s)
                ON CONFLICT (name, base_url) DO NOTHING
                RETURNING id
                """,
                (name, base_url),
            )
            row = cur.fetchone()
            if row is None:
                # Row already existed; fetch its id.
                cur.execute(
                    f"SELECT id FROM {T_SOURCES} WHERE name = %s AND base_url = %s",
                    (name, base_url),
                )
                row = cur.fetchone()
        conn.commit()
        return row[0]

    def load_document(
        self, conn, source_id: uuid.UUID, external_id: str
    ) -> tuple[uuid.UUID, str, dict[str, Any], bool] | None:
        """Return (id, content, metadata, fresh_enough) or None if no row."""
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, content, metadata, last_fetched_at
                FROM {T_DOCUMENTS}
                WHERE source_id = %s AND external_id = %s
                """,
                (source_id, external_id),
            )
            row = cur.fetchone()
        if not row:
            return None
        doc_id, content, meta, last_fetch = row
        meta_d = meta if isinstance(meta, dict) else {}
        now = datetime.now(timezone.utc)
        fresh = bool(last_fetch and (now - last_fetch).total_seconds() < _CACHE_TTL_SECONDS)
        return (doc_id, content, meta_d, fresh)

    def count_chunks_for_document(self, conn, document_id: uuid.UUID) -> int:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*) FROM {T_CHUNKS} WHERE document_id = %s",
                (document_id,),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0

    def upsert_document(
        self,
        conn,
        source_id: uuid.UUID,
        doc: FetchedDocument,
    ) -> uuid.UUID:
        meta_json = json.dumps(doc.metadata)
        ext_ts = doc.external_updated_at
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {T_DOCUMENTS} (
                    id, source_id, external_id, content, metadata,
                    version, external_updated_at, last_fetched_at
                )
                VALUES (gen_random_uuid(), %s, %s, %s, %s::jsonb, %s, %s, now())
                ON CONFLICT (source_id, external_id)
                DO UPDATE SET
                    content = EXCLUDED.content,
                    metadata = EXCLUDED.metadata,
                    version = EXCLUDED.version,
                    external_updated_at = EXCLUDED.external_updated_at,
                    last_fetched_at = now()
                RETURNING id
                """,
                (
                    source_id,
                    doc.external_id,
                    doc.body,
                    meta_json,
                    doc.version,
                    ext_ts,
                ),
            )
            doc_id = cur.fetchone()[0]
            cur.execute(f"DELETE FROM {T_CHUNKS} WHERE document_id = %s", (doc_id,))
        conn.commit()
        return doc_id

    def replace_chunks(
        self,
        conn,
        document_id: uuid.UUID,
        chunks: Sequence[tuple[int, str, Sequence[float], dict[str, Any]]],
    ) -> None:
        """chunks: (index, text, embedding vector, metadata dict)."""
        import psycopg

        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {T_CHUNKS} WHERE document_id = %s", (document_id,))
            for idx, text, emb, meta in chunks:
                if len(emb) != self.embedding_dimensions:
                    raise ContextAwareFatalError(
                        f"Embedding length {len(emb)} != CONTEXT_EMBEDDING_DIMENSIONS "
                        f"({self.embedding_dimensions})"
                    )
                vec_lit = "[" + ",".join(str(float(x)) for x in emb) + "]"
                cur.execute(
                    f"""
                    INSERT INTO {T_CHUNKS} (document_id, chunk_index, content, embedding, metadata)
                    VALUES (%s, %s, %s, %s::vector, %s::jsonb)
                    """,
                    (document_id, idx, text, vec_lit, json.dumps(meta)),
                )
        try:
            conn.commit()
        except psycopg.Error as e:
            conn.rollback()
            raise ContextAwareFatalError(f"Failed to store embeddings: {e}") from e

    def search_chunks(
        self,
        conn,
        query_embedding: Sequence[float],
        limit: int = 12,
    ) -> list[str]:
        if len(query_embedding) != self.embedding_dimensions:
            return []
        vec_lit = "[" + ",".join(str(float(x)) for x in query_embedding) + "]"
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT content FROM {T_CHUNKS}
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (vec_lit, limit),
            )
            rows = cur.fetchall()
        return [r[0] for r in rows]
