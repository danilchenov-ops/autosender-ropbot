-- Посевы Telega.in: реестр размещений и экономика «цена подписчика».
CREATE TABLE IF NOT EXISTS telega_placements (
  id            bigserial PRIMARY KEY,
  ocid          bigint UNIQUE,               -- id заявки в telega.in (#4145993)
  pid           bigint,                      -- id проекта telega.in
  platform      text NOT NULL CHECK (platform IN ('tg','max')),
  slug          text NOT NULL,               -- канал-площадка (tomsk_region70, act54_max)
  channel_name  text,
  subs_channel  integer,                     -- подписчиков у площадки
  fmt           text,                        -- 1/24, 2/48, 30 дней
  price         numeric(12,2) NOT NULL,      -- цена размещения, ₽ (как в кабинете)
  published_at  timestamptz,                 -- факт публикации (MSK → tz)
  window_h      integer NOT NULL DEFAULT 48, -- окно учёта подписок после поста, часов
  invite_link   text,                        -- TG: именованная ссылка seed_<slug>_<date>
  views         integer,                     -- просмотры поста по Telega
  note          text,
  created_at    timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS telega_placements_pub_idx ON telega_placements (published_at);

-- Подписчики по размещению.
-- TG: точно — вступления по именованной ссылке (минус выходы в окне).
-- MAX: прирост счётчика за окно минус вступления, привязанные к Директу (exact/campaign).
CREATE OR REPLACE VIEW v_telega_econ AS
WITH tg AS (
  SELECT p.id,
         count(*) FILTER (WHERE m.action='join')  AS joins,
         count(*) FILTER (WHERE m.action='leave') AS leaves
  FROM telega_placements p
  LEFT JOIN tg_channel_members m
    ON p.platform='tg' AND m.invite_link = p.invite_link
   AND m.event_at BETWEEN p.published_at AND p.published_at + (p.window_h||' hours')::interval
  GROUP BY p.id
), mx AS (
  SELECT p.id,
         (SELECT members FROM max_channel_counts c WHERE c.at <= p.published_at ORDER BY c.at DESC LIMIT 1) AS m0,
         (SELECT members FROM max_channel_counts c WHERE c.at <= p.published_at + (p.window_h||' hours')::interval ORDER BY c.at DESC LIMIT 1) AS m1,
         (SELECT count(*) FROM max_channel_members mm
           WHERE mm.action='join' AND mm.match_quality IN ('exact','campaign')
             AND mm.event_at BETWEEN p.published_at AND p.published_at + (p.window_h||' hours')::interval) AS ads_joins,
         (SELECT count(*) FROM max_channel_members mm
           WHERE mm.action='join'
             AND mm.event_at BETWEEN p.published_at - interval '7 days' AND p.published_at) / 7.0 / 24 * p.window_h AS base_joins
  FROM telega_placements p WHERE p.platform='max'
)
SELECT p.id, p.ocid, p.platform, p.slug, p.channel_name, p.fmt, p.price, p.published_at, p.window_h, p.views,
       CASE WHEN p.platform='tg' THEN tg.joins - tg.leaves
            ELSE greatest(0, round(coalesce(mx.m1 - mx.m0,0) - coalesce(mx.ads_joins,0) - coalesce(mx.base_joins,0))) END AS subs,
       tg.joins AS tg_joins, tg.leaves AS tg_leaves,
       mx.m0, mx.m1, mx.ads_joins, round(mx.base_joins,1) AS base_joins,
       CASE WHEN p.platform='tg' AND tg.joins - tg.leaves > 0 THEN round(p.price / (tg.joins - tg.leaves))
            WHEN p.platform='max' AND (mx.m1 - mx.m0 - coalesce(mx.ads_joins,0) - coalesce(mx.base_joins,0)) > 0
              THEN round(p.price / (mx.m1 - mx.m0 - coalesce(mx.ads_joins,0) - coalesce(mx.base_joins,0)))
       END AS cost_per_sub,
       CASE WHEN p.views > 0 THEN round(p.price / p.views * 1000) END AS cpm,
       (now() >= p.published_at + (p.window_h||' hours')::interval) AS window_closed
FROM telega_placements p
LEFT JOIN tg ON tg.id = p.id
LEFT JOIN mx ON mx.id = p.id
ORDER BY p.published_at DESC;
