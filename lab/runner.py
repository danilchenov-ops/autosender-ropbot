# -*- coding: utf-8 -*-
"""Прогон промта 1 по выборке. Режимы: bench | run | stat.

Запуск:
    .venv/bin/python runner.py bench --n 6
    .venv/bin/python runner.py run --model gemini-3.7-flash --workers 6
    .venv/bin/python runner.py stat
"""
import argparse
import json
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import psycopg2.extras
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from anon import scrub, build_extra          # noqa: E402
from prompts import PROMPT1, PROMPT1_LEAN    # noqa: E402

BASE = "https://api.proxyapi.ru"


def env(name, default=None):
    """Читаем /opt/ropbot/.env, не вынося значения наружу."""
    try:
        for line in open("/opt/ropbot/.env", encoding="utf-8"):
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return os.getenv(name, default)


KEY = env("PROXYAPI_KEY") or ""
if not KEY:
    raise SystemExit("нет PROXYAPI_KEY")
import urllib.parse as _up
_PW = _up.quote(env("PG_PASSWORD") or "rop", safe="")
DSN = f"postgresql://rop:{_PW}@127.0.0.1:5432/rop"

_lock = threading.Lock()


def db():
    return psycopg2.connect(DSN)


# ---------------------------------------------------------------- подготовка

def manager_names(conn):
    with conn.cursor() as c:
        c.execute("SELECT full_name FROM managers")
        return [r[0] for r in c.fetchall()]


def build_lines(segments, extra):
    """Сегменты whisper -> пронумерованные обезличенные строки."""
    lines = []
    for s in segments or []:
        t = (s.get("text") or "").strip()
        if not t:
            continue
        lines.append(scrub(t, extra))
    return lines


def fetch_dialogs(conn, where, limit=None):
    sql = f"""
        SELECT s.call_id, s.grp, t.segments, t.text
        FROM lab_sample s
        JOIN transcripts t ON t.call_id = s.call_id
        WHERE {where}
        ORDER BY s.call_id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as c:
        c.execute(sql)
        return c.fetchall()


# ------------------------------------------------------------------- вызовы

def call_model(model, prompt, timeout=300, thinking=None):
    gc = {
        "temperature": 0,
        "responseMimeType": "application/json",
        "maxOutputTokens": 16000,
    }
    if thinking == "off":
        gc["thinkingConfig"] = {"thinkingBudget": 0}
    elif thinking in ("low", "high", "minimal"):
        gc["thinkingConfig"] = {"thinkingLevel": thinking}
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": gc}
    r = requests.post(
        f"{BASE}/google/v1beta/models/{model}:generateContent",
        headers={"Authorization": f"Bearer {KEY}"},
        json=body, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:400]}")
    d = r.json()
    usage = d.get("usageMetadata", {}) or {}
    tin = usage.get("promptTokenCount", 0)
    tout = (usage.get("candidatesTokenCount", 0) or 0) + \
           (usage.get("thoughtsTokenCount", 0) or 0)
    globals()["LAST_THOUGHTS"] = usage.get("thoughtsTokenCount", 0) or 0
    cand = (d.get("candidates") or [{}])[0]
    parts = ((cand.get("content") or {}).get("parts") or [])
    txt = "".join(p.get("text", "") for p in parts)
    if not txt:
        raise RuntimeError(f"пустой ответ: {json.dumps(d)[:400]}")
    return txt, tin, tout


def parse_json(txt):
    txt = txt.strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-z]*\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt)
    return json.loads(txt)


# ------------------------------------------------------- проверка цитатности

NORM = re.compile(r"[^а-яёa-z0-9]+")


def norm(s):
    return NORM.sub(" ", (s or "").lower().replace("ё", "е")).strip()


def collect_quotes(card):
    q = []
    g = card.get
    for st in (g("structure") or []):
        q.append(st.get("start_quote"))
    op = g("opening") or {}
    q.append(op.get("manager_first_line"))
    di = g("discovery") or {}
    q += list(di.get("questions_asked") or [])
    q += [di.get("pain_uncovered"), di.get("criteria_of_choice")]
    pr = g("presentation") or {}
    q += list(pr.get("value_props_used") or [])
    q += list(pr.get("proof_used") or [])
    for o in (g("objections") or []):
        q += [o.get("objection_quote"), o.get("manager_response_quote")]
    cl = g("closing") or {}
    q += [cl.get("next_step_proposed"), cl.get("client_verbal_commitment")]
    for n in (g("notable_moments") or []):
        q.append(n.get("quote"))
    out = []
    for x in q:
        if not x or not isinstance(x, str):
            continue
        if norm(x) in ("нет", "", "не называлась"):
            continue
        if len(norm(x)) < 8:
            continue
        out.append(x)
    return out


def quote_fidelity(card, full_text):
    hay = norm(full_text)
    qs = collect_quotes(card)
    hits = sum(1 for q in qs if norm(q) in hay)
    return hits, len(qs)


ROLE_MAP = {"M": "М", "М": "М", "K": "К", "К": "К", "C": "К", "С": "К"}


def norm_roles(roles, n_lines):
    """Модель отвечает то латиницей, то кириллицей. Приводим к М/К."""
    if not roles:
        return None
    r = "".join(ROLE_MAP.get(ch, "") for ch in roles)
    if not r:
        return None
    if len(r) < n_lines:                 # добиваем ролью последней строки
        r = r + r[-1] * (n_lines - len(r))
    return r[:n_lines]


def talk_share(roles, lines):
    """Доля слов менеджера — считаем сами, не спрашивая модель."""
    r = norm_roles(roles, len(lines))
    if not r:
        return None
    wm = wc = 0
    for ch, ln in zip(r, lines):
        n = len(ln.split())
        if ch == "М":
            wm += n
        else:
            wc += n
    tot = wm + wc
    return round(100.0 * wm / tot, 1) if tot else None


def make_prompt(call_id, lines, lean=False):
    numbered = "\n".join(f"{i+1}. {t}" for i, t in enumerate(lines))
    tpl = PROMPT1_LEAN if lean else PROMPT1
    return tpl.format(transcript=numbered, n_lines=len(lines),
                      dialog_id=str(call_id))


# ---------------------------------------------------------------------- bench

def cmd_bench(args):
    models = args.models.split(",")
    conn = db()
    extra = build_extra(manager_names(conn))
    rows = fetch_dialogs(
        conn,
        "s.excluded IS NULL AND s.split='train' AND s.duration BETWEEN 120 AND 900",
        limit=args.n * 3)
    # берём вперемешку WON и LOST
    won = [r for r in rows if r["grp"] == "WON"][: args.n // 2]
    lost = [r for r in rows if r["grp"] == "LOST"][: args.n - len(won)]
    picked = won + lost
    print(f"диалогов в бенче: {len(picked)}, модели: {models}")

    if not args.keep:
        with conn.cursor() as c:
            c.execute("DELETE FROM lab_bench")
            conn.commit()

    for model in models:
        for r in picked:
            lines = build_lines(r["segments"], extra)
            if len(lines) < 8:
                continue
            prompt = make_prompt(r["call_id"], lines, lean=args.lean)
            t0 = time.time()
            try:
                txt, tin, tout = call_model(model, prompt,
                                            thinking=args.thinking)
                card = parse_json(txt)
                roles = (card.get("roles") or "").replace(" ", "")
                hits, tot = quote_fidelity(card, "\n".join(lines))
                err = None
            except Exception as e:
                card, roles, hits, tot, tin, tout = None, None, 0, 0, 0, 0
                err = str(e)[:500]
            sec = round(time.time() - t0, 1)
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO lab_bench(call_id,model,card,roles,quote_hits,"
                    "quote_total,tokens_in,tokens_out,sec,err) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (r["call_id"], model,
                     json.dumps(card, ensure_ascii=False) if card else None,
                     roles, hits, tot, tin, tout, sec, err))
                conn.commit()
            print(f"  {model} #{r['call_id']} {sec}s "
                  f"цитаты {hits}/{tot} tok {tin}/{tout} {err or ''}")
    print("готово")


# ------------------------------------------------------------------------ run

def cmd_run(args):
    conn = db()
    extra = build_extra(manager_names(conn))
    extra_where = f" AND ({args.where})" if args.where else ""
    rows = fetch_dialogs(
        conn,
        "s.excluded IS NULL AND s.call_id NOT IN "
        "(SELECT call_id FROM lab_cards WHERE err IS NULL)" + extra_where,
        limit=args.limit)
    print(f"к обработке: {len(rows)} диалогов, модель {args.model}", flush=True)
    done = {"n": 0, "tin": 0, "tout": 0, "err": 0, "stopped": False}

    def spent():
        return (done["tin"] / 1000.0 * args.rub_in
                + done["tout"] / 1000.0 * args.rub_out)

    def work(r):
        if done["stopped"]:
            return
        lines = build_lines(r["segments"], extra)
        if len(lines) < 8:
            return
        prompt = make_prompt(r["call_id"], lines, lean=args.lean)
        card = roles = None
        tin = tout = 0
        err = None
        for attempt in range(4):
            try:
                txt, tin, tout = call_model(args.model, prompt,
                                            thinking=args.thinking)
                card = parse_json(txt)
                roles = (card.get("roles") or "").replace(" ", "")
                err = None
                break
            except Exception as e:
                err = str(e)[:500]
                if "429" in err or "503" in err or "500" in err:
                    time.sleep(8 * (attempt + 1))
                elif "HTTP 4" in err:
                    break
                else:
                    time.sleep(3)
        ts = talk_share(roles, lines) if roles else None
        roles = norm_roles(roles, len(lines)) if roles else None
        c2 = db()
        with c2.cursor() as c:
            c.execute(
                "INSERT INTO lab_cards(call_id,model,card,roles,lines_n,talk_share,"
                "tokens_in,tokens_out,err) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (call_id) DO UPDATE SET model=EXCLUDED.model,"
                "card=EXCLUDED.card,roles=EXCLUDED.roles,lines_n=EXCLUDED.lines_n,"
                "talk_share=EXCLUDED.talk_share,tokens_in=EXCLUDED.tokens_in,"
                "tokens_out=EXCLUDED.tokens_out,err=EXCLUDED.err,created_at=now()",
                (r["call_id"], args.model,
                 json.dumps(card, ensure_ascii=False) if card else None,
                 roles, len(lines), ts, tin, tout, err))
            c2.commit()
        c2.close()
        with _lock:
            done["n"] += 1
            done["tin"] += tin
            done["tout"] += tout
            if err:
                done["err"] += 1
            if done["n"] % 25 == 0:
                print(f"  {done['n']}/{len(rows)} ошибок {done['err']} "
                      f"tok {done['tin']}/{done['tout']} "
                      f"≈{spent():.0f} ₽", flush=True)
            if args.max_rub and spent() >= args.max_rub and not done["stopped"]:
                done["stopped"] = True
                print(f"СТОП: достигнут потолок {args.max_rub} ₽ "
                      f"на {done['n']} диалогах", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, rows))
    print(f"готово: {done}", flush=True)


def cmd_stat(args):
    conn = db()
    with conn.cursor() as c:
        c.execute("""SELECT s.grp, s.split, count(*) , count(*) FILTER (WHERE k.err IS NOT NULL),
                            sum(k.tokens_in), sum(k.tokens_out)
                     FROM lab_cards k JOIN lab_sample s USING(call_id)
                     GROUP BY 1,2 ORDER BY 1,2""")
        for r in c.fetchall():
            print(r)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bench")
    b.add_argument("--n", type=int, default=6)
    b.add_argument("--models", default="gemini-3.7-flash,gemini-3.1-pro-preview,gemini-3.5-flash")
    b.add_argument("--thinking", default=None)
    b.add_argument("--lean", action="store_true")
    b.add_argument("--keep", action="store_true")
    b.set_defaults(func=cmd_bench)
    r = sub.add_parser("run")
    r.add_argument("--model", default="gemini-3.7-flash")
    r.add_argument("--workers", type=int, default=6)
    r.add_argument("--thinking", default=None)
    r.add_argument("--lean", action="store_true")
    r.add_argument("--where", default=None)
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--max-rub", type=float, default=None,
                   help="потолок расхода, ₽; при достижении прогон встаёт")
    r.add_argument("--rub-in", type=float, default=0.0,
                   help="цена за 1000 входных токенов, ₽")
    r.add_argument("--rub-out", type=float, default=0.0,
                   help="цена за 1000 выходных токенов, ₽")
    r.set_defaults(func=cmd_run)
    s = sub.add_parser("stat")
    s.set_defaults(func=cmd_stat)
    a = p.parse_args()
    a.func(a)
