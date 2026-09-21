CREATE TABLE IF NOT EXISTS sales (
    order_id BIGINT NOT NULL,
    order_date DATE NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    ingestion_id BIGINT NOT NULL,
    region TEXT NOT NULL,
    revenue NUMERIC(14,2) NOT NULL
);

INSERT INTO sales (order_id, order_date, updated_at, ingestion_id, region, revenue) VALUES
    (1001, DATE '2026-09-10', TIMESTAMPTZ '2026-09-10 10:00:00+00', 1, 'central', 125.00),
    (1001, DATE '2026-09-10', TIMESTAMPTZ '2026-09-10 11:00:00+00', 2, 'central', 130.00),
    (1002, DATE '2026-09-10', TIMESTAMPTZ '2026-09-10 10:30:00+00', 3, 'west', 220.00),
    (1003, DATE '2026-09-11', TIMESTAMPTZ '2026-09-11 09:00:00+00', 4, 'east', 95.00);
