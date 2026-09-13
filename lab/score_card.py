# -*- coding: utf-8 -*-
"""Карточка оценки разговора и её валидация на отложенной выборке (промт 4).

Веса пропорциональны разрыву WON/LOST, замеренному на зеркальной выборке
исходящих звонков мая-июля (76 WON против 98 LOST), с проверкой внутри
полос длительности. Критерии, не пережившие проверку, в карточку не вошли.

Балл считается кодом по карточке разговора, а не моделью на глаз.
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402
from aggregate import card_features  # noqa: E402

# критерий -> (вес, функция уровня 0/1/2)
CRITERIA = {
    "next_step": (27, lambda f: 2 if f["next_step_specific"]
                  else (1 if f["next_step_proposed"] else 0)),
    "pain": (21, lambda f: 2 if f["pain_uncovered"]
             else (1 if f["questions_total"] >= 4 else 0)),
    "decision_maker": (17, lambda f: 2 if f["decision_maker_identified"] else 0),
    "value_to_pain": (16, lambda f: 2 if f["linked_to_client_pain"]
                      else (1 if f["value_props_n"] >= 2 else 0)),
    "date_fixed": (10, lambda f: 2 if f["date_time_fixed"] else 0),
    "timeline": (9, lambda f: 2 if f["timeline_discussed"] else 0),
}
TITLES = {
    "next_step": "Следующий шаг",
    "pain": "Проблема клиента вытащена",
    "decision_maker": "Выяснено, кто принимает решение",
    "value_to_pain": "Выгода привязана к проблеме клиента",
    "date_fixed": "Зафиксированы дата и время",
    "timeline": "Выяснен срок покупки",
}


def score(f):
    total = 0
    parts = {}
    for key, (weight, level) in CRITERIA.items():
        lv = level(f)
        pts = weight * lv / 2.0
        parts[key] = (lv, round(pts, 1))
        total += pts
    return round(total, 1), parts


def load(split, out_only=True, may_jul=True):
    conn = runner.db()
    where = ["s.excluded IS NULL", "s.is_first", "k.err IS NULL",
             f"s.split='{split}'"]
    if out_only:
        where.append("s.direction='out'")
    if may_jul:
        where.append("s.call_start < '2026-08-01'")
    with conn.cursor() as c:
        c.execute(f"""select s.call_id, s.grp, s.duration, s.manager, k.card, k.talk_share
                      from lab_cards k join lab_sample s using(call_id)
                      where {' AND '.join(where)} order by s.call_id""")
        return [(cid, grp, dur, mgr,
                 card_features(cd, float(ts) if ts is not None else None))
                for cid, grp, dur, mgr, cd, ts in c.fetchall()]


def report(rows, title):
    print(f"\n===== {title} =====")
    scored = [(cid, grp, dur, *score(f), f) for cid, grp, dur, _, f in rows]
    for grp in ("WON", "LOST"):
        xs = [s for _, g, _, s, _, _ in scored if g == grp]
        if not xs:
            continue
        xs_sorted = sorted(xs)
        n = len(xs)
        print(f"{grp}: N={n} среднее {sum(xs)/n:5.1f} "
              f"медиана {xs_sorted[n//2]:5.1f} "
              f"25-й {xs_sorted[n//4]:5.1f} 75-й {xs_sorted[3*n//4]:5.1f} "
              f"мин {min(xs):.0f} макс {max(xs):.0f}")
    w = [s for _, g, _, s, _, _ in scored if g == "WON"]
    l = [s for _, g, _, s, _, _ in scored if g == "LOST"]
    if w and l:
        import statistics as st
        pooled = st.pstdev(w + l) or 1
        d = (sum(w)/len(w) - sum(l)/len(l)) / pooled
        # доля пар, где WON выше LOST — AUC
        wins = sum(1 for a in w for b in l if a > b)
        ties = sum(1 for a in w for b in l if a == b)
        auc = (wins + 0.5*ties) / (len(w)*len(l))
        print(f"разрыв средних {sum(w)/len(w)-sum(l)/len(l):+.1f} балла, "
              f"эффект Коэна d={d:.2f}, AUC={auc:.3f}")
        for thr in (40, 50, 60):
            wt = 100*sum(1 for x in w if x >= thr)/len(w)
            lt = 100*sum(1 for x in l if x >= thr)/len(l)
            print(f"  порог {thr}: выигранных выше {wt:.0f}%, проигранных выше {lt:.0f}%")
    return scored


def criteria_power(rows):
    """Различающая сила каждого критерия отдельно."""
    print("\n--- вклад критериев (доля с уровнем 2) ---")
    w = [f for _, g, _, _, f in rows if g == "WON"]
    l = [f for _, g, _, _, f in rows if g == "LOST"]
    for key, (weight, level) in CRITERIA.items():
        wp = 100*sum(1 for f in w if level(f) == 2)/len(w)
        lp = 100*sum(1 for f in l if level(f) == 2)/len(l)
        print(f"  {TITLES[key]:38} вес {weight:2}  WON {wp:5.1f}%  "
              f"LOST {lp:5.1f}%  разрыв {wp-lp:+6.1f}")


if __name__ == "__main__":
    tr = load("train")
    ho = load("holdout")
    report(tr, "ОБУЧАЮЩАЯ (на ней строили)")
    criteria_power(tr)
    sc = report(ho, "КОНТРОЛЬНАЯ (модель её не видела)")
    criteria_power(ho)
    print("\n--- аномалии контрольной выборки ---")
    sc_sorted = sorted(sc, key=lambda x: x[3])
    print("выигранные с низким баллом:")
    for cid, g, dur, s, parts, f in sc_sorted:
        if g == "WON" and s < 40:
            print(f"  #{cid} балл {s} длит {dur}с  " +
                  " ".join(f"{k}={v[0]}" for k, v in parts.items()))
    print("проигранные с высоким баллом:")
    for cid, g, dur, s, parts, f in reversed(sc_sorted):
        if g == "LOST" and s >= 60:
            print(f"  #{cid} балл {s} длит {dur}с  " +
                  " ".join(f"{k}={v[0]}" for k, v in parts.items()))
