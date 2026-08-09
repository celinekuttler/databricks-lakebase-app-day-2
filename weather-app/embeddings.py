"""
Shared sentence-transformer helper used by BOTH the Flask app (POST /weather/search)
and the ingest script (notebooks/ingest_weather_embeddings.py).

The model is loaded ONCE (lazily, on first use) and cached in a module-level
global so the search endpoint does not reload it per request.

The model is pinned to `sentence-transformers/all-MiniLM-L6-v2` (384-dim) to
stay compatible with the existing news pipeline, so both can share the same
pgvector column conventions and distance operator (cosine).
"""

import os
from functools import lru_cache
from typing import Any

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# Keep HuggingFace caches under an explicit dir so notebook runtimes (which
# mount /tmp) don't have to re-download the model on every run.
os.environ.setdefault("HF_HOME", "/tmp/.cache/huggingface")
os.environ.setdefault("TRANSFORMERS_CACHE", "/tmp/.cache/huggingface")
os.environ.setdefault("HF_HUB_CACHE", "/tmp/.cache/huggingface")


@lru_cache(maxsize=1)
def get_model() -> Any:
    """Load the SentenceTransformer once and cache it (thread-safe-ish via lru_cache)."""
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(MODEL_NAME, cache_folder=os.environ["HF_HUB_CACHE"])


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of strings into a list of EMBEDDING_DIM-length float vectors."""
    if not texts:
        return []
    model = get_model()
    vectors = model.encode(texts, show_progress_bar=False)
    return [list(map(float, v)) for v in vectors]


def to_vector_literal(embedding: list[float]) -> str:
    """
    Format an embedding as a Postgres array literal string ('{v1,v2,...}') so it
    can be passed to psycopg2 and cast with `%s::vector` in SQL.
    """
    return "{" + ",".join(repr(float(x)) for x in embedding) + "}"
