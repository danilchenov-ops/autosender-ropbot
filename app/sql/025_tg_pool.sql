-- Персональные пригласительные ссылки: кто получил, с каким ClientID, кто вступил.
-- Связка Telegram ↔ Метрика ↔ CRM для офлайн-конверсий и пути клиента.

ALTER TABLE tg_invite_links ADD COLUMN IF NOT EXISTS pool_id     bigint;      -- id в sqlite прокладки
ALTER TABLE tg_invite_links ADD COLUMN IF NOT EXISTS personal    boolean DEFAULT false;
ALTER TABLE tg_invite_links ADD COLUMN IF NOT EXISTS issued_at   timestamptz; -- когда выдана посетителю
ALTER TABLE tg_invite_links ADD COLUMN IF NOT EXISTS client_id   text;        -- ym ClientID
ALTER TABLE tg_invite_links ADD COLUMN IF NOT EXISTS yclid       text;
ALTER TABLE tg_invite_links ADD COLUMN IF NOT EXISTS ua_hash     text;
ALTER TABLE tg_invite_links ADD COLUMN IF NOT EXISTS ip_hash     text;
CREATE INDEX IF NOT EXISTS tg_invite_links_client ON tg_invite_links (client_id);

ALTER TABLE tg_channel_members ADD COLUMN IF NOT EXISTS client_id        text;
ALTER TABLE tg_channel_members ADD COLUMN IF NOT EXISTS yclid            text;
ALTER TABLE tg_channel_members ADD COLUMN IF NOT EXISTS campaign         text;   -- метка из ссылки
ALTER TABLE tg_channel_members ADD COLUMN IF NOT EXISTS metrika_sent_at  timestamptz;
CREATE INDEX IF NOT EXISTS tg_channel_members_client ON tg_channel_members (client_id);

-- Контрольная сумма: число участников канала по данным Telegram, раз в запуск.
CREATE TABLE IF NOT EXISTS tg_channel_counts (
    at        timestamptz NOT NULL DEFAULT now(),
    chat_id   bigint NOT NULL,
    members   integer NOT NULL
);
CREATE INDEX IF NOT EXISTS tg_channel_counts_chat ON tg_channel_counts (chat_id, at);

-- Путь клиента: подписчик TG → лид в CRM (по ClientID Метрики) → сделка.
CREATE OR REPLACE VIEW v_tg_subscriber_journey AS
SELECT m.event_at AS subscribed_at,
       m.chat_id, m.user_id, m.username, m.first_name,
       coalesce(m.campaign, m.invite_name) AS source,
       m.client_id,
       l.id           AS lead_id,
       l.date_create  AS lead_at,
       l.status_semantic AS lead_status,
       l.utm_campaign AS lead_utm_campaign,
       d.id           AS deal_id,
       d.stage_semantic AS deal_status,
       d.closedate    AS deal_closed_at,
       (d.stage_semantic = 'S') AS won
FROM tg_channel_members m
LEFT JOIN leads l ON l.ym_client_id = m.client_id AND m.client_id IS NOT NULL
LEFT JOIN deals d ON d.lead_id = l.id
WHERE m.action = 'join';
