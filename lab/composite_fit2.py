# -*- coding: utf-8 -*-
"""Шаг 2: (а) проверка лишних признаков, (б) вес карточки разговора по lab-выборке,
(в) проверка утечки в блоке поведения (окно обучения v2b заканчивается ~7 июля)."""
import math
import os
import sys

import numpy as np

sys.path.insert(0, "/app")
sys.path.insert(0, "/lab")
from common import db                       # noqa: E402
import scorer                               # noqa: E402


def fit(X, y, l2=1.0, iters=200):
    n, k = X.shape
    X1 = np.hstack([np.ones((n, 1)), X])
    b = np.zeros(k + 1)
    pen = np.eye(k + 1) * l2
    pen[0, 0] = 0.0
    for _ in range(iters):
        z = X1 @ b
        p = 1 / (1 + np.exp(-np.clip(z, -30, 30)))
        w = np.clip(p * (1 - p), 1e-6, None)
        g = X1.T @ (y - p) - pen @ b
        H = X1.T @ (X1 * w[:, None]) + pen
        step = np.linalg.solve(H, g)
        b += step
        if np.abs(step).max() < 1e-8:
            break
    return b


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
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


FROM, TO = "2026-06-01", "2026-08-01"

COHORT = f"""
WITH base AS (
  SELECT l.id, l.phone_e164, l.date_create, (l.status_semantic='S') AS won
  FROM leads l
  WHERE l.date_create >= '{FROM}' AND l.date_create < '{TO}'
    AND l.phone_kind='mobile' AND l.source_id IS DISTINCT FROM 'PARTNER'
    AND coalesce(l.utm_source,'') <> 'PARTNER'
), w AS (
  SELECT b.*, c.*
  FROM base b, LATERAL (
    SELECT count(*) FILTER (WHERE direction='in' AND duration>0) AS in_ans,
           coalesce(max(duration) FILTER (WHERE duration>0),0)   AS max_dur,
           count(*) FILTER (WHERE duration>0)                    AS talks,
           count(*) FILTER (WHERE direction='out')               AS outs,
           coalesce(extract(epoch FROM (max(call_start) FILTER (WHERE duration>0)
                    - min(call_start) FILTER (WHERE duration>0)))/86400.0, 0) AS span_d,
           coalesce(sum(duration) FILTER (WHERE duration>0),0)   AS talk_sec
    FROM calls WHERE phone_e164=b.phone_e164
      AND call_start BETWEEN b.date_create - interval '30 min'
                         AND b.date_create + interval '30 days') c
)
SELECT id, phone_e164, won, in_ans, max_dur, talks, outs, span_d, talk_sec, date_create FROM w ORDER BY id
"""


def beh_logodds(conn):
    weights, _q, p0 = scorer.load_weights(conn, "v2b")
    base_logit = math.log(p0 / (1 - p0))
    cfg = scorer.MODELS["v2b"]
    rows = conn.execute(cfg["sql"] + f"""
        AND l.date_create >= '{FROM}' AND l.date_create < '{TO}'""").fetchall()
    out = {}
    for r in rows:
        f = cfg["features"](r)
        p = scorer.score_p(f, weights, base_logit, cfg["factors"], cfg["pair"])
        out[r[0]] = math.log(p / (1 - p)) - base_logit
    return out


def cv_auc(X, y, phones, folds_n=5):
    folds = np.array([abs(hash(p)) % folds_n for p in phones])
    oof = np.zeros(len(y))
    for f in range(folds_n):
        tr, te = folds != f, folds == f
        bb = fit(X[tr], y[tr])
        oof[te] = np.hstack([np.ones((te.sum(), 1)), X[te]]) @ bb
    return auc(y, oof), oof


with db() as conn:
    BEH = beh_logodds(conn)
    rows = conn.execute(COHORT).fetchall()

y = np.array([1.0 if r[2] else 0.0 for r in rows])
phones = [r[1] or "" for r in rows]
dates = [r[9] for r in rows]


def build(row, extra):
    lid, _ph, _w, in_ans, max_dur, talks, outs, span_d, talk_sec, _dt = row
    lo = BEH.get(lid, 0.0)
    f = [lo, 1.0 if lid in BEH else 0.0,
         1.0 if 120 <= max_dur < 420 else 0.0,
         1.0 if max_dur >= 420 else 0.0,
         1.0 if in_ans == 1 else 0.0,
         1.0 if in_ans >= 2 else 0.0]
    if "talks" in extra:
        f += [1.0 if 3 <= talks <= 5 else 0.0, 1.0 if talks >= 6 else 0.0]
    if "span" in extra:
        f += [1.0 if span_d >= 1 else 0.0, 1.0 if span_d >= 5 else 0.0]
    if "talksec" in extra:
        f += [1.0 if talk_sec >= 900 else 0.0]
    return f


BASE_N = ["поведение", "поведение известно", "разговор 2-7 мин", "разговор 7+ мин",
          "перезвонил 1 раз", "перезвонил 2+ раза"]
VARIANTS = {
    "база": ([], BASE_N),
    "+ число разговоров": (["talks"], BASE_N + ["разговоров 3-5", "разговоров 6+"]),
    "+ длина диалога": (["span"], BASE_N + ["диалог 1+ день", "диалог 5+ дней"]),
    "+ сумма разговоров": (["talksec"], BASE_N + ["суммарно 15+ мин"]),
    "всё": (["talks", "span", "talksec"], BASE_N + ["разговоров 3-5", "разговоров 6+",
            "диалог 1+ день", "диалог 5+ дней", "суммарно 15+ мин"]),
}
print("=== какие признаки стоит держать (AUC вне выборки) ===")
best = None
for title, (extra, names) in VARIANTS.items():
    X = np.array([build(r, extra) for r in rows])
    a, _ = cv_auc(X, y, phones)
    print(f"  {title:22} признаков {X.shape[1]:2}  AUC {a:.4f}")
    if best is None or a > best[0]:
        best = (a, title, extra, names)
print(f"  -> лучший: {best[1]}")

X = np.array([build(r, best[2]) for r in rows])
b = fit(X, y)
print("\n=== коэффициенты лучшего варианта ===")
print(f"  {'свободный член':26} {b[0]:+.3f}")
for n, c in zip(best[3], b[1:]):
    print(f"  {n:26} {c:+.3f}  (x{math.exp(c):.2f})")

# — утечка блока поведения: окно обучения v2b кончается ~45 дней назад (7 июля)
X0 = np.array([build(r, []) for r in rows])
for label, mask in (("июнь-7 июля (внутри окна обучения v2b)",
                     np.array([d.strftime('%Y-%m-%d') < '2026-07-08' for d in dates])),
                    ("8-31 июля (вне окна)",
                     np.array([d.strftime('%Y-%m-%d') >= '2026-07-08' for d in dates]))):
    bb = fit(X0[mask], y[mask])
    print(f"\n{label}: n={mask.sum()}, продаж {int(y[mask].sum())}, "
          f"коэффициент поведения {bb[1]:+.3f}, AUC поведения {auc(y[mask], X0[mask][:,0]):.3f}")
