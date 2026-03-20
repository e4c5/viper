# PGVector Database Schema for Context-Aware Review

This document defines the normalized PostgreSQL schema with `pgvector` for storing and retrieving context (GitHub Issues, Jira tickets, Confluence pages) used in code reviews.

**Implementation note:** The agent uses tables named `review_context_sources`, `review_context_documents`, and `review_context_chunks` (same columns and relationships as below) to avoid collisions in shared databases.

## Extensions
The following extensions must be enabled in the PostgreSQL database:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

## Schema Diagrams

### Entities and Relationships

- **`sources`**: Metadata about context sources (GitHub, Jira, Confluence).
- **`documents`**: The main content fetched from a source (e.g., a specific Jira ticket).
- **`chunks`**: Split segments of a document with their corresponding vector embeddings for semantic search.

---

## Table Definitions

### 1. `sources`
Stores configuration and metadata for different context providers.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `uuid` | `PRIMARY KEY, DEFAULT gen_random_uuid()` | Unique identifier for the source. |
| `name` | `varchar(255)` | `NOT NULL` | Name of the source (e.g., 'github', 'jira', 'confluence'). |
| `base_url` | `text` | `NOT NULL` | The base URL for the source API/web interface. |
| `created_at` | `timestamp with time zone` | `DEFAULT now()` | Record creation timestamp. |

**Indexes:**
- `UNIQUE (name, base_url)`: Prevents cross-instance cache collisions when multiple GitHub/Jira/Confluence roots share the same database.

### 2. `documents`
Stores the normalized content of a fetched reference.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `uuid` | `PRIMARY KEY, DEFAULT gen_random_uuid()` | Unique identifier for the document. |
| `source_id` | `uuid` | `REFERENCES sources(id) ON DELETE CASCADE` | Link to the source. |
| `external_id` | `varchar(1024)` | `NOT NULL` | The ID in the remote system (e.g., 'PROJ-123', 'issue#42'). |
| `content` | `text` | `NOT NULL` | The full normalized text/markdown content. |
| `metadata` | `jsonb` | `DEFAULT '{}'` | Additional structured data (labels, status, author, etc.). |
| `version` | `varchar(255)` | | Remote version identifier if available. |
| `external_updated_at` | `timestamp with time zone` | | When the document was last updated in the remote system. |
| `last_fetched_at` | `timestamp with time zone` | `DEFAULT now()` | When we last synchronized this document. |

**Indexes:**
- `UNIQUE (source_id, external_id)`: Ensures we don't duplicate documents from the same source.

### 3. `chunks`
Segments of a document used for vector similarity search.

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | `uuid` | `PRIMARY KEY, DEFAULT gen_random_uuid()` | Unique identifier for the chunk. |
| `document_id` | `uuid` | `REFERENCES documents(id) ON DELETE CASCADE` | Link to the parent document. |
| `chunk_index` | `integer` | `NOT NULL` | The order of the chunk within the document. |
| `content` | `text` | `NOT NULL` | The text content of this specific chunk. |
| `embedding` | `vector(1536)` | `NOT NULL` | The vector embedding (size depends on the model, e.g., 1536 for OpenAI `text-embedding-3-small`). |
| `metadata` | `jsonb` | `DEFAULT '{}'` | Chunk-specific metadata (e.g., section headers). |

**Indexes:**
- `document_id`: For fast retrieval of all chunks belonging to a document.
- `embedding`: HNSW or IVFFlat index for fast similarity search.

```sql
CREATE INDEX ON review_context_chunks USING hnsw (embedding vector_cosine_ops);
```

---

## Data Integrity and Performance

1. **Normalization**: Content is stored once in `documents`, while `chunks` only store segments required for RAG.
2. **Freshness**: Use `external_updated_at` and `last_fetched_at` to determine if a document needs re-fetching.
3. **Cascading Deletes**: Deleting a source or document automatically cleans up associated documents and chunks.
4. **Vector Model**: The `vector(N)` size should be adjusted based on the specific embedding model configured in `LLM_MODEL` or a dedicated embedding setting.
