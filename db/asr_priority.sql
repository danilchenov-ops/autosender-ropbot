-- Очередь расшифровки: вперёд то, что нужно для портрета менеджера.
-- Машина упирается в процессор (4 ядра, ~0,8 реального времени на запись),
-- поэтому важен порядок, а не пропускная способность.
UPDATE asr_queue q
SET priority = p.want
FROM (
    SELECT c.id,
           CASE
             WHEN c.call_start > now() - interval '30 days'
                  AND c.duration >= 90 AND c.direction = 'out'
                  AND c.crm_entity_id IS NOT NULL           THEN 5
             WHEN c.call_start > now() - interval '30 days'
                  AND c.duration >= 90                      THEN 4
             WHEN c.duration >= 90                          THEN 3
             ELSE 1
           END AS want
    FROM calls c
    WHERE c.phone_kind <> 'junk'
) p
WHERE p.id = q.call_id AND q.status = 'pending' AND q.priority IS DISTINCT FROM p.want
  -- Приоритеты 6 и выше зарезервированы под ручные кампании (см.
  -- projects/etalonnyy-skript.md, lab/match_queue.py). Этот почасовой пересчёт
  -- их не трогает, иначе он затирает вручную поднятую выборку — что и случилось
  -- 21.08.2026 с зеркальной выборкой LOST.
  AND q.priority < 6;
