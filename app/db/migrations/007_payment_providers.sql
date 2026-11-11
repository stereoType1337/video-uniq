-- RUB payment providers (Kassa AI, Platega): raw callbacks are stored BEFORE verification
-- (the audit trail when a provider claims it sent a callback).
CREATE TABLE IF NOT EXISTS webhook_logs (
    id           BIGSERIAL   PRIMARY KEY,
    provider     TEXT        NOT NULL,
    payload      JSONB       NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    payment_id   BIGINT      REFERENCES payments(id)
);
CREATE INDEX IF NOT EXISTS idx_webhook_logs_payment ON webhook_logs (payment_id);
