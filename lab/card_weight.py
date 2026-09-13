# -*- coding: utf-8 -*-
"""Вес блока «разговор» в лог-шансах: сколько добавляет карточка эталонного
скрипта сверх того, что уже видно по длительности разговора.

Выборка lab_sample зеркальная (WON и LOST подобраны по месяцу, источнику и
менеджеру), поэтому логистическая регрессия на ней даёт условное отношение
шансов — его можно переносить в составную оценку.
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner                       # noqa: E402
from aggregate import card_features  # noqa: E402
from score_card import score         # noqa: E402


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


conn = runner.db()
with conn.cursor() as c:
    c.execute("""select s.call_id, s.grp, s.duration, s.split, k.card, k.talk_share
                 from lab_cards k join lab_sample s using(call_id)
                 where s.excluded is null and s.is_first and k.err is null
                   and s.direction='out' and s.call_start < '2026-08-01'
                 order by s.call_id""")
    rows = c.fetchall()

y, sc, dur, split = [], [], [], []
for cid, grp, d, sp, card, ts in rows:
    f = card_features(card, float(ts) if ts is not None else None)
    s, _ = score(f)
    y.append(1.0 if grp == "WON" else 0.0)
    sc.append(s)
    dur.append(d or 0)
    split.append(sp)

y = np.array(y); sc = np.array(sc); dur = np.array(dur); split = np.array(split)
print(f"разговоров {len(y)}: WON {int(y.sum())}, LOST {int((1-y).sum())}")
print(f"балл карточки: WON среднее {sc[y==1].mean():.1f}, LOST {sc[y==0].mean():.1f}, "
      f"AUC {auc(y, sc):.3f}")

# полосы длительности — чтобы вес карточки был сверх длительности, а не вместо
band = np.stack([(dur >= 120) & (dur < 420), dur >= 420], axis=1).astype(float)
print(f"AUC одной длительности: {auc(y, dur):.3f}")

for title, X in (("только карточка", sc.reshape(-1, 1) / 10.0),
                 ("только длительность", band),
                 ("карточка + длительность", np.hstack([sc.reshape(-1, 1) / 10.0, band]))):
    b = fit(X, y)
    z = np.hstack([np.ones((len(y), 1)), X]) @ b
    parts = "  ".join(f"{v:+.3f}" for v in b[1:])
    print(f"\n{title}: коэффициенты {parts}   AUC {auc(y, z):.3f}")

b = fit(np.hstack([sc.reshape(-1, 1) / 10.0, band]), y)
per10 = b[1]
print(f"\n--- вывод ---")
print(f"каждые 10 баллов карточки: лог-шанс {per10:+.3f} (шансы x{math.exp(per10):.2f}) "
      f"сверх длительности")
print(f"разброс карточки 0..100 баллов = {per10*10:+.2f} лог-шанса")

# проверка на отложенной выборке
for sp in ("train", "holdout"):
    m = split == sp
    if m.sum() > 4:
        print(f"  {sp}: n={m.sum()}, AUC карточки {auc(y[m], sc[m]):.3f}, "
              f"AUC длительности {auc(y[m], dur[m]):.3f}")
