-- Нормализация телефонов и отсев мусорных номеров

CREATE OR REPLACE FUNCTION phone_e164(raw TEXT) RETURNS TEXT AS $$
  SELECT CASE
    WHEN d = '' THEN NULL
    WHEN length(d) = 11 AND left(d,1) IN ('7','8') THEN '7' || right(d,10)
    WHEN length(d) = 10 AND left(d,1) = '9'        THEN '7' || d
    ELSE NULL
  END
  FROM (SELECT regexp_replace(COALESCE(raw,''), '[^0-9]', '', 'g') AS d) t;
$$ LANGUAGE sql IMMUTABLE;

-- Вид номера: mobile / landline / junk / none
CREATE OR REPLACE FUNCTION phone_kind(raw TEXT) RETURNS TEXT AS $$
  SELECT CASE
    WHEN e IS NULL THEN CASE WHEN COALESCE(raw,'') = '' THEN 'none' ELSE 'junk' END
    WHEN (SELECT count(DISTINCT ch) FROM unnest(string_to_array(right(e,10), NULL)) ch) <= 3 THEN 'junk'
    WHEN right(e,10) ~ '(\d)\1{4,}' THEN 'junk'
    WHEN right(e,10) IN ('9876543210','1234567890','0123456789') THEN 'junk'
    WHEN left(right(e,10),1) = '9' THEN 'mobile'
    ELSE 'landline'
  END
  FROM (SELECT phone_e164(raw) AS e) t;
$$ LANGUAGE sql IMMUTABLE;

ALTER TABLE leads ADD COLUMN IF NOT EXISTS phone_e164 TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS phone_kind TEXT;

-- разовое заполнение; дальше поля проставляет триггер leads_phone
UPDATE leads SET phone_e164 = phone_e164(phone), phone_kind = phone_kind(phone)
WHERE phone_kind IS NULL;

CREATE INDEX IF NOT EXISTS idx_leads_e164 ON leads (phone_e164);
CREATE INDEX IF NOT EXISTS idx_leads_kind ON leads (phone_kind);

-- Живые лиды: только пригодные номера
DROP VIEW IF EXISTS v_real_leads;
CREATE VIEW v_real_leads AS
SELECT l.*,
       COALESCE(NULLIF(l.utm_source,''), l.source_id, 'не указан') AS source,
       s.name AS status_name,
       (EXTRACT(hour FROM l.date_create AT TIME ZONE 'Asia/Vladivostok') BETWEEN 10 AND 18) AS in_office_hours
FROM leads l
LEFT JOIN crm_dict s ON s.kind = 'STATUS' AND s.status_id = l.status_id
WHERE l.phone_kind IN ('mobile', 'landline');

-- Уникальные клиенты: несколько лидов на один номер — это один человек
DROP VIEW IF EXISTS v_lead_dupes;
CREATE VIEW v_lead_dupes AS
SELECT phone_e164,
       count(*)                                    AS leads_count,
       min(date_create)                            AS first_seen,
       max(date_create)                            AS last_seen,
       count(DISTINCT assigned_by)                 AS managers,
       bool_or(status_semantic = 'S')              AS ever_won
FROM leads
WHERE phone_kind = 'mobile'
GROUP BY phone_e164;
