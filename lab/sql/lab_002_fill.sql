-- Заполнение выборки. Единица наблюдения — разговор (звонок с расшифровкой),
-- единица группировки — клиент (номер телефона), по правилу реестра №2.
-- Пересобирается целиком при каждом запуске.

TRUNCATE lab_sample;

WITH cl AS (
    SELECT l.phone_e164 AS client,
           bool_or(l.status_semantic = 'S') AS ever_won,
           bool_or(l.status_semantic = 'P') AS in_progress
    FROM leads l
    WHERE l.phone_kind = 'mobile' AND l.phone_e164 IS NOT NULL
    GROUP BY 1
),
lead_first AS (   -- первый лид клиента: канал входа
    SELECT DISTINCT ON (l.phone_e164)
           l.phone_e164 AS client,
           COALESCE(NULLIF(l.utm_source, ''), l.source_id) AS source,
           NULLIF(l.utm_campaign, '') AS campaign,
           l.id AS lead_id
    FROM leads l
    WHERE l.phone_kind = 'mobile' AND l.phone_e164 IS NOT NULL
    ORDER BY l.phone_e164, l.date_create
),
lead_last AS (    -- последний лид: чем кончилось
    SELECT DISTINCT ON (l.phone_e164)
           l.phone_e164 AS client,
           s.name AS status_name,
           l.status_semantic
    FROM leads l
    LEFT JOIN crm_dict s ON s.kind = 'STATUS' AND s.status_id = l.status_id
    WHERE l.phone_kind = 'mobile' AND l.phone_e164 IS NOT NULL
    ORDER BY l.phone_e164, l.date_create DESC
),
sums AS (         -- сумма сделки по клиенту, если проставлена
    SELECT l.phone_e164 AS client, max(NULLIF(l.opportunity, 0)) AS deal_sum
    FROM leads l WHERE l.phone_kind = 'mobile' GROUP BY 1
),
part AS (         -- клиенты, засветившиеся как PARTNER хоть одним лидом
    SELECT DISTINCT l.phone_e164 AS client
    FROM leads l
    WHERE l.phone_kind = 'mobile'
      AND COALESCE(NULLIF(l.utm_source, ''), l.source_id) = 'PARTNER'
),
tc AS (
    SELECT c.id AS call_id,
           c.phone_e164 AS client,
           CASE WHEN cl.ever_won THEN 'WON' ELSE 'LOST' END AS grp,
           row_number() OVER (PARTITION BY c.phone_e164 ORDER BY c.call_start) AS seq,
           c.call_start, c.direction, c.duration, c.portal_user_id
    FROM transcripts t
    JOIN calls c ON c.id = t.call_id
    JOIN cl ON cl.client = c.phone_e164
    WHERE c.phone_kind = 'mobile'
      AND NOT cl.in_progress          -- незакрытые клиенты в сравнение не берём
      AND c.duration >= 60            -- короче минуты — не разговор
      AND length(coalesce(t.text,'')) > 400
)
INSERT INTO lab_sample (call_id, client, grp, seq, is_first, split, source, campaign,
                        manager, manager_id, call_start, direction, duration,
                        deal_sum, lost_status, lead_id, excluded)
SELECT tc.call_id, tc.client, tc.grp, tc.seq,
       tc.seq = 1,
       CASE WHEN abs(hashtext(tc.client)) % 4 = 0 THEN 'holdout' ELSE 'train' END,
       lf.source, lf.campaign,
       COALESCE(mg.full_name, tc.portal_user_id::text),
       tc.portal_user_id,
       tc.call_start, tc.direction, tc.duration,
       s.deal_sum,
       CASE WHEN tc.grp = 'LOST' THEN ll.status_name END,
       lf.lead_id,
       CASE WHEN p.client IS NOT NULL THEN 'PARTNER' END
FROM tc
LEFT JOIN lead_first lf ON lf.client = tc.client
LEFT JOIN lead_last  ll ON ll.client = tc.client
LEFT JOIN sums       s  ON s.client  = tc.client
LEFT JOIN part       p  ON p.client  = tc.client
LEFT JOIN managers   mg ON mg.portal_user_id = tc.portal_user_id;
