# -*- coding: utf-8 -*-
"""Патч 09.09.2026: «Аналитика по % конверсии» + гайды по клику на стадию.

1. Подпись над «Сейчас»: клик по цифре — фильтр CRM.
2. «Воронка по менеджерам» → «Аналитика по % конверсии» везде.
3. Клик по цифре стадии (5 стадий) — индивидуальный гайд менеджеру:
   его цифры за 60 дней (как в KPI) против выигранных сделок и лучшего
   в отделе + что делать. Шаблон, без LLM (решение Тимофея 09.09).
"""
import shutil
P = "/opt/ropbot/web/dash.py"
shutil.copy(P, P + ".bak-guide")
s = open(P, encoding="utf-8").read()


def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (s.count(a), a[:80])
    s = s.replace(a, b)


# ── 1. подпись над «Сейчас» ─────────────────────────────────────────────────
rep('''    return f"""
<h2>Сейчас</h2>
<section class="card">
  <table><thead><tr><th>{e(first_col)}</th>''',
'''    return f"""
<h2>Сейчас <span class="h2-hint">Клик по цифре открывает в CRM фильтр
  с этими лидами; «Внимание» и «Толкнуть» раскрывают список прямо здесь.</span></h2>
<section class="card">
  <table><thead><tr><th>{e(first_col)}</th>''')

rep("th.hint{cursor:help;",
    ".h2-hint{font:400 13px/1.35 inherit;color:var(--ink3);text-transform:none;"
    "letter-spacing:0;margin-left:10px;vertical-align:middle}\n"
    "th.hint{cursor:help;")

# ── 2. переименование ────────────────────────────────────────────────────────
rep("""    return ('<h2>Воронка по менеджерам</h2><section class="card">'""",
    """    return ('<h2>Аналитика по % конверсии <span class="h2-hint">Клик '
            'по цифре стадии — подсказка, как улучшить показатель.</span></h2>'
            '<section class="card">'""")

# ── 3. гайды ─────────────────────────────────────────────────────────────────
GUIDE = r'''
# ── Гайды к «Аналитике по % конверсии» (решение Тимофея 09.09.2026) ───────────
# Клик по цифре стадии открывает менеджеру индивидуальную подсказку: его
# цифры за GUIDE_DAYS дней (как в KPI) против выигранных сделок и лучшего
# в отделе, и что менять. Текст — шаблон с живыми цифрами, без LLM.
# Видят все всех (чужой гайд — способ увидеть, как делает лучший).
# Считается один раз на сборку (_CACHE) для всех окон таблицы.
GUIDE_DAYS = 60
GUIDE_WON_DAYS = 150       # выигранных мало — окно шире, как в Таймингах
GUIDE_MIN_N = 3            # меньше окон — цифры не показываем, только правило

# Стадия таблицы -> как считать. st=None — шаг 0 (заявка → Паспорт),
# st='12' — шаг 4 (торги → продажа), остальное — TIMINGS_SLICES.
GUIDE_STEPS = {
    "s1": {"title": "Заявка → «Паспорт»", "st": None},
    "s2": {"title": "«Паспорт» → «Договор»", "st": "8",
           "succ": ("10", "11", "12", "13", "CONVERTED")},
    "s3": {"title": "«Договор» → «Оплата обеспечительного»", "st": "10",
           "succ": ("11", "12", "13", "CONVERTED")},
    "s4": {"title": "«Оплата обеспечительного» → «На торгах»", "st": "11",
           "succ": ("12", "13", "CONVERTED")},
    "s5": {"title": "«На торгах» → «Продажа»", "st": "12"},
}

# Что делать — из наших же замеров (см. комментарии у _tm_step0_block,
# TIMINGS_W_SPEED, _tm_step4_block). Цифры менеджера подставляются выше.
GUIDE_RULES = {
    "s1": [
        "Настойчивость решает: при 5 и более звонках до «Паспорта» доходит "
        "вдвое больше заявок, чем при одном-двух. Не бросайте после первого "
        "недозвона — ставьте дело на следующий день и звоните снова.",
        "Скорость тоже работает: звонок в первый час даёт вдвое больше "
        "продвижений, чем звонок через день. Новая заявка в рабочее "
        "время — телефон в руки в первые 15 минут.",
        "В разговоре — три вещи: бюджет, срок, следующий шаг с датой. "
        "Без них заявка не доходит до паспорта.",
    ],
    "s2": [
        "Главное — скорость первого звонка после смены статуса: звонок "
        "в первые 15 минут даёт 66% продаж, через сутки и позже — 10%. "
        "Поставили «Паспорт» — тут же договорились, когда придёт скан.",
        "Каждой заявке в статусе — хотя бы один звонок. Количество сверх "
        "одного-двух продажу не добавляет, а вот заявка вовсе без звонка "
        "почти всегда теряется.",
        "Ставьте дело на конкретное время «получить паспорт» — заявка не "
        "должна висеть в статусе без даты следующего касания.",
    ],
    "s3": [
        "После «Договор» первый звонок — в течение часа: подтвердить, что "
        "договор получен, и назвать срок и сумму обеспечительного платежа.",
        "Один-два содержательных звонка достаточно; важно, чтобы звонок "
        "был у каждой заявки. Проверьте заявки без звонка — они и есть потери.",
        "Снимайте возражения про деньги прямо: платёж возвратный, "
        "реквизиты от юрлица. Не ждите, пока клиент спросит сам.",
    ],
    "s4": [
        "После «Оплата обеспечительного» звоните сразу: подтвердить "
        "получение, объяснить, что дальше — подбор лотов и выход на торги.",
        "Здесь заявка уже тёплая, потери — от тишины. Каждой заявке "
        "звонок в первый час и дело с датой следующего контакта.",
    ],
    "s5": [
        "На торгах решают сопровождающие звонки: с ними до продажи доходит "
        "94% заявок, без них — 82%. Норма — 3–5 звонков за время торгов, "
        "по каждому лоту и результату.",
        "Скорость первого звонка после выхода на торги на продажу не влияет "
        "— важен ритм: звонок раз в 1–2 дня, а не один в начале.",
        "Заявка «На торгах» дольше 10 дней без звонка — столбец «Толкнуть» "
        "в таблице «Сейчас»: позвонить или честно закрыть.",
    ],
}


def _gd_rows0(conn, since, until):
    """Строки шага 0 (заявка → Паспорт), как в _tm_step0_block."""
    cols = ("lead_id t0 t_end next_stage next_name title phone uid "
            "mname mlast calls talks dogovor fc lc bq ta ss").split()
    rows = [dict(zip(cols, r)) for r in conn.execute(
        S0_SQL, {"since": since, "until": until}).fetchall()]
    dept = _tm_dept5(conn)
    out = []
    for r in rows:
        if r["uid"] not in dept:
            continue
        r["first_call"] = r["fc"]
        r["first_min"] = ((r["fc"] - r["t0"]).total_seconds() / 60
                          if r["fc"] else None)
        r["gap"] = ((r["lc"] - r["fc"]).total_seconds() / 60 / (r["calls"] - 1)
                    if r["calls"] >= 2 else None)
        wm = ((r["t_end"] - r["t0"]).total_seconds() / 60 if r["t_end"] else None)
        r["valid"] = wm is None or wm >= TIMINGS_MIN_WINDOW
        r["reached"] = r["next_stage"] in ("8", "10", "11", "12", "13", "CONVERTED")
        out.append(r)
    return out


def _gd_rows4(conn, since, until):
    """Строки шага 4 (торги → продажа), как в _tm_step4_block."""
    cols = ("lead_id t0 t_end next_stage next_name title phone uid "
            "mname mlast calls_ts talks").split()
    rows = [dict(zip(cols, r)) for r in conn.execute(
        S4_SQL, {"since": since, "until": until}).fetchall()]
    dept = _tm_dept5(conn)
    out = []
    for r in rows:
        if r["uid"] not in dept:
            continue
        ts = r["calls_ts"] or []
        r["calls"] = len(ts)
        r["first_call"] = ts[0] if ts else None
        r["first_min"] = ((ts[0] - r["t0"]).total_seconds() / 60 if ts else None)
        gaps = [(b - a).total_seconds() / 60 for a, b in zip(ts, ts[1:])]
        r["gap"] = sum(gaps) / len(gaps) if gaps else None
        wm = ((r["t_end"] - r["t0"]).total_seconds() / 60 if r["t_end"] else None)
        r["valid"] = wm is None or wm >= TIMINGS_MIN_WINDOW
        r["reached"] = r["next_stage"] == "13"
        out.append(r)
    return out


def _gd_stat(rr):
    """Сводка по строкам: медиана первого звонка, звонков на заявку,
    интервал, без звонка, доведено."""
    vv = [r for r in rr if r["valid"]]
    firsts = [r["first_min"] for r in vv if r["first_min"] is not None]
    gaps = [r["gap"] for r in vv if r.get("gap") is not None]
    return {
        "n": len(rr), "valid_n": len(vv),
        "med_first": _tm_median(firsts),
        "avg_calls": (sum(r["calls"] for r in vv) / len(vv)) if vv else None,
        "med_gap": _tm_median(gaps) if gaps else None,
        "no_call": sum(1 for r in vv if not r["first_call"] and r["t_end"] is not None),
        "reached": sum(1 for r in rr if r["reached"]),
    }


def guide_data(conn, now):
    """{step: {"by": {uid: stat}, "dept": stat, "won": stat, "best": uid}}"""
    if "guide" in _CACHE:
        return _CACHE["guide"]
    since = now - dt.timedelta(days=GUIDE_DAYS)
    since_won = now - dt.timedelta(days=GUIDE_WON_DAYS)
    until = now + dt.timedelta(days=1)
    out = {}
    for key, cfg_ in GUIDE_STEPS.items():
        if cfg_["st"] is None:
            rows = _gd_rows0(conn, since, until)
            rows_w = _gd_rows0(conn, since_won, until)
            won = [r for r in rows_w if r["reached"]]
        elif cfg_["st"] == "12":
            rows = _gd_rows4(conn, since, until)
            rows_w = _gd_rows4(conn, since_won, until)
            won = [r for r in rows_w if r["reached"]]
        else:
            rows = _tm_rows(conn, cfg_["st"], cfg_["succ"], since)
            for r in rows:
                r["gap"] = None
            won = _tm_rows(conn, cfg_["st"], cfg_["succ"], since_won, won=True)
            for r in won:
                r["gap"] = None
        by = {}
        for r in rows:
            by.setdefault(r["uid"], []).append(r)
        by = {u: _gd_stat(rr) for u, rr in by.items()}
        # лучший — по доле доведённых при достаточной выборке
        cands = [(u, v) for u, v in by.items() if v["n"] >= 8]
        best = max(cands, key=lambda kv: kv[1]["reached"] / kv[1]["n"])[0] if cands else None
        out[key] = {"by": by, "dept": _gd_stat(rows), "won": _gd_stat(won),
                    "best": best}
    _CACHE["guide"] = out
    return out


def _gd_pct(st):
    return 100.0 * st["reached"] / st["n"] if st and st["n"] else None


def _gd_cmp_line(label, mine, ref, fmt, better_low=True):
    """Строка сравнения «у вас … · у выигранных …» с оценкой."""
    if mine is None or ref is None:
        return ""
    worse = (mine > ref * 1.25) if better_low else (mine < ref * 0.8)
    cls = "gd-bad" if worse else "gd-ok"
    return (f'<div class="gd-cmp"><span>{label}</span>'
            f'<b class="{cls}">{fmt(mine)}</b>'
            f'<span class="dim">у выигранных сделок {fmt(ref)}</span></div>')


def guide_box(key, uid, name, names, gd):
    """Разметка одного гайда: менеджер × стадия."""
    step = GUIDE_STEPS[key]
    d = gd[key]
    me = d["by"].get(uid)
    won, dept, best = d["won"], d["dept"], d["best"]
    hdr = (f'<div class="gd-h">{e(name)} · {step["title"]}'
           f'<span class="dim"> · за {GUIDE_DAYS} дней, как в KPI</span></div>')
    body = []
    if not me or me["n"] < GUIDE_MIN_N:
        body.append(f'<p class="gd-p">За {GUIDE_DAYS} дней у {e(name)} '
                    f'в этой стадии {me["n"] if me else 0} '
                    f'{plural(me["n"] if me else 0, "заявка", "заявки", "заявок")}'
                    ' — мало, чтобы судить по цифрам. Правила ниже общие.</p>')
    else:
        p_me, p_dep = _gd_pct(me), _gd_pct(dept)
        line = (f'<p class="gd-p">Доведено дальше: <b>{me["reached"]} из {me["n"]}</b>'
                + (f' ({p_me:.0f}%)' if p_me is not None else "")
                + (f', в отделе {p_dep:.0f}%' if p_dep is not None else ""))
        if best and best != uid and best in d["by"]:
            pb = _gd_pct(d["by"][best])
            line += f', у лучшего ({e(short(names.get(best, "")))}) {pb:.0f}%'
        body.append(line + ".</p>")
        cmp_ = []
        cmp_.append(_gd_cmp_line("Первый звонок после стадии, медиана",
                                 me["med_first"], won["med_first"], _tm_min_txt))
        cmp_.append(_gd_cmp_line("Звонков на заявку", me["avg_calls"], won["avg_calls"],
                                 lambda x: f"{x:.1f}", better_low=False))
        if me.get("med_gap") is not None and won.get("med_gap") is not None:
            cmp_.append(_gd_cmp_line("Интервал между звонками", me["med_gap"],
                                     won["med_gap"], _tm_min_txt))
        if me["no_call"]:
            cmp_.append(f'<div class="gd-cmp"><span>Заявок без единого звонка</span>'
                        f'<b class="gd-bad">{me["no_call"]}</b>'
                        f'<span class="dim">каждая — почти наверняка потеря</span></div>')
        body.append("".join(c for c in cmp_ if c))
    body.append('<div class="gd-do">Что делать</div><ul class="gd-ul">'
                + "".join(f"<li>{r}</li>" for r in GUIDE_RULES[key]) + "</ul>")
    return (f'<div class="gd" id="gd-{uid}-{key}" hidden>{hdr}'
            + "".join(body) + "</div>")


def guide_block(conn, names, now):
    """Все гайды страницы (скрыты) + JS переключения."""
    gd = guide_data(conn, now)
    boxes = [guide_box(k, u, nm, names, gd)
             for u, nm in names.items() for k in GUIDE_STEPS]
    return ('<div class="gd-wrap">' + "".join(boxes) + "</div>"
            + """<script>
(function(){
  var cs = document.querySelectorAll('a.gd-c');
  for (var i = 0; i < cs.length; i++) cs[i].addEventListener('click', function(ev){
    ev.preventDefault();
    var id = this.getAttribute('data-gd'), box = document.getElementById(id);
    if (!box) return;
    var open = box.hidden;
    var all = document.querySelectorAll('.gd');
    for (var j = 0; j < all.length; j++) all[j].hidden = true;
    var on = document.querySelectorAll('a.gd-c.on');
    for (var j = 0; j < on.length; j++) on[j].classList.remove('on');
    if (open) { box.hidden = false; this.classList.add('on');
      box.scrollIntoView({block: 'nearest', behavior: 'smooth'}); }
  });
})();
</script>""")

'''
rep("def _vz_funnel_one(conn, names, colors, since, until, label):",
    GUIDE + "\ndef _vz_funnel_one(conn, names, colors, since, until, label):")

# ячейка стадии — ссылка на гайд
rep('''    def cell(v, key, prev_key, dept_pct):
        p = pct(v[key], v[prev_key])
        if p is None:
            return '<td class="num dim">—</td>'
        cls = ""
        if dept_pct and v[prev_key] >= 8:
            cls = ("tm-good" if p >= dept_pct * 1.2
                   else "tm-bad" if p <= dept_pct * 0.8 else "")
        return (f'<td class="num">{v[key]}'
                f'<span class="sub2 {cls}">{p:.0f}%</span></td>')

    order = sorted(rows, key=lambda u: -(pct(rows[u]["s5"], rows[u]["n"]) or 0))
    trs = []
    for u in order:
        v = rows[u]
        tds = "".join(cell(v, k, pk, pct(dept[k], dept[pk]))
                      for k, _, pk in FUNNEL_STEPS)''',
'''    def cell(v, key, prev_key, dept_pct, u):
        p = pct(v[key], v[prev_key])
        if p is None:
            return '<td class="num dim">—</td>'
        cls = ""
        if dept_pct and v[prev_key] >= 8:
            cls = ("tm-good" if p >= dept_pct * 1.2
                   else "tm-bad" if p <= dept_pct * 0.8 else "")
        # клик — гайд менеджеру по этой стадии (решение Тимофея 09.09)
        return (f'<td class="num"><a class="gd-c" href="#" '
                f'data-gd="gd-{u}-{key}" title="как улучшить">{v[key]}'
                f'<span class="sub2 {cls}">{p:.0f}%</span></a></td>')

    order = sorted(rows, key=lambda u: -(pct(rows[u]["s5"], rows[u]["n"]) or 0))
    trs = []
    for u in order:
        v = rows[u]
        tds = "".join(cell(v, k, pk, pct(dept[k], dept[pk]), u)
                      for k, _, pk in FUNNEL_STEPS)''')

# гайды один раз под всеми окнами
rep('''            + "".join(secs) +
            '<p class="vz-note">Стадия считается по максимальной достигнутой: ''',
'''            + "".join(secs)
            + guide_block(conn, names, now) +
            '<p class="vz-note">Стадия считается по максимальной достигнутой: ''')

# CSS
rep(".vz-note{font-size:13px;color:var(--ink2);max-width:900px;margin:10px 0 0}",
    ".vz-note{font-size:13px;color:var(--ink2);max-width:900px;margin:10px 0 0}\n"
    "a.gd-c{color:inherit;text-decoration:none;display:inline-block;"
    "border-bottom:1px dotted var(--ink3);cursor:pointer}\n"
    "a.gd-c .sub2{display:block}\n"
    "a.gd-c.on{border-bottom:2px solid var(--blue)}\n"
    ".gd{margin-top:14px;padding:14px 16px;border:1px solid var(--line);"
    "border-radius:10px;background:var(--bg);max-width:820px}\n"
    ".gd-h{font-weight:700;font-size:15px;margin-bottom:6px}\n"
    ".gd-p{margin:6px 0;font-size:14px}\n"
    ".gd-cmp{display:grid;grid-template-columns:minmax(200px,1fr) auto 1fr;"
    "gap:4px 14px;align-items:baseline;font-size:13.5px;padding:3px 0}\n"
    ".gd-cmp b.gd-bad{color:var(--bad)}\n.gd-cmp b.gd-ok{color:var(--good)}\n"
    ".gd-do{font-weight:700;margin-top:10px;font-size:13px;color:var(--ink2);"
    "text-transform:uppercase;letter-spacing:.04em}\n"
    ".gd-ul{margin:6px 0 0;padding-left:18px;font-size:13.5px;line-height:1.45}\n"
    ".gd-ul li{margin:4px 0}")

open(P, "w", encoding="utf-8").write(s)
print("ok")
