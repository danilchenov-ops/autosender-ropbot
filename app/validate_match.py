"""Валидация матчинга заявок с визитами Метрики БЕЗ ClientID.

Берём лиды, у которых ClientID известен (истина), прячем его и пытаемся найти
клиента по косвенным ключам: окно времени, город по IP, UTM-кампания, цель Venoo.
Меряем точность и покрытие — по ним решаем, включать ли матчинг для безымянных заявок.

Запуск: python validate_match.py > /tmp/validate_match.json
"""
import datetime as dt
import json
import os
import time
from collections import defaultdict

import requests

from common import db, log

TOKEN = os.getenv("YANDEX_METRIKA_TOKEN", "").strip()
COUNTER = 56331115
API = "https://api-metrika.yandex.net/stat/v1/data"
import sys as _sys
STRICT = "--strict" in _sys.argv
WINDOW_BEFORE = (30 if STRICT else 90) * 60
WINDOW_AFTER = 3 * 60


def day_visits(day):
    rows, offset = [], 1
    while True:
        r = requests.get(API, params={
            "ids": COUNTER, "date1": day, "date2": day,
            "metrics": ("ym:s:visits,ym:s:goal354790295reaches,"
                        "ym:s:goal335664517reaches,ym:s:goal63078325reaches"),
            "dimensions": "ym:s:clientID,ym:s:dateTime,ym:s:regionCity,ym:s:UTMCampaign",
            "accuracy": "full", "limit": 10000, "offset": offset, "lang": "ru",
        }, headers={"Authorization": f"OAuth {TOKEN}"}, timeout=120)
        r.raise_for_status()
        data = r.json().get("data", [])
        for row in data:
            d, m = row["dimensions"], row["metrics"]
            rows.append({
                "cid": d[0]["name"], "dt": d[1]["name"],
                "city": d[2]["name"] or "", "utm": d[3]["name"] or "",
                "g_venoo": (m[1] or 0) > 0,
            })
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


def main():
    with db() as conn:
        leads = conn.execute("""
            SELECT l.id, l.ym_client_id,
                   to_char(l.date_create AT TIME ZONE 'Asia/Vladivostok','YYYY-MM-DD'),
                   to_char(l.date_create AT TIME ZONE 'Asia/Vladivostok','YYYY-MM-DD HH24:MI:SS'),
                   coalesce(l.ip_city,''), coalesce(nullif(l.utm_campaign,''),''),
                   (l.title = 'Venyoo.ru')
            FROM leads l
            WHERE l.ym_client_id IS NOT NULL
              AND l.date_create >= now() - interval '21 days'
              AND l.date_create <  now() - interval '1 day'
            ORDER BY l.date_create DESC LIMIT 400""").fetchall()

    by_day = defaultdict(list)
    for lead in leads:
        by_day[lead[2]].append(lead)

    stats = dict(total=0, nocand=0, cand1=0, correct1=0, multi=0,
                 multi_top_correct=0, top_correct=0)
    for day, ls in sorted(by_day.items(), reverse=True):
        try:
            visits = day_visits(day)
        except Exception as e:  # noqa: BLE001
            log.warning("день %s: %s", day, str(e)[:120])
            continue
        time.sleep(1)
        log.info("день %s: %s визитов, %s лидов", day, len(visits), len(ls))
        for (_lid, true_cid, _d, t, city, utm, is_venyoo) in ls:
            t0 = dt.datetime.fromisoformat(t)
            lo = (t0 - dt.timedelta(seconds=WINDOW_BEFORE)).strftime("%Y-%m-%d %H:%M:%S")
            hi = (t0 + dt.timedelta(seconds=WINDOW_AFTER)).strftime("%Y-%m-%d %H:%M:%S")
            cand = [v for v in visits if lo <= v["dt"] <= hi]
            if STRICT:
                if utm:
                    cand = [v for v in cand if v["utm"] == utm]
                nc = norm_city(city)
                if nc:
                    cand = [v for v in cand if v["city"] and norm_city(v["city"]) == nc]
                if is_venyoo:
                    cand = [v for v in cand if v["g_venoo"]]
            else:
                if utm:
                    cand = [v for v in cand if not v["utm"] or v["utm"] == utm]
                nc = norm_city(city)
                if nc:
                    cand = [v for v in cand if not v["city"] or norm_city(v["city"]) == nc]
                if is_venyoo:
                    goal_hits = [v for v in cand if v["g_venoo"]]
                    if goal_hits:
                        cand = goal_hits
            cand.sort(key=lambda v: abs((dt.datetime.fromisoformat(v["dt"]) - t0).total_seconds()))
            cids = []
            for v in cand:
                if v["cid"] not in cids:
                    cids.append(v["cid"])

            stats["total"] += 1
            if not cids:
                stats["nocand"] += 1
                continue
            if cids[0] == true_cid:
                stats["top_correct"] += 1
            if len(cids) == 1:
                stats["cand1"] += 1
                if cids[0] == true_cid:
                    stats["correct1"] += 1
            else:
                stats["multi"] += 1
                if cids[0] == true_cid:
                    stats["multi_top_correct"] += 1

    matched = stats["total"] - stats["nocand"]
    out = {
        **stats,
        "точность_при_единственном": round(100.0 * stats["correct1"] / stats["cand1"], 1) if stats["cand1"] else None,
        "точность_ближайшего": round(100.0 * stats["top_correct"] / matched, 1) if matched else None,
        "покрытие": round(100.0 * matched / stats["total"], 1) if stats["total"] else None,
    }
    log.info("ВАЛИДАЦИЯ МАТЧИНГА: %s", json.dumps(out, ensure_ascii=False))
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
