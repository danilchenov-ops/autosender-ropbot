-- 013_profile.sql — портрет менеджера: дисциплина воронки, активность, качество, результат.
-- Правила счёта — /opt/knowledge (analytics-rules): только пригодные телефоны,
-- счёт по клиенту (phone_e164), PARTNER вне сравнения, текущий период не для выводов.
-- Идемпотентно.

-- ── Рабочие минуты между двумя моментами ────────────────────────────────────
-- Скорость реакции нельзя мерить календарным временем: заявка в 23:40 и звонок
-- в 10:05 — это 10 минут работы, а не 385. Рабочий день 10:00–19:00 Владивосток,
-- без выходных (отдел звонит семь дней в неделю).
CREATE OR REPLACE FUNCTION work_minutes(a timestamptz, b timestamptz)
RETURNS numeric AS $$
    SELECT CASE WHEN a IS NULL OR b IS NULL OR b <= a THEN 0 ELSE COALESCE((
        SELECT sum(EXTRACT(EPOCH FROM (least(b, w.e) - greatest(a, w.s))) / 60.0)
        FROM generate_series(
                 date_trunc('day', a AT TIME ZONE 'Asia/Vladivostok'),
                 date_trunc('day', least(b, a + interval '30 days') AT TIME ZONE 'Asia/Vladivostok'),
                 interval '1 day') g(d)
        CROSS JOIN LATERAL (
            SELECT (g.d + interval '10 hours') AT TIME ZONE 'Asia/Vladivostok' AS s,
                   (g.d + interval '19 hours') AT TIME ZONE 'Asia/Vladivostok' AS e
        ) w
        WHERE least(b, w.e) > greatest(a, w.s)
    ), 0) END;
$$ LANGUAGE sql STABLE;

-- ── Оценка разговора моделью ────────────────────────────────────────────────
-- Записи одноканальные: механические метрики (доля речи, вопросы) на них не считаются,
-- роли определяет только модель. Добавляем ей два поля.
ALTER TABLE call_scores ADD COLUMN IF NOT EXISTS talk_share        NUMERIC;
ALTER TABLE call_scores ADD COLUMN IF NOT EXISTS questions_manager INTEGER;

-- ── Присутствие в Битриксе ──────────────────────────────────────────────────
-- Битрикс не отдаёт timeman (нет прав у вебхука), поэтому снимаем IS_ONLINE
-- опросом раз в 5 минут и сами набираем историю. Это «в системе», а не «работал».
CREATE TABLE IF NOT EXISTS user_presence (
    portal_user_id BIGINT      NOT NULL,
    ts             TIMESTAMPTZ NOT NULL,
    is_online      BOOLEAN     NOT NULL,
    last_activity  TIMESTAMPTZ,
    PRIMARY KEY (portal_user_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_presence_ts ON user_presence (ts DESC);

-- Табель Битрикса. Права `timeman` выданы вебхуку 20.08.2026. Метод отдаёт только
-- текущий день, истории через REST нет — копим сами опросом presence.py.
CREATE TABLE IF NOT EXISTS timeman_days (
    portal_user_id BIGINT NOT NULL,
    day            DATE   NOT NULL,
    status         TEXT,
    time_start     TIMESTAMPTZ,
    time_finish    TIMESTAMPTZ,
    duration_sec   INTEGER,
    leaks_sec      INTEGER,
    entry_id       BIGINT,
    updated_at     TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (portal_user_id, day)
);
CREATE INDEX IF NOT EXISTS idx_timeman_day ON timeman_days (day DESC);

CREATE OR REPLACE VIEW v_manager_timeman_daily AS
SELECT t.day, t.portal_user_id,
       COALESCE(mg.full_name, t.portal_user_id::text)  AS manager,
       t.status, t.time_start, t.time_finish,
       round(t.duration_sec / 3600.0, 1)               AS hours,
       round(t.leaks_sec / 60.0)                       AS leaks_min
FROM timeman_days t
LEFT JOIN managers mg ON mg.portal_user_id = t.portal_user_id
WHERE t.time_start IS NOT NULL;

-- Минуты в системе по дням (Владивосток). Шаг опроса — 5 минут.
CREATE OR REPLACE VIEW v_manager_online_daily AS
SELECT
    (p.ts AT TIME ZONE 'Asia/Vladivostok')::date            AS day,
    p.portal_user_id,
    COALESCE(mg.full_name, p.portal_user_id::text)          AS manager,
    count(*) FILTER (WHERE p.is_online) * 5                 AS online_min,
    count(*)                                                AS samples,
    min(p.ts) FILTER (WHERE p.is_online)                    AS first_seen,
    max(p.ts) FILTER (WHERE p.is_online)                    AS last_seen
FROM user_presence p
LEFT JOIN managers mg ON mg.portal_user_id = p.portal_user_id
GROUP BY 1, 2, 3;

-- ── Лид с признаками отработки ──────────────────────────────────────────────
-- Один ряд на карточку лида. Звонки считаются ПО НОМЕРУ КЛИЕНТА, а не по карточке:
-- один человек регулярно порождает несколько карточек на разных менеджеров.
-- зависимые из 015 сносим первыми: они пересоздаются следующей миграцией
DROP VIEW IF EXISTS v_manager_ideal;
DROP VIEW IF EXISTS v_lead_ideal;
DROP VIEW IF EXISTS v_manager_result;
DROP VIEW IF EXISTS v_manager_weekly;
DROP VIEW IF EXISTS v_lead_worked;
CREATE VIEW v_lead_worked AS
SELECT
    l.id                                                            AS lead_id,
    l.date_create,
    date_trunc('week', l.date_create AT TIME ZONE 'Asia/Vladivostok')::date AS week,
    l.assigned_by,
    COALESCE(mg.full_name, l.assigned_by::text)                     AS manager,
    l.phone_e164,
    l.status_semantic,
    COALESCE(NULLIF(l.utm_source, ''), l.source_id)                 AS source,
    m.value                                                         AS mark,
    CASE WHEN m.value ~ '^[ABCD]' THEN left(m.value, 1) END         AS grade,
    t.first_out,
    t.first_contact,
    t.calls_own,
    t.calls_any,
    t.calls_3d,
    t.answered_any,
    t.in_answered,
    -- скорость реакции в РАБОЧИХ минутах до первого контакта
    work_minutes(l.date_create, t.first_contact)                    AS touch_min,
    -- настоящая потеря: по номеру не было НИ ОДНОГО контакта — ни исходящего,
    -- ни принятого входящего. Заявка от входящего звонка не «брошена» из-за того,
    -- что менеджер не перезвонил: он уже говорил с клиентом.
    (t.contacts_any = 0)                                            AS untouched,
    -- дубль: своя карточка не тронута, но клиента ведут по соседней
    (t.contacts_own = 0 AND t.contacts_any > 0)                     AS worked_elsewhere,
    (t.answered_any > 0 OR t.in_answered > 0)                       AS reached
FROM leads l
LEFT JOIN managers mg ON mg.portal_user_id = l.assigned_by
LEFT JOIN lead_marks m ON m.lead_id = l.id
LEFT JOIN LATERAL (
    SELECT
        min(c.call_start) FILTER (WHERE c.direction = 'out')                     AS first_out,
        count(*) FILTER (WHERE c.direction = 'out'
                           AND c.portal_user_id = l.assigned_by)                 AS calls_own,
        count(*) FILTER (WHERE c.direction = 'out')                              AS calls_any,
        count(*) FILTER (WHERE c.direction = 'out'
                           AND c.call_start < l.date_create + interval '3 days') AS calls_3d,
        count(*) FILTER (WHERE c.direction = 'out' AND c.duration > 0)           AS answered_any,
        min(c.call_start) FILTER (WHERE c.direction = 'out'
                            OR (c.direction = 'in' AND c.duration > 0))           AS first_contact,
        count(*) FILTER (WHERE c.direction = 'in' AND c.duration > 0)            AS in_answered,
        count(*) FILTER (WHERE (c.direction = 'out'
                                OR (c.direction = 'in' AND c.duration > 0))
                           AND c.portal_user_id = l.assigned_by)                 AS contacts_own,
        count(*) FILTER (WHERE c.direction = 'out'
                           OR (c.direction = 'in' AND c.duration > 0))           AS contacts_any
    FROM calls c
    WHERE c.phone_e164 = l.phone_e164
      AND c.call_start >= l.date_create - interval '30 minutes'
) t ON TRUE
WHERE l.phone_kind = 'mobile'
  AND l.source_id IS DISTINCT FROM 'PARTNER'
  AND COALESCE(l.utm_source, '') <> 'PARTNER';

-- ── Недельный портрет ───────────────────────────────────────────────────────
DROP VIEW IF EXISTS v_manager_weekly;
CREATE VIEW v_manager_weekly AS
WITH wk AS (
    SELECT week, assigned_by AS uid, manager,
           count(*)                                                  AS leads,
           count(*) FILTER (WHERE grade = 'A')                       AS a_leads,
           count(*) FILTER (WHERE grade = 'B')                       AS b_leads,
           count(*) FILTER (WHERE grade IS NOT NULL)                 AS graded,
           count(*) FILTER (WHERE untouched)                         AS untouched,
           count(*) FILTER (WHERE worked_elsewhere)                  AS dupes,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY touch_min)
               FILTER (WHERE first_contact IS NOT NULL)              AS p50_touch_min,
           count(*) FILTER (WHERE first_contact IS NOT NULL AND touch_min <= 15) AS touched_15m,
           count(*) FILTER (WHERE reached)                           AS reached,
           -- упорство: не дозвонились, но сделали не меньше 5 попыток за 3 дня
           count(*) FILTER (WHERE NOT reached AND calls_3d >= 5)     AS persisted,
           count(*) FILTER (WHERE NOT reached)                       AS not_reached
    FROM v_lead_worked
    GROUP BY 1, 2, 3
),
cl AS (
    SELECT date_trunc('week', c.call_start AT TIME ZONE 'Asia/Vladivostok')::date AS week,
           c.portal_user_id AS uid,
           count(*) FILTER (WHERE c.direction = 'out')                 AS out_calls,
           count(*) FILTER (WHERE c.direction = 'out' AND c.duration > 0) AS answered_calls,
           round(sum(c.duration) FILTER (WHERE c.duration > 0) / 60.0)  AS talk_min,
           count(DISTINCT (c.call_start AT TIME ZONE 'Asia/Vladivostok')::date) AS active_days
    FROM calls c
    WHERE c.phone_kind <> 'junk'
    GROUP BY 1, 2
),
ms AS (  -- пропущенные входящие и перезвон по этому номеру (логика v_missed_with_callback,
         -- продублирована намеренно: не хотим зависеть от порядка миграций)
    SELECT date_trunc('week', m.call_start AT TIME ZONE 'Asia/Vladivostok')::date AS week,
           m.portal_user_id AS uid,
           count(*)                                                    AS missed,
           count(*) FILTER (WHERE cb.delay_min <= 60)                  AS cb_1h,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY cb.delay_min)   AS p50_cb_min
    FROM calls m
    LEFT JOIN LATERAL (
        SELECT EXTRACT(EPOCH FROM (c.call_start - m.call_start)) / 60.0 AS delay_min
        FROM calls c
        WHERE c.phone_number = m.phone_number AND c.direction = 'out'
          AND c.call_start > m.call_start AND c.duration > 0
        ORDER BY c.call_start LIMIT 1
    ) cb ON TRUE
    WHERE m.direction = 'in' AND m.is_missed AND m.phone_kind <> 'junk'
    GROUP BY 1, 2
),
sc AS (  -- качество разговора: только оценка модели, механика на моно-записи не работает
    SELECT date_trunc('week', c.call_start AT TIME ZONE 'Asia/Vladivostok')::date AS week,
           c.portal_user_id AS uid,
           count(*)                                                    AS scored,
           round(avg(s.card_score)::numeric, 1)                        AS card_score,
           count(s.card_score)                                         AS carded,
           round(avg(s.talk_share)::numeric, 2)                        AS talk_share,
           round(avg(s.questions_manager)::numeric, 1)                 AS questions,
           count(*) FILTER (WHERE s.need_identified)                   AS need_ok,
           count(*) FILTER (WHERE s.price_named)                       AS price_named,
           count(*) FILTER (WHERE s.next_step_dated)                   AS step_dated,
           count(*) FILTER (WHERE array_length(s.objections, 1) > 0)   AS with_obj,
           count(*) FILTER (WHERE s.objections_handled)                AS obj_ok
    FROM call_scores s
    JOIN calls c ON c.id = s.call_id
    GROUP BY 1, 2
),
pr AS (  -- обещания: договорённость с датой и был ли дозвон в окне [−1; +2] дня
    SELECT date_trunc('week', f.due_date::timestamp)::date            AS week,
           f.manager_id                                               AS uid,
           count(*)                                                   AS promises,
           count(*) FILTER (WHERE k.kept)                             AS promises_kept
    FROM followups f
    LEFT JOIN LATERAL (
        SELECT true AS kept FROM calls c
        WHERE c.phone_e164 = f.phone_e164 AND c.direction = 'out' AND c.duration > 0
          AND c.call_start >= (f.due_date - 1)::timestamptz
          AND c.call_start <  (f.due_date + 3)::timestamptz
        LIMIT 1
    ) k ON TRUE
    GROUP BY 1, 2
),
onl AS (
    SELECT date_trunc('week', day)::date AS week, portal_user_id AS uid,
           round(sum(online_min) / 60.0, 1)                           AS online_h,
           count(*) FILTER (WHERE online_min > 0)                     AS online_days
    FROM v_manager_online_daily
    GROUP BY 1, 2
),
tm AS (
    SELECT date_trunc('week', day)::date AS week, portal_user_id AS uid,
           round(sum(duration_sec) / 3600.0, 1)                       AS tm_hours,
           count(*)                                                   AS tm_days,
           percentile_cont(0.5) WITHIN GROUP (
               ORDER BY EXTRACT(EPOCH FROM (time_start AT TIME ZONE 'Asia/Vladivostok')
                                - date_trunc('day', time_start AT TIME ZONE 'Asia/Vladivostok'))
           )::int                                                     AS tm_start_sec
    FROM timeman_days
    WHERE time_start IS NOT NULL
    GROUP BY 1, 2
)
SELECT
    COALESCE(wk.week, cl.week)                                        AS week,
    COALESCE(wk.uid, cl.uid)                                          AS portal_user_id,
    COALESCE(wk.manager, mg.full_name, cl.uid::text)                  AS manager,
    COALESCE(wk.leads, 0)          AS leads,
    COALESCE(wk.a_leads, 0)        AS a_leads,
    COALESCE(wk.b_leads, 0)        AS b_leads,
    COALESCE(wk.graded, 0)         AS graded,
    COALESCE(wk.untouched, 0)      AS untouched,
    COALESCE(wk.dupes, 0)          AS dupes,
    round(wk.p50_touch_min)::int   AS p50_touch_min,
    COALESCE(wk.touched_15m, 0)    AS touched_15m,
    COALESCE(wk.reached, 0)        AS reached,
    COALESCE(wk.persisted, 0)      AS persisted,
    COALESCE(wk.not_reached, 0)    AS not_reached,
    COALESCE(cl.out_calls, 0)      AS out_calls,
    COALESCE(cl.answered_calls, 0) AS answered_calls,
    COALESCE(cl.talk_min, 0)       AS talk_min,
    COALESCE(cl.active_days, 0)    AS active_days,
    COALESCE(ms.missed, 0)         AS missed,
    COALESCE(ms.cb_1h, 0)          AS cb_1h,
    round(ms.p50_cb_min)::int      AS p50_cb_min,
    COALESCE(sc.scored, 0)         AS scored,
    sc.card_score, sc.carded, sc.talk_share, sc.questions,
    COALESCE(sc.need_ok, 0)        AS need_ok,
    COALESCE(sc.price_named, 0)    AS price_named,
    COALESCE(sc.step_dated, 0)     AS step_dated,
    COALESCE(sc.with_obj, 0)       AS with_obj,
    COALESCE(sc.obj_ok, 0)         AS obj_ok,
    COALESCE(pr.promises, 0)       AS promises,
    COALESCE(pr.promises_kept, 0)  AS promises_kept,
    onl.online_h, onl.online_days,
    tm.tm_hours, tm.tm_days, tm.tm_start_sec
FROM wk
FULL JOIN cl  ON cl.week = wk.week AND cl.uid = wk.uid
LEFT JOIN ms  ON ms.week = COALESCE(wk.week, cl.week) AND ms.uid = COALESCE(wk.uid, cl.uid)
LEFT JOIN sc  ON sc.week = COALESCE(wk.week, cl.week) AND sc.uid = COALESCE(wk.uid, cl.uid)
LEFT JOIN pr  ON pr.week = COALESCE(wk.week, cl.week) AND pr.uid = COALESCE(wk.uid, cl.uid)
LEFT JOIN onl ON onl.week = COALESCE(wk.week, cl.week) AND onl.uid = COALESCE(wk.uid, cl.uid)
LEFT JOIN tm  ON tm.week = COALESCE(wk.week, cl.week) AND tm.uid = COALESCE(wk.uid, cl.uid)
LEFT JOIN managers mg ON mg.portal_user_id = COALESCE(wk.uid, cl.uid);

-- ── Результат: только вызревшие лиды ────────────────────────────────────────
-- Окно [сегодня−135; сегодня−45] дней: 90-й процентиль цикла сделки 38 дней,
-- свежие лиды дают заниженную конверсию (правило №6).
DROP VIEW IF EXISTS v_manager_result;
CREATE VIEW v_manager_result AS
SELECT
    w.assigned_by                                          AS portal_user_id,
    w.manager,
    count(*)                                               AS leads,
    count(*) FILTER (WHERE w.status_semantic = 'S')        AS won,
    round(100.0 * count(*) FILTER (WHERE w.status_semantic = 'S') / NULLIF(count(*), 0), 2) AS conv_pct,
    count(*) FILTER (WHERE w.grade IN ('A', 'B'))          AS ab_leads,
    round(100.0 * count(*) FILTER (WHERE w.status_semantic = 'S' AND w.grade IN ('A','B'))
          / NULLIF(count(*) FILTER (WHERE w.grade IN ('A','B')), 0), 2) AS conv_ab_pct,
    round(100.0 * count(*) FILTER (WHERE w.status_semantic = 'S' AND w.grade IN ('C','D'))
          / NULLIF(count(*) FILTER (WHERE w.grade IN ('C','D')), 0), 2) AS conv_cd_pct,
    COALESCE(sum(d.won_sum), 0)                            AS revenue
FROM v_lead_worked w
LEFT JOIN LATERAL (
    SELECT sum(opportunity) AS won_sum FROM deals
    WHERE lead_id = w.lead_id AND stage_semantic = 'S'
) d ON TRUE
WHERE w.date_create >= now() - interval '135 days'
  AND w.date_create <  now() - interval '45 days'
GROUP BY 1, 2;
