"""Разовый досчёт «Visual» задним числом (29.08.2026, решение Тимофея).

Та же модель, что в visual_daily.py (продажи в балл не входят), но каждое
прошлое число считается в границах своего дня — как посчитал бы ночной
снимок в 23:55 того дня: звонки и закрытия окон после конца дня не видны.
Перезаписывает дни целиком (upsert), запускать безопасно повторно.
"""
import datetime as dt
import json
import sys

from common import db

VLD = dt.timezone(dt.timedelta(hours=10))
MIN_WINDOW_MIN = 5
START = dt.date(2026, 7, 15)


def speed_pts(fmin):
    if fmin is None:
        return 0.0
    if fmin <= 15:
        return 100.0
    if fmin <= 60:
        return 70.0
    if fmin <= 240:
        return 45.0
    if fmin <= 1440:
        return 30.0
    return 10.0


def calls_pts(n):
    return {0: 0, 1: 40, 2: 40, 3: 70, 4: 70}.get(n, 100)


def day_points(conn, mgrs, day):
    d0 = dt.datetime.combine(day, dt.time(0, 0), VLD)
    d1 = d0 + dt.timedelta(days=1)
    pts = {u: {"s0": 0.0, "s123": 0.0, "s4": 0.0, "ideal": 0.0}
           for u in mgrs}

    # шаг 0: заявки, созданные в этот день; звонки — до конца дня
    rows = conn.execute("""
        SELECT l.assigned_by, l.date_create, t.fc, coalesce(t.calls, 0)
          FROM leads l
          LEFT JOIN LATERAL (
              SELECT min(c.call_start) AS fc, count(*) AS calls
                FROM calls c
               WHERE c.direction='out' AND c.call_start > l.date_create
                 AND c.call_start < %(d1)s
                 AND ((c.crm_entity_type='LEAD' AND c.crm_entity_id=l.id)
                      OR (l.phone_e164 IS NOT NULL
                          AND c.phone_e164=l.phone_e164))) t ON TRUE
         WHERE l.date_create >= %(d0)s AND l.date_create < %(d1)s
           AND coalesce(l.phone_kind,'') NOT IN ('junk','none')
           AND l.source_id IS DISTINCT FROM 'PARTNER'
           AND l.source_id IS DISTINCT FROM 'CALL'
           AND l.status_id IS DISTINCT FROM '31'""",
        {"d0": d0, "d1": d1}).fetchall()
    for uid, t0, fc, calls in rows:
        if uid not in pts:
            continue
        t0v = t0.astimezone(VLD)
        day_ok = t0v.weekday() < 5 and 10 <= t0v.hour < 18
        cp = calls_pts(calls)
        fmin = (fc - t0).total_seconds() / 60 if fc else None
        score = 0.6 * cp + 0.4 * speed_pts(fmin) if day_ok else cp
        pts[uid]["s0"] += score / 10
        pts[uid]["ideal"] += 10

    # шаги 1–3: окна, открытые в этот день; конец окна и звонки — до конца дня
    rows = conn.execute("""
        SELECT l.assigned_by, ev.t0, nx.t_end, t.fc
          FROM (SELECT owner_id, stage_id, min(created_time) AS t0
                  FROM stage_history
                 WHERE entity_kind='lead' AND stage_id IN ('8','10','11')
                   AND created_time >= %(d0)s AND created_time < %(d1)s
                 GROUP BY owner_id, stage_id) ev
          JOIN leads l ON l.id = ev.owner_id
          LEFT JOIN LATERAL (
              SELECT s.created_time AS t_end FROM stage_history s
               WHERE s.entity_kind='lead' AND s.owner_id=ev.owner_id
                 AND s.created_time>ev.t0 AND s.created_time < %(d1)s
                 AND s.stage_id<>ev.stage_id
               ORDER BY s.created_time LIMIT 1) nx ON TRUE
          LEFT JOIN LATERAL (
              SELECT min(c.call_start) AS fc FROM calls c
               WHERE c.direction='out' AND c.call_start>ev.t0
                 AND c.call_start<=coalesce(nx.t_end, %(d1)s)
                 AND ((c.crm_entity_type='LEAD' AND c.crm_entity_id=l.id)
                      OR (l.phone_e164 IS NOT NULL
                          AND c.phone_e164=l.phone_e164))) t ON TRUE""",
        {"d0": d0, "d1": d1}).fetchall()
    for uid, t0, t_end, fc in rows:
        if uid not in pts:
            continue
        if t_end is not None and (t_end - t0).total_seconds() < MIN_WINDOW_MIN * 60:
            continue                      # задним числом — без баллов
        fmin = (fc - t0).total_seconds() / 60 if fc else None
        score = 0.7 * speed_pts(fmin) + 0.3 * (100.0 if fc else 0.0)
        pts[uid]["s123"] += score          # окно весит x10: середина воронки
        pts[uid]["ideal"] += 100           # двигает продажи (проверка 29.08)

    # шаг 4: звонки этого дня по торгам, открытым на конец дня
    rows = conn.execute("""
        SELECT l.assigned_by, count(*) AS calls_today
          FROM (SELECT owner_id, min(created_time) AS t0
                  FROM stage_history
                 WHERE entity_kind='lead' AND stage_id='12'
                   AND created_time < %(d1)s
                   AND created_time >= %(d1)s - interval '90 days'
                 GROUP BY owner_id) ev
          JOIN leads l ON l.id = ev.owner_id
          JOIN calls c
            ON c.direction='out'
           AND c.call_start >= %(d0)s AND c.call_start < %(d1)s
           AND c.call_start > ev.t0
           AND ((c.crm_entity_type='LEAD' AND c.crm_entity_id=l.id)
                OR (l.phone_e164 IS NOT NULL AND c.phone_e164=l.phone_e164))
         WHERE NOT EXISTS (
               SELECT 1 FROM stage_history s
                WHERE s.entity_kind='lead' AND s.owner_id=ev.owner_id
                  AND s.created_time>ev.t0 AND s.created_time < %(d1)s
                  AND (s.stage_id='13' OR s.stage_semantic IN ('F','S')))
         GROUP BY l.assigned_by, l.id""", {"d0": d0, "d1": d1}).fetchall()
    for uid, calls_today in rows:
        if uid not in pts:
            continue
        pts[uid]["s4"] += 10 * min(calls_today, 3)

    # эталон торгов: ежедневное касание каждого открытого на конец дня торга
    rows = conn.execute("""
        SELECT l.assigned_by, count(*)
          FROM (SELECT owner_id, min(created_time) AS t0
                  FROM stage_history
                 WHERE entity_kind='lead' AND stage_id='12'
                   AND created_time < %(d1)s
                   AND created_time >= %(d1)s - interval '90 days'
                 GROUP BY owner_id) ev
          JOIN leads l ON l.id = ev.owner_id
         WHERE NOT EXISTS (
               SELECT 1 FROM stage_history s
                WHERE s.entity_kind='lead' AND s.owner_id=ev.owner_id
                  AND s.created_time>ev.t0 AND s.created_time < %(d1)s
                  AND (s.stage_id='13' OR s.stage_semantic IN ('F','S')))
         GROUP BY l.assigned_by""", {"d1": d1}).fetchall()
    for uid, n_open in rows:
        if uid in pts:
            pts[uid]["ideal"] += 10 * n_open
    return pts


def main():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS visual_points (
            day date NOT NULL,
            portal_user_id integer NOT NULL,
            pts numeric NOT NULL,
            parts jsonb,
            computed_at timestamptz DEFAULT now(),
            PRIMARY KEY (day, portal_user_id))""")
    mgrs = [r[0] for r in conn.execute(
        "SELECT portal_user_id FROM dash_tokens "
        "WHERE active AND kind='manager' AND portal_user_id IS NOT NULL")]
    today = dt.datetime.now(VLD).date()
    day = START
    while day <= today:
        pts = day_points(conn, mgrs, day)
        for uid, p in pts.items():
            total = round(p["s0"] + p["s123"] + p["s4"], 1)
            conn.execute("""
                INSERT INTO visual_points (day, portal_user_id, pts, parts)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (day, portal_user_id)
                DO UPDATE SET pts = EXCLUDED.pts, parts = EXCLUDED.parts,
                              computed_at = now()""",
                (day, uid, total,
                 json.dumps({k: round(v, 1) for k, v in p.items()})))
        sys.stdout.write(day.isoformat() + ": " + ", ".join(
            f"{u}={round(sum(p.values()), 1)}" for u, p in pts.items()) + "\n")
        sys.stdout.flush()
        day += dt.timedelta(days=1)


if __name__ == "__main__":
    main()
