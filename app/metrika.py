"""Метрика: что клиент делал на сайте ДО заявки. Привязка — Yandex ClientID из лида.

Для каждого лида с ym_client_id спрашивает у Метрики визиты этого клиента
и сохраняет снимок «до момента заявки» в lead_visits. Визиты, начавшиеся
после создания лида, не учитываются. Часовой пояс счётчика — Asia/Vladivostok.

Запуск:
    python metrika.py enrich --days 2 --limit 150     # свежие (каждые 5–15 минут)
    python metrika.py enrich --days 400 --limit 3500  # ночной бэкфилл истории
"""
import datetime as dt
import json
import os
import re
import sys
import time

import requests

from common import db, log

TOKEN = os.getenv("YANDEX_METRIKA_TOKEN", "").strip()
COUNTERS = [int(x) for x in os.getenv("METRIKA_COUNTERS", "56331115,98314285").split(",")]
API = "https://api-metrika.yandex.net/stat/v1/data"
RX_LOT = re.compile(r"/\d{5,}$")
SLEEP = 1.0


def fetch_visits(counter_id, client_id, date1, date2):
    """Список визитов клиента. Возвращает [], если данных нет; None при 400 (кривой запрос)."""
    params = {
        "ids": counter_id,
        "date1": date1, "date2": date2,
        "metrics": "ym:s:visits,ym:s:pageviews,ym:s:avgVisitDurationSeconds",
        "dimensions": ("ym:s:dateTime,ym:s:startURLPath,ym:s:lastsignTrafficSource,"
                       "ym:s:deviceCategory,ym:s:regionCity,ym:s:UTMCampaign"),
        "filters": f"ym:s:clientID=='{client_id}'",
        "accuracy": "full", "limit": 250,
    }
    for attempt in range(2):
        r = requests.get(API, params=params, headers={"Authorization": f"OAuth {TOKEN}"}, timeout=60)
        if r.status_code == 429 and attempt == 0:
            log.warning("Метрика просит подождать (429)")
            time.sleep(60)
            continue
        break
    if r.status_code == 400:
        log.warning("Метрика 400 (счётчик %s, клиент %s): %s", counter_id, client_id, r.text[:120])
        return None
    r.raise_for_status()
    out = []
    for row in r.json().get("data", []):
        d, m = row["dimensions"], row["metrics"]
        out.append({
            "dt": d[0]["name"], "path": d[1]["name"] or "/",
            "source": d[2]["name"], "device": d[3]["name"], "region": d[4]["name"],
            "campaign": d[5]["name"] or "",
            "pv": int(m[1] or 0), "sec": int(m[2] or 0),
        })
    return out


def summarize(visits, lead_dt_local):
    before = [v for v in visits if v["dt"] <= lead_dt_local]
    if not before:
        return None
    before.sort(key=lambda v: v["dt"])
    first = dt.datetime.fromisoformat(before[0]["dt"])
    lead = dt.datetime.fromisoformat(lead_dt_local)
    last = before[-1]
    campaigns = [v["campaign"] for v in before if v["campaign"]]
    return {
        "visits_before": len(before),
        "days_since_first": (lead.date() - first.date()).days,
        "pageviews": sum(v["pv"] for v in before),
        "seconds": sum(v["sec"] for v in before),
        "distinct_landings": len({v["path"] for v in before}),
        "saw_lot": any(RX_LOT.search(v["path"]) for v in before),
        "last_source": last["source"], "device": last["device"], "region": last["region"],
        "last_campaign": campaigns[-1] if campaigns else None,
        "raw": before[-40:],
    }


def enrich(days, limit, skip=0):
    if not TOKEN:
        log.error("Не задан YANDEX_METRIKA_TOKEN")
        return
    with db() as conn:
        rows = conn.execute(
            """SELECT l.id, l.ym_client_id,
                      to_char(l.date_create AT TIME ZONE 'Asia/Vladivostok', 'YYYY-MM-DD HH24:MI:SS'),
                      to_char(least(l.date_create AT TIME ZONE 'Asia/Vladivostok',
                                    now() AT TIME ZONE 'Asia/Vladivostok'), 'YYYY-MM-DD'),
                      to_char(l.date_create AT TIME ZONE 'Asia/Vladivostok' - interval '365 days', 'YYYY-MM-DD')
               FROM leads l
               WHERE l.ym_client_id IS NOT NULL
                 AND l.date_create < now() - interval '45 minutes'
                 AND l.date_create >= now() - (%s || ' days')::interval
                 AND l.date_create <  now() - (%s || ' days')::interval
                 AND NOT EXISTS (SELECT 1 FROM lead_visits v WHERE v.lead_id = l.id)
               ORDER BY l.date_create DESC LIMIT %s""",
            (days, skip, limit)).fetchall()
        done = errors = 0
        for lead_id, cid, lead_dt, d2, d1 in rows:
            try:
                summary = None
                for counter in COUNTERS:
                    visits = fetch_visits(counter, cid, d1, d2)
                    time.sleep(SLEEP)
                    if visits:  # None (400) и [] — пробуем следующий счётчик
                        summary = summarize(visits, lead_dt)
                        if summary:
                            summary["counter"] = counter
                            break
                if summary:
                    conn.execute(
                        """INSERT INTO lead_visits (lead_id, client_id, counter_id, visits_before,
                               days_since_first, pageviews, seconds, distinct_landings, saw_lot,
                               last_source, device, region, last_campaign, raw)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (lead_id) DO UPDATE SET
                             visits_before=EXCLUDED.visits_before, pageviews=EXCLUDED.pageviews,
                             seconds=EXCLUDED.seconds, saw_lot=EXCLUDED.saw_lot,
                             last_campaign=EXCLUDED.last_campaign, fetched_at=now(), raw=EXCLUDED.raw""",
                        (lead_id, cid, summary["counter"], summary["visits_before"],
                         summary["days_since_first"], summary["pageviews"], summary["seconds"],
                         summary["distinct_landings"], summary["saw_lot"], summary["last_source"],
                         summary["device"], summary["region"], summary["last_campaign"],
                         json.dumps(summary["raw"], ensure_ascii=False)))
                else:
                    conn.execute(
                        """INSERT INTO lead_visits (lead_id, client_id, visits_before)
                           VALUES (%s,%s,0) ON CONFLICT (lead_id) DO NOTHING""",
                        (lead_id, cid))
                done += 1
                errors = 0
            except requests.RequestException as e:
                errors += 1
                log.warning("Метрика, лид %s: %s", lead_id, str(e)[:150])
                if errors >= 5:
                    log.error("Метрика: 5 ошибок подряд, стоп")
                    break
                time.sleep(10)
        if done:
            log.info("Метрика: обогащено лидов %s", done)




# ── Сопоставление по времени для заявок без ClientID ─────────────────────────
# Строгие ключи: окно [T−30 мин; T+3 мин], совпадение UTM и города (если есть у
# обеих сторон), для Venyoo — обязательная цель «Venoo». Матч засчитывается только
# при ЕДИНСТВЕННОМ кандидате. Валидация 20.08: точность 65,7%, поэтому категория
# таких лидов идёт с меткой «≈» и проверяется замером отдельно.

GOAL_VENOO = 354790295


def day_dump(day):
    rows, offset = [], 1
    while True:
        r = requests.get(API, params={
            "ids": COUNTERS[0], "date1": day, "date2": day,
            "metrics": f"ym:s:visits,ym:s:goal{GOAL_VENOO}reaches",
            "dimensions": "ym:s:clientID,ym:s:dateTime,ym:s:regionCity,ym:s:UTMCampaign",
            "accuracy": "full", "limit": 10000, "offset": offset, "lang": "ru",
        }, headers={"Authorization": f"OAuth {TOKEN}"}, timeout=120)
        r.raise_for_status()
        data = r.json().get("data", [])
        for row in data:
            d, m = row["dimensions"], row["metrics"]
            rows.append({"cid": d[0]["name"], "dt": d[1]["name"],
                         "city": d[2]["name"] or "", "utm": d[3]["name"] or "",
                         "g_venoo": (m[1] or 0) > 0})
        if len(data) < 10000:
            break
        offset += 10000
        time.sleep(1)
    return rows


def norm_city(c):
    c = (c or "").lower().replace("ё", "е")
    for junk in ("г ", "г. ", "город "):
        if c.startswith(junk):
            c = c[len(junk):]
    return c.strip()


def match_time(limit):
    if not TOKEN:
        log.error("Не задан YANDEX_METRIKA_TOKEN")
        return
    from collections import defaultdict
    with db() as conn:
        rows = conn.execute(
            """SELECT l.id,
                      to_char(l.date_create AT TIME ZONE 'Asia/Vladivostok','YYYY-MM-DD'),
                      to_char(l.date_create AT TIME ZONE 'Asia/Vladivostok','YYYY-MM-DD HH24:MI:SS'),
                      to_char(l.date_create AT TIME ZONE 'Asia/Vladivostok' - interval '365 days','YYYY-MM-DD'),
                      coalesce(l.ip_city,''), coalesce(nullif(l.utm_campaign,''),''),
                      (l.title = 'Venyoo.ru')
               FROM leads l
               WHERE l.date_create >= '2026-08-01'
                 AND l.date_create < now() - interval '1 hour'
                 AND l.ym_client_id IS NULL
                 AND l.phone_kind IN ('mobile','landline')
                 AND l.source_id IS DISTINCT FROM 'PARTNER'
                 AND NOT EXISTS (SELECT 1 FROM lead_visits v WHERE v.lead_id = l.id)
               ORDER BY l.date_create DESC LIMIT %s""", (limit,)).fetchall()
        if not rows:
            return
        by_day = defaultdict(list)
        for r in rows:
            by_day[r[1]].append(r)
        matched = none = 0
        for day, ls in sorted(by_day.items(), reverse=True):
            try:
                visits = day_dump(day)
            except requests.RequestException as e:
                log.warning("матчинг, день %s: %s", day, str(e)[:120])
                continue
            time.sleep(1)
            for (lid, _d, t, d1, city, utm, is_venyoo) in ls:
                t0 = dt.datetime.fromisoformat(t)
                lo = (t0 - dt.timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
                hi = (t0 + dt.timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
                cand = [v for v in visits if lo <= v["dt"] <= hi]
                if utm:
                    cand = [v for v in cand if v["utm"] == utm]
                nc = norm_city(city)
                if nc:
                    cand = [v for v in cand if v["city"] and norm_city(v["city"]) == nc]
                if is_venyoo:
                    cand = [v for v in cand if v["g_venoo"]]
                cids = sorted({v["cid"] for v in cand})
                if len(cids) == 1:
                    try:
                        vis = fetch_visits(COUNTERS[0], cids[0], d1, t[:10])
                        time.sleep(SLEEP)
                        summary = summarize(vis, t) if vis else None
                    except requests.RequestException as e:
                        log.warning("матчинг, лид %s: %s", lid, str(e)[:120])
                        continue
                    if summary:
                        conn.execute(
                            """INSERT INTO lead_visits (lead_id, client_id, counter_id, visits_before,
                                   days_since_first, pageviews, seconds, distinct_landings, saw_lot,
                                   last_source, device, region, last_campaign, match_kind, raw)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'time',%s)
                               ON CONFLICT (lead_id) DO NOTHING""",
                            (lid, cids[0], COUNTERS[0], summary["visits_before"],
                             summary["days_since_first"], summary["pageviews"], summary["seconds"],
                             summary["distinct_landings"], summary["saw_lot"], summary["last_source"],
                             summary["device"], summary["region"], summary["last_campaign"],
                             json.dumps(summary["raw"], ensure_ascii=False)))
                        matched += 1
                        continue
                conn.execute(
                    """INSERT INTO lead_visits (lead_id, client_id, visits_before, match_kind)
                       VALUES (%s,'-',0,'time_none') ON CONFLICT (lead_id) DO NOTHING""", (lid,))
                none += 1
        log.info("матчинг по времени: сопоставлено %s, без матча %s", matched, none)


if __name__ == "__main__":
    args = sys.argv[1:]
    days = int(args[args.index("--days") + 1]) if "--days" in args else 14
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 300
    skip = int(args[args.index("--skip") + 1]) if "--skip" in args else 0
    if args and args[0] == "match":
        match_time(limit)
    else:
        enrich(days, limit, skip)
