# -*- coding: utf-8 -*-
"""Патч web/dash.py: вкладка РОПа «Качество» — QA-оценка разговоров."""
import io

P = "/opt/ropbot/web/dash.py"
src = io.open(P, encoding="utf-8").read()

# 1. вкладка -------------------------------------------------------------
old = '''            ("etalon", "etalon.html", "Эталон")]'''
new = '''            ("etalon", "etalon.html", "Эталон"),
            ("kachestvo", "kachestvo.html", "Качество")]'''
assert old in src, "tabs anchor"
src = src.replace(old, new, 1)

# 2. CSS -----------------------------------------------------------------
old = """.foot{font-size:13px;color:var(--ink2);line-height:1.6}"""
new = """/* качество разговоров: чарты */
.qa-lead{display:flex;gap:18px;align-items:baseline;flex-wrap:wrap;margin-bottom:2px}
.qa-lead b{font-size:34px;line-height:1;font-variant-numeric:tabular-nums}
.qa-note{font-size:13px;color:var(--ink2);line-height:1.55;margin:10px 0 0}
.qa-warn{font-size:13px;color:var(--serious);line-height:1.5;margin:8px 0 0}
.qa-grid2{display:grid;grid-template-columns:1.15fr .85fr;gap:16px;align-items:start}
@media(max-width:1100px){.qa-grid2{grid-template-columns:1fr}}
.qa-radars{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
gap:10px}
.qa-rad{text-align:center}
.qa-rad figcaption{font-size:12.5px;font-weight:600;margin-top:2px}
.qa-rad .sub2{font-weight:400}
.qa-hm{display:grid;gap:2px;font-size:12px;align-items:center}
.qa-hm .hh{font-size:11px;color:var(--ink3);text-align:center;line-height:1.15;
padding-bottom:2px;align-self:end}
.qa-hm .hn{font-weight:600;font-size:13px;white-space:nowrap}
.qa-cell{border-radius:5px;padding:6px 2px;text-align:center;
font-variant-numeric:tabular-nums;color:var(--ink)}
.qa-cell.na{color:var(--ink3);background:var(--bg)}
.qa-tbl td.q{text-align:left;color:var(--ink2);font-size:12.5px}
.qa-cards{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
@media(max-width:1100px){.qa-cards{grid-template-columns:1fr}}
.qa-face{width:44px;height:44px;border-radius:50%;background:var(--blue-soft);
color:var(--blue);font-weight:700;font-size:16px;display:flex;align-items:center;
justify-content:center;flex:0 0 44px}
.qa-chead{display:flex;gap:12px;align-items:center;margin-bottom:10px}
.qa-chead .nm{font-weight:650;font-size:15px}
.qa-blocks{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:10px 0}
.qa-b{background:var(--bg);border-radius:9px;padding:8px;text-align:center}
.qa-b b{display:block;font-size:17px;font-variant-numeric:tabular-nums}
.qa-b span{font-size:11px;color:var(--ink3)}
.qa-lists{display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:13px}
@media(max-width:700px){.qa-lists{grid-template-columns:1fr}}
.qa-lists h4{margin:0 0 4px;font-size:12px;text-transform:uppercase;
letter-spacing:.03em;color:var(--ink3)}
.qa-lists li{margin-bottom:3px;line-height:1.4}
.qa-lists ul{margin:0;padding-left:16px}
.qa-quote{border-left:3px solid var(--line);padding:4px 10px;margin:8px 0 0;
font-size:13px;color:var(--ink2);line-height:1.5}
.qa-quote.best{border-left-color:var(--good)}
.qa-quote.worst{border-left-color:var(--bad)}
.qa-quote b{color:var(--ink);font-weight:600}
.foot{font-size:13px;color:var(--ink2);line-height:1.6}"""
assert old in src, "css anchor"
src = src.replace(old, new, 1)

# 3. страница ------------------------------------------------------------
code = '''

# ── вкладка РОПа «Качество разговоров» (QA-оценка) ───────────────────────────
# Данные — call_qa (app/qa.py + промт разборщика). Баллы 0–10 по 16 метрикам,
# блоки A 25% / B 30% / C 35% / D 10%. A3 (доля речи и монолог) считается кодом
# по ролям, остальное — модель с цитатой-доказательством на каждую метрику.
#
# Почему радар разнесён на малые множители, а не один график на всех: палитра
# прогнана валидатором — шесть цветных полигонов не различаются ни при
# дальтонизме, ни в тёмной теме. Каждому свой радар, серый контур — отдел,
# шкалы одинаковые: сравнение остаётся, читаемость появляется.

QA_DAYS = 30
QA_MIN_CALLS = 5           # меньше — «мало данных», в рейтинг не берём
QA_BLOCKS = [("a", "Структура", 0.25), ("b", "Диалог", 0.30),
             ("c", "Результат", 0.35), ("d", "Эмоции", 0.10)]
QA_METRICS = [
    ("a1", "A1", "Этапы звонка"), ("a2", "A2", "Инициатива"),
    ("a3", "A3", "Доля речи"),
    ("b1", "B1", "Потребность"), ("b2", "B2", "Выгоды"),
    ("b3", "B3", "Слушание"), ("b4", "B4", "Возражения"),
    ("b5", "B5", "Чистота речи"),
    ("c1", "C1", "Закрытие"), ("c2", "C2", "Следующий шаг"),
    ("c3", "C3", "Квалификация"), ("c4", "C4", "Цена"),
    ("c5", "C5", "Расширение чека"),
    ("d1", "D1", "Подстройка"), ("d2", "D2", "Негатив"), ("d3", "D3", "Тон"),
]
QA_LONG = {"a1": "соблюдение этапов звонка", "a2": "инициатива в разговоре",
           "a3": "доля речи и длина монолога",
           "b1": "выявление потребности", "b2": "презентация через выгоды",
           "b3": "активное слушание", "b4": "отработка возражений",
           "b5": "чистота речи", "c1": "попытка закрытия",
           "c2": "качество следующего шага", "c3": "квалификация",
           "c4": "работа с ценой", "c5": "расширение чека",
           "d1": "подстройка под клиента", "d2": "реакция на негатив",
           "d3": "энергия и тон"}

QA_SQL = f"""
  SELECT q.portal_user_id AS uid, count(*) AS n,
         {", ".join(f"avg(q.{k}) AS {k}" for k, _s, _t in QA_METRICS)},
         avg(q.block_a) AS block_a, avg(q.block_b) AS block_b,
         avg(q.block_c) AS block_c, avg(q.block_d) AS block_d,
         avg(q.integral) AS integral, stddev_samp(q.integral) AS sd,
         100.0 * count(*) FILTER (WHERE q.c2 >= 4)
               / NULLIF(count(*) FILTER (WHERE q.c2 IS NOT NULL), 0) AS conv,
         avg(q.talk_share) AS talk_share, avg(q.max_mono_sec) AS mono
    FROM call_qa q
   WHERE q.call_start >= now() - interval '{QA_DAYS} days'
     AND q.portal_user_id IN ({WORKING})
   GROUP BY 1
"""

QA_EDGE_SQL = f"""
  SELECT uid, kind, call_id, integral, quotes, call_start, crm_id FROM (
    SELECT q.portal_user_id AS uid, q.call_id, q.integral, q.quotes,
           q.call_start, c.crm_entity_id AS crm_id,
           row_number() OVER (PARTITION BY q.portal_user_id
                              ORDER BY q.integral DESC NULLS LAST) AS rn_hi,
           row_number() OVER (PARTITION BY q.portal_user_id
                              ORDER BY q.integral ASC NULLS LAST) AS rn_lo
      FROM call_qa q JOIN calls c ON c.id = q.call_id
     WHERE q.call_start >= now() - interval '{QA_DAYS} days'
       AND q.portal_user_id IN ({WORKING}) AND q.integral IS NOT NULL
  ) t, LATERAL (SELECT CASE WHEN rn_hi = 1 THEN 'best'
                            WHEN rn_lo = 1 THEN 'worst' END AS kind) k
   WHERE k.kind IS NOT NULL
"""


def qa_fetch(conn):
    if "qa" in _CACHE:
        return _CACHE["qa"]
    cur = conn.execute(QA_SQL)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        for c in cols:
            if c not in ("uid", "n") and r[c] is not None:
                r[c] = float(r[c])
    names = {r[0]: human_name(r[1], r[2]) for r in conn.execute(f"""
        SELECT m.portal_user_id, m.name, m.last_name FROM managers m
         WHERE m.portal_user_id IN ({WORKING})""").fetchall()}
    edges = {}
    for uid, kind, call_id, integral, quotes, when, crm_id in conn.execute(
            QA_EDGE_SQL):
        edges[(uid, kind)] = dict(call_id=call_id, integral=float(integral),
                                  quotes=quotes or {}, when=when, crm=crm_id)
    period = conn.execute(
        f"""SELECT min(call_start) AT TIME ZONE '{TZ}',
                   max(call_start) AT TIME ZONE '{TZ}', count(*)
              FROM call_qa
             WHERE call_start >= now() - interval '{QA_DAYS} days'""").fetchone()
    _CACHE["qa"] = (rows, names, edges, period)
    return _CACHE["qa"]


def qa_dept(rows):
    """Среднее по отделу, взвешенное по числу звонков."""
    out = {}
    keys = [k for k, _s, _t in QA_METRICS] + ["block_a", "block_b", "block_c",
                                              "block_d", "integral"]
    for k in keys:
        num = sum(r[k] * r["n"] for r in rows if r.get(k) is not None)
        den = sum(r["n"] for r in rows if r.get(k) is not None)
        out[k] = num / den if den else None
    return out


def qa_mix(v, weak=False):
    """Цвет ячейки тепловой карты: расходящаяся шкала вокруг 5 из 10.

    Слоты статуса (плохо/хорошо) с нейтральной серединой на фоне поверхности —
    не радуга. color-mix держит обе темы одной формулой.
    """
    if v is None:
        return "var(--bg)"
    d = max(-1.0, min(1.0, (v - 5.0) / 5.0))
    pct = int(round(abs(d) * 62))
    if pct < 4:
        return "var(--bg)"
    tone = "var(--good)" if d > 0 else "var(--bad)"
    return f"color-mix(in oklab, {tone} {pct}%, var(--surface))"


def qa_radar(vals, ref, size=170, label=None):
    """Радар по четырём блокам: серый контур — отдел, синий — менеджер."""
    cx = cy = size / 2
    r = size / 2 - 26
    n = len(QA_BLOCKS)
    import math
    def pt(i, frac):
        a = -math.pi / 2 + 2 * math.pi * i / n
        rr = r * max(0.0, min(1.0, frac))
        return cx + rr * math.cos(a), cy + rr * math.sin(a)
    grid = []
    for ring in (0.25, 0.5, 0.75, 1.0):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                       (pt(i, ring) for i in range(n)))
        grid.append(f'<polygon class="grid" fill="none" points="{pts}"></polygon>')
    for i in range(n):
        x, y = pt(i, 1.0)
        grid.append(f'<line class="grid" x1="{cx}" y1="{cy}" x2="{x:.1f}" '
                    f'y2="{y:.1f}"></line>')
    def poly(source, cls, style):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                       (pt(i, (source.get("block_" + k) or 0) / 10.0)
                        for i, (k, _t, _w) in enumerate(QA_BLOCKS)))
        return f'<polygon class="{cls}" points="{pts}" style="{style}"></polygon>'
    body = "".join(grid)
    body += poly(ref, "ref", "fill:none;stroke:var(--ink3);stroke-width:1.5;"
                             "stroke-dasharray:4 3")
    body += poly(vals, "me", "fill:color-mix(in oklab,var(--blue) 22%,transparent);"
                             "stroke:var(--blue);stroke-width:2;"
                             "stroke-linejoin:round")
    for i, (k, t, _w) in enumerate(QA_BLOCKS):
        x, y = pt(i, 1.22)
        anchor = ("middle" if i in (0, 2) else
                  "start" if i == 1 else "end")
        v = vals.get("block_" + k)
        body += (f'<text class="ax" x="{x:.1f}" y="{y + 3:.1f}" '
                 f'text-anchor="{anchor}">{e(t)} {num(v, 1)}</text>')
    title = f'<title>{e(label)}</title>' if label else ""
    return (f'<svg class="chart" viewBox="0 0 {size} {size}" role="img" '
            f'style="max-width:{size}px;margin:0 auto">{title}{body}</svg>')


def qa_bars(rows, names, dept, w=560):
    """Интегральный балл по менеджерам + линия среднего по отделу."""
    if not rows:
        return ""
    rows = sorted(rows, key=lambda r: -(r["integral"] or 0))
    bh, gap, top, left = 26, 10, 16, 118
    h = top + len(rows) * (bh + gap) + 26
    inner = w - left - 46
    mx = 10.0
    avg = dept["integral"] or 0
    ax = left + inner * avg / mx
    body = [f'<line class="ref" x1="{ax:.1f}" y1="{top - 8}" x2="{ax:.1f}" '
            f'y2="{h - 24}"></line>',
            f'<text class="ax" x="{ax:.1f}" y="{h - 8}" text-anchor="middle">'
            f'среднее по отделу {num(avg, 1)}</text>']
    for i, r in enumerate(rows):
        y = top + i * (bh + gap)
        v = r["integral"] or 0
        bw = max(3.0, inner * v / mx)
        lead = (i == 0 and r["n"] >= QA_MIN_CALLS)
        fill = "var(--good)" if lead else "var(--blue)"
        body.append(
            f'<g><title>{e(names.get(r["uid"], ""))}: {num(v, 2)} из 10, '
            f'{r["n"]} {plural(r["n"], "звонок", "звонка", "звонков")}</title>'
            f'<text class="ax" x="{left - 8}" y="{y + bh * 0.68:.0f}" '
            f'text-anchor="end" style="font-size:12.5px;fill:var(--ink)">'
            f'{e(names.get(r["uid"], ""))}</text>'
            f'<rect x="{left}" y="{y}" width="{bw:.1f}" height="{bh}" rx="4" '
            f'style="fill:{fill}"></rect>'
            f'<text class="ax" x="{left + bw + 7:.1f}" y="{y + bh * 0.68:.0f}" '
            f'style="fill:var(--ink);font-variant-numeric:tabular-nums">'
            f'{num(v, 2)}</text></g>')
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" role="img">'
            f'<title>Интегральный балл по менеджерам</title>'
            + "".join(body) + '</svg>')


def qa_scatter(rows, names, w=560, h=330):
    """Балл × стабильность. Ось Y перевёрнута: чем выше, тем ровнее."""
    pts = [r for r in rows if r["integral"] is not None and r["sd"] is not None]
    if not pts:
        return ('<p class="qa-note">Стабильность появится, когда у менеджеров '
                'наберётся хотя бы по два оценённых разговора.</p>')
    pad_l, pad_b, pad_t, pad_r = 44, 40, 22, 14
    xs = [r["integral"] for r in pts]
    ys = [r["sd"] for r in pts]
    x0, x1 = min(min(xs) - .6, 3), max(max(xs) + .6, 7)
    y0, y1 = 0, max(max(ys) * 1.25, 1.0)
    def px(v):
        return pad_l + (w - pad_l - pad_r) * (v - x0) / (x1 - x0)
    def py(v):  # перевёрнутая: маленький разброс — вверху
        return pad_t + (h - pad_t - pad_b) * (v - y0) / (y1 - y0)
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    body = [
        f'<line class="grid" x1="{pad_l}" y1="{h - pad_b}" x2="{w - pad_r}" '
        f'y2="{h - pad_b}"></line>',
        f'<line class="grid" x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" '
        f'y2="{h - pad_b}"></line>',
        f'<line class="ref" x1="{px(mx):.1f}" y1="{pad_t}" x2="{px(mx):.1f}" '
        f'y2="{h - pad_b}"></line>',
        f'<line class="ref" x1="{pad_l}" y1="{py(my):.1f}" x2="{w - pad_r}" '
        f'y2="{py(my):.1f}"></line>',
        f'<text class="ax" x="{w - pad_r - 4}" y="{pad_t + 14}" '
        f'text-anchor="end" style="font-weight:700;fill:var(--good)">'
        f'Лидеры: балл выше среднего, разброс ниже</text>',
        f'<text class="ax" x="{(w + pad_l) / 2:.0f}" y="{h - 6}" '
        f'text-anchor="middle">интегральный балл →</text>',
        f'<text class="ax" x="12" y="{(h - pad_b + pad_t) / 2:.0f}" '
        f'text-anchor="middle" transform="rotate(-90 12 '
        f'{(h - pad_b + pad_t) / 2:.0f})">← разброс между звонками</text>',
    ]
    for r in pts:
        x, y = px(r["integral"]), py(r["sd"])
        good = r["integral"] >= mx and r["sd"] <= my
        col = "var(--good)" if good else "var(--blue)"
        nm = (names.get(r["uid"], "") or "").split()
        body.append(
            f'<g><title>{e(names.get(r["uid"], ""))}: балл {num(r["integral"], 2)}, '
            f'разброс {num(r["sd"], 2)}, {r["n"]} '
            f'{plural(r["n"], "звонок", "звонка", "звонков")}</title>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" style="fill:{col};'
            f'stroke:var(--surface);stroke-width:2"></circle>'
            f'<text class="ax" x="{x:.1f}" y="{y - 12:.1f}" text-anchor="middle" '
            f'style="fill:var(--ink);font-size:11.5px">'
            f'{e(nm[-1] if nm else "")}</text></g>')
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" role="img">'
            f'<title>Балл против стабильности</title>' + "".join(body) + '</svg>')


def qa_edges(rows, names, dept):
    """Топ-3 сильных и топ-3 слабых метрики относительно отдела."""
    res = {}
    for r in rows:
        diffs = []
        for k, short, title in QA_METRICS:
            v, d = r.get(k), dept.get(k)
            if v is None or d is None:
                continue
            diffs.append((v - d, k, short, title, v))
        diffs.sort(reverse=True)
        res[r["uid"]] = (diffs[:3], list(reversed(diffs))[:3])
    return res


def qa_quote_for(edge, key):
    q = (edge or {}).get("quotes") or {}
    return q.get(key.upper())


def page_kachestvo(conn):
    rows, names, edges, period = qa_fetch(conn)
    if not rows:
        return ('<section class="card"><p class="muted">Оценок пока нет: '
                'карточка качества считается на разговорах, разобранных после '
                '25.08.2026.</p></section>')
    dept = qa_dept(rows)
    ranked = sorted([r for r in rows if r["n"] >= QA_MIN_CALLS],
                    key=lambda r: -(r["integral"] or 0))
    thin = [r for r in rows if r["n"] < QA_MIN_CALLS]
    edge_metrics = qa_edges(rows, names, dept)
    d0, d1, total = period
    counts = [r["n"] for r in rows]
    spread = (max(counts) / max(1, min(counts))) if counts else 1

    head = (f'<p class="sub" style="margin-bottom:14px">Период: '
            f'{ru_date(d0.date())} — {ru_date(d1.date())} · '
            f'{total} {plural(total, "разговор", "разговора", "разговоров")} · '
            f'{len(rows)} {plural(len(rows), "менеджер", "менеджера", "менеджеров")}'
            f' · окно {QA_DAYS} дней</p>')

    warn = ""
    if spread > 3:
        warn = ('<p class="qa-warn">Разница в числе разговоров между '
                'менеджерами больше чем втрое — для части сравнений данных '
                'недостаточно, смотрите на число звонков в таблице.</p>')

    # ── рейтинг ────────────────────────────────────────────────────────────
    trs = []
    for i, r in enumerate(ranked + thin):
        lead = (i == 0 and r in ranked)
        mark = ' style="background:color-mix(in oklab,var(--good) 12%,transparent)"'
        sd = num(r["sd"], 2) if r["sd"] is not None else "—"
        note = "" if r["n"] >= QA_MIN_CALLS else \
            '<span class="sub2">мало данных</span>'
        trs.append(
            f'<tr{mark if lead else ""}>'
            f'<td class="mname">{"🏆 " if lead else ""}{e(names.get(r["uid"], ""))}'
            f'{note}</td>'
            f'<td><b>{num(r["integral"], 2)}</b></td>'
            f'<td>{num(r["block_a"], 1)}</td><td>{num(r["block_b"], 1)}</td>'
            f'<td>{num(r["block_c"], 1)}</td><td>{num(r["block_d"], 1)}</td>'
            f'<td>{sd}</td><td>{num(r["conv"], 0)}%</td><td>{r["n"]}</td></tr>')
    table = (
        '<h2>Рейтинг менеджеров</h2><section class="card">'
        '<table class="qa-tbl"><thead><tr><th>Менеджер</th><th>Интеграл</th>'
        '<th>A структура</th><th>B диалог</th><th>C результат</th>'
        '<th>D эмоции</th><th>Разброс</th><th>Следующий шаг</th>'
        '<th>Звонков</th></tr></thead><tbody>' + "".join(trs) + '</tbody></table>'
        '<p class="qa-note">Интеграл = A×0,25 + B×0,30 + C×0,35 + D×0,10. '
        'Разброс — стандартное отклонение интеграла между звонками, меньше '
        'значит ровнее. «Следующий шаг» — доля звонков, где договорённость '
        'хотя бы намечена (C2 ≥ 4).</p>' + warn + '</section>')

    # ── радары ────────────────────────────────────────────────────────────
    rad = "".join(
        f'<figure class="qa-rad" style="margin:0">'
        + qa_radar(r, dept, label=names.get(r["uid"], ""))
        + f'<figcaption>{e(names.get(r["uid"], ""))}'
          f'<span class="sub2">интеграл {num(r["integral"], 2)} · '
          f'{r["n"]} {plural(r["n"], "звонок", "звонка", "звонков")}</span>'
          f'</figcaption></figure>'
        for r in ranked + thin)
    radars = (
        '<h2>Портрет по блокам</h2><section class="card">'
        f'<div class="qa-radars">{rad}</div>'
        '<p class="qa-note">Синий — менеджер, пунктирный серый — среднее по '
        'отделу, шкала у всех одна (0–10). Шесть цветных полигонов на одном '
        'радаре неразличимы при дальтонизме и в тёмной теме — проверено '
        'валидатором палитры, поэтому портреты разнесены.</p></section>')

    # ── столбцы и разброс ─────────────────────────────────────────────────
    charts = (
        '<h2>Балл и стабильность</h2><div class="qa-grid2">'
        '<section class="card">' + qa_bars(rows, names, dept)
        + '<p class="qa-note">Зелёный — лидер периода. Пунктир — среднее по '
          'отделу.</p></section>'
        '<section class="card">' + qa_scatter(rows, names)
        + '<p class="qa-note">Правый верхний угол — высокий балл при ровном '
          'качестве от звонка к звонку.</p></section></div>')

    # ── тепловая карта ────────────────────────────────────────────────────
    order = ranked + thin
    cols = f"minmax(120px,150px) repeat({len(QA_METRICS)},1fr)"
    cells = ['<div></div>']
    for k, short, title in QA_METRICS:
        cells.append(f'<div class="hh" title="{e(QA_LONG[k])}"><b>{short}</b><br>'
                     f'{e(title)}</div>')
    for r in order:
        cells.append(f'<div class="hn">{e(names.get(r["uid"], ""))}</div>')
        for k, short, title in QA_METRICS:
            v = r.get(k)
            cls = "qa-cell" + ("" if v is not None else " qa-cell na")
            cells.append(
                f'<div class="{cls}" style="background:{qa_mix(v)}" '
                f'title="{e(names.get(r["uid"], ""))} · {short} '
                f'{e(QA_LONG[k])}: {num(v, 1)}">{num(v, 1)}</div>')
    heat = ('<h2>Метрики по менеджерам</h2><section class="card">'
            f'<div class="qa-hm" style="grid-template-columns:{cols}">'
            + "".join(cells) + '</div>'
            '<p class="qa-note">Зелёное — выше середины шкалы, красное — ниже, '
            'серое — метрика в этих звонках не применялась (N/A) и в среднее '
            'не входит. A3 считается кодом по разметке ролей: норма доли речи '
            '40–60%, штраф за монолог длиннее полутора минут. По отделу доля '
            'речи менеджера сейчас около '
            f'{num((dept.get("a3") is not None) and 100 * (sum((r.get("talk_share") or 0) * r["n"] for r in rows) / max(1, sum(r["n"] for r in rows))) or 0, 0)}% — '
            'это системная особенность разговоров, а не провал одного человека; '
            'на исход сделки доля речи, по нашим прошлым замерам, не влияет.'
            '</p></section>')

    # ── карточки ──────────────────────────────────────────────────────────
    cards = []
    for r in order:
        uid = r["uid"]
        nm = names.get(uid, "")
        initials = "".join(p[0] for p in nm.split()[:2]).upper()
        strong, weak = edge_metrics.get(uid, ([], []))
        best, worst = edges.get((uid, "best")), edges.get((uid, "worst"))
        blocks = "".join(
            f'<div class="qa-b"><b>{num(r.get("block_" + k), 1)}</b>'
            f'<span>{e(t)}</span></div>' for k, t, _w in QA_BLOCKS)

        def li(items, sign):
            out = []
            for diff, k, short, title, v in items:
                q = qa_quote_for(best if sign > 0 else worst, k)
                qt = (f'<span class="sub2">«{e(str(q)[:110])}»</span>'
                      if q else "")
                out.append(f'<li><b>{short}</b> {e(QA_LONG[k])} — '
                           f'{num(v, 1)} '
                           f'({"+" if diff >= 0 else "−"}{num(abs(diff), 1)} '
                           f'к отделу){qt}</li>')
            return "".join(out) or "<li>—</li>"

        q_best = next((qa_quote_for(best, k) for k, _s, _t in QA_METRICS
                       if qa_quote_for(best, k)), None)
        q_worst = next((qa_quote_for(worst, k) for k in ("c2", "b4", "c1", "a2")
                        if qa_quote_for(worst, k)), None)
        quotes = ""
        if best and q_best:
            link = crm_link("LEAD", best["crm"]) if best.get("crm") else None
            a = (f' · <a href="{link}" target="_blank" rel="noopener">карточка</a>'
                 if link else "")
            quotes += (f'<p class="qa-quote best"><b>Лучший звонок '
                       f'{num(best["integral"], 1)}:</b> «{e(str(q_best)[:180])}»'
                       f'{a}</p>')
        if worst and q_worst:
            link = crm_link("LEAD", worst["crm"]) if worst.get("crm") else None
            a = (f' · <a href="{link}" target="_blank" rel="noopener">карточка</a>'
                 if link else "")
            quotes += (f'<p class="qa-quote worst"><b>Слабый звонок '
                       f'{num(worst["integral"], 1)}:</b> «{e(str(q_worst)[:180])}»'
                       f'{a}</p>')
        cards.append(
            f'<section class="card"><div class="qa-chead">'
            f'<div class="qa-face">{e(initials)}</div><div>'
            f'<div class="nm">{e(nm)}</div>'
            f'<span class="sub2">интеграл {num(r["integral"], 2)} · разброс '
            f'{num(r["sd"], 2) if r["sd"] is not None else "—"} · {r["n"]} '
            f'{plural(r["n"], "звонок", "звонка", "звонков")} · следующий шаг '
            f'{num(r["conv"], 0)}%</span></div></div>'
            f'<div class="qa-blocks">{blocks}</div>'
            f'<div class="qa-lists"><div><h4>Сильные стороны</h4><ul>'
            f'{li(strong, 1)}</ul></div><div><h4>Зоны роста</h4><ul>'
            f'{li(weak, -1)}</ul></div></div>{quotes}</section>')
    cards_html = ('<h2>Карточки менеджеров</h2><div class="qa-cards">'
                  + "".join(cards) + '</div>')

    # ── вывод ─────────────────────────────────────────────────────────────
    concl = qa_conclusion(ranked, thin, names, dept, edge_metrics)

    foot = ('<p class="foot">Как это считается: каждый разговор длиннее минуты '
            'разбирается моделью тем же проходом, что и договорённости, — '
            'по 16 метрикам с цитатой-доказательством на каждую. Баллы блоков '
            'и интеграл складывает код, доля речи и длина монолога считаются '
            'механически по разметке ролей. Метрика, которая в звонке не '
            'применима, помечается N/A и в среднее не входит. Инструмент для '
            'разбора и обучения, не для премий: у модели своя погрешность, '
            'а запись одноканальная.</p>')
    return (head + table + radars + charts + heat + cards_html + concl + foot)


def qa_conclusion(ranked, thin, names, dept, edge_metrics):
    if not ranked:
        return ('<h2>Вывод</h2><section class="card"><p class="qa-note">'
                'Оценённых разговоров пока слишком мало, чтобы называть '
                'лидера.</p></section>')
    lead = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    ln, sn = names.get(lead["uid"], ""), names.get(second["uid"], "") if second else ""
    gap = (lead["integral"] - second["integral"]) if second else None
    strong, _ = edge_metrics.get(lead["uid"], ([], []))
    s_txt = ", ".join(QA_LONG[k] for _d, k, _s, _t, _v in strong[:2]) or "процесс"
    close = ""
    if second:
        close = (f'Ближайший преследователь — {e(sn)} ({num(second["integral"], 2)}), '
                 f'отставание {num(gap, 2)} балла: ')
        if gap is not None and gap < 0.3:
            close += 'разрыв в пределах погрешности, лидерство условное. '
        else:
            close += 'разрыв заметный, но одного периода мало. '
    rec = []
    for r in ranked + thin:
        _s, weak = edge_metrics.get(r["uid"], ([], []))
        if not weak:
            continue
        topics = ", ".join(QA_LONG[k] for _d, k, _sh, _t, _v in weak[:2])
        note = " (мало данных)" if r["n"] < QA_MIN_CALLS else ""
        rec.append(f'<li><b>{e(names.get(r["uid"], ""))}</b>{note} — {topics}</li>')
    return (
        '<h2>Вывод</h2><section class="card">'
        f'<p class="qa-note" style="font-size:14px">Лидер периода — '
        f'<b>{e(ln)}</b> с интегральным баллом {num(lead["integral"], 2)} из 10 '
        f'на {lead["n"]} {plural(lead["n"], "звонке", "звонках", "звонках")}; '
        f'сильнее всего у него {s_txt}. {close}'
        'Средний балл по отделу — '
        f'{num(dept["integral"], 2)}; самый слабый блок у всех — '
        f'{e(min(QA_BLOCKS, key=lambda b: dept.get("block_" + b[0]) or 99)[1].lower())}, '
        'туда и стоит вкладывать обучение.</p>'
        '<h4 style="margin:12px 0 4px;font-size:12px;text-transform:uppercase;'
        'letter-spacing:.03em;color:var(--ink3)">Кому что тренировать</h4>'
        f'<ul class="qa-note" style="margin:0;padding-left:18px">{"".join(rec)}</ul>'
        '</section>')

'''
anchor = ('ROP_PAGES = {"kontrol": page_kontrol, "menedzhery": page_managers,\n'
          '             "slepok": page_slepok, "etalon": page_etalon}')
assert anchor in src, "rop_pages anchor"
src = src.replace(anchor, code + (
    'ROP_PAGES = {"kontrol": page_kontrol, "menedzhery": page_managers,\n'
    '             "slepok": page_slepok, "etalon": page_etalon,\n'
    '             "kachestvo": page_kachestvo}'), 1)

# 4. сборка --------------------------------------------------------------
old = '''                    ("etalon", "etalon.html")],'''
new = '''                    ("etalon", "etalon.html"),
                    ("kachestvo", "kachestvo.html")],'''
assert old in src, "pageset anchor"
src = src.replace(old, new, 1)

io.open(P, "w", encoding="utf-8").write(src)
print("dash.py patched:", len(src), "bytes")
