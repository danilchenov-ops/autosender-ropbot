"""Замер: что именно стоит дорого при сборке панелей. Разовый скрипт."""
import time
import datetime as dt

from common import db

conn = db()


def t(label, fn):
    a = time.time()
    r = fn()
    print(f"{label:44} {time.time() - a:6.2f} с   {r}")


WORKING = ("SELECT portal_user_id FROM dash_tokens "
           "WHERE active AND kind = 'manager' AND portal_user_id IS NOT NULL")

t("NOW_SQL (таблица «Сейчас»)", lambda: len(conn.execute(f"""
  SELECT l.assigned_by, count(*),
         count(*) FILTER (WHERE l.status_id = 'NEW'),
         count(*) FILTER (WHERE l.status_id = '32'),
         count(*) FILTER (WHERE l.status_id = 'IN_PROCESS'),
         count(*) FILTER (WHERE l.status_id = '35'),
         count(*) FILTER (WHERE l.status_id = '8'),
         count(*) FILTER (WHERE l.status_id = '12'),
         count(*) FILTER (WHERE l.status_id = '10'),
         count(*) FILTER (WHERE l.status_id = '11')
    FROM leads l
   WHERE l.assigned_by IN ({WORKING}) AND l.status_semantic = 'P'
   GROUP BY 1""").fetchall()))

t("fetch_scored 14 дней (столбец «Скрипт»)", lambda: len(conn.execute("""
  SELECT c.portal_user_id, s.raw,
         coalesce(t.text ~* 'договор', false),
         coalesce(t.text ~* 'видео|трансляц', false),
         NOT EXISTS (SELECT 1 FROM calls c2 WHERE c2.phone_e164 = c.phone_e164
                       AND c2.duration >= 60 AND c2.call_start < c.call_start)
    FROM call_scores s JOIN calls c ON c.id = s.call_id
    LEFT JOIN transcripts t ON t.call_id = c.id
   WHERE s.raw IS NOT NULL
     AND (c.call_start AT TIME ZONE 'Asia/Vladivostok')
         >= (now() AT TIME ZONE 'Asia/Vladivostok') - interval '14 days'
""").fetchall()))

t("CONV_SQL (конверсия 4 мес)", lambda: len(conn.execute(f"""
  SELECT l.assigned_by, to_char(date_trunc('month', l.date_create), 'YYYY-MM'),
         count(*), count(*) FILTER (WHERE l.status_semantic = 'S'),
         count(*) FILTER (WHERE l.status_semantic = 'P')
    FROM leads l
   WHERE l.assigned_by IN ({WORKING})
     AND l.date_create >= date_trunc('month', now()) - interval '3 months'
   GROUP BY 1, 2""").fetchall()))

t("CALLS_SQL (вечера/выходные 4 мес)", lambda: len(conn.execute(f"""
  SELECT uid, to_char(date_trunc('month', ts), 'YYYY-MM'),
         extract(isodow from ts)::int, count(*), count(*), count(DISTINCT ts::date)
    FROM (SELECT c.portal_user_id AS uid,
                 (c.call_start AT TIME ZONE 'Asia/Vladivostok') AS ts
            FROM calls c
           WHERE c.direction = 'out' AND c.duration > 0
             AND c.portal_user_id IN ({WORKING})
             AND c.call_start >= date_trunc('month', now()) - interval '3 months') x
   GROUP BY 1, 2, 3""").fetchall()))
