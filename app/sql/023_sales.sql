-- 023_sales.sql — подтверждение продаж руководителем.
-- Решение Тимофея 02.09.2026: менеджер иногда ставит «ТС куплен» ошибочно
-- (у Симаненко 5 из 12 за август — свежие заявки, прыгнувшие в конец воронки
-- за часы, без торгов и без сделки). Теперь каждый переход в стадию 13
-- уходит Светлане в бот вопросом с кнопками, и счёт идёт по её ответу.
-- Идемпотентно.

CREATE TABLE IF NOT EXISTS sale_confirm (
    lead_id      bigint PRIMARY KEY,
    portal_user_id integer,
    t13          timestamptz NOT NULL,     -- когда лид попал в стадию 13
    chat_id      bigint,                   -- кому ушёл вопрос
    message_id   bigint,
    sent_at      timestamptz,
    answer       text,                     -- 'yes' / 'no' / NULL (ждём)
    answered_at  timestamptz,
    answered_by  bigint,
    note         text
);

CREATE INDEX IF NOT EXISTS sale_confirm_t13 ON sale_confirm (t13);
CREATE INDEX IF NOT EXISTS sale_confirm_answer ON sale_confirm (answer);

-- Дата, с которой продажи считаются по подтверждению руководителя.
-- Всё, что раньше, считается автоматическим правилом: лид был на торгах
-- и у него есть связанная сделка (проверено на августе — совпало
-- с ручным учётом Тимофея у четверых из пяти менеджеров).
DROP VIEW IF EXISTS v_sales;
CREATE VIEW v_sales AS
WITH t13 AS (
    SELECT owner_id AS lead_id, min(created_time) AS t13
      FROM stage_history
     WHERE entity_kind = 'lead' AND stage_id = '13'
     GROUP BY owner_id)
SELECT l.id                       AS lead_id,
       l.assigned_by              AS portal_user_id,
       t.t13,
       (l.date_create AT TIME ZONE 'Asia/Vladivostok')::date AS lead_day,
       (t.t13 AT TIME ZONE 'Asia/Vladivostok')::date         AS sale_day,
       c.answer,
       -- прошёл торги и есть сделка — «защита от дурака» без человека
       (EXISTS (SELECT 1 FROM stage_history s
                 WHERE s.entity_kind = 'lead' AND s.owner_id = l.id
                   AND s.stage_id = '12')
        AND EXISTS (SELECT 1 FROM deals d WHERE d.lead_id = l.id)) AS auto_ok,
       CASE
           WHEN c.answer = 'yes' THEN TRUE
           WHEN c.answer = 'no'  THEN FALSE
           ELSE (EXISTS (SELECT 1 FROM stage_history s
                          WHERE s.entity_kind = 'lead' AND s.owner_id = l.id
                            AND s.stage_id = '12')
                 AND EXISTS (SELECT 1 FROM deals d WHERE d.lead_id = l.id))
       END                        AS counted
  FROM t13 t
  JOIN leads l ON l.id = t.lead_id
  LEFT JOIN sale_confirm c ON c.lead_id = l.id;
