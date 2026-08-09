-- Setup script for weather_documents table
-- Run this manually in your Lakebase Postgres database (or let the app's
-- ensure_weather_table() create it automatically on first /weather/sync).

-- Create the weather documents table
CREATE TABLE IF NOT EXISTS weather_documents (
    id TEXT PRIMARY KEY,
    location TEXT NOT NULL,
    source_type TEXT NOT NULL,
    headline TEXT NOT NULL,
    narrative_text TEXT NOT NULL,
    effective_at TIMESTAMPTZ,
    payload JSONB NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Create indexes for source/location lookups
CREATE INDEX IF NOT EXISTS idx_weather_documents_source_type
ON weather_documents (source_type);

CREATE INDEX IF NOT EXISTS idx_weather_documents_location
ON weather_documents (location);

-- Verify the table was created
SELECT
    table_name,
    column_name,
    data_type
FROM information_schema.columns
WHERE table_name = 'weather_documents'
ORDER BY ordinal_position;
