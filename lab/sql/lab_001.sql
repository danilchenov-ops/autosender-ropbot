-- Лаборатория «Эталонный скрипт»: отдельные таблицы, продовые не трогаем.
-- Идемпотентно.

CREATE TABLE IF NOT EXISTS lab_sample (
    call_id      bigint PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
    client       text NOT NULL,               -- phone_e164, единица «клиент»
    grp          text NOT NULL,               -- WON | LOST
    seq          int  NOT NULL,               -- номер разговора внутри клиента
    is_first     boolean NOT NULL,            -- первый содержательный разговор
    split        text NOT NULL,               -- train | holdout
    source       text,
    campaign     text,
    manager      text,
    manager_id   int,
    call_start   timestamptz,
    direction    text,
    duration     int,
    deal_sum     numeric,
    lost_status  text,                        -- название статуса лида (для LOST)
    lead_id      bigint,
    excluded     text                         -- причина исключения или NULL
);
CREATE INDEX IF NOT EXISTS idx_lab_sample_grp ON lab_sample(grp, split);
CREATE INDEX IF NOT EXISTS idx_lab_sample_client ON lab_sample(client);

CREATE TABLE IF NOT EXISTS lab_cards (
    call_id     bigint PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
    model       text,
    card        jsonb,
    roles       text,                         -- строка ролей по строкам транскрипта
    lines_n     int,
    talk_share  numeric,                      -- доля слов менеджера, считается детерминированно
    tokens_in   int,
    tokens_out  int,
    err         text,
    created_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_lab_cards_err ON lab_cards(err) WHERE err IS NOT NULL;

CREATE TABLE IF NOT EXISTS lab_bench (
    id          bigserial PRIMARY KEY,
    call_id     bigint,
    model       text,
    card        jsonb,
    roles       text,
    quote_hits  int,
    quote_total int,
    tokens_in   int,
    tokens_out  int,
    sec         numeric,
    err         text,
    created_at  timestamptz DEFAULT now()
);
