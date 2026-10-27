-- Pre-project schema, captured before the project migration.

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, parent_run_id TEXT REFERENCES runs(id),
                    created_at TEXT NOT NULL, operator TEXT NOT NULL, status TEXT NOT NULL,
                    input_json TEXT NOT NULL, config_json TEXT NOT NULL, prompt_json TEXT NOT NULL,
                    raw_output TEXT, validation_json TEXT, output_json TEXT,
                    error TEXT, latency_ms REAL, total_latency_ms REAL, usage_json TEXT,
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS case_versions (
                    run_id TEXT NOT NULL REFERENCES runs(id), case_id TEXT NOT NULL,
                    version INTEGER NOT NULL, content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, operator TEXT NOT NULL,
                    PRIMARY KEY(run_id,case_id,version)
                );
                CREATE TABLE IF NOT EXISTS case_heads (
                    run_id TEXT NOT NULL, case_id TEXT NOT NULL, version INTEGER NOT NULL,
                    revision INTEGER NOT NULL, status TEXT NOT NULL,
                    PRIMARY KEY(run_id,case_id),
                    FOREIGN KEY(run_id,case_id,version) REFERENCES case_versions(run_id,case_id,version)
                );
                CREATE TABLE IF NOT EXISTS clarifications (
                    run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL,
                    content_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                    revision INTEGER NOT NULL DEFAULT 1, resolution_json TEXT,
                    PRIMARY KEY(run_id,id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(id), case_id TEXT, version INTEGER,
                    action TEXT NOT NULL, operator TEXT NOT NULL, created_at TEXT NOT NULL,
                    reason TEXT NOT NULL, before_status TEXT, after_status TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    format TEXT NOT NULL, filename TEXT NOT NULL, mime TEXT NOT NULL,
                    operator TEXT NOT NULL, created_at TEXT NOT NULL, content BLOB NOT NULL,
                    content_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq);
                PRAGMA user_version=1;
