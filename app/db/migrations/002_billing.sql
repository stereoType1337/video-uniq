-- Billing: credit ledger, Stars payments, job reservations, trial and referral bookkeeping.

ALTER TABLE users ADD COLUMN IF NOT EXISTS utm                  TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_granted_at     TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_rewarded_at TIMESTAMPTZ;
ALTER TABLE users ADD CONSTRAINT users_balance_non_negative CHECK (balance >= 0);

CREATE TABLE IF NOT EXISTS payments (
    id                  BIGSERIAL   PRIMARY KEY,
    user_id             BIGINT      NOT NULL REFERENCES users(id),
    pack_code           TEXT        NOT NULL,
    credits             INTEGER     NOT NULL CHECK (credits > 0),
    amount              INTEGER     NOT NULL CHECK (amount > 0),
    currency            TEXT        NOT NULL,                 -- XTR for Telegram Stars
    provider            TEXT        NOT NULL,                 -- stars
    provider_payment_id TEXT,                                 -- telegram_payment_charge_id
    provider_payload    JSONB,                                -- raw successful_payment
    status              TEXT        NOT NULL DEFAULT 'pending', -- pending | success | cancelled
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at             TIMESTAMPTZ,
    UNIQUE (provider, provider_payment_id)
);
CREATE INDEX IF NOT EXISTS idx_payments_pending ON payments (provider, status, created_at);

-- Ledger: every balance change is a row with the balance it produced.
-- kind: trial | purchase | reserve | refund | referral | expire | admin
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS balance_after INTEGER NOT NULL;
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS job_id        BIGINT REFERENCES jobs(id);
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS payment_id    BIGINT REFERENCES payments(id);
-- Database-level guarantees against double charge / double credit:
CREATE UNIQUE INDEX IF NOT EXISTS uq_tx_job_reserve ON transactions (job_id) WHERE kind = 'reserve';
CREATE UNIQUE INDEX IF NOT EXISTS uq_tx_job_refund  ON transactions (job_id) WHERE kind = 'refund';
CREATE UNIQUE INDEX IF NOT EXISTS uq_tx_purchase    ON transactions (payment_id) WHERE kind = 'purchase';

-- Reservation: credits are taken when the job is queued, only delivered copies are charged.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS credits_reserved INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS credits_charged  INTEGER;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS settled_at       TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_jobs_unsettled ON jobs (created_at) WHERE settled_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_users_expiring ON users (last_purchase_at) WHERE balance > 0;
