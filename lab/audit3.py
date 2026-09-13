# -*- coding: utf-8 -*-
"""Проверка требований руководителя на данных (22.08):
1) квалификационные вопросы (срок / бюджет / модель) — задаются ли и КОГДА;
2) предложение договора — частота в WON и LOST;
3) прокси карточки v2 — разделяет ли группы не хуже v1.
Всё регулярками, LLM не используется. 0 ₽.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402
from audit import fetch, two_prop_p  # noqa: E402
from aggregate import card_features  # noqa: E402

Q_TIME = re.compile(r'когда.{0,40}(покупк|планир|брать|приобрет|заказ)|'
                    r'как скоро|в какие сроки|когда планируете', re.I)
Q_BUDGET = re.compile(r'бюджет|какую сумму|сколько.{0,25}(денег|средств|готовы)|'
                      r'в какие деньги|по деньгам.{0,15}(сколько|какой)', re.I)
Q_MODEL = re.compile(r'какой (год|кузов|автомобиль|марк)|как(ая|ую) (машин|модел|марк)|'
                     r'что (вас )?интересует|какого года|год рассматр|кто интересует', re.I)
CONTRACT = re.compile(r'(заключ|подпис|составл|отправл|пришл|скин|сдела)\w*.{0,40}договор|'
                      r'договор\w*.{0,30}(заключ|подпис|составл|отправл|пришл|скин)', re.I)


def mgr_lines(roles, segments):
    lines = [s.get("text", "").strip() for s in (segments or [])
             if (s.get("text") or "").strip()]
    if not roles or len(roles) != len(lines):
        return [(i / max(1, len(lines) - 1) if len(lines) > 1 else 0, ln)
                for i, ln in enumerate(lines)]
    n = max(1, len(lines) - 1)
    return [(i / n, ln) for i, (r, ln) in enumerate(zip(roles, lines)) if r == "М"]


def first_pos(pl, rx):
    """Позиция первого совпадения (0..1) или None."""
    for pos, ln in pl:
        if rx.search(ln):
            return pos
    return None


def stat(w, l, name, fn):
    xw = sum(1 for r in w if fn(r))
    xl = sum(1 for r in l if fn(r))
    p, z = two_prop_p(xw, len(w), xl, len(l))
    print(f"{name:44} WON {100*xw/len(w):5.1f}% ({xw:3})  "
          f"LOST {100*xl/len(l):5.1f}% ({xl:3})  "
          f"разрыв {100*xw/len(w)-100*xl/len(l):+6.1f}  p={p:.4f}")


def main():
    rows = fetch("train")
    recs = []
    for cid, grp, mgr, dur, card, ts, roles, segs, text in rows:
        pl = mgr_lines(roles, segs)
        f = card_features(card, float(ts) if ts is not None else None)
        recs.append({
            "cid": cid, "grp": grp, "f": f,
            "t_pos": first_pos(pl, Q_TIME),
            "b_pos": first_pos(pl, Q_BUDGET),
            "m_pos": first_pos(pl, Q_MODEL),
            "contract": any(CONTRACT.search(ln) for _, ln in pl),
        })
    w = [r for r in recs if r["grp"] == "WON"]
    l = [r for r in recs if r["grp"] == "LOST"]
    print(f"обучающая, исходящие, май-июль: WON {len(w)} LOST {len(l)}\n")

    print("--- квалификация: вопрос вообще задан (регулярка) ---")
    stat(w, l, "вопрос про срок покупки", lambda r: r["t_pos"] is not None)
    stat(w, l, "вопрос про бюджет", lambda r: r["b_pos"] is not None)
    stat(w, l, "вопрос про модель/год", lambda r: r["m_pos"] is not None)

    print("\n--- квалификация: задан РАНО (в первой трети разговора) ---")
    stat(w, l, "срок — в первой трети", lambda r: r["t_pos"] is not None and r["t_pos"] <= 0.33)
    stat(w, l, "бюджет — в первой трети", lambda r: r["b_pos"] is not None and r["b_pos"] <= 0.33)
    stat(w, l, "модель — в первой трети", lambda r: r["m_pos"] is not None and r["m_pos"] <= 0.33)
    stat(w, l, "2 из 3 — в первой половине",
         lambda r: sum(1 for k in ("t_pos", "b_pos", "m_pos")
                       if r[k] is not None and r[k] <= 0.5) >= 2)

    print("\n--- договор ---")
    stat(w, l, "менеджер предложил договор", lambda r: r["contract"])

    # средняя позиция вопросов
    for k, nm in (("t_pos", "срок"), ("b_pos", "бюджет"), ("m_pos", "модель")):
        for gname, g in (("WON", w), ("LOST", l)):
            xs = sorted(r[k] for r in g if r[k] is not None)
            if xs:
                print(f"медиана позиции «{nm}» {gname}: {xs[len(xs)//2]:.2f}", end="   ")
        print()

    # --- карточка v2 (прокси на имеющихся полях + регулярки) ---
    from score_card import CRITERIA
    def v2(r):
        f = r["f"]
        s = 0.0
        # квалификация 20: срок 8, бюджет 6, модель 6
        s += 8 if f["timeline_discussed"] else 0
        s += 6 if f["budget_discussed"] else 0
        s += 6 if r["m_pos"] is not None else 0
        # следующий шаг 25
        s += 25 if f["next_step_specific"] else (10 if f["next_step_proposed"] else 0)
        # договор 15
        s += 15 if r["contract"] else 0
        # безопасность/видео 10 (прокси: видео-упоминание менеджера)
        # (проактивную безопасность без видео регуляркой не ловим — прокси занижен)
        # состав решения 10, привязка выгоды 10, срок след. действия 10
        s += 10 if f["decision_maker_identified"] else 0
        s += 10 if f["linked_to_client_pain"] else (4 if f["value_props_n"] >= 2 else 0)
        s += 10 if f["date_time_fixed"] else 0
        # понижающий коэффициент: <2 из 3 квалификаций в первой половине
        quals = sum(1 for k in ("t_pos", "b_pos", "m_pos")
                    if r[k] is not None and r[k] <= 0.5)
        if quals < 2:
            s *= 0.7
        return s

    def auc(sw, sl):
        wins = sum(1 for a in sw for b in sl if a > b)
        ties = sum(1 for a in sw for b in sl if a == b)
        return (wins + 0.5 * ties) / (len(sw) * len(sl))

    for split in ("train", "holdout"):
        rows2 = fetch(split)
        rc = []
        for cid, grp, mgr, dur, card, ts, roles, segs, text in rows2:
            pl = mgr_lines(roles, segs)
            rc.append({"grp": grp,
                       "f": card_features(card, float(ts) if ts is not None else None),
                       "t_pos": first_pos(pl, Q_TIME), "b_pos": first_pos(pl, Q_BUDGET),
                       "m_pos": first_pos(pl, Q_MODEL),
                       "contract": any(CONTRACT.search(ln) for _, ln in pl)})
        sw = [v2(r) for r in rc if r["grp"] == "WON"]
        sl = [v2(r) for r in rc if r["grp"] == "LOST"]
        print(f"\nv2-прокси на {split}: WON ср {sum(sw)/len(sw):.1f}  "
              f"LOST ср {sum(sl)/len(sl):.1f}  разрыв {sum(sw)/len(sw)-sum(sl)/len(sl):+.1f}  "
              f"AUC {auc(sw, sl):.3f}  (n={len(sw)}/{len(sl)})")


if __name__ == "__main__":
    main()
