-- Привязка телеграм-аккаунтов к сотрудникам Битрикса

CREATE TABLE IF NOT EXISTS tg_users (
    chat_id     BIGINT PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    last_name   TEXT,
    manager_id  INTEGER REFERENCES managers(portal_user_id),
    is_boss     BOOLEAN DEFAULT FALSE,   -- получает общую сводку
    active      BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tg_manager ON tg_users (manager_id);

-- Журнал отправленных сообщений: защита от повторов
CREATE TABLE IF NOT EXISTS tg_sent (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT,
    kind        TEXT,         -- followup / digest / ...
    ref_id      BIGINT,       -- id напоминания или иной сущности
    sent_at     TIMESTAMPTZ DEFAULT now(),
    ok          BOOLEAN,
    error       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_sent_once ON tg_sent (kind, ref_id, chat_id)
    WHERE ok;

CREATE TABLE IF NOT EXISTS tg_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
