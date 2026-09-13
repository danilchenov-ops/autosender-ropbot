-- Представления, учитывающие реальную структуру портала:
-- звонки привязаны к ЛИДАМ, воронка живёт в статусах лидов, а не в сделках.

DROP VIEW IF EXISTS v_call_deal;
DROP VIEW IF EXISTS v_call_context;

-- Звонок вместе со всем контекстом: менеджер, лид, статус, источник, оценка разговора
CREATE VIEW v_call_context AS
SELECT
    c.id              AS call_id,
    c.call_start,
    c.direction,
    c.duration,
    c.is_missed,
    c.phone_number,
    c.portal_user_id,
    mg.full_name      AS manager,
    c.crm_entity_type,
    c.crm_entity_id,
    l.id              AS lead_id,
    l.status_id       AS lead_status,
    ls.name           AS lead_status_name,
    l.status_semantic AS lead_semantic,
    COALESCE(NULLIF(l.utm_source, ''), l.source_id) AS source,
    NULLIF(l.utm_campaign, '')                      AS campaign,
    d.id              AS deal_id,
    d.stage_id,
    d.stage_semantic,
    d.opportunity,
    m.manager_talk_ratio,
    m.monologue_flag,
    m.questions_manager,
    s.score,
    s.outcome,
    s.next_step_dated,
    (t.call_id IS NOT NULL) AS has_transcript
FROM calls c
LEFT JOIN managers     mg ON mg.portal_user_id = c.portal_user_id
LEFT JOIN leads        l  ON c.crm_entity_type = 'LEAD' AND l.id = c.crm_entity_id
LEFT JOIN crm_dict     ls ON ls.kind = 'STATUS' AND ls.status_id = l.status_id
LEFT JOIN deals        d  ON c.crm_entity_type = 'DEAL' AND d.id = c.crm_entity_id
LEFT JOIN call_metrics m  ON m.call_id = c.id
LEFT JOIN call_scores  s  ON s.call_id = c.id
LEFT JOIN transcripts  t  ON t.call_id = c.id;

-- Воронка по статусам лидов: где именно осыпается
DROP VIEW IF EXISTS v_lead_funnel;
CREATE VIEW v_lead_funnel AS
SELECT
    date_trunc('month', l.date_create)                AS month,
    COALESCE(NULLIF(l.utm_source, ''), l.source_id, 'не указан') AS source,
    NULLIF(l.utm_campaign, '')                        AS campaign,
    l.status_id,
    COALESCE(s.name, l.status_id)                     AS status_name,
    l.status_semantic,
    count(*)                                          AS leads,
    count(DISTINCT l.assigned_by)                     AS managers
FROM leads l
LEFT JOIN crm_dict s ON s.kind = 'STATUS' AND s.status_id = l.status_id
GROUP BY 1, 2, 3, 4, 5, 6;

-- Сколько звонков потребовалось лиду и чем он кончился
DROP VIEW IF EXISTS v_lead_activity CASCADE;
CREATE VIEW v_lead_activity AS
SELECT
    l.id                                              AS lead_id,
    l.date_create,
    l.assigned_by,
    mg.full_name                                      AS manager,
    l.status_id,
    l.status_semantic,
    COALESCE(NULLIF(l.utm_source, ''), l.source_id)   AS source,
    NULLIF(l.utm_campaign, '')                        AS campaign,
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
GROUP BY l.id, l.date_create, l.assigned_by, mg.full_name,
         l.status_id, l.status_semantic, l.utm_source, l.source_id, l.utm_campaign;
