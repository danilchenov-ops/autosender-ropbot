"""Скоринг заявок: шанс дойти до продажи, оценивается ДО первого звонка.

Две модели, считаются параллельно (для честного сравнения 2 сентября):

  v2b — БОЕВАЯ. Только поведение из Метрики по ClientID (lead_visits): визиты,
        давность, глубина, время, лоты, источник визита, кампания Директа,
        устройство. Никаких сигналов из CRM — по решению Тимофея 19.08.
  v1  — теневая. Признаки карточки CRM (кампания, текст формы, время, повторность).
        В дайджест не идёт, копится для сравнения моделей.

Веса — сглаженные лог-отношения шансов (наивный Байес). Факт продажи для обучения —
status_semantic='S' (за ним деньги); менеджерские статусы не используются нигде.
Первая оценка лида каждой моделью замораживается в lead_scores.

Запуск:
    python scorer.py train --model all     — пересчёт весов
    python scorer.py score --model all     — оценка свежих (каждые 5 мин)
    python scorer.py score --model v2b --days 30
"""
import json
import math
import re
import sys

from common import db, log
from migrate import migrate

SHRINK = 0.75
K = 250
MIN_N = 60
TRAIN_FROM_DAYS = 440
TRAIN_TO_DAYS = 45
GRADE_A, GRADE_B, GRADE_C = 88, 60, 20

# ── Модель v2b: поведение из Метрики ─────────────────────────────────────────

V2_SQL = """
SELECT l.id, l.status_semantic,
       v.visits_before, v.days_since_first, v.pageviews, v.seconds,
       v.distinct_landings, v.saw_lot, v.last_source, v.device,
       coalesce(nullif(v.last_campaign,''),'-') AS campaign,
       coalesce((SELECT bool_or((e->>'path') ~ %(trust_rx)s)
                 FROM jsonb_array_elements(v.raw) e), false) AS trust_pages
FROM leads l
JOIN lead_visits v ON v.lead_id = l.id AND v.visits_before >= 1
WHERE l.source_id IS DISTINCT FROM 'PARTNER'
  AND l.phone_kind IN ('mobile','landline')
"""

# «Страницы доверия»: человек проверяет компанию, а не листает каталог.
# 20.08.2026 на истории: 3,54% против 1,16%, эффект держится внутри групп по глубине.
TRUST_RX = r"^/(favourites|about|reviews|how-buy|contacts|delivery|autosender-otzyvy)"

# регулярка подставляется литералом: в запросах уже есть позиционные %s для дат,
# смешивать их с именованными параметрами psycopg не даёт
V2_SQL = V2_SQL.replace("%(trust_rx)s", "'" + TRUST_RX + "'")

SRC_MAP = {"ad": "ad", "search": "search", "direct": "direct", "link": "link",
           "social": "social", "internal": "internal", "recommend": "recommend"}


def norm_source(s):
    s = (s or "").lower()
    for k in SRC_MAP:
        if k in s:
            return SRC_MAP[k]
    return "other"


def features_v2(row):
    (_id, _st, visits, days, pages, secs, _landings, saw_lot,
     src, device, campaign, trust) = row
    visits, days, pages, secs = visits or 0, days or 0, pages or 0, secs or 0
    return {
        "b_visits": "v1" if visits <= 1 else "v2_3" if visits <= 3 else "v4p",
        "b_days": "d0" if days == 0 else "d1_7" if days <= 7 else "d8p",
        "b_pages": "p1_2" if pages <= 2 else "p3_10" if pages <= 10 else "p11_30" if pages <= 30 else "p31p",
        "b_time": "t_lt60" if secs < 60 else "t1_10m" if secs < 600 else "t10mp",
        "b_lot": "1" if saw_lot else "0",
        "b_device": device if device in ("PC", "Smartphones") else "other",
        "b_source": norm_source(src),
        "b_campaign": campaign,
        "b_trust": "1" if trust else "0",
    }


V2_FACTORS = ["b_visits", "b_days", "b_pages", "b_time", "b_lot",
              "b_device", "b_source", "b_campaign", "b_trust"]

REASONS_V2 = {
    ("b_visits", "v4p"): "4+ визитов до заявки",
    ("b_visits", "v2_3"): "возвращался на сайт",
    ("b_visits", "v1"): "первый визит",
    ("b_days", "d8p"): "изучает сайт больше недели",
    ("b_days", "d1_7"): "вернулся в течение недели",
    ("b_pages", "p31p"): "смотрел 30+ страниц",
    ("b_pages", "p11_30"): "глубокий просмотр каталога",
    ("b_pages", "p1_2"): "1–2 страницы",
    ("b_time", "t10mp"): "на сайте больше 10 минут",
    ("b_time", "t_lt60"): "меньше минуты на сайте",
    ("b_lot", "1"): "доходил до конкретных лотов",
    ("b_source", "ad"): "пришёл с рекламы",
    ("b_source", "search"): "пришёл из поиска",
    ("b_source", "direct"): "зашёл напрямую",
    ("b_trust", "1"): "смотрел отзывы / о компании / условия",
}

# ── Модель v1: карточка CRM (теневая) ────────────────────────────────────────

RX_COLD = re.compile(r"просто хочу узнать цену|узнать цену|прицениваюсь|пока смотрю|просто интересуюсь", re.I)
RX_FAST = re.compile(r"в течение месяца|ближайшее время|месяц\s*-\s*два|2 месяца|срочно|как можно быстрее|^скоро|\sскоро", re.I)
RX_SPEC = re.compile(r"\d{4}|аукцион|лот\b|растамож|стоимост|№|комплектац|пробег", re.I)
RX_LOT = re.compile(r"/\d{5,}(?:[?#].*)?$")
RX_FILTER = re.compile(r"[?&](year_from|year_to|disp_|price|mileage|cursor=)")

V1_SQL = """
SELECT l.id, l.status_semantic,
       coalesce(nullif(l.utm_campaign,''),'-')      AS campaign,
       coalesce(nullif(l.source_id,''),'-')         AS source,
       coalesce(l.phone_kind,'none')                AS phone_kind,
       (EXTRACT(hour FROM l.date_create AT TIME ZONE 'Asia/Vladivostok') BETWEEN 10 AND 18) AS office_hours,
       coalesce(l.raw->>'COMMENTS','')              AS comments,
       coalesce(l.form_text,'')                     AS form_text,
       l.page_url,
       EXISTS (SELECT 1 FROM leads p
               WHERE p.phone_e164 = l.phone_e164 AND p.id <> l.id
                 AND p.date_create < l.date_create
                 AND p.date_create >= l.date_create - interval '365 days') AS repeat_client,
       EXISTS (SELECT 1 FROM stage_history sh WHERE sh.entity_kind='lead'
               AND sh.owner_id = l.id AND sh.stage_id IN ('11','12','13')) AS prepay,
       EXTRACT(hour FROM l.date_create AT TIME ZONE 'Asia/Vladivostok') AS hh,
       EXTRACT(dow  FROM l.date_create AT TIME ZONE 'Asia/Vladivostok') AS dw
FROM leads l
WHERE l.source_id IS DISTINCT FROM 'PARTNER'
"""


def features_v1(row):
    (_id, _st, campaign, source, phone_kind, office, comments, form_text,
     page_url, repeat_client, _prepay, hh, dw) = row
    f = {"campaign": campaign, "source": source, "phone_kind": phone_kind,
         "office_hours": "1" if office else "0",
         "repeat_client": "1" if repeat_client else "0"}
    ft = form_text or ""
    f["form_srok"] = ("none" if not ft.strip() else
                      "cold" if RX_COLD.search(ft) else
                      "fast" if RX_FAST.search(ft) else "mid")
    cm = (comments or "").strip()
    f["free_text"] = ("none" if not cm else
                      "specific" if RX_SPEC.search(cm) else
                      "long" if len(cm) >= 40 else "short")
    u = page_url or ""
    f["url"] = ("lot" if RX_LOT.search(u) else
                "filtered" if RX_FILTER.search(u) else
                "page" if u else "none")
    h = int(hh)
    f["hour"] = "night" if h < 10 or h >= 23 else "day" if h < 18 else "eve"
    f["dow"] = "we" if int(dw) in (0, 6) else "wd"
    return f


V1_FACTORS = ["campaign", "source", "form_srok", "free_text", "url",
              "office_hours", "phone_kind", "repeat_client", "hour", "dow"]

# target: "sale" — status_semantic='S'; "prepay" — переход в 11/12/13 (stage_history).
# Переходы в 11/12/13 есть только с апреля 2026, поэтому у цели prepay окно короче.
# Параметры подобраны 15.09.2026, 5-fold CV — см. changelog реестра.
MODELS = {
    "v2b": {"sql": V2_SQL, "features": features_v2, "factors": V2_FACTORS,
            "pair": None, "target": "sale", "k": 75, "min_n": 30,
            "from_days": 440, "to_days": 45},
    "v1": {"sql": V1_SQL, "features": features_v1, "factors": V1_FACTORS,
           "pair": ("campaign", "source"),  # источник добавляется, только если нет веса кампании
           "target": "prepay", "k": 75, "min_n": 30,
           "from_days": 140, "to_days": 14},
}


def label(model, row):
    """1, если заявка дошла до целевого события модели."""
    if MODELS[model]["target"] == "prepay":
        return 1 if row[10] else 0
    return 1 if row[1] == "S" else 0

# ── Общий механизм ───────────────────────────────────────────────────────────

def score_p(f, weights, base_logit, factors, pair):
    acc = 0.0
    for fa in factors:
        if pair:
            main, fallback = pair
            if fa == fallback and (main, f[main]) in weights and f[main] != "-":
                continue
            if fa == main and ((main, f[main]) not in weights or f[main] == "-"):
                continue
        w = weights.get((fa, f[fa]))
        if w:
            acc += w[0] if isinstance(w, tuple) else w
    return 1 / (1 + math.exp(-(base_logit + SHRINK * acc)))


def train(model):
    cfg_m = MODELS[model]
    with db() as conn:
        K, MIN_N = cfg_m["k"], cfg_m["min_n"]
        rows = conn.execute(
            cfg_m["sql"] + """
              AND l.date_create >= now() - (%s || ' days')::interval
              AND l.date_create <  now() - (%s || ' days')::interval""",
            (cfg_m["from_days"], cfg_m["to_days"])).fetchall()
        if len(rows) < 500:
            log.error("%s: слишком мало лидов для обучения (%s)", model, len(rows))
            return
        n_tot = len(rows)
        s_tot = sum(label(model, r) for r in rows)
        p0 = s_tot / n_tot
        log.info("%s: обучение на %s лидах, %s событий (%s), база %.2f%%",
                 model, n_tot, s_tot, cfg_m["target"], 100 * p0)

        stats = {fa: {} for fa in cfg_m["factors"]}
        cache = []
        for r in rows:
            f = cfg_m["features"](r)
            cache.append(f)
            for fa in cfg_m["factors"]:
                n, s = stats[fa].get(f[fa], (0, 0))
                stats[fa][f[fa]] = (n + 1, s + label(model, r))

        odds0 = p0 / (1 - p0)
        weights = {}
        for fa in cfg_m["factors"]:
            for v, (n, s) in stats[fa].items():
                if n >= MIN_N:
                    p = (s + K * p0) / (n + K)
                    weights[(fa, v)] = (math.log(p / (1 - p) / odds0), n, s)

        base_logit = math.log(odds0)
        ps = sorted(score_p(f, weights, base_logit, cfg_m["factors"], cfg_m["pair"]) for f in cache)
        quantiles = [ps[min(len(ps) - 1, int(i / 100 * len(ps)))] for i in range(101)]

        conn.execute("DELETE FROM score_weights WHERE model_version = %s", (model,))
        for (fa, v), (w, n, s) in weights.items():
            conn.execute("INSERT INTO score_weights (model_version, factor, value, weight, n, sales) "
                         "VALUES (%s,%s,%s,%s,%s,%s)", (model, fa, v, round(w, 4), n, s))
        conn.execute("INSERT INTO score_weights (model_version, factor, value, weight, n, sales) "
                     "VALUES (%s,'_meta','p0',%s,%s,%s)", (model, p0, n_tot, s_tot))
        for i, q in enumerate(quantiles):
            conn.execute("INSERT INTO score_weights (model_version, factor, value, weight) "
                         "VALUES (%s,'_calib',%s,%s)", (model, f"q{i}", q))
        log.info("%s: сохранено %s весов", model, len(weights))


def load_weights(conn, model):
    weights, calib, p0 = {}, {}, None
    for fa, v, w in conn.execute(
            "SELECT factor, value, weight FROM score_weights WHERE model_version=%s", (model,)):
        if fa == "_meta" and v == "p0":
            p0 = float(w)
        elif fa == "_calib":
            calib[int(v[1:])] = float(w)
        else:
            weights[(fa, v)] = float(w)
    if p0 is None or len(calib) != 101:
        return None
    return weights, [calib[i] for i in range(101)], p0


def reason_text_v1(fa, v, w):
    m = {("form_srok", "fast"): "срок — ближайший месяц", ("form_srok", "cold"): "«просто узнать цену»",
         ("free_text", "specific"): "конкретика в запросе", ("repeat_client", "1"): "повторное обращение",
         ("office_hours", "0"): "пришла ночью", ("office_hours", "1"): "рабочие часы",
         ("phone_kind", "landline"): "стационарный номер", ("url", "lot"): "страница лота",
         ("url", "filtered"): "подбор по фильтрам",
         ("hour", "night"): "пришла ночью", ("hour", "eve"): "пришла вечером",
         ("dow", "we"): "выходной день"}
    if (fa, v) in m:
        return m[(fa, v)]
    if fa in ("campaign", "source"):
        return f"{'кампания' if fa == 'campaign' else 'источник'} {v} ({'сильная' if w > 0 else 'слабая'})"
    return None


def reasons_for(model, f, weights, factors, pair):
    contrib = []
    for fa in factors:
        if pair and fa == pair[1] and (pair[0], f[pair[0]]) in weights and f[pair[0]] != "-":
            continue
        w = weights.get((fa, f[fa]), 0.0)
        w = w[0] if isinstance(w, tuple) else w
        if abs(w) > 0.08:
            contrib.append((abs(w), fa, f[fa], w))
    contrib.sort(reverse=True)
    out = []
    for _, fa, v, w in contrib[:4]:
        t = REASONS_V2.get((fa, v)) if model == "v2b" else reason_text_v1(fa, v, w)
        if not t and model == "v2b" and fa == "b_campaign":
            t = f"кампания {v} ({'сильная' if w > 0 else 'слабая'})"
        if t:
            out.append(("+ " if w > 0 else "− ") + t)
    return out[:3]


def score(model, days=14):
    cfg_m = MODELS[model]
    with db() as conn:
        lw = load_weights(conn, model)
        if not lw:
            log.warning("%s: весов нет — сначала train", model)
            return
        weights, quantiles, p0 = lw
        base_logit = math.log(p0 / (1 - p0))
        rows = conn.execute(
            cfg_m["sql"] + """
              AND l.date_create >= now() - (%s || ' days')::interval
              AND NOT EXISTS (SELECT 1 FROM lead_scores s
                              WHERE s.lead_id = l.id AND s.model_version = %s)
            ORDER BY l.date_create""", (days, model)).fetchall()
        done = 0
        for r in rows:
            f = cfg_m["features"](r)
            p = score_p(f, weights, base_logit, cfg_m["factors"], cfg_m["pair"])
            pct = max(0, min(100, sum(1 for q in quantiles if q <= p) - 1))
            grade = "A" if pct >= GRADE_A else "B" if pct >= GRADE_B else "C" if pct >= GRADE_C else "D"
            conn.execute(
                """INSERT INTO lead_scores (lead_id, model_version, score, grade, p_est, reasons, features)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (lead_id, model_version) DO NOTHING""",
                (r[0], model, pct, grade, round(p, 5),
                 json.dumps(reasons_for(model, f, weights, cfg_m["factors"], cfg_m["pair"]),
                            ensure_ascii=False),
                 json.dumps(f, ensure_ascii=False)))
            done += 1
        if done:
            log.info("%s: оценено лидов %s", model, done)


def classes(model):
    """Фактическая конверсия по классам на обучающем окне (внутривыборочная оценка)."""
    cfg_m = MODELS[model]
    with db() as conn:
        lw = load_weights(conn, model)
        if not lw:
            print(f"{model}: весов нет")
            return
        weights, quantiles, p0 = lw
        base_logit = math.log(p0 / (1 - p0))
        rows = conn.execute(
            cfg_m["sql"] + """
              AND l.date_create >= now() - (%s || ' days')::interval
              AND l.date_create <  now() - (%s || ' days')::interval""",
            (cfg_m["from_days"], cfg_m["to_days"])).fetchall()
        agg = {}
        for r in rows:
            f = cfg_m["features"](r)
            p = score_p(f, weights, base_logit, cfg_m["factors"], cfg_m["pair"])
            pct = max(0, min(100, sum(1 for q in quantiles if q <= p) - 1))
            g = "A" if pct >= GRADE_A else "B" if pct >= GRADE_B else "C" if pct >= GRADE_C else "D"
            n, sl = agg.get(g, (0, 0))
            agg[g] = (n + 1, sl + label(model, r))
        print(f"{model} (окно {cfg_m['from_days']}-{cfg_m['to_days']} дн., "
              f"цель {cfg_m['target']}, база {100*p0:.2f}%)")
        for g in "ABCD":
            n, sl = agg.get(g, (0, 0))
            if n:
                print(f"  {g}: лидов {n}, событий {sl}, конверсия {100.0*sl/n:.2f}%")


if __name__ == "__main__":
    migrate()
    args = sys.argv[1:]
    cmd = args[0] if args else "score"
    model = args[args.index("--model") + 1] if "--model" in args else "all"
    days = int(args[args.index("--days") + 1]) if "--days" in args else 14
    targets = ["v2b", "v1"] if model == "all" else [model]
    for m in targets:
        if cmd == "train":
            train(m)
        elif cmd == "score":
            score(m, days)
        elif cmd == "classes":
            classes(m)
