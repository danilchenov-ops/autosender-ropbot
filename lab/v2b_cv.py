# -*- coding: utf-8 -*-
"""Честная перекрёстная проверка боевой модели v2b.

Скоринг заявок замеряли внутри выборки: веса считали на тех же лидах, на которых
потом смотрели конверсию классов. Здесь переобучаем модель 5 раз, каждый раз
пряча пятую часть лидов, и меряем AUC ровно на спрятанном.
"""
import math
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, "/app")
from common import db     # noqa: E402
import scorer             # noqa: E402

FOLDS = 5


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


def train_on(cache, ys, factors, pair):
    """Повторяет логику scorer.train на подвыборке."""
    n_tot, s_tot = len(ys), int(sum(ys))
    p0 = s_tot / n_tot
    stats = defaultdict(dict)
    for f, yy in zip(cache, ys):
        for fa in factors:
            n, s = stats[fa].get(f[fa], (0, 0))
            stats[fa][f[fa]] = (n + 1, s + int(yy))
    odds0 = p0 / (1 - p0)
    weights = {}
    for fa in factors:
        for v, (n, s) in stats[fa].items():
            if n >= scorer.MIN_N:
                p = (s + scorer.K * p0) / (n + scorer.K)
                weights[(fa, v)] = math.log(p / (1 - p) / odds0)
    return weights, math.log(odds0)


with db() as conn:
    cfg = scorer.MODELS["v2b"]
    rows = conn.execute(cfg["sql"] + """
        AND l.date_create >= now() - (%s || ' days')::interval
        AND l.date_create <  now() - (%s || ' days')::interval""",
        (scorer.TRAIN_FROM_DAYS, scorer.TRAIN_TO_DAYS)).fetchall()

cache = [cfg["features"](r) for r in rows]
y = np.array([1.0 if r[1] == "S" else 0.0 for r in rows])
ids = [r[0] for r in rows]
print(f"обучающее окно v2b: лидов {len(y)}, продаж {int(y.sum())}, база {100*y.mean():.2f}%")

# внутри выборки — как считали раньше
w_all, bl_all = train_on(cache, y, cfg["factors"], cfg["pair"])
ins = np.array([scorer.score_p(f, w_all, bl_all, cfg["factors"], cfg["pair"]) for f in cache])
print(f"AUC внутри выборки (как мерили до сих пор): {auc(y, ins):.3f}")

# вне выборки
folds = np.array([i % FOLDS for i in range(len(y))])
rng = np.random.default_rng(7)
rng.shuffle(folds)
oof = np.zeros(len(y))
for f in range(FOLDS):
    tr, te = folds != f, folds == f
    w, bl = train_on([c for c, m in zip(cache, tr) if m], y[tr], cfg["factors"], cfg["pair"])
    for i in np.where(te)[0]:
        oof[i] = scorer.score_p(cache[i], w, bl, cfg["factors"], cfg["pair"])
a_oof = auc(y, oof)
print(f"AUC вне выборки (переобучение без своей пятой части): {a_oof:.3f}")

# что даёт каждый признак по отдельности, вне выборки
print("\n--- вклад признаков, вне выборки ---")
for drop in cfg["factors"]:
    keep = [x for x in cfg["factors"] if x != drop]
    oof2 = np.zeros(len(y))
    for f in range(FOLDS):
        tr, te = folds != f, folds == f
        w, bl = train_on([c for c, m in zip(cache, tr) if m], y[tr], keep, cfg["pair"])
        for i in np.where(te)[0]:
            oof2[i] = scorer.score_p(cache[i], w, bl, keep, cfg["pair"])
    print(f"  без «{drop}»: AUC {auc(y, oof2):.3f}  ({auc(y, oof2)-a_oof:+.3f})")

# конверсия по классам вне выборки
q = np.quantile(oof, np.arange(0, 1.01, 0.01))
pct = np.array([max(0, min(100, int(np.sum(q <= p)) - 1)) for p in oof])
print("\n--- конверсия по классам, вне выборки ---")
for g, lo, hi in (("A", 88, 101), ("B", 60, 88), ("C", 20, 60), ("D", 0, 20)):
    m = (pct >= lo) & (pct < hi)
    if m.sum():
        print(f"  {g}: лидов {m.sum():5}, продаж {int(y[m].sum()):3}, "
              f"конверсия {100*y[m].mean():.2f}%")
