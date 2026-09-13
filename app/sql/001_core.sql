-- Схема базы «Виртуальный РОП»
-- Аудио не хранится. Хранятся только метаданные, расшифровки и оценки.

CREATE TABLE IF NOT EXISTS managers (
    portal_user_id  INTEGER PRIMARY KEY,
    name            TEXT,
    last_name       TEXT,
    full_name       TEXT,
    email           TEXT,
    department      TEXT,
    active          BOOLEAN DEFAULT TRUE,
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS calls (
    id                BIGSERIAL PRIMARY KEY,
    b24_call_id       TEXT UNIQUE NOT NULL,
    portal_user_id    INTEGER,
    call_type         INTEGER,              -- 1 исходящий, 2 входящий, 3 входящий с перенаправлением, 4 обратный
    direction         TEXT,                 -- in / out
    phone_number      TEXT,
    portal_number     TEXT,
    call_start        TIMESTAMPTZ,
    duration          INTEGER,              -- секунды разговора
    failed_code       TEXT,                 -- 200 успешно, 304 отклонён, 603/486 занято и т.д.
    is_missed         BOOLEAN,
    crm_entity_type   TEXT,
    crm_entity_id     BIGINT,
    crm_activity_id   BIGINT,
    record_url        TEXT,
    record_file_id    BIGINT,
    record_duration   INTEGER,
    cost              NUMERIC,
    raw               JSONB,
    created_at        TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_calls_start      ON calls (call_start DESC);
CREATE INDEX IF NOT EXISTS idx_calls_user       ON calls (portal_user_id);
CREATE INDEX IF NOT EXISTS idx_calls_phone      ON calls (phone_number);
CREATE INDEX IF NOT EXISTS idx_calls_missed     ON calls (is_missed) WHERE is_missed;
CREATE INDEX IF NOT EXISTS idx_calls_crm        ON calls (crm_entity_type, crm_entity_id);

-- Очередь расшифровки
CREATE TABLE IF NOT EXISTS asr_queue (
    call_id      BIGINT PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending / processing / done / failed / skipped
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT,
    updated_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_asr_status ON asr_queue (status, updated_at);

CREATE TABLE IF NOT EXISTS transcripts (
    call_id         BIGINT PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
    text            TEXT,
    segments        JSONB,        -- [{start, end, speaker, text}]
    language        TEXT,
    model           TEXT,
    stereo          BOOLEAN,      -- были ли раздельные каналы (надёжное разделение ролей)
    audio_sec       NUMERIC,
    processing_sec  NUMERIC,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Метрики по правилам, без LLM, считаются для 100% расшифрованных звонков
CREATE TABLE IF NOT EXISTS call_metrics (
    call_id             BIGINT PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
    manager_talk_ratio  NUMERIC,   -- доля времени, которую говорит менеджер
    longest_pause_sec   NUMERIC,
    silence_ratio       NUMERIC,
    interruptions       INTEGER,
    words_manager       INTEGER,
    words_client        INTEGER,
    questions_manager   INTEGER,
    monologue_flag      BOOLEAN,   -- монолог менеджера дольше 60 сек
    stopwords           TEXT[],    -- сработавшие стоп-слова
    computed_at         TIMESTAMPTZ DEFAULT now()
);

-- Оценка звонка языковой моделью
CREATE TABLE IF NOT EXISTS call_scores (
    call_id             BIGINT PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
    need_identified     BOOLEAN,
    price_named         BOOLEAN,
    objections          TEXT[],
    objections_handled  BOOLEAN,
    next_step           TEXT,
    next_step_dated     BOOLEAN,
    outcome             TEXT,
    score               INTEGER,   -- 0..10 по чек-листу
    summary             TEXT,
    raw                 JSONB,
    model               TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sync_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ── Представления для анализа ────────────────────────────────────────────────

-- Пропущенные входящие и был ли перезвон этому номеру
CREATE OR REPLACE VIEW v_missed_with_callback AS
SELECT
    m.id                AS call_id,
    m.call_start,
    m.phone_number,
    m.portal_user_id,
    mg.full_name        AS manager,
    cb.call_start       AS callback_at,
    cb.portal_user_id   AS callback_by,
    EXTRACT(EPOCH FROM (cb.call_start - m.call_start)) / 60.0 AS callback_delay_min
FROM calls m
LEFT JOIN managers mg ON mg.portal_user_id = m.portal_user_id
LEFT JOIN LATERAL (
    SELECT c.call_start, c.portal_user_id
    FROM calls c
    WHERE c.phone_number = m.phone_number
      AND c.direction = 'out'
      AND c.call_start > m.call_start
      AND c.duration > 0
    ORDER BY c.call_start
    LIMIT 1
) cb ON TRUE
WHERE m.direction = 'in' AND m.is_missed;

-- Сводка по менеджерам за период
CREATE OR REPLACE VIEW v_manager_daily AS
SELECT
    date_trunc('day', c.call_start)                              AS day,
    c.portal_user_id,
    COALESCE(mg.full_name, c.portal_user_id::text)               AS manager,
    count(*) FILTER (WHERE c.direction = 'out')                  AS out_calls,
    count(*) FILTER (WHERE c.direction = 'in')                   AS in_calls,
    count(*) FILTER (WHERE c.direction = 'in' AND c.is_missed)   AS missed,
    sum(c.duration) FILTER (WHERE c.duration > 0)                AS talk_sec,
    round(avg(c.duration) FILTER (WHERE c.duration > 20), 1)     AS avg_talk_sec,
    round(avg(m.manager_talk_ratio)::numeric, 3)                 AS avg_talk_ratio,
    round(avg(s.score)::numeric, 2)                              AS avg_score
FROM calls c
LEFT JOIN managers mg    ON mg.portal_user_id = c.portal_user_id
LEFT JOIN call_metrics m ON m.call_id = c.id
LEFT JOIN call_scores  s ON s.call_id = c.id
GROUP BY 1, 2, 3;
