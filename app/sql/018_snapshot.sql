-- 018_snapshot.sql — слепок работы менеджеров: ежедневный снимок параметров.
-- Заполняется app/snapshot.py (подаётся через stdin, в образ не запечён).
-- Ключ: дата снимка × менеджер × период × параметр × страта класса.
-- portal_user_id = 0 — весь отдел. grade: all / A / B / C / D / X (нет данных) /
-- std (взвешено по общеотдельскому составу классов — прямая стандартизация).
-- Идемпотентно.

CREATE TABLE IF NOT EXISTS manager_snapshot (
    snap_date      date        NOT NULL,
    portal_user_id integer     NOT NULL,
    period         text        NOT NULL,  -- '7d' | '30d' | 'mom' | 'mat'
    param          text        NOT NULL,  -- C1..T4p, см. snapshot.py
    grade          text        NOT NULL DEFAULT 'all',
    value          numeric,
    denom          numeric,
    created_at     timestamptz DEFAULT now(),
    PRIMARY KEY (snap_date, portal_user_id, period, param, grade)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_param
    ON manager_snapshot (param, period, snap_date);
