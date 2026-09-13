# -*- coding: utf-8 -*-
"""Подбор весов составной оценки на истории июнь-июль 2026.

Три блока доказательств:
  BEH  — поведение на сайте до заявки (модель v2b), лог-шансы относительно базы
  TALK — что было в разговорах: состоялся ли, длина самого длинного
  RESP — отклик клиента: сам перезванивает, обращается повторно

Считаем лог-шансы, а не проценты: пропущенный блок даёт ровно 0 и не искажает
сумму — это и есть «нормировать по доступному».
"""
import json
import math
import sys

import numpy as np

sys.path.insert(0, "/app")
from common import db          # noqa: E402
import scorer                  # noqa: E402

FROM, TO = "2026-06-01", "2026-08-01"
WINDOW_D = 30                  # окно наблюдения отклика после заявки

COHORT = f"""
WITH base AS (
  SELECT l.id, l.phone_e164, l.date_create, (l.status_semantic='S') AS won
  FROM leads l
  WHERE l.date_create >= '{FROM}' AND l.date_create < '{TO}'
    AND l.phone_kind = 'mobile'
    AND l.source_id IS DISTINCT FROM 'PARTNER'
    AND coalesce(l.utm_source,'') <> 'PARTNER'
)
SELECT b.id, b.phone_e164, b.date_create, b.won,
  (SELECT count(*) FROM calls c WHERE c.phone_e164=b.phone_e164
     AND c.direction='in' AND c.duration>0
     AND c.call_start BETWEEN b.date_create - interval '30 min'
                          AND b.date_create + interval '{WINDOW_D} days') AS in_ans,
  (SELECT coalesce(max(c.duration),0) FROM calls c WHERE c.phone_e164=b.phone_e164
     AND c.duration>0
     AND c.call_start BETWEEN b.date_create - interval '30 min'
                          AND b.date_create + interval '{WINDOW_D} days') AS max_dur,
  (SELECT count(*) FROM calls c WHERE c.phone_e164=b.phone_e164
     AND c.duration>0
     AND c.call_start BETWEEN b.date_create - interval '30 min'
                          AND b.date_create + interval '{WINDOW_D} days') AS talks,
  (SELECT count(*) FROM leads l2 WHERE l2.phone_e164=b.phone_e164
     AND l2.date_create <= b.date_create) AS n_leads
FROM base b ORDER BY b.id
"""


def beh_logodds(conn):
    """lead_id -> лог-шанс поведения относительно базы (0 если поведения нет)."""
    lw = scorer.load_weights(conn, "v2b")
    if not lw:
        raise SystemExit("нет весов v2b")
    weights, _q, p0 = lw
    base_logit = math.log(p0 / (1 - p0))
    cfg = scorer.MODELS["v2b"]
    rows = conn.execute(cfg["sql"] + f"""
        AND l.date_create >= '{FROM}' AND l.date_create < '{TO}'""").fetchall()
    out = {}
    for r in rows:
        f = cfg["features"](r)
        p = scorer.score_p(f, weights, base_logit, cfg["factors"], cfg["pair"])
        out[r[0]] = math.log(p / (1 - p)) - base_logit
    return out, p0


def bands(row, lo):
    """Признаки-индикаторы. Порядок совпадает с NAMES."""
    _id, _ph, _dt, _won, in_ans, max_dur, talks, n_leads = row
    return [
        lo,
        1.0 if _id in HAS_BEH else 0.0,
        1.0 if 120 <= max_dur < 420 else 0.0,
        1.0 if max_dur >= 420 else 0.0,
        1.0 if in_ans == 1 else 0.0,
        1.0 if in_ans >= 2 else 0.0,
        1.0 if n_leads >= 2 else 0.0,
    ]


NAMES = ["поведение (лог-шансы v2b)", "поведение известно",
         "разговор 2-7 мин", "разговор 7+ мин",
         "клиент перезвонил 1 раз", "клиент перезвонил 2+ раза",
         "повторное обращение"]


def fit(X, y, l2=1.0, iters=200):
    """Логистическая регрессия, IRLS с гребневым штрафом."""
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


with db() as conn:
    BEH, P0 = beh_logodds(conn)
    HAS_BEH = set(BEH)
    rows = conn.execute(COHORT).fetchall()

y = np.array([1.0 if r[3] else 0.0 for r in rows])
X = np.array([bands(r, BEH.get(r[0], 0.0)) for r in rows])
ids = [r[0] for r in rows]
phones = [r[1] or "" for r in rows]

print(f"когорта {FROM}..{TO}: заявок {len(rows)}, продаж {int(y.sum())}, "
      f"база {100*y.mean():.2f}%, поведение известно у {sum(1 for i in ids if i in HAS_BEH)}")

b = fit(X, y)
print("\n--- коэффициенты (лог-шансы) ---")
print(f"  {'свободный член':34} {b[0]:+.3f}")
for name, c in zip(NAMES, b[1:]):
    print(f"  {name:34} {c:+.3f}   (шансы x{math.exp(c):.2f})")

folds = np.array([abs(hash(p)) % 5 for p in phones])
oof = np.zeros(len(y))
for f in range(5):
    tr, te = folds != f, folds == f
    bb = fit(X[tr], y[tr])
    oof[te] = np.hstack([np.ones((te.sum(), 1)), X[te]]) @ bb
print(f"\nAUC составной оценки, вне выборки: {auc(y, oof):.3f}")
for name, col in zip(NAMES, X.T):
    if len(np.unique(col)) > 1:
        print(f"  AUC одного признака {name:34} {auc(y, col):.3f}")

np.save("/tmp/X.npy", X)
np.save("/tmp/y.npy", y)
json.dump({"ids": ids, "phones": phones, "b": list(b), "p0": P0, "oof": list(oof)},
          open("/tmp/fit.json", "w"))
print("\nсохранено /tmp/fit.json")
