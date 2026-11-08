CREATE TABLE IF NOT EXISTS users (
    id               BIGSERIAL PRIMARY KEY,
    tg_id            BIGINT      NOT NULL UNIQUE,
    username         TEXT,
    balance          INTEGER     NOT NULL DEFAULT 0,
    mode             TEXT        NOT NULL DEFAULT 'base',
    copies           SMALLINT    NOT NULL DEFAULT 5,
    camera_profiles  BOOLEAN     NOT NULL DEFAULT FALSE,
    api_token        TEXT        UNIQUE,
    ref_code         TEXT        NOT NULL UNIQUE,
    referred_by      BIGINT      REFERENCES users(id),
    last_purchase_at TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transactions (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT      NOT NULL REFERENCES users(id),
    amount           INTEGER     NOT NULL,
    kind             TEXT        NOT NULL,          -- purchase | spend | refund | referral | expire
    provider         TEXT,
    provider_payload JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS jobs (
    id               BIGSERIAL PRIMARY KEY,
    user_id          BIGINT      NOT NULL REFERENCES users(id),
    source_sha256    TEXT        NOT NULL,
    source_key       TEXT        NOT NULL,
    mode             TEXT        NOT NULL,
    copies           SMALLINT    NOT NULL,
    status           TEXT        NOT NULL DEFAULT 'queued',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS copies (
    id               BIGSERIAL PRIMARY KEY,
    job_id           BIGINT      NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    copy_index       SMALLINT    NOT NULL,
    seed             TEXT        NOT NULL,
    plan_json        JSONB       NOT NULL,
    output_key       TEXT,
    profile_id       INTEGER,
    status           TEXT        NOT NULL DEFAULT 'pending',
    UNIQUE (job_id, copy_index)
);

CREATE TABLE IF NOT EXISTS fingerprints (
    copy_id          BIGINT      PRIMARY KEY REFERENCES copies(id) ON DELETE CASCADE,
    phash_json       JSONB       NOT NULL,
    chromaprint      TEXT,
    sha256           TEXT        NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata_profiles (
    id                INTEGER     PRIMARY KEY,
    kind              TEXT        NOT NULL,         -- editor | camera
    make              TEXT,
    model             TEXT,
    os_version        TEXT,
    software          TEXT,
    filename_template TEXT        NOT NULL,
    tz_offsets        TEXT[]      NOT NULL DEFAULT '{}',
    geo_boxes         JSONB       NOT NULL DEFAULT '[]',
    weight            REAL        NOT NULL DEFAULT 1.0,
    enabled           BOOLEAN     NOT NULL DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_jobs_user_status ON jobs (user_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs (source_sha256);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions (user_id, created_at);
