# -*- coding: utf-8 -*-
"""Патч web/dash.py: вкладка РОПа «Эталон» + личные разрывы менеджера."""
import io, sys

P = "/opt/ropbot/web/dash.py"
src = io.open(P, encoding="utf-8").read()

# 1. CSS ----------------------------------------------------------------------
css_anchor = """.foot{font-size:13px;color:var(--ink2);line-height:1.6}
.foot li{margin-bottom:6px}"""
css_new = css_anchor + """
/* эталон: кружки-донаты */
.dn{position:relative;width:78px;margin:0 auto}
.dn svg{width:78px;height:78px;transform:rotate(-90deg);display:block}
.dn-bg{fill:none;stroke:var(--line);stroke-width:7}
.dn-fg{fill:none;stroke-width:7;stroke-linecap:round}
.dn-t{position:absolute;inset:0;display:flex;flex-direction:column;
align-items:center;justify-content:center;text-align:center;pointer-events:none}
.dn-t b{font-size:14.5px;line-height:1.1;font-variant-numeric:tabular-nums}
.dn-t span{font-size:10px;color:var(--ink3);margin-top:1px}
.dn-tr{position:absolute;top:0;right:2px;font-style:normal;font-size:12px;font-weight:700}
.dn-tr.up{color:var(--good)}.dn-tr.down{color:var(--bad)}.dn-tr.flat{color:var(--ink3)}
.dn.none svg{opacity:.35}
.dn.none .dn-t b{color:var(--ink3);font-weight:600}
.et-grid{display:grid;gap:10px 8px;align-items:center;margin:6px 0 2px}
.et-h{font-size:12px;color:var(--ink2);text-align:center;line-height:1.3;align-self:end}
.et-h b{display:block;font-size:12.5px;color:var(--ink)}
.et-h .bestof{color:var(--ink3);font-size:11px;display:block;margin-top:1px}
.et-name{font-weight:600;font-size:14px}
.et-name .star{display:block;font-size:11px;color:var(--blue)}
.et-name .who2{display:block;font-size:11px;color:var(--ink3);font-weight:400}
.et-legend{font-size:13px;color:var(--ink2);margin:2px 0 14px;line-height:1.55}
.gap-i{padding:9px 0;border-top:1px solid var(--line);font-size:14px;line-height:1.5}
.gap-i:first-child{border-top:0;padding-top:2px}
.gap-price{color:var(--ink2);font-size:13px;display:block}
.gap-n{display:inline-block;width:20px;height:20px;border-radius:50%;
background:var(--blue);color:#fff;font-size:12px;font-weight:700;text-align:center;
line-height:20px;margin-right:7px}
.et-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
@media(max-width:1100px){.et-cards{grid-template-columns:1fr}}
@media(max-width:900px){.dn{width:62px}.dn svg{width:62px;height:62px}
.dn-t b{font-size:12.5px}}"""
assert css_anchor in src, "css anchor"
src = src.replace(css_anchor, css_new, 1)

# 2. Вкладка ------------------------------------------------------------------
old = '''ROP_TABS = [("menedzhery", "", "Менеджеры"),
            ("slepok", "slepok.html", "Слепок работы")]'''
new = '''ROP_TABS = [("menedzhery", "", "Менеджеры"),
            ("slepok", "slepok.html", "Слепок работы"),
            ("etalon", "etalon.html", "Эталон")]'''
assert old in src, "tabs anchor"
src = src.replace(old, new, 1)

# 3. Код страницы -------------------------------------------------------------
code = '''

# ── вкладка РОПа «Эталон» + личные разрывы менеджера ─────────────────────────
# Данные — manager_snapshot (app/snapshot.py, крон ежедневно). Берётся страта
# 'std' (нормировка на состав классов отдела), когда посчитана, иначе 'all'.
# Эталон по каждому параметру — лучший менеджер этого параметра (n >= 10).
# Веса и цена разрывов — этап 3 слепка (projects/slepok-menedzherov.md):
# в цене — ПОЛОВИНА фактической разницы конверсий вне выборки, вилка ×0,5…×2
# от неё. Самоотбор не вычищен, истинную цену покажет только эксперимент.

ET_STAR = 8829   # Жернов — эталон отдела (этап 3, 25.08.2026): устойчивый
                 # топ-2 конверсии вызревших в двух периодах + лучший процесс.

ET_LEVERS = [    # (код, подпись, ед., больше=лучше, вес)
    ("P3",  "5+ звонков за 3 дня", "%", True, 35),
    ("H3",  "5+ попыток по недозвонам", "%", True, 30),
    ("D1",  "Конкретный следующий шаг", "%", True, 20),
    ("T4P", "Техники продаж", "%", True, 15),
]
ET_DISC = [
    ("C1",  "Первый отклик", "раб.мин", False, 0),
    ("H2",  "Брошено 0–1 звонком", "%", False, 0),
    ("GI2", "Закрыто без разговора", "%", False, 0),
    ("GI3", "Просроченные дела", "лидов", False, 0),
    ("D2",  "Договорённость выполнена", "%", True, 0),
    ("GO1", "Реакция на ♛", "раб.ч", False, 0),
]
ET_MONITOR = [
    ("R2",  "3+ разговора", "%", True, 0),
    ("G1",  "Первый разговор", "мин", True, 0),
    ("REZ", "Конверсия вызревших", "%", True, 0),
]
ET_PERIOD = {"GI3": "mom", "REZ": "mat"}        # остальные — окно 30 дней
ET_PRICE = {"P3": 0.0325, "H3": 0.021}          # Δ конверсии в цене, доля
ET_MIN_N = 10                                   # меньше — «мало данных»


def et_data(conn):
    """(дата снимка, дата сравнения, тек, прош, {uid: имя})."""
    if "etalon" in _CACHE:
        return _CACHE["etalon"]
    last = conn.execute("SELECT max(snap_date) FROM manager_snapshot").fetchone()[0]
    prev = None
    if last:
        prev = conn.execute(
            "SELECT max(snap_date) FROM manager_snapshot WHERE snap_date <= %s",
            (last - dt.timedelta(days=7),)).fetchone()[0]
    names = {r[0]: human_name(r[1], r[2]) for r in conn.execute(f"""
        SELECT m.portal_user_id, m.name, m.last_name FROM managers m
         WHERE m.portal_user_id IN ({WORKING})""").fetchall()}

    def load(day):
        d = {}
        if not day:
            return d
        for uid, period, param, grade, v, n in conn.execute(
                """SELECT portal_user_id, period, param, grade, value, denom
                     FROM manager_snapshot
                    WHERE snap_date = %s AND grade IN ('all','std')""", (day,)):
            if period != ET_PERIOD.get(param, "30d"):
                continue
            key = (uid, param)
            if grade == "std" or key not in d:
                d[key] = (float(v), float(n or 0))
        return d

    _CACHE["etalon"] = (last, prev, load(last), load(prev), names)
    return _CACHE["etalon"]


def et_best(cur, names, param, up):
    """Эталон параметра: (значение, uid лучшего) при выборке n >= ET_MIN_N."""
    vals = [(cur[(u, param)][0], u) for u in names
            if (u, param) in cur and cur[(u, param)][1] >= ET_MIN_N]
    if not vals:
        return None, None
    return max(vals) if up else min(vals)


def et_rel(v, best, up):
    """Насколько близко к эталону, 0..1."""
    if v is None or best is None:
        return None
    if up:
        return 1.0 if best <= 0 else max(0.0, min(1.0, v / best))
    return 1.0 if v <= best else max(0.0, min(1.0, (best + 0.5) / (v + 0.5)))


def et_fmt(param, v):
    if v is None:
        return "—"
    if param == "G1":
        return num(v / 60, 1)
    if param in ("C1", "GI3"):
        return num(v, 0)
    if param == "REZ":
        return num(v, 2)
    return num(v, 1)


def et_trend(up, v, pv):
    """Стрелка к снимку недельной давности, с поправкой на направление."""
    if v is None or pv is None:
        return ""
    ch = (v - pv) / max(abs(pv), 1e-9)
    better = ch > 0.03 if up else ch < -0.03
    worse = ch < -0.03 if up else ch > 0.03
    if better:
        return '<i class="dn-tr up" title="лучше, чем неделю назад">▲</i>'
    if worse:
        return '<i class="dn-tr down" title="хуже, чем неделю назад">▼</i>'
    return '<i class="dn-tr flat" title="без изменений">·</i>'


def donut(rel, text, sub="", trend="", full=False):
    """Кружок-донат: заливка — близость к эталону, цвет — статус."""
    if rel is None:
        return (f'<div class="dn none"><svg viewBox="0 0 60 60" aria-hidden="true">'
                f'<circle class="dn-bg" cx="30" cy="30" r="24"></circle></svg>'
                f'<div class="dn-t"><b>{text}</b><span>{e(sub)}</span></div></div>')
    color = ("var(--blue)" if full else
             "var(--good)" if rel >= 0.85 else
             "var(--warn)" if rel >= 0.55 else "var(--bad)")
    dash = max(5.0, min(1.0, rel) * 150.8)
    return (f'<div class="dn"><svg viewBox="0 0 60 60" aria-hidden="true">'
            f'<circle class="dn-bg" cx="30" cy="30" r="24"></circle>'
            f'<circle class="dn-fg" cx="30" cy="30" r="24" '
            f'style="stroke:{color};stroke-dasharray:{dash:.1f} 150.8"></circle>'
            f'</svg><div class="dn-t"><b>{text}</b><span>{e(sub)}</span></div>'
            f'{trend}</div>')


def et_gaps(cur, names, uid):
    """Топ-3 разрыва менеджера по значимым параметрам, с ценой где честно."""
    out = []
    for param, label, unit, up, weight in ET_LEVERS:
        mine = cur.get((uid, param))
        if not mine or mine[1] < ET_MIN_N:
            continue
        best, bu = et_best(cur, names, param, up)
        if best is None or bu == uid:
            continue
        rel = et_rel(mine[0], best, up)
        if rel is None or rel >= 0.9:
            continue
        g = dict(param=param, label=label, unit=unit, v=mine[0], n=mine[1],
                 best=best, who=names.get(bu, ""), score=weight * (1 - rel))
        if param in ET_PRICE:
            mid = mine[1] * (best - mine[0]) / 100.0 * ET_PRICE[param]
            g["deals"] = (mid / 2, mid, mid * 2)
        out.append(g)
    out.sort(key=lambda x: -x["score"])
    return out[:3]


def et_price_text(g):
    if "deals" not in g:
        return ("цена в сделках не оценивается: карточкам разговоров меньше "
                "месяца")
    lo, mid, hi = g["deals"]
    if hi < 0.05:
        return "цена разрыва меньше 0,1 сделки в месяц"
    return (f"≈{num(mid, 1)} сделки в месяц, "
            f"осторожная вилка {num(lo, 1)}–{num(hi, 1)}")


def et_block(cur, prv, names, order, plist, title, note):
    cols = f"minmax(150px,180px) repeat({len(plist)},1fr)"
    head = ['<div></div>']
    for param, label, unit, up, _w in plist:
        best, bu = et_best(cur, names, param, up)
        bname = ((names.get(bu) or "").split() or [""])[0] if bu else ""
        bestof = (f'<span class="bestof">эталон {et_fmt(param, best)} · '
                  f'{e(bname)}</span>'
                  if best is not None else '<span class="bestof">нет данных</span>')
        head.append(f'<div class="et-h"><b>{e(label)}</b>{e(unit)}{bestof}</div>')
    rows = []
    for uid in order:
        star = ('<span class="star">★ эталон отдела</span>'
                if uid == ET_STAR else "")
        rows.append(f'<div class="et-name">{e(names[uid])}{star}</div>')
        for param, label, unit, up, _w in plist:
            mine = cur.get((uid, param))
            if not mine:
                rows.append(donut(None, "—", "нет данных"))
                continue
            v, n = mine
            best, bu = et_best(cur, names, param, up)
            rel = et_rel(v, best, up)
            sub = "мало данных" if n < ET_MIN_N else unit
            pv = prv.get((uid, param), (None,))[0] if prv else None
            rows.append(donut(rel if n >= ET_MIN_N else None,
                              et_fmt(param, v), sub,
                              et_trend(up, v, pv), full=(bu == uid)))
    return (f'<h2>{e(title)}</h2><section class="card">'
            f'<div class="et-grid" style="grid-template-columns:{cols}">'
            + "".join(head) + "".join(rows)
            + f'</div><p class="et-legend" style="margin:12px 0 0">{note}</p>'
            '</section>')


def page_etalon(conn):
    last, prev, cur, prv, names = et_data(conn)
    if not last:
        return ('<section class="card"><p class="muted">Снимков слепка ещё '
                'нет — таблица manager_snapshot пуста.</p></section>')
    # порядок: эталон первым, дальше по близости к эталонам рычагов
    def mean_rel(uid):
        rs = []
        for param, _l, _u, up, _w in ET_LEVERS:
            m = cur.get((uid, param))
            b, _ = et_best(cur, names, param, up)
            r = et_rel(m[0], b, up) if m and m[1] >= ET_MIN_N else None
            if r is not None:
                rs.append(r)
        return sum(rs) / len(rs) if rs else 0
    order = sorted(names, key=lambda u: (u != ET_STAR, -mean_rel(u)))

    intro = (f'<p class="et-legend">Каждый кружок — параметр менеджера за '
             f'последние 30 дней против <b>эталона</b> — лучшего в отделе по '
             f'этому параметру (его кружок синий и полный). Заливка — '
             f'насколько близко к эталону, стрелка — динамика к снимку '
             f'недельной давности. Числа нормированы на состав классов '
             f'A/B/C/D, где классы уже накоплены. Эталон отдела по результату '
             f'и процессу — <b>{e(names.get(ET_STAR, ""))}</b>. Снимок за '
             f'{ru_date(last)}, обновляется каждый день.</p>')

    blocks = et_block(cur, prv, names, order, ET_LEVERS,
                      "Рычаги результата",
                      "Эти параметры проверены на связь с покупкой вне выборки "
                      "(этап 3 слепка): плотность и настойчивость дают "
                      "конверсию в разы, следующий шаг и техники подтверждены "
                      "на зеркальной выборке разговоров. «Техники продаж» — "
                      "пока прокси по имеющимся полям, новые поля разборщика "
                      "копятся с 25.08.")
    blocks += et_block(cur, prv, names, order, ET_DISC,
                       "Дисциплина",
                       "Порядок в работе: скорость, брошенные, гигиена CRM, "
                       "обещания. Связь с конверсией на вызревших заявках не "
                       "подтверждена (у скорости — даже перевёрнута, вероятен "
                       "конфаундер источника), поэтому цену разрывов здесь не "
                       "считаем — но это лицо отдела перед клиентом.")
    blocks += et_block(cur, prv, names, order, ET_MONITOR,
                       "Следствия интереса и результат",
                       "Растянутость диалога и длина разговора — следствие "
                       "интереса клиента, а не действие менеджера: наблюдаем, "
                       "не давим. Конверсия — по вызревшим заявкам 45–135 "
                       "дней, сегодняшняя работа отразится в ней через "
                       "полтора месяца.")

    cards = []
    for uid in order:
        gaps = et_gaps(cur, names, uid)
        if not gaps:
            body = ('<p class="sm">Заметных разрывов с эталоном по значимым '
                    'параметрам нет.</p>')
        else:
            body = "".join(
                f'<div class="gap-i"><span class="gap-n">{i+1}</span>'
                f'<b>{e(g["label"])}</b>: у него {et_fmt(g["param"], g["v"])}'
                f'{e(g["unit"])} — эталон {et_fmt(g["param"], g["best"])}'
                f'{e(g["unit"])} ({e((g["who"].split() or [""])[0])})'
                f'<span class="gap-price">{et_price_text(g)}</span></div>'
                for i, g in enumerate(gaps))
        cards.append(f'<section class="card"><div class="et-name" '
                     f'style="margin-bottom:6px">{e(names[uid])}</div>'
                     + body + '</section>')

    outro = ('<p class="foot">Как это считается: снимок '
             '<code>manager_snapshot</code> раз в день; только пригодные '
             'номера, без PARTNER, окно звонков от первой карточки номера, '
             'дубли менеджеру не в вину. Цена разрыва — консервативно: '
             'половина фактической разницы конверсий вне выборки, вилка от '
             'четверти до полной; самоотбор не вычищен, истинную цену '
             'покажет только эксперимент. Это инструмент роста, не рейтинг '
             'и не основа премий.</p>')

    return (intro + blocks
            + '<h2>Разрывы и цена</h2><div class="et-cards">'
            + "".join(cards) + '</div>' + outro)


def mgr_gaps_block(conn, uid):
    """Личные топ-3 разрыва на дашборде менеджера. Не рейтинг: только свои
    точки роста и пример эталона."""
    try:
        last, prev, cur, prv, names = et_data(conn)
    except Exception:
        return ""
    if not last or uid not in names:
        return ""
    gaps = et_gaps(cur, names, uid)
    if not gaps:
        return ""
    items = []
    for i, g in enumerate(gaps):
        who = e((g["who"].split() or ["лучшего"])[0]) if g["who"] else "лучшего"
        items.append(
            f'<div class="gap-i"><span class="gap-n">{i+1}</span>'
            f'<b>{e(g["label"])}</b>: у {who} — '
            f'{et_fmt(g["param"], g["best"])}{e(g["unit"])}, у тебя — '
            f'{et_fmt(g["param"], g["v"])}{e(g["unit"])}.'
            f'<span class="gap-price">{et_price_text(g)}</span></div>')
    return ('<h2>Три точки роста</h2><section class="card" '
            'style="max-width:560px">' + "".join(items)
            + '<p class="crit-f" style="margin-top:12px">Сравнение с лучшим '
            'в отделе по каждому параметру за 30 дней, с поправкой на '
            'качество достающихся заявок. Это инструмент роста — не рейтинг '
            'и не премии.</p></section>')

'''
anchor = 'ROP_PAGES = {"kontrol": page_kontrol, "menedzhery": page_managers,\n             "slepok": page_slepok}'
assert anchor in src, "rop_pages anchor"
src = src.replace(anchor, code + 'ROP_PAGES = {"kontrol": page_kontrol, "menedzhery": page_managers,\n             "slepok": page_slepok, "etalon": page_etalon}', 1)

# 4. Сборка бандлов -----------------------------------------------------------
old = '''    "all": {"admin": [("hub", "index.html")],
            "rop": [("menedzhery", "index.html"), ("slepok", "slepok.html")],'''
new = '''    "all": {"admin": [("hub", "index.html")],
            "rop": [("menedzhery", "index.html"), ("slepok", "slepok.html"),
                    ("etalon", "etalon.html")],'''
assert old in src, "pageset anchor"
src = src.replace(old, new, 1)

# 5. Личные разрывы на дашборде менеджера -------------------------------------
old = '''      "иначе он потеряется."}</p>
</section>"""'''
new = '''      "иначе он потеряется."}</p>
</section>""" + mgr_gaps_block(conn, uid)'''
assert old in src, "mgr anchor"
src = src.replace(old, new, 1)

io.open(P, "w", encoding="utf-8").write(src)
print("dash.py patched:", len(src), "bytes")
