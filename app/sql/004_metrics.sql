-- Метрики, которые считаются и для одноканальных записей

ALTER TABLE call_metrics ADD COLUMN IF NOT EXISTS words_total        INTEGER;
ALTER TABLE call_metrics ADD COLUMN IF NOT EXISTS segments_count     INTEGER;
ALTER TABLE call_metrics ADD COLUMN IF NOT EXISTS wpm                NUMERIC;   -- слов в минуту речи
ALTER TABLE call_metrics ADD COLUMN IF NOT EXISTS questions_total    INTEGER;   -- вопросы по всему диалогу
ALTER TABLE call_metrics ADD COLUMN IF NOT EXISTS longest_block_sec  NUMERIC;   -- самый длинный непрерывный кусок речи
ALTER TABLE call_metrics ADD COLUMN IF NOT EXISTS price_mentioned    BOOLEAN;
ALTER TABLE call_metrics ADD COLUMN IF NOT EXISTS next_step_hint     BOOLEAN;
ALTER TABLE call_metrics ADD COLUMN IF NOT EXISTS has_roles          BOOLEAN;   -- были ли раздельные каналы

-- Свод по разговорам: то, что видно без языковой модели
DROP VIEW IF EXISTS v_talk_quality;
CREATE VIEW v_talk_quality AS
SELECT
    c.id                AS call_id,
    c.call_start,
    c.direction,
    c.duration,
    COALESCE(mg.full_name, c.portal_user_id::text) AS manager,
    c.crm_entity_type,
    c.crm_entity_id,
    m.words_total,
    m.wpm,
    m.questions_total,
    m.longest_block_sec,
    m.monologue_flag,
    m.silence_ratio,
    m.longest_pause_sec,
    m.price_mentioned,
    m.next_step_hint,
    m.stopwords,
    m.has_roles,
    m.manager_talk_ratio
FROM transcripts t
JOIN calls c            ON c.id = t.call_id
LEFT JOIN managers mg   ON mg.portal_user_id = c.portal_user_id
LEFT JOIN call_metrics m ON m.call_id = t.call_id;
