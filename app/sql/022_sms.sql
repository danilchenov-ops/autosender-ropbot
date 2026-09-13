-- СМС от менеджеров через SMSGate (sms-gate.app, режим Cloud server).
-- Решение Тимофея 25.08.2026: правило №1 — новая заявка → СМС-приветствие
-- от закреплённого менеджера, не чаще одной отправки на номер за месяц.
-- Запуск отложен: сначала витрина у РОПа и заполнение реквизитов.

-- Телефон менеджера как шлюз. Логин/пароль — из приложения SMSGate
-- (Settings → Cloud server) на телефоне каждого менеджера.
CREATE TABLE IF NOT EXISTS sms_devices (
    portal_user_id integer PRIMARY KEY,
    login          text NOT NULL,
    password       text NOT NULL,
    active         boolean NOT NULL DEFAULT true,
    note           text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- Очередь и журнал отправок. Строка создаётся правилом (status='planned'),
-- отправляется демоном (sent → delivered/failed по вебхуку или опросу).
CREATE TABLE IF NOT EXISTS sms_outbox (
    id          bigserial PRIMARY KEY,
    rule        text NOT NULL,                 -- 'new_lead' | 'manual' | ...
    lead_id     bigint,
    phone_e164  text NOT NULL,
    portal_user_id integer NOT NULL,           -- с чьего телефона шлём
    body        text NOT NULL,
    parts       smallint NOT NULL DEFAULT 1,   -- во сколько СМС уложился текст
    status      text NOT NULL DEFAULT 'planned',
        -- planned: создана правилом, отправка ещё выключена или в очереди
        -- sent:    принята телефоном/облаком
        -- delivered / failed: финал по статусу шлюза
        -- no_report: телефон отправил, но оператор не вернул отчёт
        --            о доставке (нормальный исход, добавлен 31.08.2026)
        -- skipped: не отправлена (лимит на номер, нет устройства и т.п.)
    external_id text,                          -- id сообщения в sms-gate.app
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz,
    final_at    timestamptz
);

CREATE INDEX IF NOT EXISTS sms_outbox_phone_idx
    ON sms_outbox (phone_e164, created_at DESC);
CREATE INDEX IF NOT EXISTS sms_outbox_status_idx
    ON sms_outbox (status) WHERE status IN ('planned', 'sent');

-- Лимит «не чаще одной отправки на номер за месяц» контролирует правило
-- (окно 30 дней от последней НЕ-failed отправки), индекс выше — для проверки.
