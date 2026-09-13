-- Сводка разговоров в карточке Битрикса: один комментарий на сущность,
-- обновляется при каждом новом разобранном звонке. Одобрено Тимофеем 22.08.2026.
CREATE TABLE IF NOT EXISTS b24_summary_comments (
    entity_type  text   NOT NULL,          -- LEAD | CONTACT
    entity_id    bigint NOT NULL,
    comment_id   bigint,                   -- ID комментария в таймлайне Битрикса
    content_hash text,                     -- md5 текста, чтобы не дёргать API зря
    updated_at   timestamptz DEFAULT now(),
    PRIMARY KEY (entity_type, entity_id)
);
