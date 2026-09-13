-- 020_qa_advice.sql — недельный план работы по каждому менеджеру.
-- Считается app/qa_advice.py (крон в ночь на понедельник), страница «Качество»
-- только показывает. Пересчёт раз в неделю — решение Тимофея 25.08.2026:
-- рекомендации не должны дёргаться каждый день, это план тренировки, а не лента.
-- Идемпотентно.

CREATE TABLE IF NOT EXISTS qa_advice (
    week_start     date    NOT NULL,   -- понедельник недели среза (Влд)
    portal_user_id integer NOT NULL,
    payload        jsonb   NOT NULL,   -- готовый разбор: метрики, возражения, план
    n_calls        integer,
    window_days    integer,
    created_at     timestamptz DEFAULT now(),
    PRIMARY KEY (week_start, portal_user_id)
);

CREATE INDEX IF NOT EXISTS idx_qa_advice_week ON qa_advice (week_start DESC);
