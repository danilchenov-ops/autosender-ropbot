-- 017_traits.sql — характеристики ведения разговора.
-- Пять черт — перегруппировка критериев карточки эталонного скрипта (веса те же,
-- сводный процент менеджера = средний card_score, шкала одна с панелями).
-- Плюс видео-приём: находка ревизии скрипта 22.08, считается регулярным
-- выражением по расшифровке, в карточку пока не входит.
-- Идемпотентно. Зависит от call_scores.raw (разборы с флагами карточки).

DROP VIEW IF EXISTS v_manager_traits;
DROP VIEW IF EXISTS v_call_traits;
CREATE VIEW v_call_traits AS
SELECT
    s.call_id,
    c.portal_user_id,
    c.call_start,
    date_trunc('week',  c.call_start AT TIME ZONE 'Asia/Vladivostok')::date AS week,
    date_trunc('month', c.call_start AT TIME ZONE 'Asia/Vladivostok')::date AS month,
    s.card_score,
    -- Глубина: вытащил задачу клиента и привязал выгоду к его словам (вес 25)
    (COALESCE((s.raw->>'need_identified')::boolean, false)
     AND COALESCE((s.raw->>'linked_to_client_pain')::boolean,
                  (s.raw->>'linked_to_client-pain')::boolean, false))       AS t_glubina,
    -- Квалификация: кто решает и когда покупка (вес 23)
    (COALESCE((s.raw->>'decision_maker_identified')::boolean, false)
     OR COALESCE((s.raw->>'timeline_discussed')::boolean, false))           AS t_kval,
    -- Конкретика: цена названа и дата/время зафиксированы (вес 21)
    (COALESCE((s.raw->>'price_named')::boolean, false)
     AND COALESCE((s.raw->>'date_time_fixed')::boolean, false))             AS t_konkretika,
    -- Доведение: сам предложил конкретный следующий шаг (вес 22)
    (COALESCE((s.raw->>'next_step_proposed')::boolean, false)
     AND COALESCE((s.raw->>'next_step_specific')::boolean, false))          AS t_dovedenie,
    -- Настойчивость: возражения были и отработаны, а не приняты (вес 9)
    (jsonb_array_length(COALESCE(s.raw->'objections', '[]'::jsonb)) > 0)    AS had_obj,
    (jsonb_array_length(COALESCE(s.raw->'objections', '[]'::jsonb)) > 0
     AND COALESCE((s.raw->>'objections_handled')::boolean, false))          AS t_nastoychivost,
    -- Видео-приём (вне процента, кандидат карточки следующей ревизии)
    (t.text ~* 'видео|видос|трансляц|запись экрана')                        AS t_video
FROM call_scores s
JOIN calls c ON c.id = s.call_id
LEFT JOIN transcripts t ON t.call_id = s.call_id
WHERE s.raw ? 'next_step_proposed'                    -- только разборы с флагами карточки
  AND s.outcome NOT IN ('не дозвонились', 'нецелевой');

CREATE VIEW v_manager_traits AS
SELECT
    v.portal_user_id,
    COALESCE(mg.full_name, v.portal_user_id::text)              AS manager,
    v.week,
    v.month,
    count(*)                                                    AS talks,
    round(avg(card_score), 1)                                   AS ball,
    round(100.0 * count(*) FILTER (WHERE t_glubina) / count(*)) AS glubina,
    round(100.0 * count(*) FILTER (WHERE t_kval) / count(*))    AS kval,
    round(100.0 * count(*) FILTER (WHERE t_konkretika) / count(*)) AS konkretika,
    round(100.0 * count(*) FILTER (WHERE t_dovedenie) / count(*))  AS dovedenie,
    round(100.0 * count(*) FILTER (WHERE t_nastoychivost)
          / NULLIF(count(*) FILTER (WHERE had_obj), 0))         AS nastoychivost,
    round(100.0 * count(*) FILTER (WHERE t_video) / count(*))   AS video
FROM v_call_traits v
LEFT JOIN managers mg ON mg.portal_user_id = v.portal_user_id
GROUP BY 1, 2, 3, 4;
