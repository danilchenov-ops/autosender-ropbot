# -*- coding: utf-8 -*-
"""Офлайн-подбор для боевой модели v2b: цель и сглаживание. Прод не трогает."""
import math, os, random
from collections import defaultdict
import psycopg

DSN = os.environ["PG_DSN"]; SHRINK = 0.75
TRUST_RX = r"^/(favourites|about|reviews|how-buy|contacts|delivery|autosender-otzyvy)"
FACTORS = ["b_visits","b_days","b_pages","b_time","b_lot","b_trust","b_source","b_device","b_campaign"]
SQL = f"""
SELECT l.id, l.status_semantic, v.visits_before, v.days_since_first, v.pageviews,
       v.seconds, v.distinct_landings, v.saw_lot, v.last_source, v.device,
       coalesce(nullif(v.last_campaign,''),'-') AS campaign,
       coalesce((SELECT bool_or((e->>'path') ~ '{TRUST_RX}') FROM jsonb_array_elements(v.raw) e), false) AS trust,
       EXISTS (SELECT 1 FROM stage_history sh WHERE sh.entity_kind='lead'
               AND sh.owner_id=l.id AND sh.stage_id IN ('11','12','13')) AS prepay
FROM leads l JOIN lead_visits v ON v.lead_id=l.id AND v.visits_before>=1
WHERE l.source_id IS DISTINCT FROM 'PARTNER' AND l.phone_kind IN ('mobile','landline')
  AND l.deleted_at IS NULL AND l.date_create >= %s AND l.date_create < '2026-09-08'
"""
SRC_MAP = {"ad":"ad","search":"search","direct":"direct","link":"link","social":"social",
           "internal":"internal","recommend":"recommend"}
def feats(r):
    (_i,_s,visits,days,pv,secs,land,lot,src,dev,camp,trust,_p) = r
    secs = secs or 0; pv = pv or 0; visits = visits or 0; days = days or 0
    return {"b_visits":"v1" if visits<=1 else "v2_4" if visits<=4 else "v5p",
            "b_days":"d0" if days<=0 else "d1_7" if days<=7 else "d8p",
            "b_pages":"p1_2" if pv<=2 else "p3_10" if pv<=10 else "p11p",
            "b_time":"t_lt60" if secs<60 else "t1_10m" if secs<600 else "t10mp",
            "b_lot":"1" if lot else "0","b_trust":"1" if trust else "0",
            "b_source":SRC_MAP.get(src,"other"),"b_device":dev or "-","b_campaign":camp}
def fit(rows,K,MIN_N):
    n=len(rows); s=sum(y for _f,y in rows)
    if not s: return None
    p0=s/n; odds0=p0/(1-p0)
    st={fa:defaultdict(lambda:[0,0]) for fa in FACTORS}
    for f,y in rows:
        for fa in FACTORS:
            c=st[fa][f[fa]]; c[0]+=1; c[1]+=y
    W={}
    for fa in FACTORS:
        for v,(nn,ss) in st[fa].items():
            if nn>=MIN_N:
                p=(ss+K*p0)/(nn+K); W[(fa,v)]=math.log(p/(1-p)/odds0)
    return W, math.log(odds0)
def score(f,W,b):
    return 1/(1+math.exp(-(b+SHRINK*sum(W.get((fa,f[fa]),0.0) for fa in FACTORS))))
def ev(sc,tf=0.40):
    s=sorted(sc,key=lambda t:-t[0]); k=max(1,int(len(s)*tf))
    ty=sum(y for _p,y in s[:k]); by=sum(y for _p,y in s[k:])
    t=100.0*ty/k; bo=100.0*by/max(1,len(s)-k)
    ka=max(1,int(len(s)*0.12)); a=100.0*sum(y for _p,y in s[:ka])/ka
    base=100.0*(ty+by)/len(s)
    return len(s),ty+by,t,bo,(t/bo if bo else 0),100.0*ty/max(1,ty+by),a/base
def run(since,tgt,K,N):
    with psycopg.connect(DSN) as c: raw=c.execute(SQL,(since,)).fetchall()
    data=[(feats(r), 1 if (r[1]=="S" if tgt=="sale" else r[12]) else 0, r) for r in raw]
    # оценка ВСЕГДА по предоплате, чтобы строки были сравнимы
    rnd=random.Random(7); idx=list(range(len(data))); rnd.shuffle(idx); out=[]
    for k in range(5):
        te=set(idx[k::5])
        tr=[(f,y) for i,(f,y,_r) in enumerate(data) if i not in te]
        m=fit(tr,K,N)
        if not m: continue
        W,b=m
        for i in te:
            f,_y,r=data[i]; out.append((score(f,W,b), 1 if r[12] else 0))
    return ev(out)
print(f"{'конфигурация v2b':<48}{'n':>7}{'соб':>5}{'top40':>8}{'bot60':>8}{'лифт':>7}{'в топе':>7}{'A/база':>8}")
print("-"*98)
for name,since,tgt,K,N in [
    ("длинное окно, цель продажа, K=250/N=60 (как сейчас)","2025-07-01","sale",250,60),
    ("длинное окно, цель продажа, K=75/N=30","2025-07-01","sale",75,30),
    ("длинное окно, цель продажа, K=40/N=25","2025-07-01","sale",40,25),
    ("цель продажа, K=250/N=60, окно с 2026-05","2026-05-01","sale",250,60),
    ("цель ПРЕДОПЛАТА, K=250/N=60, окно с 2026-05","2026-05-01","pre",250,60),
    ("цель ПРЕДОПЛАТА, K=75/N=30,  окно с 2026-05","2026-05-01","pre",75,30),
]:
    n,e,t,b,l,cap,ab=run(since,tgt,K,N)
    print(f"{name:<48}{n:>7}{e:>5}{t:>7.2f}%{b:>7.2f}%{l:>7.2f}{cap:>6.0f}%{ab:>8.2f}")
