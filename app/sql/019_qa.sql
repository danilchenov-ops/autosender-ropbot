-- 019_qa.sql — QA-карточка разговора: 16 метрик по четырём блокам.
-- Заполняется app/extractor.py тем же вызовом модели, что и остальной разбор
-- (входные токены не дублируются). A3 считается кодом по ролям, не моделью.
-- Идемпотентно.

CREATE TABLE IF NOT EXISTS call_qa (
    call_id        bigint PRIMARY KEY,
    portal_user_id integer,
    call_start     timestamptz,
    a1 numeric, a2 numeric, a3 numeric,
    b1 numeric, b2 numeric, b3 numeric, b4 numeric, b5 numeric,
    c1 numeric, c2 numeric, c3 numeric, c4 numeric, c5 numeric,
    d1 numeric, d2 numeric, d3 numeric,
    block_a  numeric,
    block_b  numeric,
    block_c  numeric,
    block_d  numeric,
    integral numeric,          -- A*0.25 + B*0.30 + C*0.35 + D*0.10
    talk_share   numeric,      -- доля слов менеджера, считается кодом
    max_mono_sec numeric,      -- самый долгий монолог менеджера, сек
    roles        text,         -- строка М/К по репликам (от модели)
    quotes       jsonb,        -- {метрика: цитата-доказательство}
    na           text[],       -- метрики, помеченные N/A (этап не нужен)
    model        text,
    created_at   timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_call_qa_user  ON call_qa (portal_user_id, call_start DESC);
CREATE INDEX IF NOT EXISTS idx_call_qa_start ON call_qa (call_start DESC);
