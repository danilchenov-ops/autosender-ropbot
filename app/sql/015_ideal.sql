-- 015_ideal.sql — заявка глазами «Идеальной сделки».
-- Признаки, у которых в разборе 21.08.2026 нашёлся значимый lift по покупке:
--   5+ попыток при недозвоне ×16,7 · клиент перезвонил 2+ раз ×13,3 ·
--   5+ звонков за 3 дня ×6,8 · первый разговор 7+ минут ×5,6 ·
--   растянутый диалог (медиана выигранной 5,9 дня против 0) · 6 разговоров против 1.
-- Идемпотентно.

DROP VIEW IF EXISTS v_manager_ideal;
DROP VIEW IF EXISTS v_lead_ideal;
CREATE VIEW v_lead_ideal AS
SELECT
    w.*,
    i.talks,
    i.first_talk_sec,
    i.talk_sec_total,
    i.client_calls_in,
    i.span_days,
    -- признаки идеальной сделки, каждый — да/нет по одной заявке
    (NOT w.reached AND w.calls_3d >= 5)                       AS f_persisted,
    (w.calls_3d >= 5)                                         AS f_dense,
    (i.first_talk_sec >= 420)                                 AS f_deep_first,
    (i.client_calls_in >= 2)                                  AS f_client_back,
    (i.span_days >= 3)                                        AS f_spread,
    (i.talks >= 3)                                            AS f_multi
FROM v_lead_worked w
LEFT JOIN LATERAL (
    SELECT
        count(*) FILTER (WHERE c.duration > 0)                        AS talks,
        (array_agg(c.duration ORDER BY c.call_start)
            FILTER (WHERE c.duration > 0))[1]                         AS first_talk_sec,
        COALESCE(sum(c.duration) FILTER (WHERE c.duration > 0), 0)    AS talk_sec_total,
        count(*) FILTER (WHERE c.direction = 'in' AND c.duration > 0) AS client_calls_in,
        COALESCE(EXTRACT(DAY FROM (max(c.call_start) FILTER (WHERE c.duration > 0)
                                 - min(c.call_start) FILTER (WHERE c.duration > 0))), 0)
                                                                      AS span_days
    FROM calls c
    WHERE c.phone_e164 = w.phone_e164
      AND c.call_start >= w.date_create - interval '5 minutes'
) i ON TRUE;

-- Сводка по менеджеру и классу заявки: что ему дали и что он с этим сделал
CREATE VIEW v_manager_ideal AS
SELECT
    assigned_by                                                       AS portal_user_id,
    manager,
    COALESCE(grade, 'нет')                                            AS grade,
    date_trunc('month', date_create AT TIME ZONE 'Asia/Vladivostok')::date AS month,
    count(*)                                                          AS leads,
    count(*) FILTER (WHERE untouched)                                 AS untouched,
    count(*) FILTER (WHERE reached)                                   AS reached,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY touch_min)
        FILTER (WHERE NOT untouched)                                  AS p50_touch,
    count(*) FILTER (WHERE NOT reached)                               AS not_reached,
    count(*) FILTER (WHERE f_persisted)                               AS persisted,
    count(*) FILTER (WHERE f_dense)                                   AS dense,
    count(*) FILTER (WHERE f_deep_first)                              AS deep_first,
    count(*) FILTER (WHERE f_client_back)                             AS client_back,
    count(*) FILTER (WHERE f_spread)                                  AS spread,
    count(*) FILTER (WHERE f_multi)                                   AS multi,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY talks)
        FILTER (WHERE reached)                                        AS p50_talks,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY first_talk_sec)
        FILTER (WHERE reached)                                        AS p50_first_talk,
    round(sum(talk_sec_total) / 60.0)                                 AS talk_min,
    count(*) FILTER (WHERE status_semantic = 'S')                     AS won
FROM v_lead_ideal
GROUP BY 1, 2, 3, 4;
