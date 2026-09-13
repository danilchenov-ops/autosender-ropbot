# -*- coding: utf-8 -*-
"""Насколько рано загорается корона.

Веса подобраны на «итоговом» состоянии заявки (окно 30 дней). Здесь считаем ту же
оценку по фактам, известным через 1, 3, 7 и 14 дней после заявки, и смотрим,
сколько продаж корона ловит ЗАРАНЕЕ, а не задним числом.
"""
import json
import math
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/app")
from common import db     # noqa: E402
import scorer             # noqa: E402

FROM, TO = "2026-06-01", "2026-08-01"
WINDOWS = [1, 3, 7, 14, 30]

SQL = """
WITH base AS (
  SELECT l.id, l.phone_e164, l.date_create, (l.status_semantic='S') AS won
  FROM leads l
  WHERE l.date_create >= %s AND l.date_create < %s
    AND l.phone_kind='mobile' AND l.source_id IS DISTINCT FROM 'PARTNER'
    AND coalesce(l.utm_source,'') <> 'PARTNER'
)
SELECT b.id, b.phone_e164, b.won, c.in_ans, c.max_dur, c.talks, c.span_d, c.talk_sec
FROM base b, LATERAL (
  SELECT count(*) FILTER (WHERE direction='in' AND duration>0) AS in_ans,
         coalesce(max(duration) FILTER (WHERE duration>0),0)   AS max_dur,
         count(*) FILTER (WHERE duration>0)                    AS talks,
         coalesce(extract(epoch FROM (max(call_start) FILTER (WHERE duration>0)
                  - min(call_start) FILTER (WHERE duration>0)))/86400.0,0) AS span_d,
         coalesce(sum(duration) FILTER (WHERE duration>0),0)   AS talk_sec
  FROM calls WHERE phone_e164=b.phone_e164
    AND call_start BETWEEN b.date_create - interval '30 min'
                       AND b.date_create + (%s || ' days')::interval) c
ORDER BY b.id
"""

cfg_json = json.load(open("/tmp/composite.json"))
COEF = cfg_json["coef"]
LO1, LO99 = cfg_json["lo1"], cfg_json["lo99"]


def nb_train(cache, ys, factors):
    p0 = float(sum(ys)) / len(ys)
    stats = defaultdict(dict)
    for f, yy in zip(cache, ys):
        for fa in factors:
            n, s = stats[fa].get(f[fa], (0, 0))
            stats[fa][f[fa]] = (n + 1, s + int(yy))
    odds0 = p0 / (1 - p0)
    w = {}
    for fa in factors:
        for v, (n, s) in stats[fa].items():
            if n >= scorer.MIN_N:
                p = (s + scorer.K * p0) / (n + scorer.K)
                w[(fa, v)] = math.log(p / (1 - p) / odds0)
    return w, math.log(odds0)


with db() as conn:
    cfg = scorer.MODELS["v2b"]
    tr_rows = conn.execute(cfg["sql"] + """
        AND l.date_create >= now() - (%s || ' days')::interval
        AND l.date_create <  now() - (%s || ' days')::interval""",
        (scorer.TRAIN_FROM_DAYS, scorer.TRAIN_TO_DAYS)).fetchall()
    co_rows = conn.execute(cfg["sql"] + f"""
        AND l.date_create >= '{FROM}' AND l.date_create < '{TO}'""").fetchall()
    w_all, bl_all = nb_train([cfg["features"](r) for r in tr_rows],
                             np.array([1.0 if r[1] == "S" else 0.0 for r in tr_rows]),
                             cfg["factors"])
    BEH = {}
    for r in co_rows:
        p = scorer.score_p(cfg["features"](r), w_all, bl_all, cfg["factors"], cfg["pair"])
        BEH[r[0]] = math.log(p / (1 - p)) - bl_all
    data = {}
    for wd in WINDOWS:
        data[wd] = conn.execute(SQL, (FROM, TO, wd)).fetchall()


def points(r):
    lid, _ph, _w, in_ans, max_dur, talks, span_d, talk_sec = r
    x = [BEH.get(lid, 0.0), 1.0 if lid in BEH else 0.0,
         1.0 if 120 <= max_dur < 420 else 0.0,
         1.0 if max_dur >= 420 else 0.0,
         1.0 if 3 <= talks <= 5 else 0.0,
         1.0 if talks >= 6 else 0.0,
         1.0 if span_d >= 1 else 0.0,
         1.0 if talk_sec >= 900 else 0.0,
         1.0 if in_ans == 1 else 0.0,
         1.0 if in_ans >= 2 else 0.0]
    lo = COEF[0] + sum(c * v for c, v in zip(COEF[1:], x))
    return max(0.0, min(100.0, 100 * (lo - LO1) / (LO99 - LO1))), max_dur


y = np.array([1.0 if r[2] else 0.0 for r in data[30]])
print(f"когорта {FROM}..{TO}: заявок {len(y)}, продаж {int(y.sum())}\n")
print(f"{'окно':>6} {'корон':>7} {'в день':>7} {'продаж':>7} {'конверсия':>10} {'поймано продаж':>15}")
for thr in (60,):
    for wd in WINDOWS:
        rows = data[wd]
        pts = np.array([points(r)[0] for r in rows])
        dur = np.array([points(r)[1] for r in rows])
        m = (pts >= thr) & (dur > 0)
        print(f"{wd:>4} дн {int(m.sum()):>7} {m.sum()/61:>7.1f} {int(y[m].sum()):>7} "
              f"{100*y[m].mean():>9.2f}% {100*y[m].sum()/y.sum():>14.1f}%")

print("\n--- где корона в первые 3 дня, а продажа случилась позже ---")
rows3 = data[3]
pts3 = np.array([points(r)[0] for r in rows3])
dur3 = np.array([points(r)[1] for r in rows3])
early = (pts3 >= 60) & (dur3 > 0)
print(f"корон на 3-й день {int(early.sum())}, из них дошли до продажи {int(y[early].sum())} "
      f"({100*y[early].mean():.1f}%)")
print(f"всего продаж {int(y.sum())}, из них корона стояла уже на 3-й день: "
      f"{int(y[early].sum())} ({100*y[early].sum()/y.sum():.0f}%)")
