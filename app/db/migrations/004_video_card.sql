-- Video card: a job starts as a draft (no credits taken) until the user presses "Start".
ALTER TABLE users ADD COLUMN IF NOT EXISTS audio_mode TEXT NOT NULL DEFAULT 'shift';
ALTER TABLE jobs  ADD COLUMN IF NOT EXISTS audio_mode TEXT NOT NULL DEFAULT 'shift';
ALTER TABLE jobs  ADD COLUMN IF NOT EXISTS source_duration REAL;
ALTER TABLE jobs  ADD COLUMN IF NOT EXISTS source_width INTEGER;
ALTER TABLE jobs  ADD COLUMN IF NOT EXISTS source_height INTEGER;
CREATE INDEX IF NOT EXISTS idx_jobs_drafts ON jobs (user_id, created_at) WHERE status = 'draft';
