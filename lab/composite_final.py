# -*- coding: utf-8 -*-
"""Итоговый подбор весов составной оценки.

Отличия от черновика:
  — лог-шансы поведения считаются ВНЕ выборки (v2b переобучается без своей пятой
    части лидов), иначе блок поведения получил бы завышенный вес;
  — вес карточки разговора берётся из зеркальной lab-выборки (card_weight.py);
  — итог проверяется перекрёстно по хешу телефона.
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
FOLDS = 5
CARD_PER10 = 0.321        # из card_weight.py, зеркальная выборка 224 разговора

COHORT = f"""
WITH base AS (
  SELECT l.id, l.phone_e164, l.date_create, (l.status_semantic='S') AS won
  FROM leads l
  WHERE l.date_create >= '{FROM}' AND l.date_create < '{TO}'
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
                       AND b.date_create + interval '30 days') c
ORDER BY b.id
"""


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
        step = np.linalg.solve(X1.T @ (X1 * w[:, None]) + pen,
                              X1.T @ (y - p) - pen @ b)
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


# ── лог-шансы поведения вне выборки ──────────────────────────────────────────
with db() as conn:
    cfg = scorer.MODELS["v2b"]
    tr_rows = conn.execute(cfg["sql"] + """
        AND l.date_create >= now() - (%s || ' days')::interval
        AND l.date_create <  now() - (%s || ' days')::interval""",
        (scorer.TRAIN_FROM_DAYS, scorer.TRAIN_TO_DAYS)).fetchall()
    co_rows = conn.execute(cfg["sql"] + f"""
        AND l.date_create >= '{FROM}' AND l.date_create < '{TO}'""").fetchall()
    rows = conn.execute(COHORT).fetchall()

tr_cache = [cfg["features"](r) for r in tr_rows]
tr_y = np.array([1.0 if r[1] == "S" else 0.0 for r in tr_rows])
tr_ids = [r[0] for r in tr_rows]
rng = np.random.default_rng(7)
tr_fold = rng.integers(0, FOLDS, len(tr_ids))
fold_of = dict(zip(tr_ids, tr_fold))

models = []
for f in range(FOLDS):
    m = tr_fold != f
    models.append(nb_train([c for c, k in zip(tr_cache, m) if k], tr_y[m], cfg["factors"]))
w_all, bl_all = nb_train(tr_cache, tr_y, cfg["factors"])

BEH = {}
for r in co_rows:
    f = cfg["features"](r)
    # лид из обучающего окна оцениваем моделью, которая его не видела
    w, bl = models[fold_of[r[0]]] if r[0] in fold_of else (w_all, bl_all)
    p = scorer.score_p(f, w, bl, cfg["factors"], cfg["pair"])
    BEH[r[0]] = math.log(p / (1 - p)) - bl

y = np.array([1.0 if r[2] else 0.0 for r in rows])
phones = [r[1] or "" for r in rows]

NAMES = ["поведение на сайте (v2b)", "поведение известно",
         "разговор 2-7 мин", "разговор 7+ мин",
         "разговоров 3-5", "разговоров 6+",
         "диалог 1+ день", "суммарно 15+ мин",
         "клиент перезвонил 1 раз", "клиент перезвонил 2+ раза"]


def build(r):
    lid, _ph, _w, in_ans, max_dur, talks, span_d, talk_sec = r
    return [BEH.get(lid, 0.0), 1.0 if lid in BEH else 0.0,
            1.0 if 120 <= max_dur < 420 else 0.0,
            1.0 if max_dur >= 420 else 0.0,
            1.0 if 3 <= talks <= 5 else 0.0,
            1.0 if talks >= 6 else 0.0,
            1.0 if span_d >= 1 else 0.0,
            1.0 if talk_sec >= 900 else 0.0,
            1.0 if in_ans == 1 else 0.0,
            1.0 if in_ans >= 2 else 0.0]


X = np.array([build(r) for r in rows])
print(f"когорта {FROM}..{TO}: заявок {len(y)}, продаж {int(y.sum())}, база {100*y.mean():.2f}%")
print(f"AUC блока поведения ВНЕ выборки: {auc(y, X[:,0]):.3f}")

b = fit(X, y)
print("\n=== веса составной оценки (лог-шансы) ===")
print(f"  {'свободный член':28} {b[0]:+.3f}")
for n, c in zip(NAMES, b[1:]):
    print(f"  {n:28} {c:+.3f}  (x{math.exp(c):.2f})")
print(f"  {'карточка разговора, за 10 б.':28} {CARD_PER10:+.3f}  (x{math.exp(CARD_PER10):.2f})  "
      f"— из зеркальной выборки")

folds = np.array([abs(hash(p)) % FOLDS for p in phones])
oof = np.zeros(len(y))
for f in range(FOLDS):
    tr, te = folds != f, folds == f
    bb = fit(X[tr], y[tr])
    oof[te] = np.hstack([np.ones((te.sum(), 1)), X[te]]) @ bb
print(f"\nAUC составной оценки вне выборки: {auc(y, oof):.3f}")

# доля разброса на каждый блок
rng_beh = float(np.percentile(X[:, 0], 99) - np.percentile(X[:, 0], 1)) * b[1]
rng_talk = float(b[3] + b[6] + b[7] + b[8]) if False else float(b[3] + b[6] + b[8])
blocks = {
    "интерес до звонка": abs(rng_beh) + abs(b[2]),
    "разговоры": abs(b[4]) + abs(b[6]) + abs(b[7]) + abs(b[8]) + abs(CARD_PER10 * 5),
    "отклик клиента": abs(b[9]) + abs(b[10]),
}
tot = sum(blocks.values())
print("\n=== фактический вклад блоков в разброс оценки ===")
for k, v in blocks.items():
    print(f"  {k:22} {100*v/tot:5.1f}%")

# перевод лог-шансов в 0-100 и пороги
lo1, lo99 = -6.72, -1.00
pts = np.clip(100 * (oof - lo1) / (lo99 - lo1), 0, 100)
print(f"\nшкала: {lo1:.2f} лог-шанса = 0 баллов, {lo99:.2f} = 100 баллов")
print("\n=== пороги (только заявки, где состоялся разговор) ===")
had_talk = X[:, 2] + X[:, 3] > 0
print(f"  таких заявок {int(had_talk.sum())} из {len(y)}, продаж {int(y[had_talk].sum())}")
for thr in (50, 60, 65, 70, 75, 80, 85):
    m = had_talk & (pts >= thr)
    if m.sum():
        print(f"  порог {thr}: помечено {int(m.sum()):4} ({100*m.sum()/had_talk.sum():4.1f}% "
              f"разговоров, ~{m.sum()/61:.1f} в день), продаж {int(y[m].sum()):3}, "
              f"конверсия {100*y[m].mean():5.2f}%, поймано {100*y[m].sum()/y.sum():4.1f}% всех продаж")

json.dump({"coef": list(b), "names": NAMES, "card_per10": CARD_PER10,
           "lo1": lo1, "lo99": lo99}, open("/tmp/composite.json", "w"), ensure_ascii=False)
print("\nсохранено /tmp/composite.json")
