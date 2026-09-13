# -*- coding: utf-8 -*-
"""Детерминированный счёт по карточкам. Модель ничего не считает — она
интерпретирует уже посчитанное.

Выдаёт:
  out/stats.json  — метрики по группам, страты, контрпримеры
  out/quotes.json — банк дословных цитат из WON, с проверкой по транскрипту
  out/objections.json — возражения с частотами
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402  (даёт db(), norm())

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
NO = ("нет", "", "не называлась", "-", "—", "нет данных")


def is_yes(v):
    return v is True


def has(v):
    if not isinstance(v, str):
        return False
    return runner.norm(v) not in [runner.norm(x) for x in NO]


def card_features(card, talk_share):
    g = card.get
    op = g("opening") or {}
    di = g("discovery") or {}
    pr = g("presentation") or {}
    cl = g("closing") or {}
    dy = g("dynamics") or {}
    obj = g("objections") or []
    qs = [q for q in (di.get("questions_asked") or []) if isinstance(q, str) and q.strip()]
    f = {
        "reason_for_call_stated": is_yes(op.get("reason_for_call_stated")),
        "permission_asked": is_yes(op.get("permission_asked")),
        "questions_total": len(qs),
        "open_questions": di.get("open_questions_count") or 0,
        "closed_questions": di.get("closed_questions_count") or 0,
        "pain_uncovered": has(di.get("pain_uncovered")),
        "budget_discussed": is_yes(di.get("budget_discussed")),
        "decision_maker_identified": is_yes(di.get("decision_maker_identified")),
        "timeline_discussed": is_yes(di.get("timeline_discussed")),
        "criteria_of_choice": has(di.get("criteria_of_choice")),
        "pitched_before_discovery": is_yes(pr.get("pitched_before_discovery")),
        "linked_to_client_pain": is_yes(pr.get("linked_to_client_pain")),
        "proof_used": len([p for p in (pr.get("proof_used") or []) if has(p)]) > 0,
        "value_props_n": len([p for p in (pr.get("value_props_used") or []) if has(p)]),
        "price_named_by_manager": is_yes(pr.get("price_named_by_manager")),
        "price_before_discovery": (pr.get("price_moment") or "").startswith("до"),
        "price_client_asked_first": "клиент" in (pr.get("price_moment") or ""),
        "objections_n": len(obj),
        "objection_resolved_share": (
            sum(1 for o in obj if (o.get("resolved") or "") == "да") / len(obj)
            if obj else None),
        "next_step_proposed": has(cl.get("next_step_proposed")),
        "next_step_specific": is_yes(cl.get("next_step_specific")),
        "date_time_fixed": is_yes(cl.get("date_time_fixed")),
        "client_commitment": has(cl.get("client_verbal_commitment")),
        "talk_share": talk_share,
        "monologue_sentences": dy.get("longest_manager_monologue_sentences") or 0,
        "client_questions": dy.get("client_questions_count") or 0,
        "interruptions": dy.get("interruptions_by_manager") or 0,
        "client_ready_on_arrival": is_yes(card.get("client_ready_on_arrival")),
    }
    return f


BOOL_KEYS = [k for k in [
    "reason_for_call_stated", "permission_asked", "pain_uncovered",
    "budget_discussed", "decision_maker_identified", "timeline_discussed",
    "criteria_of_choice", "pitched_before_discovery", "linked_to_client_pain",
    "proof_used", "price_named_by_manager", "price_before_discovery",
    "price_client_asked_first", "next_step_proposed", "next_step_specific",
    "date_time_fixed", "client_commitment"]]
NUM_KEYS = ["questions_total", "open_questions", "closed_questions",
            "value_props_n", "objections_n", "talk_share",
            "monologue_sentences", "client_questions", "interruptions"]


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


def median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return round((xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2), 2)


SRC_BUCKET = {
    "direct": "Директ", "autosendersite_max_direct": "Директ",
    "autosendersite_tg_direct": "Директ", "reklama_tg": "Директ",
    "9025240049": "Звонок на рекламный номер", "9644454909": "Звонок на рекламный номер",
    "CALL": "Звонок", "WEB": "Форма сайта", "OTHER": "Прочее",
}


def bucket(src):
    return SRC_BUCKET.get(src or "", "Прочее")


def main(scope):
    conn = runner.db()
    where = "s.excluded IS NULL AND k.err IS NULL AND k.card IS NOT NULL AND s.is_first"
    # matched — зеркальная выборка: только май-июль, где обе группы сопоставимы
    if scope in ("matched", "matched_holdout"):
        where += " AND s.call_start < '2026-08-01'"
    where += (" AND s.split='holdout'" if scope.endswith("holdout")
              else " AND s.split='train'")
    with conn.cursor() as c:
        c.execute(f"""
            SELECT s.call_id, s.grp, s.client, s.source, s.manager, s.call_start,
                   s.duration, s.is_first, s.split, k.card, k.talk_share, t.text
            FROM lab_cards k
            JOIN lab_sample s USING(call_id)
            JOIN transcripts t ON t.call_id = s.call_id
            WHERE {where}
            ORDER BY s.call_id""")
        rows = c.fetchall()
    print(f"карточек в анализе: {len(rows)}")

    recs = []
    for (cid, grp, client, source, manager, cs, dur, isf, split,
         card, ts, text) in rows:
        f = card_features(card, float(ts) if ts is not None else None)
        recs.append({
            "call_id": cid, "grp": grp, "client": client,
            "src": bucket(source), "source": source, "manager": manager,
            "month": cs.strftime("%Y-%m") if cs else None,
            "duration": dur, "is_first": isf, "f": f, "card": card, "text": text,
        })

    won = [r for r in recs if r["grp"] == "WON"]
    lost = [r for r in recs if r["grp"] == "LOST"]

    stats = {"n_won": len(won), "n_lost": len(lost), "scope": scope,
             "clients_won": len({r["client"] for r in won}),
             "clients_lost": len({r["client"] for r in lost}),
             "metrics": {}, "strata": {}, "composition": {}}

    # состав групп — сырьё для проверки конфаундеров
    for key in ("src", "manager", "month"):
        stats["composition"][key] = {
            "WON": Counter(r[key] for r in won).most_common(),
            "LOST": Counter(r[key] for r in lost).most_common(),
        }
    stats["composition"]["duration_sec"] = {
        "WON": {"mean": mean([r["duration"] for r in won]),
                "median": median([r["duration"] for r in won])},
        "LOST": {"mean": mean([r["duration"] for r in lost]),
                 "median": median([r["duration"] for r in lost])},
    }
    stats["composition"]["calls_per_client"] = {
        "WON": round(len(won) / max(1, len({r["client"] for r in won})), 2),
        "LOST": round(len(lost) / max(1, len({r["client"] for r in lost})), 2),
    }

    def share(rs, k):
        vals = [r["f"][k] for r in rs]
        return (sum(1 for v in vals if v), len(vals))

    for k in BOOL_KEYS:
        w_hit, w_n = share(won, k)
        l_hit, l_n = share(lost, k)
        stats["metrics"][k] = {
            "kind": "share",
            "won": [w_hit, w_n, round(100 * w_hit / w_n, 1) if w_n else None],
            "lost": [l_hit, l_n, round(100 * l_hit / l_n, 1) if l_n else None],
            "gap_pp": (round(100 * w_hit / w_n - 100 * l_hit / l_n, 1)
                       if w_n and l_n else None),
            # контрпримеры: WON без признака, LOST с признаком
            "won_without": w_n - w_hit, "lost_with": l_hit,
        }
    for k in NUM_KEYS:
        stats["metrics"][k] = {
            "kind": "num",
            "won": {"mean": mean([r["f"][k] for r in won]),
                    "median": median([r["f"][k] for r in won])},
            "lost": {"mean": mean([r["f"][k] for r in lost]),
                     "median": median([r["f"][k] for r in lost])},
        }

    # проверка внутри однородных страт: держится ли направление разрыва
    for k in BOOL_KEYS:
        holds = broke = thin = 0
        detail = []
        for stkey in sorted({(r["src"], r["manager"]) for r in recs}):
            w = [r for r in won if (r["src"], r["manager"]) == stkey]
            l = [r for r in lost if (r["src"], r["manager"]) == stkey]
            if len(w) < 3 or len(l) < 3:
                thin += 1
                continue
            wp = 100 * sum(1 for r in w if r["f"][k]) / len(w)
            lp = 100 * sum(1 for r in l if r["f"][k]) / len(l)
            gap = stats["metrics"][k]["gap_pp"] or 0
            ok = (wp - lp) * (1 if gap >= 0 else -1) > 0
            holds += ok
            broke += (not ok)
            detail.append([f"{stkey[0]} / {stkey[1]}", len(w), len(l),
                           round(wp, 1), round(lp, 1)])
        stats["strata"][k] = {"holds": holds, "broke": broke,
                              "thin_skipped": thin, "detail": detail}

    # ---- банк цитат из WON, только те, что реально есть в транскрипте
    def verified(rs, path_fn):
        out = []
        for r in rs:
            hay = runner.norm(r["text"])
            for q, tag in path_fn(r["card"]):
                if not has(q):
                    continue
                if runner.norm(q) in hay:
                    out.append({"call_id": r["call_id"], "tag": tag,
                                "quote": q.strip(), "src": r["src"],
                                "manager": r["manager"]})
        return out

    def paths(card):
        op = card.get("opening") or {}
        di = card.get("discovery") or {}
        pr = card.get("presentation") or {}
        cl = card.get("closing") or {}
        yield (op.get("manager_first_line"), "открытие")
        for q in (di.get("questions_asked") or []):
            yield (q, "вопрос")
        yield (di.get("criteria_of_choice"), "критерий выбора")
        for v in (pr.get("value_props_used") or []):
            yield (v, "выгода")
        for p in (pr.get("proof_used") or []):
            yield (p, "доказательство")
        yield (cl.get("next_step_proposed"), "следующий шаг")
        yield (cl.get("client_verbal_commitment"), "согласие клиента")
        for n in (card.get("notable_moments") or []):
            yield (n.get("quote"), "переломный момент")

    quotes = {"WON": verified(won, paths), "LOST": verified(lost, paths)}

    # ---- возражения
    obj = {"WON": defaultdict(list), "LOST": defaultdict(list)}
    for r in recs:
        for o in (r["card"].get("objections") or []):
            t = (o.get("type") or "другое").strip()
            hay = runner.norm(r["text"])
            oq = o.get("objection_quote") or ""
            mq = o.get("manager_response_quote") or ""
            obj[r["grp"]][t].append({
                "call_id": r["call_id"],
                "objection": oq.strip(),
                "objection_verified": runner.norm(oq) in hay,
                "response": mq.strip(),
                "response_verified": runner.norm(mq) in hay,
                "technique": o.get("technique"),
                "resolved": o.get("resolved"),
            })
    objections = {g: {t: v for t, v in sorted(d.items(),
                                              key=lambda x: -len(x[1]))}
                  for g, d in obj.items()}

    os.makedirs(OUT, exist_ok=True)
    json.dump(stats, open(f"{OUT}/stats_{scope}.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(quotes, open(f"{OUT}/quotes_{scope}.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump(objections, open(f"{OUT}/objections_{scope}.json", "w",
                               encoding="utf-8"), ensure_ascii=False, indent=1)
    print("записано в", OUT)
    print("цитат WON:", len(quotes["WON"]), "LOST:", len(quotes["LOST"]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "first")
