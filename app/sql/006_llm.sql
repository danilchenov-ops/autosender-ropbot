-- Разбор разговоров языковой моделью: извлечённые факты и договорённости

CREATE TABLE IF NOT EXISTS call_extractions (
    call_id             BIGINT PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
    agreement           BOOLEAN,
    due_date            DATE,
    date_approximate    BOOLEAN,
    phrase              TEXT,        -- как срок назван словами
    quote               TEXT,        -- цитата из разговора
    initiator           TEXT,        -- клиент / менеджер
    promised_by_manager TEXT,
    confidence          NUMERIC,
    budget_rub          NUMERIC,
    car                 TEXT,
    city                TEXT,
    model               TEXT,
    tokens_in           INTEGER,
    tokens_out          INTEGER,
    raw                 JSONB,
    created_at          TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_extr_due ON call_extractions (due_date)
    WHERE agreement;

CREATE TABLE IF NOT EXISTS llm_queue (
    call_id    BIGINT PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'pending',   -- pending / processing / done / failed / skipped
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llmq ON llm_queue (status, updated_at);

-- Напоминания. Пока только копятся, ничего не отправляется.
CREATE TABLE IF NOT EXISTS followups (
    id           BIGSERIAL PRIMARY KEY,
    call_id      BIGINT UNIQUE REFERENCES calls(id) ON DELETE CASCADE,
    lead_id      BIGINT,
    manager_id   INTEGER,
    phone_e164   TEXT,
    due_date     DATE NOT NULL,
    approximate  BOOLEAN DEFAULT FALSE,
    quote        TEXT,
    promised     TEXT,
    confidence   NUMERIC,
    status       TEXT NOT NULL DEFAULT 'new',   -- new / contacted / sent / dismissed / stale
    contacted_at TIMESTAMPTZ,                   -- если менеджер связался сам
    created_at   TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_fu_due ON followups (due_date, status);
CREATE INDEX IF NOT EXISTS idx_fu_mgr ON followups (manager_id);

-- Что созрело сегодня и по чему менеджер ещё не связался
DROP VIEW IF EXISTS v_followups_due;
CREATE VIEW v_followups_due AS
SELECT f.id, f.call_id, f.lead_id, f.due_date, f.approximate,
       COALESCE(mg.full_name, f.manager_id::text) AS manager,
       f.phone_e164, f.quote, f.promised, f.confidence,
       c.call_start          AS agreed_at,
       l.title               AS lead_title,
       s.name                AS lead_status,
       -- был ли контакт после того разговора
       (SELECT max(c2.call_start) FROM calls c2
         WHERE c2.direction = 'out' AND c2.duration > 0
           AND phone_e164(c2.phone_number) = f.phone_e164
           AND c2.call_start > c.call_start) AS contacted_since
FROM followups f
JOIN calls c            ON c.id = f.call_id
LEFT JOIN managers mg   ON mg.portal_user_id = f.manager_id
LEFT JOIN leads l       ON l.id = f.lead_id
LEFT JOIN crm_dict s    ON s.kind = 'STATUS' AND s.status_id = l.status_id
WHERE f.status = 'new' AND f.due_date <= current_date;
