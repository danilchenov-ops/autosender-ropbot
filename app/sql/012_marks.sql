-- Журнал маркировки лидов в Битриксе (категория клиента + огонёк в названии)

CREATE TABLE IF NOT EXISTS lead_marks (
    lead_id     bigint PRIMARY KEY,
    value       text NOT NULL,
    fire        boolean NOT NULL DEFAULT false,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE lead_visits ADD COLUMN IF NOT EXISTS match_kind text NOT NULL DEFAULT 'exact';

-- Оригинальные названия лидов до маркировки: страховка для отката значков
CREATE TABLE IF NOT EXISTS lead_title_backup (
    lead_id        bigint PRIMARY KEY,
    original_title text,
    saved_at       timestamptz NOT NULL DEFAULT now()
);
