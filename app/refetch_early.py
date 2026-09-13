"""Разовый перечит Метрики для заявок, обогащённых раньше 45 минут.

До правки 22.08 enrich забирал поведение сразу после создания заявки — сессия,
в которой заявку оставили, ещё не была досчитана Метрикой. Такие строки
lead_visits занижены, а NOT EXISTS в enrich не даёт их перечитать.

Этот скрипт находит строки, где fetched_at < date_create + 45 минут, и
перечитывает их принудительно; INSERT ... ON CONFLICT DO UPDATE в базе уже есть.
Замороженные lead_scores НЕ трогаются — они эталон для замера 2 сентября.
Составная оценка с 22.08 считает блок поведения живьём и подхватит новые данные
следующим кроном.

Запуск: python refetch_early.py [--limit 300]
"""
import json
import sys
import time

import requests

from common import db, log
from metrika import COUNTERS, SLEEP, fetch_visits, summarize

Q = """
SELECT l.id, l.ym_client_id,
       to_char(l.date_create AT TIME ZONE 'Asia/Vladivostok', 'YYYY-MM-DD HH24:MI:SS'),
       to_char(l.date_create AT TIME ZONE 'Asia/Vladivostok', 'YYYY-MM-DD'),
       to_char(l.date_create AT TIME ZONE 'Asia/Vladivostok' - interval '365 days', 'YYYY-MM-DD')
FROM leads l
JOIN lead_visits v ON v.lead_id = l.id
WHERE v.fetched_at < l.date_create + interval '45 minutes'
  AND l.ym_client_id IS NOT NULL
  AND l.date_create < now() - interval '45 minutes'
ORDER BY l.date_create DESC LIMIT %s
"""


def main():
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 300
    with db() as conn:
        rows = conn.execute(Q, (limit,)).fetchall()
        log.info("перечит ранних: кандидатов %s", len(rows))
        done = better = errors = 0
        for lead_id, cid, lead_dt, d2, d1 in rows:
            try:
                summary = None
                for counter in COUNTERS:
                    visits = fetch_visits(counter, cid, d1, d2)
                    time.sleep(SLEEP)
                    if visits:
                        summary = summarize(visits, lead_dt)
                        if summary:
                            summary["counter"] = counter
                            break
                if summary:
                    old = conn.execute(
                        "SELECT pageviews, seconds FROM lead_visits WHERE lead_id=%s",
                        (lead_id,)).fetchone()
                    conn.execute(
                        """UPDATE lead_visits SET
                             counter_id=%s, visits_before=%s, days_since_first=%s,
                             pageviews=%s, seconds=%s, distinct_landings=%s, saw_lot=%s,
                             last_source=%s, device=%s, region=%s, last_campaign=%s,
                             raw=%s, fetched_at=now()
                           WHERE lead_id=%s""",
                        (summary["counter"], summary["visits_before"],
                         summary["days_since_first"], summary["pageviews"],
                         summary["seconds"], summary["distinct_landings"],
                         summary["saw_lot"], summary["last_source"], summary["device"],
                         summary["region"], summary["last_campaign"],
                         json.dumps(summary["raw"], ensure_ascii=False), lead_id))
                    if old and (summary["pageviews"] or 0) > (old[0] or 0):
                        better += 1
                else:
                    # клиента так и не видно — оставляем строку как есть,
                    # но сдвигаем fetched_at, чтобы не перечитывать вечно
                    conn.execute(
                        "UPDATE lead_visits SET fetched_at=now() WHERE lead_id=%s",
                        (lead_id,))
                done += 1
                errors = 0
            except requests.RequestException as e:
                errors += 1
                log.warning("перечит, лид %s: %s", lead_id, str(e)[:150])
                if errors >= 5:
                    log.error("перечит: 5 ошибок подряд, стоп")
                    break
                time.sleep(10)
        log.info("перечит ранних: обработано %s, данных стало больше у %s", done, better)


if __name__ == "__main__":
    main()
