-- Schema for the uploader's durable state.
-- Applied idempotently on every start; see store.migrate().

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    movie_id     INTEGER,
    imdb_id      TEXT,
    tmdb_id      TEXT,
    title        TEXT NOT NULL DEFAULT '',
    year         INTEGER,

    -- Absolute, already-resolved path under paths.media_root.
    folder_path  TEXT    NOT NULL,
    size_bytes   INTEGER NOT NULL DEFAULT 0,

    part_size    INTEGER NOT NULL DEFAULT 0,
    part_count   INTEGER NOT NULL DEFAULT 0,

    state        TEXT    NOT NULL DEFAULT 'discovered',
    source       TEXT    NOT NULL DEFAULT 'inbox',

    attempts     INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error   TEXT,

    priority     INTEGER NOT NULL DEFAULT 100,

    -- Lease held by the worker currently processing this job. Expired leases
    -- are reclaimable, which is what makes crash recovery work.
    lease_owner     TEXT,
    lease_expires_at TEXT,

    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

-- Enforces one job per movie folder regardless of how many intake paths fired.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_folder ON jobs(folder_path);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);

CREATE TABLE IF NOT EXISTS parts (
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,

    -- Emitted Telegram filename, persisted so a retry reuses the same name.
    name       TEXT    NOT NULL,

    byte_offset INTEGER NOT NULL,
    byte_size  INTEGER NOT NULL,

    -- Populated only after a verified upload. A resumed job skips rows that
    -- have a file_id, which is what makes restart-mid-upload cheap.
    chat_id    INTEGER,
    thread_id  INTEGER,
    message_id INTEGER,
    file_id    TEXT,
    file_size  INTEGER,

    state      TEXT    NOT NULL DEFAULT 'pending',
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    uploaded_at TEXT,

    PRIMARY KEY (job_id, idx)
);

CREATE INDEX IF NOT EXISTS idx_parts_job_state ON parts(job_id, state);
CREATE INDEX IF NOT EXISTS idx_parts_pending ON parts(state, job_id);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    ts     TEXT NOT NULL DEFAULT (datetime('now')),
    level  TEXT NOT NULL DEFAULT 'info',
    event  TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id DESC);