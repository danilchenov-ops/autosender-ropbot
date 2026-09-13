"""Первый аудит: считает всё, что можно посчитать без языковых моделей.

Запуск:  docker compose run --rm collector python report.py
         docker compose run --rm collector python report.py 30    # окно в днях
"""
import sys

from common import db

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90


def head(title):
    print("\n" + "═" * 78)
    print(title)
    print("═" * 78)


def table(conn, sql, params=None, note=None):
    cur = conn.execute(sql, params or ())
    rows = cur.fetchall()
    cols = [d.name for d in cur.description]
    if not rows:
        print("  нет данных")
        return
    widths = [max(len(c), *(len(fmt(r[i])) for r in rows)) for i, c in enumerate(cols)]
    print("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)))
    print("  " + "  ".join("─" * w for w in widths))
    for r in rows:
        print("  " + "  ".join(fmt(v).ljust(widths[i]) for i, v in enumerate(r)))
    if note:
        print(f"\n  {note}")


def fmt(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def main():
    with db() as conn:
        print(f"\nАУДИТ ОТДЕЛА ПРОДАЖ — окно {DAYS} дней")
        print("Лиды и звонки с непригодным номером телефона в статистику не входят")

        head("1. Что вообще собрано")
        table(conn, """
            SELECT (SELECT count(*) FROM calls)                            AS звонков,
                   (SELECT count(*) FROM transcripts)                      AS расшифровок,
                   (SELECT count(*) FROM leads)                            AS лидов,
                   (SELECT count(*) FROM deals)                            AS сделок,
                   (SELECT count(*) FROM stage_history)                    AS переходов,
                   (SELECT min(call_start)::date FROM calls)               AS звонки_с,
                   (SELECT max(call_start)::date FROM calls)               AS звонки_по
        """)

        head("2. Пропущенные входящие по менеджерам")
        table(conn, """
            SELECT COALESCE(mg.full_name, c.portal_user_id::text) AS менеджер,
                   count(*) FILTER (WHERE c.direction='in')                 AS входящих,
                   count(*) FILTER (WHERE c.is_missed)                      AS пропущено,
                   round(100.0 * count(*) FILTER (WHERE c.is_missed)
                         / NULLIF(count(*) FILTER (WHERE c.direction='in'),0), 1) AS "процент",
                   count(*) FILTER (WHERE c.direction='out')                AS исходящих
            FROM calls c
            LEFT JOIN managers mg ON mg.portal_user_id = c.portal_user_id
            WHERE c.call_start > now() - (%s || ' days')::interval
              AND c.phone_kind IN ('mobile','landline')
            GROUP BY 1 HAVING count(*) FILTER (WHERE c.direction='in') > 0
            ORDER BY 3 DESC LIMIT 20
        """, (DAYS,))

        head("3. Перезвонили ли на пропущенные")
        table(conn, """
            SELECT count(*)                                                  AS пропущено,
                   count(*) FILTER (WHERE callback_at IS NULL)               AS без_перезвона,
                   round(100.0 * count(*) FILTER (WHERE callback_at IS NULL)
                         / NULLIF(count(*),0), 1)                            AS "процент_потерь",
                   round(percentile_cont(0.5) WITHIN GROUP
                         (ORDER BY callback_delay_min)::numeric, 1)          AS медиана_минут,
                   count(*) FILTER (WHERE callback_delay_min <= 5)           AS перезвон_до_5_мин
            FROM v_missed_with_callback
            WHERE call_start > now() - (%s || ' days')::interval
        """, (DAYS,), note="Пропущенный без перезвона — это оплаченный лид, выброшенный в мусор.")

        head("4. Когда именно теряем звонки — по часам")
        table(conn, """
            SELECT EXTRACT(hour FROM call_start AT TIME ZONE 'Europe/Moscow')::int AS час_мск,
                   count(*)                                    AS входящих,
                   count(*) FILTER (WHERE is_missed)           AS пропущено,
                   round(100.0 * count(*) FILTER (WHERE is_missed)
                         / NULLIF(count(*),0), 1)              AS "процент"
            FROM calls
            WHERE direction='in' AND phone_kind IN ('mobile','landline')
              AND call_start > now() - (%s || ' days')::interval
            GROUP BY 1 ORDER BY 1
        """, (DAYS,))

        head("5. Скорость первого касания по лиду и её цена")
        table(conn, """
            WITH b AS (
              SELECT CASE
                       WHEN first_touch_min IS NULL           THEN 'ни разу не позвонили'
                       WHEN first_touch_min < 5               THEN 'до 5 минут'
                       WHEN first_touch_min < 30              THEN '5–30 минут'
                       WHEN first_touch_min < 120             THEN '30 мин – 2 часа'
                       WHEN first_touch_min < 1440            THEN '2 – 24 часа'
                       ELSE 'больше суток'
                     END AS скорость_реакции,
                     status_semantic
              FROM v_lead_activity
              WHERE date_create > now() - (%s || ' days')::interval
            )
            SELECT скорость_реакции,
                   count(*)                                        AS лидов,
                   count(*) FILTER (WHERE status_semantic='S')     AS успешных,
                   round(100.0 * count(*) FILTER (WHERE status_semantic='S')
                         / NULLIF(count(*),0), 2)                  AS "конверсия"
            FROM b GROUP BY 1
            ORDER BY CASE скорость_реакции
                       WHEN 'до 5 минут' THEN 1 WHEN '5–30 минут' THEN 2
                       WHEN '30 мин – 2 часа' THEN 3 WHEN '2 – 24 часа' THEN 4
                       WHEN 'больше суток' THEN 5 ELSE 6 END
        """, (DAYS,), note="Главный управляемый фактор. Если конверсия падает с задержкой — это чистые потерянные деньги.")

        head("6. Сколько касаний нужно до успеха")
        table(conn, """
            SELECT CASE WHEN calls_total = 0 THEN '0'
                        WHEN calls_total = 1 THEN '1'
                        WHEN calls_total = 2 THEN '2'
                        WHEN calls_total = 3 THEN '3'
                        WHEN calls_total <= 5 THEN '4–5'
                        WHEN calls_total <= 9 THEN '6–9'
                        ELSE '10+' END                             AS звонков_по_лиду,
                   count(*)                                        AS лидов,
                   count(*) FILTER (WHERE status_semantic='S')     AS успешных,
                   round(100.0 * count(*) FILTER (WHERE status_semantic='S')
                         / NULLIF(count(*),0), 2)                  AS "конверсия"
            FROM v_lead_activity
            WHERE date_create > now() - (%s || ' days')::interval
            GROUP BY 1 ORDER BY 1
        """, (DAYS,), note="Где конверсия перестаёт расти — там и предел осмысленной настойчивости.")

        head("7. Длина цикла: от лида до успеха")
        table(conn, """
            SELECT count(*)                                              AS успешных_лидов,
                   round(percentile_cont(0.5) WITHIN GROUP (ORDER BY d)::numeric,1) AS медиана_дней,
                   round(percentile_cont(0.9) WITHIN GROUP (ORDER BY d)::numeric,1) AS "90_процентиль",
                   round(max(d)::numeric,1)                              AS максимум
            FROM (
              SELECT EXTRACT(EPOCH FROM (l.date_closed - l.date_create))/86400.0 AS d
              FROM leads l
              WHERE l.status_semantic='S' AND l.date_closed IS NOT NULL
                AND l.date_create > now() - interval '365 days'
            ) t WHERE d >= 0
        """, note="Если медиана заметно больше 90 дней — трёхмесячного окна звонков не хватит для честной атрибуции.")

        head("8. Менеджеры: нагрузка против результата")
        table(conn, """
            SELECT COALESCE(mg.full_name, l.assigned_by::text)        AS менеджер,
                   count(*)                                           AS лидов,
                   count(*) FILTER (WHERE l.status_semantic='S')      AS успешных,
                   round(100.0 * count(*) FILTER (WHERE l.status_semantic='S')
                         / NULLIF(count(*),0), 2)                     AS "конверсия",
                   round(avg(a.calls_total)::numeric, 1)              AS звонков_на_лид,
                   round(percentile_cont(0.5) WITHIN GROUP
                         (ORDER BY a.first_touch_min)::numeric, 1)    AS медиана_реакции_мин
            FROM leads l
            LEFT JOIN managers mg      ON mg.portal_user_id = l.assigned_by
            LEFT JOIN v_lead_activity a ON a.lead_id = l.id
            WHERE l.date_create > now() - (%s || ' days')::interval
              AND l.phone_kind IN ('mobile','landline')
            GROUP BY 1 HAVING count(*) >= 20
            ORDER BY 4 DESC NULLS LAST
        """, (DAYS,))

        head("9. Источники: откуда приходят деньги")
        table(conn, """
            SELECT COALESCE(NULLIF(utm_source,''), source_id, 'не указан') AS источник,
                   count(*)                                        AS лидов,
                   count(*) FILTER (WHERE status_semantic='S')     AS успешных,
                   round(100.0 * count(*) FILTER (WHERE status_semantic='S')
                         / NULLIF(count(*),0), 2)                  AS "конверсия"
            FROM leads
            WHERE date_create > now() - (%s || ' days')::interval
              AND phone_kind IN ('mobile','landline')
            GROUP BY 1 HAVING count(*) >= 10
            ORDER BY 2 DESC LIMIT 15
        """, (DAYS,))

        head("10. На каких статусах осыпаются лиды")
        table(conn, """
            SELECT COALESCE(s.name, l.status_id)  AS статус,
                   l.status_semantic              AS тип,
                   count(*)                       AS лидов,
                   round(100.0 * count(*) / SUM(count(*)) OVER (), 1) AS "доля"
            FROM leads l
            LEFT JOIN crm_dict s ON s.kind='STATUS' AND s.status_id = l.status_id
            WHERE l.date_create > now() - (%s || ' days')::interval
              AND l.phone_kind IN ('mobile','landline')
            GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20
        """, (DAYS,))

        head("11. Разговоры: что уже видно в расшифровках")
        table(conn, """
            SELECT count(*)                                           AS расшифровано,
                   round(avg(c.duration)::numeric, 0)                 AS средняя_длина_сек,
                   count(*) FILTER (WHERE m.monologue_flag)           AS с_монологом,
                   round(avg(m.questions_manager)::numeric, 1)        AS вопросов_за_звонок,
                   count(*) FILTER (WHERE array_length(m.stopwords,1) > 0) AS со_стоп_словами
            FROM transcripts t
            JOIN calls c        ON c.id = t.call_id
            LEFT JOIN call_metrics m ON m.call_id = t.call_id
            WHERE c.call_start > now() - (%s || ' days')::interval
        """, (DAYS,))

        print("\nГотово.\n")


if __name__ == "__main__":
    main()
