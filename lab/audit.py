# -*- coding: utf-8 -*-
"""Независимый аудит скрипта: пересчёт цифр свежим кодом, значимость,
устойчивость по менеджерам и проверка видео-приёма текстовым поиском.

Ничего не берёт из aggregate.py, кроме card_features (сами признаки),
частоты считает заново. LLM не используется — экономика проверки 0 ₽.
"""
import json
import math
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402
from aggregate import card_features  # noqa: E402


def two_prop_p(x1, n1, x2, n2):
    """Двусторонний z-тест для двух долей."""
    p1, p2 = x1 / n1, x2 / n2
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return 1.0
    z = (p1 - p2) / se
    # нормальное приближение
    return 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2)))), z


def fetch(split, extra=""):
    conn = runner.db()
    with conn.cursor() as c:
        c.execute(f"""
            select s.call_id, s.grp, s.manager, s.duration, k.card, k.talk_share,
                   k.roles, t.segments, t.text
            from lab_cards k
            join lab_sample s using(call_id)
            join transcripts t on t.call_id = s.call_id
            where s.excluded is null and s.is_first and k.err is null
              and s.direction='out' and s.call_start < '2026-08-01'
              and s.split='{split}' {extra}
            order by s.call_id""")
        return c.fetchall()


# --- видео-приём: реплики менеджера, содержащие видео/камеры/трансляцию
VIDEO = re.compile(r'видео|видос|камер|трансляц|прямой эфир|запись экрана', re.I)
MSGR = re.compile(r'\bмакс\b|whatsapp|ватсап|вотсап|телеграм|telegram', re.I)


def manager_lines(roles, segments):
    out = []
    lines = [s.get("text", "").strip() for s in (segments or []) if (s.get("text") or "").strip()]
    if not roles or len(roles) != len(lines):
        return lines  # ролей нет — берём всё (консервативно)
    return [ln for r, ln in zip(roles, lines) if r == "М"]


def analyze(rows, label):
    print(f"\n================ {label} ================")
    by = {"WON": [], "LOST": []}
    for cid, grp, mgr, dur, card, ts, roles, segs, text in rows:
        f = card_features(card, float(ts) if ts is not None else None)
        ml = " ".join(manager_lines(roles, segs))
        f["_video"] = bool(VIDEO.search(ml))
        f["_msgr"] = bool(MSGR.search(ml))
        by[grp].append((cid, mgr, dur, f))
    w, l = by["WON"], by["LOST"]
    nw, nl = len(w), len(l)
    print(f"WON {nw} / LOST {nl}")

    keys = ["next_step_specific", "pain_uncovered", "decision_maker_identified",
            "linked_to_client_pain", "date_time_fixed", "timeline_discussed",
            "next_step_proposed", "budget_discussed", "client_commitment",
            "_video", "_msgr"]
    print(f"{'признак':28} {'WON':>7} {'LOST':>7} {'разрыв':>8} {'p':>8} {'z':>6}")
    res = {}
    for k in keys:
        xw = sum(1 for _, _, _, f in w if f[k])
        xl = sum(1 for _, _, _, f in l if f[k])
        p, z = two_prop_p(xw, nw, xl, nl)
        res[k] = (xw, nw, xl, nl, p, z)
        print(f"{k:28} {100*xw/nw:6.1f}% {100*xl/nl:6.1f}% "
              f"{100*xw/nw-100*xl/nl:+7.1f} {p:8.4f} {z:+6.2f}")

    # устойчивость по менеджерам (top-факторы)
    print("\n-- по менеджерам (WON% / LOST%, только с 5+ и 5+ наблюдений) --")
    for k in ["next_step_specific", "decision_maker_identified", "_video"]:
        parts = []
        for mgr in sorted({m for _, m, _, _ in w} | {m for _, m, _, _ in l}):
            ww = [f for _, m, _, f in w if m == mgr]
            ll = [f for _, m, _, f in l if m == mgr]
            if len(ww) < 5 or len(ll) < 5:
                continue
            wp = 100 * sum(1 for f in ww if f[k]) / len(ww)
            lp = 100 * sum(1 for f in ll if f[k]) / len(ll)
            parts.append(f"{mgr.split()[0]}:{wp:.0f}/{lp:.0f}")
        print(f"  {k:28} " + "  ".join(parts))
    return by


def video_context(rows, grp_filter, limit=12):
    print(f"\n-- контекст видео-упоминаний ({grp_filter}) --")
    n = 0
    for cid, grp, mgr, dur, card, ts, roles, segs, text in rows:
        if grp != grp_filter:
            continue
        for ln in manager_lines(roles, segs):
            if VIDEO.search(ln):
                print(f"  [#{cid}] {re.sub(r'\\s+',' ',ln)[:150]}")
                n += 1
                break
        if n >= limit:
            break


def bootstrap_auc(scores_w, scores_l, iters=4000, seed=17):
    """Бутстрэп-CI для AUC. Свой LCG, т.к. random детерминизм не критичен."""
    state = seed
    def rnd(n):
        nonlocal state
        state = (state * 6364136223846793005 + 1442695040888963407) % (2**64)
        return state % n
    aucs = []
    for _ in range(iters):
        bw = [scores_w[rnd(len(scores_w))] for _ in scores_w]
        bl = [scores_l[rnd(len(scores_l))] for _ in scores_l]
        wins = sum(1 for a in bw for b in bl if a > b)
        ties = sum(1 for a in bw for b in bl if a == b)
        aucs.append((wins + 0.5 * ties) / (len(bw) * len(bl)))
    aucs.sort()
    return aucs[int(0.025 * iters)], aucs[int(0.975 * iters)]


def main():
    tr = fetch("train")
    ho = fetch("holdout")
    analyze(tr, "ОБУЧАЮЩАЯ (исходящие, май-июль)")
    analyze(ho, "КОНТРОЛЬНАЯ")
    video_context(tr, "WON")
    video_context(tr, "LOST")

    # CI для AUC карточки на контроле
    from score_card import CRITERIA
    KEYS = ["next_step", "decision_maker", "value_to_pain", "date_fixed", "timeline"]
    W = {"next_step": 34, "decision_maker": 22, "value_to_pain": 20,
         "date_fixed": 13, "timeline": 11}
    def sc(f):
        return sum(W[k] * CRITERIA[k][1](f) / 2.0 for k in KEYS)
    for label, rows in (("обучающей", tr), ("контрольной", ho)):
        w = [sc(card_features(c, float(ts) if ts is not None else None))
             for _, g, _, _, c, ts, _, _, _ in rows if g == "WON"]
        l = [sc(card_features(c, float(ts) if ts is not None else None))
             for _, g, _, _, c, ts, _, _, _ in rows if g == "LOST"]
        lo, hi = bootstrap_auc(w, l)
        wins = sum(1 for a in w for b in l if a > b)
        ties = sum(1 for a in w for b in l if a == b)
        auc = (wins + 0.5 * ties) / (len(w) * len(l))
        print(f"\nAUC на {label}: {auc:.3f}, 95% CI [{lo:.3f}; {hi:.3f}] "
              f"(WON {len(w)}, LOST {len(l)})")


if __name__ == "__main__":
    main()
