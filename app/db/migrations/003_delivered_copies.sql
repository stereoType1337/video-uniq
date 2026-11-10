-- How many copies actually reached the user (what was charged for). Filled by settle_job.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS copies_delivered SMALLINT;
UPDATE jobs SET copies_delivered = credits_charged * copies / NULLIF(credits_reserved, 0)
WHERE settled_at IS NOT NULL AND copies_delivered IS NULL;
