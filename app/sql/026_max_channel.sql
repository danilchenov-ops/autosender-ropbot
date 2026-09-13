-- Точный учёт подписок MAX: клики с прокладки, события канала от бота, привязка по времени.
-- В MAX нет персональных ссылок, поэтому вступление сопоставляется с кликом по окну времени.

CREATE TABLE IF NOT EXISTS max_clicks (
    id          bigserial PRIMARY KEY,
    pool_id     bigint UNIQUE,            -- id в sqlite прокладки
    clicked_at  timestamptz NOT NULL,
    client_id   text,                     -- ym ClientID
    yclid       text,
    campaign    text,                     -- hot / involved / <id> / null
    ua_hash     text,
    ip_hash     text,
    member_id   bigint                    -- вступление, к которому привязан клик
);
CREATE INDEX IF NOT EXISTS max_clicks_at ON max_clicks (clicked_at);

CREATE TABLE IF NOT EXISTS max_channel_members (
    id              bigserial PRIMARY KEY,
    pool_id         bigint UNIQUE,        -- id события в sqlite прокладки
    event_at        timestamptz NOT NULL, -- timestamp из Update (MAX)
    received_at     timestamptz,          -- когда вебхук дошёл до прокладки
    chat_id         bigint NOT NULL,
    user_id         bigint,
    first_name      text,
    action          text NOT NULL,        -- join / leave
    raw             jsonb,
    click_id        bigint,               -- привязанный клик (max_clicks.id)
    match_quality   text,                 -- exact / campaign / ambiguous / organic
    match_delay_s   integer,              -- секунд от клика до вступления
    client_id       text,
    yclid           text,
    campaign        text,
    metrika_sent_at timestamptz
);
CREATE INDEX IF NOT EXISTS max_channel_members_at ON max_channel_members (event_at);
CREATE INDEX IF NOT EXISTS max_channel_members_user ON max_channel_members (user_id);

CREATE TABLE IF NOT EXISTS max_channel_counts (
    at        timestamptz NOT NULL DEFAULT now(),
    chat_id   bigint NOT NULL,
    members   integer NOT NULL
);
CREATE INDEX IF NOT EXISTS max_channel_counts_chat ON max_channel_counts (chat_id, at);

-- Подписки MAX по дням и источникам (как v_tg_subs_daily).
CREATE OR REPLACE VIEW v_max_subs_daily AS
SELECT date_trunc('day', event_at AT TIME ZONE 'Europe/Moscow')::date AS day_msk,
       chat_id,
       CASE WHEN action='join' THEN coalesce(campaign, match_quality, 'organic') ELSE '(выход)' END AS source,
       count(*) FILTER (WHERE action='join')  AS joins,
       count(*) FILTER (WHERE action='leave') AS leaves,
       count(*) FILTER (WHERE action='join') - count(*) FILTER (WHERE action='leave') AS net
FROM max_channel_members
GROUP BY 1,2,3;

-- Путь клиента: подписчик MAX → лид (по ClientID) → сделка.
CREATE OR REPLACE VIEW v_max_subscriber_journey AS
SELECT m.event_at AS subscribed_at,
       m.chat_id, m.user_id, m.first_name,
       coalesce(m.campaign, m.match_quality) AS source,
       m.client_id,
       l.id           AS lead_id,
       l.date_create  AS lead_at,
       l.status_semantic AS lead_status,
       d.id           AS deal_id,
       d.stage_semantic AS deal_status,
       d.closedate    AS deal_closed_at,
       (d.stage_semantic = 'S') AS won
FROM max_channel_members m
LEFT JOIN leads l ON l.ym_client_id = m.client_id AND m.client_id IS NOT NULL
LEFT JOIN deals d ON d.lead_id = l.id
WHERE m.action = 'join';

-- Оба канала вместе.
CREATE OR REPLACE VIEW v_subs_daily AS
SELECT 'tg' AS channel, day_msk, invite_name AS source, joins, leaves, net FROM v_tg_subs_daily
UNION ALL
SELECT 'max', day_msk, source, joins, leaves, net FROM v_max_subs_daily;
