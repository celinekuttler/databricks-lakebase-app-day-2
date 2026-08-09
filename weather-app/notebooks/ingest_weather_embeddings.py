#!/usr/bin/env python3
"""
Weather documents -> pgvector embeddings (Lakebase).

Plain-Python ETL (no Spark) mirroring notebooks/ingest_ticker_news_embeddings.py,
written with psycopg2 via the same lakebase.get_connection() helper the Flask
app uses.

Pipeline:
1. Ensure `weather_documents` and `weather_embeddings` tables exist (pgvector).
2. Read rows from `weather_documents` that are NOT yet embedded (document ids
   absent from `weather_embeddings`) - makes re-runs idempotent.
3. Chunk `narrative_text` with a sliding window (CHUNK_SIZE / CHUNK_OVERLAP).
4. Embed each chunk with `embeddings.embed_texts` (all-MiniLM-L6-v2, 384-dim,
   model loaded once).
5. Batch-upsert into `weather_embeddings` with psycopg2.extras.execute_values,
   casting the embedding column via `%s::vector`.

Run locally (with LAKEBASE_URL set):
    python notebooks/ingest_weather_embeddings.py
"""

import os
import sys
from pathlib import Path

# Make `import lakebase` / `import embeddings` resolve from the repo root
# regardless of the working directory. Inside a Databricks notebook `__file__`
# is not defined, so fall back to probing the current working directory (and
# its parent) for the repo marker file.
def _repo_root() -> Path:
    try:
        return Path(__file__).resolve().parent.parent
    except NameError:
        pass
    for candidate in (Path(os.getcwd()), Path(os.getcwd()).parent):
        if (candidate / "lakebase.py").exists():
            return candidate
    return Path(os.getcwd())


sys.path.insert(0, str(_repo_root()))

# Ensure sentence-transformers is installed (for interactive runs)
try:
    import sentence_transformers
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "sentence-transformers>=3.0.0"])

from psycopg2.extras import execute_values

import embeddings
import lakebase

# ---------------------------------------------------------------------------
# Config (widget params when run as a Databricks job, env vars when run
# locally - mirrors the widget params of the news notebook)
# ---------------------------------------------------------------------------

def _dbutils():
    """Return the Databricks dbutils object if running inside a notebook/job."""
    try:
        from databricks.sdk.runtime import dbutils
        return dbutils
    except Exception:
        try:
            import IPython

            ipy = IPython.get_ipython()
            return ipy.user_ns.get("dbutils")
        except Exception:
            return None


def _config(name: str, default: str) -> str:
    """Read a config value from a notebook widget (job) or an env var (local)."""
    db = _dbutils()
    if db is not None:
        try:
            return db.widgets.get(name)
        except Exception:
            pass
    return os.environ.get(name, default)


WEATHER_TABLE_NAME = _config("weather_table_name", "weather_documents")
WEATHER_EMBEDDINGS_TABLE_NAME = _config(
    "weather_embeddings_table_name", "weather_embeddings"
)
EMBEDDING_MODEL = _config("embedding_model", embeddings.MODEL_NAME)
EMBEDDING_DIM = embeddings.EMBEDDING_DIM
CHUNK_SIZE = int(_config("chunk_size", "800"))
CHUNK_OVERLAP = int(_config("chunk_overlap", "100"))
BATCH_SIZE = int(_config("batch_size", "64"))

if EMBEDDING_MODEL != embeddings.MODEL_NAME:
    raise ValueError(
        f"EMBEDDING_MODEL={EMBEDDING_MODEL!r} != {embeddings.MODEL_NAME!r}. "
        f"The search endpoint embeds queries with {embeddings.MODEL_NAME!r}; "
        f"ingesting with a different model would make similarity search "
        f"meaningless. Update embeddings.py if you want a different model."
    )


def ensure_tables():
    """Create weather_documents and weather_embeddings (pgvector) if missing."""
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {WEATHER_TABLE_NAME} (
                    id TEXT PRIMARY KEY,
                    location TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    headline TEXT NOT NULL,
                    narrative_text TEXT NOT NULL,
                    effective_at TIMESTAMPTZ,
                    payload JSONB NOT NULL,
                    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {WEATHER_EMBEDDINGS_TABLE_NAME} (
                    id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES {WEATHER_TABLE_NAME}(id),
                    chunk_index INT NOT NULL,
                    chunk_text TEXT NOT NULL,
                    embedding VECTOR({EMBEDDING_DIM}) NOT NULL,
                    model_name TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{WEATHER_EMBEDDINGS_TABLE_NAME}_embedding
                ON {WEATHER_EMBEDDINGS_TABLE_NAME}
                USING hnsw (embedding vector_cosine_ops)
                """
            )
            conn.commit()


def load_unembedded_documents() -> list[dict]:
    """Rows from weather_documents with no corresponding embedding yet."""
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT d.id, d.location, d.source_type, d.headline, d.narrative_text
                FROM {WEATHER_TABLE_NAME} d
                WHERE TRIM(d.narrative_text) != ''
                  AND d.id NOT IN (
                      SELECT e.document_id FROM {WEATHER_EMBEDDINGS_TABLE_NAME} e
                  )
                ORDER BY d.synced_at ASC
                """
            )
            return cur.fetchall()


def chunk_text(text: str) -> list[str]:
    """
    Sliding-window chunker. Text shorter than CHUNK_SIZE stays as a single
    chunk; longer text is split into overlapping CHUNK_SIZE windows with
    CHUNK_OVERLAP characters of overlap.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= CHUNK_SIZE:
        return [text]
    chunks = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    for start in range(0, len(text), step):
        chunk = text[start : start + CHUNK_SIZE].strip()
        if chunk:
            chunks.append(chunk)
        if start + CHUNK_SIZE >= len(text):
            break
    return chunks


def upsert_embeddings(rows: list[tuple]) -> int:
    """
    Batch-upsert (document_id, chunk_index, chunk_text, embedding) tuples into
    weather_embeddings via execute_values, casting embedding via %s::vector.
    Returns the number of rows written (duplicates skipped via ON CONFLICT).
    """
    if not rows:
        return 0
    insert_sql = f"""
        INSERT INTO {WEATHER_EMBEDDINGS_TABLE_NAME} (
            id, document_id, chunk_index, chunk_text, embedding, model_name, created_at
        ) VALUES %s
        ON CONFLICT (id) DO NOTHING
    """
    template = "(%s, %s, %s, %s, %s::vector, %s, now())"
    with lakebase.get_connection() as conn:
        with conn.cursor() as cur:
            execute_values(
                cur, insert_sql, rows, template=template, page_size=min(200, BATCH_SIZE)
            )
            conn.commit()
            return cur.rowcount


def main() -> int:
    print(f"Model: {EMBEDDING_MODEL} -> {EMBEDDING_DIM} dims")
    print(f"Chunking: size={CHUNK_SIZE}, overlap={CHUNK_OVERLAP}")
    ensure_tables()

    documents = load_unembedded_documents()
    print(f"Found {len(documents)} unembedded document(s) in {WEATHER_TABLE_NAME}")

    total_chunks = 0
    rows_to_insert: list[tuple] = []

    for doc in documents:
        chunks = chunk_text(doc["narrative_text"])
        for chunk_index, chunk in enumerate(chunks):
            rows_to_insert.append(
                (
                    f'{doc["id"]}_{chunk_index}',
                    doc["id"],
                    chunk_index,
                    chunk,
                    None,  # embedding filled below
                    EMBEDDING_MODEL,
                )
            )
        total_chunks += len(chunks)

    print(f"Produced {total_chunks} chunk(s) across {len(documents)} document(s)")

    written = 0
    for i in range(0, len(rows_to_insert), BATCH_SIZE):
        batch = rows_to_insert[i : i + BATCH_SIZE]
        texts = [row[3] for row in batch]
        vectors = embeddings.embed_texts(texts)
        # Stringified array literal + %s::vector cast (see upsert_embeddings).
        db_rows = [
            (row[0], row[1], row[2], row[3], embeddings.to_vector_literal(v), row[5])
            for row, v in zip(batch, vectors)
        ]
        written += upsert_embeddings(db_rows)
        print(f"  Wrote {written}/{total_chunks} embeddings")

    print(f"Done. {written} embeddings written to {WEATHER_EMBEDDINGS_TABLE_NAME}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
