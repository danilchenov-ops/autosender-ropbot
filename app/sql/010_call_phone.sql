-- Нормализованный номер прямо в таблице звонков: нужен для быстрых связок по клиенту

ALTER TABLE calls ADD COLUMN IF NOT EXISTS phone_e164 TEXT;

UPDATE calls SET phone_e164 = phone_e164(phone_number) WHERE phone_e164 IS NULL;

CREATE INDEX IF NOT EXISTS idx_calls_e164 ON calls (phone_e164);
CREATE INDEX IF NOT EXISTS idx_calls_e164_out ON calls (phone_e164) WHERE direction = 'out';

CREATE OR REPLACE FUNCTION calls_phone_trg() RETURNS trigger AS $$
BEGIN
  NEW.phone_kind := phone_kind(NEW.phone_number);
  NEW.phone_e164 := phone_e164(NEW.phone_number);
  RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS calls_phone ON calls;
CREATE TRIGGER calls_phone BEFORE INSERT OR UPDATE OF phone_number ON calls
  FOR EACH ROW EXECUTE FUNCTION calls_phone_trg();

-- Клиент = номер телефона. Звонки считаются по номеру, а не по карточке лида.
DROP VIEW IF EXISTS v_lead_with_client;
DROP VIEW IF EXISTS v_clients;
CREATE VIEW v_clients AS
SELECT
    l.phone_e164,
    count(*)                              AS leads_count,
    count(DISTINCT l.assigned_by)         AS managers_count,
    min(l.date_create)                    AS first_lead_at,
    max(l.date_create)                    AS last_lead_at,
    bool_or(l.status_semantic = 'S')      AS ever_won,
    bool_or(l.status_semantic = 'P')      AS in_progress,
    COALESCE(c.calls_out, 0)              AS calls_out,
    COALESCE(c.calls_answered, 0)         AS calls_answered,
    c.first_touch_at,
    c.last_touch_at
FROM leads l
LEFT JOIN LATERAL (
    SELECT count(*) FILTER (WHERE direction = 'out')                    AS calls_out,
           count(*) FILTER (WHERE direction = 'out' AND duration > 0)   AS calls_answered,
           min(call_start)                                              AS first_touch_at,
           max(call_start)                                              AS last_touch_at
    FROM calls WHERE phone_e164 = l.phone_e164
) c ON TRUE
WHERE l.phone_kind = 'mobile'
GROUP BY l.phone_e164, c.calls_out, c.calls_answered, c.first_touch_at, c.last_touch_at;

CREATE VIEW v_lead_with_client AS
SELECT
    l.id                                            AS lead_id,
    l.date_create,
    l.assigned_by,
    COALESCE(mg.full_name, l.assigned_by::text)     AS manager,
    l.phone,
    l.phone_e164,
    l.status_id,
    l.status_semantic,
    s.name                                          AS status_name,
    COALESCE(NULLIF(l.utm_source,''), l.source_id)  AS source,
    COALESCE(NULLIF(l.title,''), 'Лид #'||l.id)     AS title,
    COALESCE(a.calls_total, 0)                      AS calls_on_lead,
    COALESCE(cl.calls_out, 0)                       AS calls_on_client,
    COALESCE(cl.leads_count, 1)                     AS client_leads,
    COALESCE(cl.managers_count, 1)                  AS client_managers,
    (COALESCE(cl.calls_out, 0) = 0)                 AS client_untouched,
    (COALESCE(cl.leads_count,1) > 1
       AND COALESCE(cl.calls_out,0) > COALESCE(a.calls_total,0)) AS worked_elsewhere
FROM leads l
LEFT JOIN managers mg       ON mg.portal_user_id = l.assigned_by
LEFT JOIN crm_dict s        ON s.kind = 'STATUS' AND s.status_id = l.status_id
LEFT JOIN v_lead_activity a ON a.lead_id = l.id
LEFT JOIN v_clients cl      ON cl.phone_e164 = l.phone_e164
WHERE l.phone_kind = 'mobile';
