# Weather Intelligence — Unstructured Data → Lakebase Vector Search → REST API

This is the homework extension of the `databricks-lakebase-app-day-2` reference
app. Instead of syncing **structured stock/news records from the Massive API**,
it harvests **unstructured weather text from the NWS (National Weather Service)
API**, vectorizes it into Lakebase (`Postgres + pgvector`), and exposes a
semantic search endpoint.

Pipeline (same shape as the reference app):

```
NWS (alerts + forecast discussions + gridpoint forecast)   weather_client.py
        │  normalize → document records
        ▼
POST /weather/sync  ──upsert──►  weather_documents (Lakebase)
                                      │  read unembedded rows
                                      ▼
                    notebooks/ingest_weather_embeddings.py
                                      │  chunk (800/100) → embed (384-dim)
                                      ▼
                              weather_embeddings (pgvector + HNSW)
                                      │  cosine search (<=>)
                                      ▼
                 POST /weather/search  {"query": "flash flood risk this weekend"}
```

## Data source choice

- **[NWS API](https://api.weather.gov)** (`api.weather.gov`): free, no API key,
  and produces exactly the kind of rich, unstructured text this homework wants —
  **active alerts** (`description` + `instruction` narrative), **Area Forecast
  Discussions** (free-text forecast narrative), and **gridpoint forecast
  periods** (`detailedForecast` text, which is usually non-empty even when no
  alerts are active). All responses are public domain, and the API is
  rate-limit friendly.
- **Caveat**: NWS only covers the **United States** — `GET /points/{lat},{lon}`
  returns 404 for non-US coordinates (e.g. Berlin/Paris). Default locations are
  therefore US cities (**Chicago, IL; Austin, TX**). Non-US locations remain
  valid config but resolve to no NWS data and are skipped with a log message.
- **[Open-Meteo Geocoding API](https://open-meteo.com/docs/geocoding-api)**:
  resolves "Chicago, IL" / "41.88,-87.63" → `(lat, lon)` (also free, no key).
  `"lat,lon"` strings are accepted directly and skip geocoding.

## Schema decisions

`weather_documents` (raw documents, created by `ensure_weather_table()` /
`sql/05_setup_weather_documents.sql`):

| column          | type      | notes                                                      |
| --------------- | --------- | ---------------------------------------------------------- |
| `id`            | TEXT PK   | stable dedup key: NWS alert UUID, or md5(location+issuance) for discussions |
| `location`      | TEXT      | the location string as given (e.g. "Chicago, IL")           |
| `source_type`   | TEXT      | `"alert"` or `"forecast"`                                  |
| `headline`      | TEXT      | alert `event` / `headline`, or "Area Forecast Discussion - <office>" |
| `narrative_text`| TEXT      | the free text to embed: `description`+`instruction` (alerts), `detailedForecast` (gridpoint periods), or `productText` (discussions) |
| `effective_at`  | TIMESTAMPTZ| alert `effective`/`sent`, discussion `issuanceTime`        |
| `payload`       | JSONB     | raw API properties, for provenance                         |
| `synced_at`     | TIMESTAMPTZ| when we upserted it                                        |

`weather_embeddings` (pgvector, created by the ingest script / `sql/06`):

| column          | type             | notes                                           |
| --------------- | ---------------- | ----------------------------------------------- |
| `id`            | TEXT PK          | `{document_id}_{chunk_index}`                   |
| `document_id`   | TEXT FK          | → `weather_documents.id`                        |
| `chunk_index`   | INT              | chunk position within the document              |
| `chunk_text`    | TEXT             | the chunk that was embedded (returned in search)|
| `embedding`     | VECTOR(384)      | `all-MiniLM-L6-v2`                              |
| `model_name`    | TEXT             | provenance                                     |
| `created_at`    | TIMESTAMPTZ      |                                                |

**Embedding model**: `sentence-transformers/all-MiniLM-L6-v2` (384-dim) — the
same model as the existing news pipeline, so both stay compatible with the same
distance conventions. The model is pinned in `embeddings.py` and loaded **once**
(lazily, module-level) so the search endpoint doesn't reload it per request.

**Chunking**: `CHUNK_SIZE=800`, `CHUNK_OVERLAP=100` (sliding window). Most NWS
texts are short enough to be a single chunk; chunking only kicks in for
combined alert `description` + `instruction` bodies and longer discussions.

**Index**: `USING hnsw (embedding vector_cosine_ops)` for fast approximate
cosine similarity search (`pgvector` extension, already enabled).

## How to run end-to-end

### 0. Prerequisites

- Python 3.10+
- A Lakebase instance + native-password role (see reference README step 2)
- `LAKEBASE_URL` set (e.g. in `.env` copied from `.env.example`)

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env   # then paste your LAKEBASE_URL into .env
```

### 1. Sync (harvest) — `POST /weather/sync`

```powershell
python app.py
```

```bash
curl -X POST http://localhost:8000/weather/sync `
  -H "Content-Type: application/json" `
  -d '{"locations": ["Chicago, IL", "Austin, TX"], "limit": 50}'
```

Defaults to Chicago + Austin when `locations` is omitted. Returns
`{"synced": N, "locations": [...]}`. Creates `weather_documents` on first call.

### 2. Vectorize — `python notebooks/ingest_weather_embeddings.py`

Reads rows in `weather_documents` that have no embedding yet, chunks +
embeds them, and upserts into `weather_embeddings` (creating the table + HNSW
index on first run). Re-runs are idempotent (`ON CONFLICT (id) DO NOTHING`).

```powershell
python notebooks/ingest_weather_embeddings.py
```

### 3. Retrieve — `POST /weather/search`

```bash
curl -X POST http://localhost:8000/weather/search `
  -H "Content-Type: application/json" `
  -d '{"query": "risk of flooding near rivers", "top_k": 5}'
```

Returns the top `top_k` matches (clamped to 1–20), ranked by cosine similarity
(`1 - (embedding <=> query)`):

```json
{
  "query": "risk of flooding near rivers",
  "top_k": 5,
  "results": [
    {
      "id": "...",
      "location": "Chicago, IL",
      "source_type": "alert",
      "headline": "Flash Flood Warning",
      "chunk_text": "...",
      "similarity": 0.53
    }
  ]
}
```

Edge cases handled: missing/malformed `query` → 400; missing `top_k` → 5;
`top_k` clamped to [1, 20]; empty/missing `weather_embeddings` → 200 with
`{"results": []}` and a message telling you to run the ingest script.

## Deploying on Databricks (do after local run works)

1. **Create a Git folder** in your workspace (Workspace → Create → Git folder)
   and point it at this repo.
2. **Create the Databricks App** (Compute → Apps → Create app → Custom):
   - Source code location → your Git folder (the one with `app.py` / `app.yaml`).
   - `app.yaml` is picked up automatically (command + env, including
     `LAKEBASE_URL` from the `database/lakebase-url` secret and the weather
     table names).
   - Deploy, open the app URL, verify `GET /healthz`.
3. **Sync**: `POST <app-url>/weather/sync` with the JSON body above.
4. **Embed**: run the ingest script either as
   - a **Databricks Workflow** — this repo ships the bundle config
     `resources/ingest_weather_embeddings_job.yml` (daily 07:00 UTC, PAUSED
     until you validate):
     - set the workspace URL in `databricks.yml`, then
       `databricks bundle deploy -t dev` and
       `databricks bundle run ingest_weather_embeddings_job -t dev`; flip
       `pause_status` to `UNPAUSED` once validated, or
   - the **Workflows UI**: create a job → task type **Notebook** → the
     `notebooks/ingest_weather_embeddings.py` file in your Git folder, with
     parameters `weather_table_name=weather_documents`,
     `weather_embeddings_table_name=weather_embeddings`,
     `embedding_model=sentence-transformers/all-MiniLM-L6-v2`, `chunk_size=800`,
     `chunk_overlap=100`, `batch_size=64`.
5. **Search**: `POST <app-url>/weather/search` with the JSON body above.

## Files added/changed

| file | purpose |
| ---- | ------- |
| `weather_client.py` | NWS + Open-Meteo client: geocode → `/points` → alerts + AFD, normalize to document records |
| `embeddings.py` | shared lazy `all-MiniLM-L6-v2` loader, `embed_texts()`, `to_vector_literal()` |
| `app.py` | `POST /weather/sync`, `POST /weather/search`, `ensure_weather_table()` |
| `lakebase.py` | `LAKEBASE_URL` env fallback so it runs locally (secret-scope path kept for Databricks) |
| `notebooks/ingest_weather_embeddings.py` | plain-psycopg2 chunk → embed → upsert ETL |
| `sql/05_setup_weather_documents.sql` | `weather_documents` DDL |
| `sql/06_setup_weather_embeddings.sql` | `weather_embeddings` DDL + HNSW index |
| `resources/ingest_weather_embeddings_job.yml` | scheduled Workflow for the ETL |
| `app.yaml`, `requirements.txt`, `.env.example` | config + `sentence-transformers` dep |

## Known limitations / improvements

- **Geocoding dependency**: city/state strings go through Open-Meteo (free,
  unauthenticated). No geocoding on NWS itself. A small bundled lat/lon table
  for the fixed default cities would remove the network dependency.
- **NWS is US-only**: `GET /points` returns 404 outside the US, so non-US
  locations (e.g. Berlin/Paris, the originally requested defaults) are skipped
  with a log message and sync 0 documents. Defaults are US cities (Chicago,
  Austin); a worldwide source (e.g. Open-Meteo) would be needed for global
  coverage.
- **Approximate index**: HNSW is approximate; for exact results an `ivfflat`
  with `lists` sized to data or a sequential scan would be exact (slower).
- **No deletes/refresh**: embeddings accumulate; documents updated by a resync
  get a *new* embedding row (id includes chunk index) but stale vectors for
  changed narratives aren't pruned. A `document_id + model + chunk_index`
  unique constraint + delete-then-insert would fix this.
- **Model download on first run**: the first embed needs to fetch
  `all-MiniLM-L6-v2` from HuggingFace (cached under `/tmp/.cache/huggingface`).
- **Retrieval only, no generation**: search returns top chunks; a RAG
  "answer the query" endpoint could be added by piping results into an LLM.
