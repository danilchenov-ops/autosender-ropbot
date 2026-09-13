-- Журнал ленты «Контроль» РОПа (ТЗ 22.08.2026, inbox/tz-kontrol-ropa.md).
-- Карточка в ленте = открытая запись (resolved_at IS NULL).
-- Повторный вход той же заявки по той же причине — не раньше чем через 7 дней
-- после резолюции (проверяет web/control.py).

CREATE TABLE IF NOT EXISTS rop_control (
    id               bigserial PRIMARY KEY,
    lead_id          bigint      NOT NULL REFERENCES leads(id),
    entered_at       timestamptz NOT NULL DEFAULT now(),
    reason           text        NOT NULL CHECK (reason IN ('closed_alive', 'going_cold')),
    score_at_entry   numeric     NOT NULL,
    manager_at_entry integer,
    resolved_at      timestamptz,
    resolution       text        CHECK (resolution IN
        ('returned', 'reassigned', 'called', 'confirmed_dead', 'expired', 'closed', 'dismissed')),
    won              boolean     -- заполняется сверкой через месяц: вернули -> купил?
);

CREATE INDEX IF NOT EXISTS idx_rop_control_open
    ON rop_control (lead_id, reason) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_rop_control_entered ON rop_control (entered_at);

-- Открытая запись по (lead_id, reason) может быть только одна
CREATE UNIQUE INDEX IF NOT EXISTS uq_rop_control_open
    ON rop_control (lead_id, reason) WHERE resolved_at IS NULL;
