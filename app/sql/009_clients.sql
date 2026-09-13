-- Правило: считать по КЛИЕНТУ (номеру телефона), а не по карточке лида.
-- Один человек часто порождает несколько лидов у разных менеджеров;
-- отсутствие звонков по одной карточке не означает, что клиенту не звонили.

DROP VIEW IF EXISTS v_clients CASCADE;
CREATE VIEW v_clients AS
SELECT
    l.phone_e164,
    count(*)                                        AS leads_count,
    count(DISTINCT l.assigned_by)                   AS managers_count,
    min(l.date_create)                              AS first_lead_at,
    max(l.date_create)                              AS last_lead_at,
    bool_or(l.status_semantic = 'S')                AS ever_won,
    bool_or(l.status_semantic = 'P')                AS in_progress,
    array_agg(DISTINCT l.assigned_by)               AS manager_ids,
    -- звонки считаем по номеру, независимо от того, к какой карточке они привязаны
    (SELECT count(*) FROM calls c
      WHERE c.direction = 'out' AND c.phone_kind = 'mobile'
        AND phone_e164(c.phone_number) = l.phone_e164)                  AS calls_out,
    (SELECT count(*) FROM calls c
      WHERE c.direction = 'out' AND c.duration > 0
        AND phone_e164(c.phone_number) = l.phone_e164)                  AS calls_answered,
    (SELECT min(c.call_start) FROM calls c
      WHERE phone_e164(c.phone_number) = l.phone_e164
        AND c.call_start >= min(l.date_create))                         AS first_touch_at,
    (SELECT max(c.call_start) FROM calls c
      WHERE phone_e164(c.phone_number) = l.phone_e164)                  AS last_touch_at
FROM leads l
WHERE l.phone_kind = 'mobile'
GROUP BY l.phone_e164;

-- Лид с пометкой, работали ли с КЛИЕНТОМ, а не только с этой карточкой
DROP VIEW IF EXISTS v_lead_with_client;
CREATE VIEW v_lead_with_client AS
SELECT
    l.id                AS lead_id,
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
    a.calls_total                                   AS calls_on_lead,
    cl.calls_out                                    AS calls_on_client,
    cl.leads_count                                  AS client_leads,
    cl.managers_count                               AS client_managers,
    -- клиента реально бросили: ни одного исходящего по номеру
    (cl.calls_out = 0)                              AS client_untouched,
    -- карточка-дубль: у клиента есть другая карточка, по которой работают
    (cl.leads_count > 1 AND cl.calls_out > COALESCE(a.calls_total, 0)) AS worked_elsewhere
FROM leads l
LEFT JOIN managers mg      ON mg.portal_user_id = l.assigned_by
LEFT JOIN crm_dict s       ON s.kind = 'STATUS' AND s.status_id = l.status_id
LEFT JOIN v_lead_activity a ON a.lead_id = l.id
LEFT JOIN v_clients cl     ON cl.phone_e164 = l.phone_e164
WHERE l.phone_kind = 'mobile';

-- Размножение заявок: один клиент за сутки породил несколько карточек
DROP VIEW IF EXISTS v_lead_fanout;
CREATE VIEW v_lead_fanout AS
SELECT phone_e164,
       date_trunc('day', date_create)::date AS day,
       count(*)                             AS leads,
       count(DISTINCT assigned_by)          AS managers,
       array_agg(id ORDER BY date_create)   AS lead_ids
FROM leads
WHERE phone_kind = 'mobile'
GROUP BY 1, 2
HAVING count(*) > 1;
