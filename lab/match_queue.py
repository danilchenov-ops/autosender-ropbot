# -*- coding: utf-8 -*-
"""Зеркальный подбор LOST под WON и переприоритизация очереди расшифровки.

Зачем. Контрольную группу расшифровывали «сначала свежие», из-за чего в ней
оказался почти один август, а WON — май-июль. Сравнивать так нельзя: разрыв
объяснится не работой менеджера, а тем, что августовские отказы не успели
дозреть (медиана цикла сделки 8,6 дня, 90-й процентиль 38).

Что делает: под каждый WON-разговор ищет LOST-разговоры того же месяца,
источника и менеджера и поднимает их в очереди ASR — первый двойник
приоритетом 9, второй 8. Всё остальное ждёт.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402
from aggregate import bucket  # noqa: E402

PER_WON = 2


def main(dry=False):
    conn = runner.db()
    with conn.cursor() as c:
        # 1. эталон — WON-разговоры мая-июля, те самые, что уже разобраны
        c.execute("""
            SELECT call_id, to_char(call_start,'YYYY-MM'), source, manager
            FROM lab_sample
            WHERE excluded IS NULL AND is_first AND grp='WON'
              AND call_start < '2026-08-01'""")
        won = [(r[0], r[1], bucket(r[2]), r[3]) for r in c.fetchall()]

        # 2. кандидаты — нерасшифрованные первые разговоры LOST-клиентов
        c.execute("""
        WITH cl AS (
          SELECT phone_e164, bool_or(status_semantic='S') won,
                 bool_or(status_semantic='P') p
          FROM leads WHERE phone_kind='mobile' AND phone_e164 IS NOT NULL
          GROUP BY 1),
        lead_first AS (
          SELECT DISTINCT ON (l.phone_e164) l.phone_e164,
                 COALESCE(NULLIF(l.utm_source,''), l.source_id) src
          FROM leads l WHERE l.phone_kind='mobile' AND l.phone_e164 IS NOT NULL
          ORDER BY l.phone_e164, l.date_create),
        cand AS (
          SELECT q.call_id, c.phone_e164, c.call_start, lf.src,
                 COALESCE(mg.full_name, c.portal_user_id::text) manager,
                 row_number() OVER (PARTITION BY c.phone_e164
                                    ORDER BY c.call_start) rn
          FROM asr_queue q
          JOIN calls c ON c.id=q.call_id
          JOIN cl ON cl.phone_e164=c.phone_e164
          LEFT JOIN lead_first lf ON lf.phone_e164=c.phone_e164
          LEFT JOIN managers mg ON mg.portal_user_id=c.portal_user_id
          WHERE q.status='pending' AND NOT cl.won AND NOT cl.p
            AND c.phone_kind='mobile' AND c.duration>=60
            AND c.call_start >= '2026-05-01' AND c.call_start < '2026-08-01')
        SELECT call_id, to_char(call_start,'YYYY-MM'), src, manager
        FROM cand WHERE rn=1""")
        cands = [(r[0], r[1], bucket(r[2]), r[3]) for r in c.fetchall()]

    pool = defaultdict(list)
    for cid, mon, src, mgr in cands:
        pool[(mon, src, mgr)].append(cid)
        pool[(mon, mgr)].append(cid)
        pool[(mon, src)].append(cid)
        pool[(mon,)].append(cid)

    used = set()
    picks = {}          # call_id -> приоритет
    misses = 0
    for _, mon, src, mgr in won:
        for slot in range(PER_WON):
            got = None
            for key in ((mon, src, mgr), (mon, mgr), (mon, src), (mon,)):
                for cid in pool.get(key, []):
                    if cid not in used:
                        got = cid
                        break
                if got:
                    break
            if got is None:
                misses += 1
                continue
            used.add(got)
            picks[got] = 9 - slot

    print(f"WON-эталонов: {len(won)}, подобрано двойников: {len(picks)}, "
          f"не нашлось: {misses}")
    by_pr = defaultdict(int)
    for pr in picks.values():
        by_pr[pr] += 1
    print("по приоритетам:", dict(by_pr))
    if dry:
        return
    with conn.cursor() as c:
        for pr in (9, 8):
            ids = [cid for cid, p in picks.items() if p == pr]
            if ids:
                c.execute("UPDATE asr_queue SET priority=%s WHERE call_id = ANY(%s)",
                          (pr, ids))
                print(f"  приоритет {pr}: обновлено {c.rowcount}")
        conn.commit()
    print("очередь переприоритизирована")


if __name__ == "__main__":
    main(dry="--dry" in sys.argv)
