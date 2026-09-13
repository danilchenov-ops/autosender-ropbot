# -*- coding: utf-8 -*-
"""Вес блока «разговор» — по ИТОГОВОЙ карточке из пяти критериев (app/card.py).

Первый замер шёл по черновой карточке из шести критериев, где ещё был
«вытащена проблема клиента». В итоговую версию он не вошёл, поэтому вес
пересчитываем.
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/ropbot/app")
import runner                        # noqa: E402
from aggregate import card_features  # noqa: E402
import card as final_card            # noqa: E402
from score_card import score as draft_score  # noqa: E402


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
        step = np.linalg.solve(X1.T @ (X1 * w[:, None]) + pen, X1.T @ (y - p) - pen @ b)
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

y, fin, dra, dur, split = [], [], [], [], []
for cid, grp, d, sp, cd, ts in rows:
    f = card_features(cd, float(ts) if ts is not None else None)
    fin.append(final_card.score(f)[0])
    dra.append(draft_score(f)[0])
    y.append(1.0 if grp == "WON" else 0.0)
    dur.append(d or 0)
    split.append(sp)

y = np.array(y); fin = np.array(fin); dra = np.array(dra)
dur = np.array(dur); split = np.array(split)
print(f"разговоров {len(y)}: WON {int(y.sum())}, LOST {int((1-y).sum())}")
for nm, s in (("итоговая карточка (5 критериев)", fin), ("черновая (6, с «проблемой»)", dra)):
    print(f"  {nm:34} WON {s[y==1].mean():5.1f}  LOST {s[y==0].mean():5.1f}  AUC {auc(y, s):.3f}")
for sp in ("train", "holdout"):
    m = split == sp
    print(f"    {sp:8} n={m.sum():3}  итоговая AUC {auc(y[m], fin[m]):.3f}  "
          f"черновая {auc(y[m], dra[m]):.3f}")

band = np.stack([(dur >= 120) & (dur < 420), dur >= 420], axis=1).astype(float)
b = fit(np.hstack([fin.reshape(-1, 1) / 10.0, band]), y)
print(f"\nитоговая карточка сверх длительности: {b[1]:+.3f} лог-шанса на 10 баллов "
      f"(шансы x{math.exp(b[1]):.2f})")
b2 = fit(fin.reshape(-1, 1) / 10.0, y)
print(f"без поправки на длительность:          {b2[1]:+.3f}")
