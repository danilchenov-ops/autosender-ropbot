-- Точный учёт подписок на Telegram-канал: событие chat_member от бота-администратора.
-- Заменяет цель Метрики `sub_tg`, которая считала клик по кнопке, а не факт подписки.

CREATE TABLE IF NOT EXISTS tg_channel_members (
    id           bigserial PRIMARY KEY,
    event_at     timestamptz NOT NULL DEFAULT now(),
    tg_date      timestamptz,                 -- время события по данным Telegram
    chat_id      bigint NOT NULL,
    chat_title   text,
    user_id      bigint NOT NULL,
    username     text,
    first_name   text,
    old_status   text,                        -- left / kicked / member / restricted / administrator
    new_status   text,
    action       text,                        -- join / leave / other
    invite_link  text,                        -- по какой ссылке вступил (если через ссылку)
    invite_name  text,                        -- имя ссылки = метка кампании
    via_request  boolean DEFAULT false,
    raw          jsonb
);
CREATE INDEX IF NOT EXISTS tg_channel_members_chat_time ON tg_channel_members (chat_id, event_at);
CREATE INDEX IF NOT EXISTS tg_channel_members_user ON tg_channel_members (chat_id, user_id);

-- Справочник пригласительных ссылок: ссылка → кампания/источник.
CREATE TABLE IF NOT EXISTS tg_invite_links (
    link        text PRIMARY KEY,
    chat_id     bigint,
    name        text,                         -- как назвали в Telegram
    campaign    text,                         -- метка для отчётов: hot / involved / seed / ...
    created_at  timestamptz DEFAULT now(),
    note        text
);

-- События про самого бота в чатах (добавили админом, сняли) — чтобы узнать chat_id канала.
CREATE TABLE IF NOT EXISTS tg_bot_chats (
    chat_id     bigint PRIMARY KEY,
    chat_type   text,
    title       text,
    bot_status  text,
    updated_at  timestamptz DEFAULT now(),
    raw         jsonb
);

-- Сводка по дням: вступления, выходы, чистый прирост, по ссылкам.
CREATE OR REPLACE VIEW v_tg_subs_daily AS
SELECT date_trunc('day', event_at AT TIME ZONE 'Europe/Moscow')::date AS day_msk,
       chat_id,
       coalesce(invite_name, '(без ссылки)') AS invite_name,
       count(*) FILTER (WHERE action='join')  AS joins,
       count(*) FILTER (WHERE action='leave') AS leaves,
       count(*) FILTER (WHERE action='join') - count(*) FILTER (WHERE action='leave') AS net
FROM tg_channel_members
GROUP BY 1,2,3;
