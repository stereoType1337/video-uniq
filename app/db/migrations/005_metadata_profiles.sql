-- Metadata profiles: definitions are synced from app/services/data/metadata_profiles.json on
-- startup; enabled/weight are operational knobs owned by the admin panel (sync keeps them).
DROP TABLE IF EXISTS metadata_profiles;  -- the 001 draft table was never used
CREATE TABLE metadata_profiles (
    id          TEXT        PRIMARY KEY,
    kind        TEXT        NOT NULL,              -- editor | camera
    weight      REAL        NOT NULL CHECK (weight >= 0),
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    definition  JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE copies ALTER COLUMN profile_id TYPE TEXT USING profile_id::text;
-- The user's "camera profiles" choice, snapshotted on the job when the card is created.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS camera_profiles BOOLEAN NOT NULL DEFAULT FALSE;
