# -*- coding: utf-8 -*-
"""Проверка составной оценки: калибровка и разделение.

С 22.08 блок поведения в composite.py считается живьём (beh_map), поэтому
проверка гоняет ровно боевой код: beh_map + evaluate.

1. Калибровка на июне-июле: обещанная вероятность против фактической.
2. Проверка по времени: веса подобраны на июне-июле, смотрим август.
"""
import sys

import numpy as np

sys.path.insert(0, "/app")
from common import db     # noqa: E402
import composite          # noqa: E402


def auc(y, s):
    s = np.asarray(s, float)
    o = np.argsort(s)
    r = np.empty(len(s), float)
    r[o] = np.arange(1, len(s) + 1)
    for v in np.unique(s):
        m = s == v
        if m.sum() > 1:
            r[m] = r[m].mean()
    n1, n0 = y.sum(), (1 - y).sum()
    if n1 == 0 or n0 == 0:
        return float("nan")
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


SQL = """
WITH base AS (
  SELECT l.id, l.phone_e164, l.date_create, (l.status_semantic='S') AS won
  FROM leads l
  WHERE l.date_create >= %s AND l.date_create < %s
    AND l.phone_kind='mobile' AND l.source_id IS DISTINCT FROM 'PARTNER'
    AND coalesce(l.utm_source,'') <> 'PARTNER'
)
SELECT b.id, 'x', b.phone_e164, c.in_ans, c.max_dur, c.talks, c.span_d,
       c.talk_sec, c.card_score, b.won
FROM base b, LATERAL (
  SELECT count(*) FILTER (WHERE k.direction='in' AND k.duration>0) AS in_ans,
         coalesce(max(k.duration) FILTER (WHERE k.duration>0),0)   AS max_dur,
         count(*) FILTER (WHERE k.duration>0)                      AS talks,
         coalesce(extract(epoch FROM (max(k.call_start) FILTER (WHERE k.duration>0)
                  - min(k.call_start) FILTER (WHERE k.duration>0)))/86400.0,0) AS span_d,
         coalesce(sum(k.duration) FILTER (WHERE k.duration>0),0)   AS talk_sec,
         max(cs.card_score)                                        AS card_score
  FROM calls k LEFT JOIN call_scores cs ON cs.call_id=k.id
  WHERE k.phone_e164=b.phone_e164
    AND k.call_start >= b.date_create - interval '30 min') c
ORDER BY b.id
"""


def run(a, b_, title, beh):
    with db() as conn:
        rows = conn.execute(SQL, (a, b_)).fetchall()
    y = np.array([1.0 if r[9] else 0.0 for r in rows])
    pts, ps, dur = [], [], []
    for r in rows:
        p_, lo, p, _parts, md = composite.evaluate(r[:9], beh.get(r[0]))
        pts.append(p_); ps.append(p); dur.append(md)
    pts, ps, dur = np.array(pts), np.array(ps), np.array(dur)
    print(f"\n===== {title}: заявок {len(y)}, продаж {int(y.sum())}, "
          f"база {100*y.mean():.2f}% =====")
    if y.sum() < 3:
        return
    print(f"AUC составной оценки: {auc(y, pts):.3f}")
    print(f"{'баллы':>10} {'заявок':>8} {'продаж':>7} {'факт':>8} {'обещано':>9}")
    for lo_, hi in ((0, 20), (20, 40), (40, 60), (60, 80), (80, 101)):
        m = (pts >= lo_) & (pts < hi)
        if m.sum():
            print(f"{lo_:>4}-{hi-1:<5} {int(m.sum()):>8} {int(y[m].sum()):>7} "
                  f"{100*y[m].mean():>7.2f}% {100*ps[m].mean():>8.2f}%")
    crown = (pts >= composite.CROWN_MIN) & (dur > 0)
    print(f"корона: {int(crown.sum())} заявок, продаж {int(y[crown].sum())}, "
          f"конверсия {100*y[crown].mean():.2f}%, "
          f"поймано {100*y[crown].sum()/max(y.sum(),1):.0f}% всех продаж")


with db() as conn:
    BEH = composite.beh_map(conn, 450)   # накрывает обе когорты

run("2026-06-01", "2026-08-01", "июнь-июль (на этих данных подбирали веса)", BEH)
run("2026-08-01", "2026-08-16", "1-15 августа (веса этих данных не видели)", BEH)
