# -*- coding: utf-8 -*-
"""Слепок работы менеджеров: снимок параметров в manager_snapshot.

В образ НЕ запечён, подаётся через stdin:
  docker exec -i ropbot-collector-1 python - < /opt/ropbot/app/snapshot.py
  docker exec -i ropbot-collector-1 python - --date 2026-08-25 < ...
  docker exec -i ropbot-collector-1 python - --retro < ...   # понедельники с 08.06

Правила счёта (claude/analytics-rules.md): только mobile, без PARTNER,
окно звонков по phone_e164 от первой карточки номера, скорость и настойчивость —
только по первичным карточкам (дубли не в вину), текущий месяц не вызрел —
конверсия считается по заявкам 45–135 дней (период 'mat').

Периоды: '7d' и '30d' — окна заявок/звонков, заканчиваются началом дня снимка
(Влд), то есть покрывают полные прошедшие сутки; 'mom' — моментальные (просрочка
дел); 'mat' — вызревшие. Страты: all/A/B/C/D/X + std (взвешено по составу
классов отдела в том же окне; для медиан — взвешенное среднее медиан страт,
приближение).
"""
import datetime as dt
import sys

from common import db, log

VLD = "Asia/Vladivostok"
JUNE = dt.date(2026, 6, 1)          # звонки честные с 20.05, правило 6: с июня
RETRO_FROM = dt.date(2026, 6, 8)    # первый понедельник с полным 7д-окном

MGR = ("SELECT portal_user_id FROM managers "
       "WHERE department='[5]' AND active AND portal_user_id <> 9151")

# первичные карточки менеджеров отдела, созданные в [c0; c1) по Влд
BASE_FIRST = f"""
  SELECT l.id, l.assigned_by AS uid, l.phone_e164, l.date_create,
         CASE WHEN m.value ~ '^[ABCD]' THEN left(m.value,1) ELSE 'X' END AS grade
  FROM leads l
  JOIN ({MGR}) mg ON mg.portal_user_id = l.assigned_by
  LEFT JOIN lead_marks m ON m.lead_id = l.id
  WHERE l.phone_kind='mobile' AND l.phone_e164 IS NOT NULL
    AND l.source_id IS DISTINCT FROM 'PARTNER'
    AND COALESCE(l.utm_source,'') <> 'PARTNER'
    AND (l.date_create AT TIME ZONE '{VLD}')::date >= %(c0)s
    AND (l.date_create AT TIME ZONE '{VLD}')::date <  %(c1)s
    AND NOT EXISTS (SELECT 1 FROM leads p
                    WHERE p.phone_e164 = l.phone_e164
                      AND p.date_create < l.date_create)
"""

# все карточки (для гигиены CRM)
BASE_ANY = BASE_FIRST.replace(
    """    AND NOT EXISTS (SELECT 1 FROM leads p
                    WHERE p.phone_e164 = l.phone_e164
                      AND p.date_create < l.date_create)
""", "")

GS = "GROUPING SETS ((uid, grade), (uid), (grade), ())"

RX_CONTRACT = (r"(заключ|подпис|составл|отправл|пришл|скин|сдела)\w*.{0,40}договор"
               r"|договор\w*.{0,30}(заключ|подпис|составл|отправл|пришл|скин)")
RX_QTIME = r"когда.{0,40}(покупк|планир|брать|приобрет|заказ)|как скоро|в какие сроки|когда планируете"
RX_QBUDGET = r"бюджет|какую сумму|сколько.{0,25}(денег|средств|готовы)|в какие деньги|по деньгам.{0,15}(сколько|какой)"
RX_QMODEL = r"какой (год|кузов|автомобиль|марк)|как(ая|ую) (машин|модел|марк)|что (вас )?интересует|какого года|год рассматр"

# --- запросы -----------------------------------------------------------------
# Каждый возвращает uid, grade и пары v_<PARAM>/n_<PARAM>. NULL uid → 0 (отдел),
# NULL grade → 'all'.

QUERIES = {}

# С1 скорость первого отклика (раб. мин, медиана) · С2 доля ≤15 раб. мин
QUERIES["speed"] = dict(shift=0, cohort=True, sql=f"""
WITH base AS ({BASE_FIRST}),
t AS (
  SELECT b.uid, b.grade, work_minutes(b.date_create, i.fc) AS touch
  FROM base b
  JOIN LATERAL (
    SELECT min(c.call_start) FILTER (WHERE c.direction='out'
                                        OR (c.direction='in' AND c.duration>0)) AS fc
    FROM calls c
    WHERE c.phone_e164 = b.phone_e164
      AND c.call_start >= b.date_create - interval '30 minutes'
  ) i ON true
  WHERE i.fc IS NOT NULL
)
SELECT uid, grade,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY touch) AS v_C1,
       count(*) AS n_C1,
       100.0 * count(*) FILTER (WHERE touch <= 15) / NULLIF(count(*),0) AS v_C2,
       count(*) AS n_C2
FROM t GROUP BY {GS}""")

# Н1 попытки за 72 ч по недозвонам (медиана) · Н2 брошено после 0–1 звонка (%)
QUERIES["persist"] = dict(shift=3, cohort=True, sql=f"""
WITH base AS ({BASE_FIRST}),
t AS (
  SELECT b.uid, b.grade, b.phone_e164, i.fo, i.fo_dur, i.first_talk,
         i.out_total, i.any_talk
  FROM base b
  JOIN LATERAL (
    SELECT min(c.call_start) FILTER (WHERE c.direction='out') AS fo,
           (array_agg(c.duration ORDER BY c.call_start)
              FILTER (WHERE c.direction='out'))[1] AS fo_dur,
           min(c.call_start) FILTER (WHERE c.duration>0) AS first_talk,
           count(*) FILTER (WHERE c.direction='out') AS out_total,
           bool_or(c.duration>0) AS any_talk
    FROM calls c
    WHERE c.phone_e164 = b.phone_e164
      AND c.call_start >= b.date_create - interval '30 minutes'
  ) i ON true
  WHERE i.fo IS NOT NULL AND i.fo_dur = 0
    AND (i.first_talk IS NULL OR i.first_talk > i.fo)
),
w AS (
  SELECT t.uid, t.grade, t.out_total, t.any_talk, a.n72
  FROM t
  JOIN LATERAL (
    SELECT count(*) AS n72 FROM calls c
    WHERE c.phone_e164 = t.phone_e164 AND c.direction='out'
      AND c.call_start >= t.fo AND c.call_start < t.fo + interval '72 hours'
  ) a ON true
)
SELECT uid, grade,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY n72) AS v_H1,
       count(*) AS n_H1,
       100.0 * count(*) FILTER (WHERE out_total <= 1 AND NOT any_talk)
             / NULLIF(count(*),0) AS v_H2,
       count(*) AS n_H2,
       100.0 * count(*) FILTER (WHERE n72 >= 5) / NULLIF(count(*),0) AS v_H3,
       count(*) AS n_H3
FROM w GROUP BY {GS}""")

# П1 исходящих за первые 3 дня (среднее) · П2 дозвон ≥30 с за 3 дня (%)
QUERIES["density"] = dict(shift=3, cohort=True, sql=f"""
WITH base AS ({BASE_FIRST}),
t AS (
  SELECT b.uid, b.grade, i.out3, i.talk3
  FROM base b
  JOIN LATERAL (
    SELECT count(*) FILTER (WHERE c.direction='out') AS out3,
           bool_or(c.duration >= 30) AS talk3
    FROM calls c
    WHERE c.phone_e164 = b.phone_e164
      AND c.call_start >= b.date_create - interval '30 minutes'
      AND c.call_start <  b.date_create + interval '3 days'
  ) i ON true
)
SELECT uid, grade,
       avg(out3) AS v_P1, count(*) AS n_P1,
       100.0 * count(*) FILTER (WHERE talk3) / NULLIF(count(*),0) AS v_P2,
       count(*) AS n_P2,
       100.0 * count(*) FILTER (WHERE out3 >= 5) / NULLIF(count(*),0) AS v_P3,
       count(*) AS n_P3
FROM t GROUP BY {GS}""")

# Р1 разговоры в ≥3 разных дня за 14 дней (%) · Р2 ≥3 разговоров (%)
QUERIES["spread"] = dict(shift=14, cohort=True, sql=f"""
WITH base AS ({BASE_FIRST}),
t AS (
  SELECT b.uid, b.grade, i.talks, i.tdays
  FROM base b
  JOIN LATERAL (
    SELECT count(*) FILTER (WHERE c.duration>0) AS talks,
           count(DISTINCT (c.call_start AT TIME ZONE '{VLD}')::date)
             FILTER (WHERE c.duration>0) AS tdays
    FROM calls c
    WHERE c.phone_e164 = b.phone_e164
      AND c.call_start >= b.date_create - interval '30 minutes'
      AND c.call_start <  b.date_create + interval '14 days'
  ) i ON true
  WHERE i.talks >= 1
)
SELECT uid, grade,
       100.0 * count(*) FILTER (WHERE tdays >= 3) / NULLIF(count(*),0) AS v_R1,
       count(*) AS n_R1,
       100.0 * count(*) FILTER (WHERE talks >= 3) / NULLIF(count(*),0) AS v_R2,
       count(*) AS n_R2
FROM t GROUP BY {GS}""")

# Ги1 часы до первого статуса после NEW (медиана) — по всем карточкам
QUERIES["hygiene1"] = dict(shift=0, cohort=True, sql=f"""
WITH base AS ({BASE_ANY}),
t AS (
  SELECT b.uid, b.grade,
         EXTRACT(epoch FROM s.t0 - b.date_create)/3600.0 AS hours
  FROM base b
  JOIN LATERAL (
    SELECT min(h.created_time) AS t0 FROM stage_history h
    WHERE h.entity_kind='lead' AND h.owner_id = b.id
      AND h.stage_id <> 'NEW' AND h.created_time > b.date_create
  ) s ON true
  WHERE s.t0 IS NOT NULL
)
SELECT uid, grade,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY hours) AS v_GI1,
       count(*) AS n_GI1
FROM t GROUP BY {GS}""")

# Ги2 закрыто в F без единого разговора по номеру (%)
QUERIES["hygiene2"] = dict(shift=0, cohort=False, sql=f"""
WITH f AS (
  SELECT h.owner_id, min(h.created_time) AS f_time
  FROM stage_history h
  WHERE h.entity_kind='lead' AND h.stage_semantic='F'
  GROUP BY 1
),
t AS (
  SELECT l.assigned_by AS uid,
         CASE WHEN m.value ~ '^[ABCD]' THEN left(m.value,1) ELSE 'X' END AS grade,
         NOT EXISTS (
           SELECT 1 FROM calls c
           WHERE c.phone_e164 = l.phone_e164 AND c.duration > 0
             AND c.call_start <= f.f_time
             AND c.call_start >= (SELECT min(p.date_create) FROM leads p
                                  WHERE p.phone_e164 = l.phone_e164)
                                 - interval '30 minutes'
         ) AS no_talk
  FROM f
  JOIN leads l ON l.id = f.owner_id
  JOIN ({MGR}) mg ON mg.portal_user_id = l.assigned_by
  LEFT JOIN lead_marks m ON m.lead_id = l.id
  WHERE l.phone_kind='mobile' AND l.phone_e164 IS NOT NULL
    AND l.source_id IS DISTINCT FROM 'PARTNER'
    AND COALESCE(l.utm_source,'') <> 'PARTNER'
    AND (f.f_time AT TIME ZONE '{VLD}')::date >= %(d0)s
    AND (f.f_time AT TIME ZONE '{VLD}')::date <  %(d1)s
)
SELECT uid, grade,
       100.0 * count(*) FILTER (WHERE no_talk) / NULLIF(count(*),0) AS v_GI2,
       count(*) AS n_GI2
FROM t GROUP BY {GS}""")

# Ги3 просроченные дела (шт, моментальный) — только для снимка за сегодня
Q_GI3 = f"""
SELECT a.responsible_id AS uid, NULL::text AS grade,
       count(DISTINCT a.owner_id)::numeric AS v_GI3,
       count(DISTINCT a.owner_id) AS n_GI3
FROM activities a
JOIN ({MGR}) mg ON mg.portal_user_id = a.responsible_id
WHERE a.provider_id='CRM_TODO' AND a.owner_type_id = 1
  AND NOT a.completed AND a.end_time IS NOT NULL
  AND (a.end_time AT TIME ZONE '{VLD}')::date < (now() AT TIME ZONE '{VLD}')::date
GROUP BY GROUPING SETS ((a.responsible_id), ())"""

# Г1 длительность первого содержательного разговора (сек, медиана) ·
# Г2 балл карточки первого содержательного разговора (0–100, среднее)
QUERIES["depth"] = dict(shift=0, cohort=False, sql=f"""
WITH ft AS (
  SELECT DISTINCT ON (c.phone_e164)
         c.phone_e164, c.id, c.call_start, c.duration, c.portal_user_id
  FROM calls c
  JOIN ({MGR}) mg ON mg.portal_user_id = c.portal_user_id
  WHERE c.duration >= 60 AND c.phone_kind='mobile' AND c.phone_e164 IS NOT NULL
  ORDER BY c.phone_e164, c.call_start
),
t AS (
  SELECT ft.portal_user_id AS uid, NULL::text AS grade, ft.duration, s.card_score
  FROM ft
  LEFT JOIN call_scores s ON s.call_id = ft.id
  WHERE (ft.call_start AT TIME ZONE '{VLD}')::date >= %(d0)s
    AND (ft.call_start AT TIME ZONE '{VLD}')::date <  %(d1)s
)
SELECT uid, grade,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY duration) AS v_G1,
       count(*) AS n_G1,
       avg(card_score) AS v_G2,
       count(card_score) AS n_G2
FROM t GROUP BY GROUPING SETS ((uid, grade), ())""")

# Д1 конкретный следующий шаг в разговоре (%) + Т-блок портрета
QUERIES["talkflags"] = dict(shift=0, cohort=False, sql=f"""
WITH t AS (
  SELECT c.portal_user_id AS uid, NULL::text AS grade,
         (s.raw->>'next_step_specific')::boolean AS nss,
         (s.raw->>'next_step_proposed')::boolean AS nsp,
         COALESCE((s.raw->>'linked_to_client_pain')::boolean,
                  (s.raw->>'linked_to_client-pain')::boolean, false) AS pain,
         COALESCE((s.raw->>'date_time_fixed')::boolean, false) AS dtf,
         jsonb_array_length(COALESCE(s.raw->'objections','[]'::jsonb)) > 0 AS had_obj,
         COALESCE(s.objections_handled, false) AS obj_ok,
         tr.text ~* %(rx_contract)s AS r_contract,
         ((tr.text ~* %(rx_qtime)s)::int + (tr.text ~* %(rx_qbudget)s)::int
          + (tr.text ~* %(rx_qmodel)s)::int) >= 2 AS r_qual,
         -- новые поля портрета (копятся после расширения промта 25.08.2026)
         s.raw ? 'next_step_by' AS has_new,
         COALESCE((s.raw->>'questions_manager')::numeric
           > COALESCE((s.raw->>'questions_client')::numeric, 0), false) AS q_lead,
         COALESCE(s.raw->>'next_step_by','') = 'менеджер' AS ns_mgr,
         COALESCE(s.raw->>'call_ended_by','') = 'менеджер' AS end_mgr,
         s.raw->>'concession' AS concession,
         (s.raw->>'first_pushback')::boolean AS pb,
         COALESCE((s.raw->>'first_pushback_handled')::boolean, false) AS pb_ok,
         COALESCE((s.raw->>'qualified_before_price')::boolean, false) AS qbp,
         COALESCE((s.raw->>'price_justified')::boolean, false) AS prj
  FROM call_scores s
  JOIN calls c ON c.id = s.call_id
  JOIN ({MGR}) mg ON mg.portal_user_id = c.portal_user_id
  LEFT JOIN transcripts tr ON tr.call_id = s.call_id
  WHERE s.outcome NOT IN ('не дозвонились','нецелевой')
    AND s.raw ? 'next_step_proposed'
    AND (c.call_start AT TIME ZONE '{VLD}')::date >= %(d0)s
    AND (c.call_start AT TIME ZONE '{VLD}')::date <  %(d1)s
)
SELECT uid, grade,
       100.0 * count(*) FILTER (WHERE nss) / NULLIF(count(*),0)              AS v_D1,
       count(*)                                                    AS n_D1,
       100.0 * count(*) FILTER (WHERE nsp) / NULLIF(count(*),0)              AS v_T1p,
       count(*)                                                    AS n_T1p,
       100.0 * count(*) FILTER (WHERE had_obj AND obj_ok)
             / NULLIF(count(*) FILTER (WHERE had_obj), 0)          AS v_T3p,
       count(*) FILTER (WHERE had_obj)                             AS n_T3p,
       avg((pain::int + dtf::int + r_contract::int + r_qual::int) / 4.0) * 100
                                                                   AS v_T4p,
       count(*)                                                    AS n_T4p,
       avg(CASE WHEN has_new THEN
             (q_lead::int + ns_mgr::int + end_mgr::int) / 3.0 END) * 100
                                                                   AS v_T1,
       count(*) FILTER (WHERE has_new)                             AS n_T1,
       100.0 * count(*) FILTER (WHERE concession = 'бесплатно')
             / NULLIF(count(*) FILTER (WHERE concession IS NOT NULL), 0)
                                                                   AS v_T2,
       count(*) FILTER (WHERE concession IS NOT NULL)              AS n_T2,
       100.0 * count(*) FILTER (WHERE pb AND pb_ok)
             / NULLIF(count(*) FILTER (WHERE pb), 0)               AS v_T3,
       count(*) FILTER (WHERE pb)                                  AS n_T3,
       avg(CASE WHEN has_new THEN
             (qbp::int + pain::int + prj::int + r_contract::int + dtf::int) / 5.0
           END) * 100                                              AS v_T4,
       count(*) FILTER (WHERE has_new)                             AS n_T4
FROM t GROUP BY GROUPING SETS ((uid, grade), ())""")

# Д2 выполненные договорённости: звонок не позже конца обещанного дня (%)
QUERIES["promises"] = dict(shift=0, cohort=False, sql=f"""
WITH t AS (
  SELECT c.portal_user_id AS uid, NULL::text AS grade,
         EXISTS (
           SELECT 1 FROM calls c2
           WHERE c2.phone_e164 = c.phone_e164 AND c2.direction='out'
             AND c2.call_start > c.call_start
             AND (c2.call_start AT TIME ZONE '{VLD}')::date <= e.due_date
         ) AS kept
  FROM call_extractions e
  JOIN calls c ON c.id = e.call_id
  JOIN ({MGR}) mg ON mg.portal_user_id = c.portal_user_id
  WHERE e.agreement AND e.due_date IS NOT NULL
    AND e.due_date >= %(d0)s AND e.due_date < %(d1)s
)
SELECT uid, grade,
       100.0 * count(*) FILTER (WHERE kept) / NULLIF(count(*),0) AS v_D2,
       count(*) AS n_D2
FROM t GROUP BY GROUPING SETS ((uid, grade), ())""")

# Го1 реакция на корону: рабочих часов от короны до следующего исходящего (медиана)
QUERIES["crown"] = dict(shift=0, cohort=False, sql=f"""
WITH t AS (
  SELECT l.assigned_by AS uid, NULL::text AS grade,
         work_minutes(lc.first_crown_at, nx.t) / 60.0 AS hours
  FROM lead_composite lc
  JOIN leads l ON l.id = lc.lead_id
  JOIN ({MGR}) mg ON mg.portal_user_id = l.assigned_by
  JOIN LATERAL (
    SELECT min(c.call_start) AS t FROM calls c
    WHERE c.phone_e164 = l.phone_e164 AND c.direction='out'
      AND c.call_start > lc.first_crown_at
  ) nx ON nx.t IS NOT NULL
  WHERE lc.first_crown_at IS NOT NULL
    AND (lc.first_crown_at AT TIME ZONE '{VLD}')::date >= %(d0)s
    AND (lc.first_crown_at AT TIME ZONE '{VLD}')::date <  %(d1)s
)
SELECT uid, grade,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY hours) AS v_GO1,
       count(*) AS n_GO1
FROM t GROUP BY GROUPING SETS ((uid, grade), ())""")

# Рез конверсия по вызревшим клиентам (первичные карточки 45–135 дней, %)
Q_REZ = f"""
WITH base AS ({BASE_FIRST}),
t AS (
  SELECT b.uid, b.grade,
         EXISTS (SELECT 1 FROM leads w
                 WHERE w.phone_e164 = b.phone_e164
                   AND w.status_semantic='S') AS won
  FROM base b
)
SELECT uid, grade,
       100.0 * count(*) FILTER (WHERE won) / NULLIF(count(*),0) AS v_REZ,
       count(*) AS n_REZ
FROM t GROUP BY {GS}"""

# параметры, по которым считается стандартизованная страта 'std'
STD_PARAMS = {"C1", "C2", "H1", "H2", "H3", "P1", "P2", "P3", "R1", "R2",
              "GI1", "GI2", "REZ"}


def collect(conn, sql, params):
    """-> {(uid, grade, param): (value, denom)}; uid NULL→0, grade NULL→'all'."""
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    out = {}
    for row in cur.fetchall():
        r = dict(zip(cols, row))
        uid = int(r["uid"]) if r["uid"] is not None else 0
        grade = r["grade"] if r["grade"] is not None else "all"
        for c in cols:
            if c.startswith("v_"):
                p = c[2:].upper()
                n = r.get("n_" + c[2:])
                if r[c] is not None and n:
                    out[(uid, grade, p)] = (float(r[c]), float(n))
    return out


def add_std(rows):
    """Прямая стандартизация: value_std = Σ w_g · value_g, w_g — доля страты g
    в когорте отдела (uid=0) того же параметра; веса перенормируются на страты,
    где у менеджера есть значение."""
    grades = ("A", "B", "C", "D", "X")
    params = {p for (_, _, p) in rows}
    uids = {u for (u, _, _) in rows if u != 0}
    extra = {}
    for p in params & STD_PARAMS:
        dept = {g: rows.get((0, g, p)) for g in grades}
        total = sum(d[1] for d in dept.values() if d)
        if not total:
            continue
        for u in uids:
            acc, wsum = 0.0, 0.0
            for g in grades:
                if dept.get(g) and (u, g, p) in rows:
                    w = dept[g][1] / total
                    acc += w * rows[(u, g, p)][0]
                    wsum += w
            if wsum >= 0.5:  # менеджер представлен хотя бы в половине потока
                extra[(u, "std", p)] = (acc / wsum,
                                        rows.get((u, "all", p), (0, 0))[1])
    rows.update(extra)


def write(conn, day, period, rows):
    conn.execute(
        "DELETE FROM manager_snapshot WHERE snap_date=%s AND period=%s",
        (day, period))
    for (uid, grade, param), (value, denom) in sorted(rows.items()):
        conn.execute(
            """INSERT INTO manager_snapshot
                 (snap_date, portal_user_id, period, param, grade, value, denom)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (day, uid, period, param, grade, round(value, 3), denom))


def snap(conn, day):
    """Один снимок за дату day (Влд): периоды 7d/30d + mom + mat."""
    today = conn.execute(
        f"SELECT (now() AT TIME ZONE '{VLD}')::date").fetchone()[0]
    rx = dict(rx_contract=RX_CONTRACT, rx_qtime=RX_QTIME,
              rx_qbudget=RX_QBUDGET, rx_qmodel=RX_QMODEL)
    total = 0
    for period, days in (("7d", 7), ("30d", 30)):
        rows = {}
        d0, d1 = day - dt.timedelta(days=days), day
        for name, q in QUERIES.items():
            sh = q["shift"]
            c0, c1 = d0 - dt.timedelta(days=sh), d1 - dt.timedelta(days=sh)
            if q["cohort"] and c0 < JUNE:
                continue  # правило 6: звонковые когорты только с июня
            params = dict(c0=c0, c1=c1, d0=d0, d1=d1, **rx)
            try:
                rows.update(collect(conn, q["sql"], params))
            except Exception:
                log.exception("Слепок %s %s %s", day, period, name)
                raise
        add_std(rows)
        write(conn, day, period, rows)
        total += len(rows)
    # вызревшие: единое окно 45–135 дней, период 'mat'
    rows = collect(conn, Q_REZ, dict(c0=day - dt.timedelta(days=135),
                                     c1=day - dt.timedelta(days=45)))
    add_std(rows)
    write(conn, day, "mat", rows)
    total += len(rows)
    # моментальные — только у сегодняшнего снимка
    if day == today:
        rows = collect(conn, Q_GI3, {})
        write(conn, day, "mom", rows)
        total += len(rows)
    log.info("Слепок %s: записано %s значений", day, total)
    return total


def main():
    args = sys.argv[1:]
    with db() as conn:
        today = conn.execute(
            f"SELECT (now() AT TIME ZONE '{VLD}')::date").fetchone()[0]
        if "--retro" in args:
            d = RETRO_FROM
            while d < today:
                snap(conn, d)
                d += dt.timedelta(days=7)
            snap(conn, today)
        elif "--date" in args:
            snap(conn, dt.date.fromisoformat(args[args.index("--date") + 1]))
        else:
            snap(conn, today)


if __name__ == "__main__":
    main()
