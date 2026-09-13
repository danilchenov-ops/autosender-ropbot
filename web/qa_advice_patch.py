# -*- coding: utf-8 -*-
"""Патч web/dash.py: детальный недельный план вместо короткого вывода."""
import io

P = "/opt/ropbot/web/dash.py"
src = io.open(P, encoding="utf-8").read()

# 1. CSS -----------------------------------------------------------------
old = """.foot{font-size:13px;color:var(--ink2);line-height:1.6}"""
new = """/* недельный план работы */
.adv{border-top:1px solid var(--line);padding:16px 0 4px}
.adv:first-of-type{border-top:0;padding-top:2px}
.adv-h{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px}
.adv-h .nm{font-weight:650;font-size:15.5px}
.adv-h .meta{font-size:12.5px;color:var(--ink3)}
.adv-h .thin{font-size:11.5px;color:var(--serious);font-weight:600}
.adv-talk{background:var(--bg);border-radius:10px;padding:10px 12px;
font-size:13.5px;line-height:1.55;margin:0 0 10px}
.adv-talk b{color:var(--ink)}
.adv-sec{font-size:11.5px;text-transform:uppercase;letter-spacing:.03em;
color:var(--ink3);margin:10px 0 4px;font-weight:600}
.adv-list{margin:0;padding-left:0;list-style:none;font-size:13.5px}
.adv-list li{line-height:1.5;margin-bottom:7px;padding-left:26px;position:relative}
.adv-list li .n{position:absolute;left:0;top:1px;width:19px;height:19px;
border-radius:50%;background:var(--blue);color:#fff;font-size:11.5px;
font-weight:700;text-align:center;line-height:19px}
.adv-list li.good .n{background:var(--good)}
.adv-list li .code{font-weight:700}
.adv-obj{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
gap:8px;font-size:13px}
.adv-o{background:var(--bg);border-radius:9px;padding:9px 11px;line-height:1.45}
.adv-o .top{display:flex;justify-content:space-between;gap:8px;
align-items:baseline;margin-bottom:3px}
.adv-o .lbl{font-weight:650}
.adv-o .pc{font-variant-numeric:tabular-nums;font-weight:700}
.adv-o .pc.bad{color:var(--bad)}.adv-o .pc.mid{color:var(--warn)}
.adv-o .pc.ok{color:var(--good)}
.adv-o .tip{color:var(--ink2);font-size:12.5px}
.adv-bad{font-size:12.5px;color:var(--ink2);margin:6px 0 0}
.adv-bad b{color:var(--ink)}
.adv-when{font-size:12.5px;color:var(--ink3);margin:0 0 12px}
.foot{font-size:13px;color:var(--ink2);line-height:1.6}"""
assert old in src, "css anchor"
src = src.replace(old, new, 1)

# 2. чтение qa_advice ----------------------------------------------------
old = "def qa_conclusion(ranked, thin, names, dept, edge_metrics):"
new = '''def qa_advice_rows(conn):
    """Последний недельный срез плана: (понедельник среза, {uid: payload})."""
    if "qa_advice" in _CACHE:
        return _CACHE["qa_advice"]
    week = conn.execute("SELECT max(week_start) FROM qa_advice").fetchone()[0]
    rows = {}
    if week:
        for uid, payload, n in conn.execute(
                """SELECT portal_user_id, payload, n_calls FROM qa_advice
                    WHERE week_start = %s""", (week,)):
            rows[uid] = payload
    _CACHE["qa_advice"] = (week, rows)
    return _CACHE["qa_advice"]


def qa_pc_class(v):
    return "bad" if v < 55 else ("mid" if v < 75 else "ok")


def qa_plan_block(conn, order, names):
    """Детальный план работы по каждому менеджеру. Пересчитывается раз
    в неделю (app/qa_advice.py, крон в ночь на понедельник) — рекомендация
    не должна меняться каждый день."""
    week, rows = qa_advice_rows(conn)
    if not week or not rows:
        return ""
    nxt = week + dt.timedelta(days=7)
    cards = []
    for uid in order:
        p = rows.get(uid)
        if not p:
            continue
        thin = ('<span class="thin">мало данных, читать как предварительное</span>'
                if p.get("thin") else "")
        head = (f'<div class="adv-h"><span class="nm">{e(p.get("name", ""))}'
                f'</span><span class="meta">{p.get("n", 0)} '
                f'{plural(p.get("n", 0), "звонок", "звонка", "звонков")} '
                f'с оценкой · интеграл {num(p.get("integral"), 2)} · '
                f'следующий шаг {num(p.get("conv"), 0)}%</span>{thin}</div>')

        talk = (f'<p class="adv-talk">{qa_md(p["talk_text"])}</p>'
                if p.get("talk_text") else "")

        gaps = p.get("gaps") or []
        gaps_html = ""
        if gaps:
            items = "".join(
                f'<li><span class="n">{i + 1}</span>'
                f'<span class="code">{e(g["code"])} {e(g["title"])}.</span> '
                f'{qa_md(g.get("text", ""))}</li>'
                for i, g in enumerate(gaps))
            gaps_html = (f'<div class="adv-sec">Над чем работать</div>'
                         f'<ul class="adv-list">{items}</ul>')

        cats = p.get("obj_cats") or []
        obj_html = ""
        if cats:
            cells = "".join(
                f'<div class="adv-o"><div class="top">'
                f'<span class="lbl">{e(c["label"])}</span>'
                f'<span class="pc {qa_pc_class(c["handled"])}">'
                f'{c["handled"]}%</span></div>'
                f'<div class="tip">{e(c["tip"])}</div>'
                f'<div class="tip" style="margin-top:3px">прозвучало '
                f'{c["n"]} {plural(c["n"], "раз", "раза", "раз")} '
                f'за 30 дней</div></div>'
                for c in cats)
            bad = p.get("obj_bad") or []
            bad_txt = ""
            if bad:
                bad_txt = ('<p class="adv-bad">Чаще всего остаётся без ответа: '
                           + ", ".join(f'<b>«{e(b["text"])}»</b> ({b["n"]})'
                                       for b in bad) + '.</p>')
            obj_html = (f'<div class="adv-sec">Возражения: доля разговоров, '
                        f'где отработал</div><div class="adv-obj">{cells}</div>'
                        f'{bad_txt}')

        strong = p.get("strong") or []
        strong_html = ""
        if strong:
            items = "".join(
                f'<li class="good"><span class="n">✓</span>'
                f'<span class="code">{e(s["code"])} {e(s["title"])}</span> — '
                f'{num(s["v"], 1)} против {num(s["dept"], 1)} по отделу</li>'
                for s in strong)
            strong_html = (f'<div class="adv-sec">На чём держится</div>'
                           f'<ul class="adv-list">{items}</ul>')

        worst = p.get("worst") or {}
        wq = worst.get("quotes") or {}
        q = next((wq[k] for k in ("C2", "B4", "C1", "A2", "B1") if wq.get(k)),
                 None)
        qhtml = ""
        if q:
            link = crm_link("LEAD", worst["crm"]) if worst.get("crm") else None
            a = (f' · <a href="{link}" target="_blank" rel="noopener">'
                 f'послушать в карточке</a>' if link else "")
            qhtml = (f'<p class="qa-quote worst"><b>Разобрать на планёрке '
                     f'(звонок на {num(worst.get("integral"), 1)}):</b> '
                     f'«{e(str(q)[:200])}»{a}</p>')

        cards.append(f'<div class="adv">{head}{talk}{gaps_html}{obj_html}'
                     f'{strong_html}{qhtml}</div>')
    if not cards:
        return ""
    return (
        '<h2>Кому что тренировать</h2><section class="card">'
        f'<p class="adv-when">Срез за 30 дней на {ru_date(week)}, '
        f'пересчитывается по понедельникам — следующий {ru_date(nxt)}. '
        'Внутри недели рекомендации не меняются: это план тренировки, '
        'а не лента. Возражения считаются по всем разобранным разговорам '
        'за 30 дней, поэтому их больше, чем звонков с полной QA-оценкой.</p>'
        + "".join(cards) + '</section>')


def qa_md(s):
    """Минимальная разметка: **жирный** -> <b>, остальное экранируется."""
    out, bold = [], False
    for i, part in enumerate(str(s).split("**")):
        out.append(("<b>" + e(part) + "</b>") if i % 2 else e(part))
    return "".join(out)


def qa_conclusion(ranked, thin, names, dept, edge_metrics):'''
assert old in src, "conclusion anchor"
src = src.replace(old, new, 1)

# 3. вызов в странице ----------------------------------------------------
old = "    concl = qa_conclusion(ranked, thin, names, dept, edge_metrics)"
new = ("    concl = (qa_conclusion(ranked, thin, names, dept, edge_metrics)\n"
       "             + qa_plan_block(conn, order, names))")
assert old in src, "call anchor"
src = src.replace(old, new, 1)

# 4. короткий вывод больше не дублирует список «кому что тренировать» -----
old = """        '<h4 style="margin:12px 0 4px;font-size:12px;text-transform:uppercase;'
        'letter-spacing:.03em;color:var(--ink3)">Кому что тренировать</h4>'
        f'<ul class="qa-note" style="margin:0;padding-left:18px">{"".join(rec)}</ul>'
        '</section>')"""
new = """        '</section>')"""
assert old in src, "short list anchor"
src = src.replace(old, new, 1)

io.open(P, "w", encoding="utf-8").write(src)
print("dash.py patched:", len(src), "bytes")
