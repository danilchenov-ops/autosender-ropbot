-- Правило: лиды и звонки с непригодным номером не участвуют в статистике.
-- Мусорные номера видны отдельно в v_junk_leads, но в метрики не попадают.

ALTER TABLE calls ADD COLUMN IF NOT EXISTS phone_kind TEXT;

UPDATE calls SET phone_kind = phone_kind(phone_number) WHERE phone_kind IS NULL;

CREATE INDEX IF NOT EXISTS idx_calls_kind ON calls (phone_kind);

CREATE OR REPLACE FUNCTION calls_phone_trg() RETURNS trigger AS $$
BEGIN
  NEW.phone_kind := phone_kind(NEW.phone_number);
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS calls_phone ON calls;
CREATE TRIGGER calls_phone BEFORE INSERT OR UPDATE OF phone_number ON calls
  FOR EACH ROW EXECUTE FUNCTION calls_phone_trg();

-- ── Представления с фильтром ────────────────────────────────────────────────

DROP VIEW IF EXISTS v_missed_with_callback;
CREATE VIEW v_missed_with_callback AS
SELECT
    m.id                AS call_id,
    m.call_start,
    m.phone_number,
    m.portal_user_id,
    mg.full_name        AS manager,
    cb.call_start       AS callback_at,
    cb.portal_user_id   AS callback_by,
    EXTRACT(EPOCH FROM (cb.call_start - m.call_start)) / 60.0 AS callback_delay_min
FROM calls m
LEFT JOIN managers mg ON mg.portal_user_id = m.portal_user_id
LEFT JOIN LATERAL (
    SELECT c.call_start, c.portal_user_id
    FROM calls c
    WHERE phone_e164(c.phone_number) = phone_e164(m.phone_number)
      AND c.direction = 'out' AND c.call_start > m.call_start AND c.duration > 0
    ORDER BY c.call_start LIMIT 1
) cb ON TRUE
WHERE m.direction = 'in' AND m.is_missed
  AND m.phone_kind IN ('mobile', 'landline');   -- мусорные номера не считаем

DROP VIEW IF EXISTS v_lead_activity CASCADE;
CREATE VIEW v_lead_activity AS
SELECT
    l.id                                              AS lead_id,
    l.date_create,
    l.assigned_by,
    mg.full_name                                      AS manager,
    l.status_id,
    l.status_semantic,
    COALESCE(NULLIF(l.utm_source,''), l.source_id)    AS source,
    NULLIF(l.utm_campaign,'')                         AS campaign,
    count(c.id)                                       AS calls_total,
    count(c.id) FILTER (WHERE c.direction = 'out')    AS calls_out,
    count(c.id) FILTER (WHERE c.direction = 'in')     AS calls_in,
    count(c.id) FILTER (WHERE c.is_missed)            AS calls_missed,
    sum(c.duration)                                   AS talk_sec,
    min(c.call_start)                                 AS first_call,
    max(c.call_start)                                 AS last_call,
    EXTRACT(EPOCH FROM (min(c.call_start) - l.date_create)) / 60.0 AS first_touch_min
FROM leads l
LEFT JOIN managers mg ON mg.portal_user_id = l.assigned_by
LEFT JOIN calls    c  ON c.crm_entity_type = 'LEAD' AND c.crm_entity_id = l.id
WHERE l.phone_kind IN ('mobile', 'landline')          -- мусорные номера не считаем
GROUP BY l.id, l.date_create, l.assigned_by, mg.full_name,
         l.status_id, l.status_semantic, l.utm_source, l.source_id, l.utm_campaign;

DROP VIEW IF EXISTS v_lead_funnel;
CREATE VIEW v_lead_funnel AS
SELECT
    date_trunc('month', l.date_create)                AS month,
    COALESCE(NULLIF(l.utm_source,''), l.source_id, 'не указан') AS source,
    NULLIF(l.utm_campaign,'')                         AS campaign,
    l.status_id,
    COALESCE(s.name, l.status_id)                     AS status_name,
    l.status_semantic,
    count(*)                                          AS leads,
    count(DISTINCT l.assigned_by)                     AS managers
FROM leads l
LEFT JOIN crm_dict s ON s.kind = 'STATUS' AND s.status_id = l.status_id
WHERE l.phone_kind IN ('mobile', 'landline')
GROUP BY 1, 2, 3, 4, 5, 6;

-- Отдельно: что именно отсеяно, чтобы это можно было проверить
DROP VIEW IF EXISTS v_junk_leads;
CREATE VIEW v_junk_leads AS
SELECT l.id, l.date_create, l.phone, l.phone_kind,
       COALESCE(NULLIF(l.utm_source,''), l.source_id) AS source,
       COALESCE(mg.full_name, l.assigned_by::text)    AS manager,
       s.name                                          AS status
FROM leads l
LEFT JOIN managers mg ON mg.portal_user_id = l.assigned_by
LEFT JOIN crm_dict s  ON s.kind = 'STATUS' AND s.status_id = l.status_id
WHERE l.phone_kind NOT IN ('mobile', 'landline');
