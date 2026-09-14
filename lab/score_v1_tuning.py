# -*- coding: utf-8 -*-
"""Эксперимент по скорингу v1 (офлайн, прод не трогает).

Сравнивает конфигурации обучения: популяция, целевая переменная, сглаживание.
Метрика — лифт top40%/bottom60% и доля предоплат в top40, 5-fold CV.
Ничего не пишет в базу.
"""
import math, os, re, sys, random
from collections import defaultdict
import psycopg

DSN = os.environ["PG_DSN"]
SHRINK = 0.75

RX_COLD = re.compile(r"просто хочу узнать цену|узнать цену|прицениваюсь|пока смотрю|просто интересуюсь", re.I)
RX_FAST = re.compile(r"в течение месяца|ближайшее время|месяц\s*-\s*два|2 месяца|срочно|как можно быстрее|^скоро|\sскоро", re.I)
RX_SPEC = re.compile(r"\d{4}|аукцион|лот\b|растамож|стоимост|№|комплектац|пробег", re.I)
RX_LOT = re.compile(r"/\d{5,}(?:[?#].*)?$")
RX_FILTER = re.compile(r"[?&](year_from|year_to|disp_|price|mileage|cursor=)")

FACTORS = ["campaign", "source", "form_srok", "free_text", "url",
           "office_hours", "phone_kind", "repeat_client"]
PAIR = ("campaign", "source")


SQL = """
SELECT l.id, l.status_semantic,
       coalesce(nullif(l.utm_campaign,''),'-') AS campaign,
       coalesce(nullif(l.source_id,''),'-')    AS source,
       coalesce(l.phone_kind,'none')           AS phone_kind,
       (EXTRACT(hour FROM l.date_create AT TIME ZONE 'Asia/Vladivostok') BETWEEN 10 AND 18) AS office_hours,
       coalesce(l.raw->>'COMMENTS','') AS comments,
       coalesce(l.form_text,'') AS form_text,
       l.page_url,
       EXISTS (SELECT 1 FROM leads p WHERE p.phone_e164=l.phone_e164 AND p.id<>l.id
               AND p.date_create < l.date_create
               AND p.date_create >= l.date_create - interval '365 days') AS repeat_client,
       EXISTS (SELECT 1 FROM stage_history sh WHERE sh.entity_kind='lead'
               AND sh.owner_id=l.id AND sh.stage_id IN ('11','12','13')) AS prepay,
       l.ym_client_id IS NOT NULL AS ym,
       l.status_id, l.date_create,
       coalesce(nullif(l.form_model,''),'-')   AS form_model,
       coalesce(nullif(l.ip_city,''),'-')      AS ip_city,
       coalesce(nullif(l.utm_medium,''),'-')   AS utm_medium,
       coalesce(nullif(l.utm_content,''),'-')  AS utm_content,
       coalesce(nullif(l.utm_source,''),'-')   AS utm_source,
       l.email IS NOT NULL AND l.email <> ''   AS has_email,
       EXTRACT(dow  FROM l.date_create AT TIME ZONE 'Asia/Vladivostok') AS dow,
       EXTRACT(hour FROM l.date_create AT TIME ZONE 'Asia/Vladivostok') AS hh,
       length(coalesce(l.form_text,'')) AS ftlen
FROM leads l
WHERE l.source_id IS DISTINCT FROM 'PARTNER' AND l.deleted_at IS NULL
  AND l.date_create >= '2026-05-01' AND l.date_create < '2026-09-08'
"""

BASE_FACTORS = ["campaign","source","form_srok","free_text","url",
                "office_hours","phone_kind","repeat_client"]
EXTRA_FACTORS = ["model","city","medium","content","utmsrc","email","dow","hour","ftlen"]
FACTORS = BASE_FACTORS
PAIR = ("campaign","source")

def feats(r):
    (_id,_st,campaign,source,phone_kind,office,comments,form_text,page_url,
     repeat_client,_pp,_ym,_sid,_dc,form_model,ip_city,um,uc,us,has_email,
     dow,hh,ftlen) = r
    f = {"campaign":campaign,"source":source,"phone_kind":phone_kind,
         "office_hours":"1" if office else "0",
         "repeat_client":"1" if repeat_client else "0"}
    ft = form_text or ""
    f["form_srok"] = ("none" if not ft.strip() else "cold" if RX_COLD.search(ft)
                      else "fast" if RX_FAST.search(ft) else "mid")
    cm = (comments or "").strip()
    f["free_text"] = ("none" if not cm else "specific" if RX_SPEC.search(cm)
                      else "long" if len(cm)>=40 else "short")
    u = page_url or ""
    f["url"] = ("lot" if RX_LOT.search(u) else "filtered" if RX_FILTER.search(u)
                else "page" if u else "none")
    f["model"]  = (form_model or "-").strip().lower()[:24]
    f["city"]   = (ip_city or "-").strip().lower()[:24]
    f["medium"] = um; f["content"] = uc[:24]; f["utmsrc"] = us
    f["email"]  = "1" if has_email else "0"
    f["dow"]    = "we" if int(dow) in (0,6) else "wd"
    h = int(hh)
    f["hour"]   = ("night" if h<10 else "day" if h<18 else "eve" if h<23 else "night")
    L = int(ftlen)
    f["ftlen"]  = ("0" if L==0 else "s" if L<30 else "m" if L<100 else "l")
    return f

def fit(rows, K, MIN_N):
    n_tot = len(rows)
    s_tot = sum(y for _f, y, _m in rows)
    p0 = s_tot / n_tot if n_tot else 0.0
    if s_tot == 0:
        return None
    stats = {fa: defaultdict(lambda: [0, 0]) for fa in FACTORS}
    for f, y, _m in rows:
        for fa in FACTORS:
            st = stats[fa][f[fa]]
            st[0] += 1; st[1] += y
    odds0 = p0 / (1 - p0)
    W = {}
    for fa in FACTORS:
        for v, (n, s) in stats[fa].items():
            if n >= MIN_N:
                p = (s + K * p0) / (n + K)
                W[(fa, v)] = math.log(p / (1 - p) / odds0)
    return W, math.log(odds0)

def score(f, W, base):
    acc = 0.0
    main, fb = PAIR
    for fa in FACTORS:
        if fa == fb and (main, f[main]) in W and f[main] != "-":
            continue
        if fa == main and ((main, f[main]) not in W or f[main] == "-"):
            continue
        acc += W.get((fa, f[fa]), 0.0)
    return 1 / (1 + math.exp(-(base + SHRINK * acc)))

def evaluate(scored, top_frac=0.40):
    """scored: [(p, y)] -> лифт top/bottom, конверсии, доля событий в топе"""
    if not scored: return None
    s = sorted(scored, key=lambda t: -t[0])
    k = max(1, int(len(s) * top_frac))
    top, bot = s[:k], s[k:]
    ty = sum(y for _p, y in top); by = sum(y for _p, y in bot)
    tcr = 100.0 * ty / len(top); bcr = 100.0 * by / len(bot) if bot else 0.0
    ka = max(1, int(len(s) * 0.12))
    acr = 100.0 * sum(y for _p, y in s[:ka]) / ka
    lift = tcr / bcr if bcr > 0 else float('inf')
    cap = 100.0 * ty / (ty + by) if (ty + by) else 0.0
    return dict(n=len(s), ev=ty + by, top_cr=tcr, bot_cr=bcr, lift=lift,
                capture=cap, a_cr=acr)


def main():
    global FACTORS
    with psycopg.connect(DSN) as conn:
        raw = conn.execute(SQL).fetchall()
    data = [(feats(r), r, dict(ym=r[11], source=r[3], phone_kind=r[4], status=r[12]))
            for r in raw]
    print(f"выгружено лидов: {len(raw)}\n")
    def pop_dist(m):
        return (m["source"] not in ("CALL","9025240049")
                and m["phone_kind"] in ("mobile","landline") and m["status"] not in ("31",))
    tgt_pre = lambda r: 1 if r[10] else 0

    variants = [
        ("база (8 признаков, как сейчас, K=250)",      BASE_FACTORS),
        ("база, K=75/N=30",                            BASE_FACTORS),
        ("+ час суток и день недели",                  BASE_FACTORS+["hour","dow"]),
        ("+ час/день + марка-модель",                  BASE_FACTORS+["hour","dow","model"]),
        ("+ час/день + марка + длина текста",          BASE_FACTORS+["hour","dow","model","ftlen"]),
    ]
    segs = (("ВСЕ раздаваемые",None),("БЕЗ Метрики",lambda m: not m["ym"]),
            ("С Метрикой",lambda m: m["ym"]))
    hdr = f"{'вариант признаков':<42}{'n':>7}{'соб':>5}{'top40':>8}{'bot60':>8}{'лифт':>7}{'в топе':>7}{'A/база':>8}"
    for seg_name, seg in segs:
        print(f"### сегмент: {seg_name}"); print(hdr); print("-"*len(hdr))
        for vname, facs in variants:
            FACTORS = facs
            rnd = random.Random(7); idx=list(range(len(data))); rnd.shuffle(idx)
            out=[]
            for k in range(5):
                test_i=set(idx[k::5])
                tr=[(f,tgt_pre(r),m) for i,(f,r,m) in enumerate(data)
                    if i not in test_i and pop_dist(m)]
                mm=fit(tr,250,60) if vname.endswith("K=250)") else fit(tr,75,30)
                if not mm: continue
                W,base=mm
                for i in test_i:
                    f,r,m=data[i]
                    if not pop_dist(m): continue
                    if seg is not None and not seg(m): continue
                    out.append((score(f,W,base),tgt_pre(r)))
            res=evaluate(out)
            b=100.0*res['ev']/res['n']
            print(f"{vname:<42}{res['n']:>7}{res['ev']:>5}{res['top_cr']:>7.2f}%"
                  f"{res['bot_cr']:>7.2f}%{res['lift']:>7.2f}{res['capture']:>6.0f}%{res['a_cr']/b:>8.2f}")
        print()
main()
