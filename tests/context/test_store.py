"""Unit tests for code_review.context.store using a fake psycopg connection.

No real database or network is involved; ``FakeConn``/``FakeCursor`` record
executed SQL and return scripted rows.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from code_review.context.errors import ContextAwareFatalError
from code_review.context.fetchers import FetchedDocument
from code_review.context.store import (
    T_CHUNKS,
    T_DOCUMENTS,
    T_SOURCES,
    ContextStore,
    _require_psycopg,
)

psycopg = pytest.importorskip("psycopg")


class FakeCursor:
    """Cursor recording statements and serving queued results."""

    def __init__(self, conn):
        self.conn = conn
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        result_sets = self.conn.results
        self._fetchone_queue = list(result_sets)
        # fetchall consumes the same scripted rows
        self._fetchall = list(result_sets)

    def fetchone(self):
        if self.conn.results:
            rows = self.conn.results.pop(0)
            return rows[0] if rows else None
        return None

    def fetchall(self):
        if self.conn.results:
            return self.conn.results.pop(0)
        return []


class FakeConn:
    def __init__(self, results=None, fail_on_execute=None, fail_on_commit=None):
        self.results = list(results or [])
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.fail_on_execute = fail_on_execute
        self.fail_on_commit = fail_on_commit

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        if self.fail_on_commit:
            raise self.fail_on_commit
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class FailingConn(FakeConn):
    """Connection whose cursor.execute raises on the configured call index."""

    def cursor(self):
        conn = self

        class _Cur(FakeCursor):
            def execute(self, sql, params=None):
                if len(conn.executed) == conn.fail_on_execute:
                    raise conn.error
                super().execute(sql, params)

        return _Cur(self)


def test_require_psycopg_missing_raises_fatal(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ContextAwareFatalError, match="psycopg is not installed"):
        _require_psycopg()


def test_init_requires_psycopg(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ContextAwareFatalError):
        ContextStore("postgresql://x", 768)


def test_connect_uses_dsn(monkeypatch):
    fake = MagicMock()
    monkeypatch.setattr(psycopg, "connect", fake)
    store = ContextStore("postgresql://db", 768)
    conn = store.connect()
    fake.assert_called_once_with("postgresql://db")
    assert conn is fake.return_value


def test_ensure_schema_creates_tables_and_commits():
    store = ContextStore("postgresql://db", 768)
    conn = FakeConn()
    store.ensure_schema(conn)

    sqls = [sql for sql, _ in conn.executed]
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sqls
    assert "CREATE EXTENSION IF NOT EXISTS pgcrypto" in sqls
    assert any(T_SOURCES in s for s in sqls)
    assert any(T_DOCUMENTS in s for s in sqls)
    assert any("vector(768)" in s and T_CHUNKS in s for s in sqls)
    assert any("hnsw" in s.lower() for s in sqls)
    assert conn.commits == 2
    assert conn.rollbacks == 0
    assert store._schema_ok is True


def test_ensure_schema_idempotent():
    store = ContextStore("postgresql://db", 768)
    conn = FakeConn()
    store.ensure_schema(conn)
    store.ensure_schema(conn)
    assert conn.executed  # first call did work
    count = len(conn.executed)
    store.ensure_schema(conn)
    assert len(conn.executed) == count


def test_ensure_schema_ddl_error_rolls_back_and_raises():
    store = ContextStore("postgresql://db", 768)
    conn = FailingConn(fail_on_execute=0)
    conn.error = psycopg.Error("boom")
    with pytest.raises(ContextAwareFatalError, match="pgvector required"):
        store.ensure_schema(conn)
    assert conn.rollbacks == 1
    assert store._schema_ok is False


def test_ensure_schema_hnsw_failure_is_non_fatal(caplog):
    store = ContextStore("postgresql://db", 768)
    conn = FailingConn(fail_on_execute=6)  # the hnsw CREATE INDEX
    conn.error = psycopg.Error("old pgvector")
    with caplog.at_level("WARNING", "code_review.context.store"):
        store.ensure_schema(conn)
    assert conn.rollbacks == 1
    assert store._schema_ok is True
    assert "HNSW" in caplog.text


def test_get_or_create_source_insert_returns_id():
    store = ContextStore("postgresql://db", 768)
    new_id = uuid.uuid4()
    conn = FakeConn(results=[[(new_id,)]])
    out = store.get_or_create_source(conn, "jira", "https://j.example")
    assert out == new_id
    sql, params = conn.executed[0]
    assert f"INSERT INTO {T_SOURCES}" in sql
    assert params == ("jira", "https://j.example")
    assert conn.commits == 1


def test_get_or_create_source_conflict_falls_back_to_select():
    store = ContextStore("postgresql://db", 768)
    existing_id = uuid.uuid4()
    conn = FakeConn(results=[[], [(existing_id,)]])
    out = store.get_or_create_source(conn, "jira", "https://j.example")
    assert out == existing_id
    assert f"SELECT id FROM {T_SOURCES}" in conn.executed[1][0]


def test_load_document_missing_returns_none():
    store = ContextStore("postgresql://db", 768)
    conn = FakeConn(results=[[]])
    assert store.load_document(conn, uuid.uuid4(), "ext-1") is None


def test_load_document_fresh_and_stale():
    store = ContextStore("postgresql://db", 768)
    doc_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    conn = FakeConn(results=[[(doc_id, "body", {"k": 1}, now)]])
    out = store.load_document(conn, uuid.uuid4(), "ext")
    assert out == (doc_id, "body", {"k": 1}, True)

    old = now - timedelta(seconds=7200)
    conn = FakeConn(results=[[(doc_id, "body", "not-a-dict", old)]])
    out = store.load_document(conn, uuid.uuid4(), "ext")
    assert out == (doc_id, "body", {}, False)


def test_load_document_null_timestamp_is_stale():
    store = ContextStore("postgresql://db", 768)
    doc_id = uuid.uuid4()
    conn = FakeConn(results=[[(doc_id, "body", {}, None)]])
    out = store.load_document(conn, uuid.uuid4(), "ext")
    assert out[3] is False


def test_count_chunks_for_document():
    store = ContextStore("postgresql://db", 768)
    doc_id = uuid.uuid4()
    conn = FakeConn(results=[[(7,)]])
    assert store.count_chunks_for_document(conn, doc_id) == 7
    conn = FakeConn(results=[[]])
    assert store.count_chunks_for_document(conn, doc_id) == 0


def test_upsert_document_inserts_and_clears_chunks():
    store = ContextStore("postgresql://db", 768)
    doc_id = uuid.uuid4()
    source_id = uuid.uuid4()
    conn = FakeConn(results=[[(doc_id,)]])
    doc = FetchedDocument(
        external_id="ISS-1",
        title="T",
        body="text body",
        metadata={"type": "issue"},
        version="v2",
        external_updated_at="2026-01-01T00:00:00Z",
    )
    out = store.upsert_document(conn, source_id, doc)
    assert out == doc_id
    insert_sql, insert_params = conn.executed[0]
    assert f"INSERT INTO {T_DOCUMENTS}" in insert_sql
    assert insert_params[0] == source_id
    assert insert_params[1] == "ISS-1"
    assert insert_params[2] == "text body"
    assert '"type": "issue"' in insert_params[3]
    delete_sql, delete_params = conn.executed[1]
    assert f"DELETE FROM {T_CHUNKS}" in delete_sql
    assert delete_params == (doc_id,)
    assert conn.commits == 1


def test_replace_chunks_inserts_vectors_and_commits():
    store = ContextStore("postgresql://db", 3)
    doc_id = uuid.uuid4()
    conn = FakeConn()
    store.replace_chunks(
        conn,
        doc_id,
        [(0, "chunk-a", [0.1, 0.2, 0.3], {"m": 1}), (1, "chunk-b", [0.0] * 3, {})],
    )
    sqls = [sql for sql, _ in conn.executed]
    assert f"DELETE FROM {T_CHUNKS}" in sqls[0]
    inserts = [s for s in sqls if f"INSERT INTO {T_CHUNKS}" in s]
    assert len(inserts) == 2
    assert conn.executed[1][1][3] == "[0.1,0.2,0.3]"
    assert conn.commits == 1


def test_replace_chunks_wrong_embedding_dim_raises():
    store = ContextStore("postgresql://db", 3)
    conn = FakeConn()
    with pytest.raises(ContextAwareFatalError, match="Embedding length 2"):
        store.replace_chunks(conn, uuid.uuid4(), [(0, "x", [0.1, 0.2], {})])


def test_replace_chunks_commit_error_rolls_back():
    store = ContextStore("postgresql://db", 3)
    conn = FakeConn(fail_on_commit=psycopg.Error("commit boom"))
    with pytest.raises(ContextAwareFatalError, match="Failed to store embeddings"):
        store.replace_chunks(conn, uuid.uuid4(), [(0, "x", [0.0] * 3, {})])
    assert conn.rollbacks == 1


def test_search_chunks_dim_mismatch_returns_empty():
    store = ContextStore("postgresql://db", 3)
    conn = FakeConn()
    assert store.search_chunks(conn, [0.1]) == []
    assert conn.executed == []


def test_search_chunks_empty_document_ids_short_circuits():
    store = ContextStore("postgresql://db", 2)
    conn = FakeConn()
    assert store.search_chunks(conn, [0.0, 0.0], document_ids=[]) == []
    assert conn.executed == []


def test_search_chunks_returns_contents():
    store = ContextStore("postgresql://db", 2)
    conn = FakeConn(results=[[("text-1",), ("text-2",)]])
    out = store.search_chunks(conn, [0.1, 0.2], limit=5)
    assert out == ["text-1", "text-2"]
    sql, params = conn.executed[0]
    assert "ORDER BY embedding <=>" in sql
    assert params == ("[0.1,0.2]", 5)


def test_search_chunks_with_document_ids_uses_any():
    store = ContextStore("postgresql://db", 2)
    ids = [uuid.uuid4(), uuid.uuid4()]
    conn = FakeConn(results=[[("t",)]])
    out = store.search_chunks(conn, [0.1, 0.2], limit=3, document_ids=ids)
    assert out == ["t"]
    sql, params = conn.executed[0]
    assert "document_id = ANY" in sql
    assert params[0] == ids
