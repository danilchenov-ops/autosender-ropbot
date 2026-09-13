"""Цифры дашборда — ссылки в CRM с готовым фильтром. Столбец «Без звонка» убран.

Просьба Тимофея 23.08: жму на «22» у Жернова в «В работе» — открывается список
лидов Битрикса, отфильтрованный по этому менеджеру и этому статусу.

Ссылки собираются на список лидов:
    /crm/lead/list/?apply_filter=Y&ASSIGNED_BY_ID[]=<uid>&STATUS_ID[]=<статус>
Для «Всего открыто» перечисляются все незакрытые статусы; для конверсии
и продаж добавляется диапазон дат (DATE_CREATE / DATE_CLOSED).

Таблицы звонков (вечера, выходные) ссылок не получают: телефония Битрикса
не фильтруется по часу и дню недели.

Разовый скрипт.
"""
import io

P = "/opt/ropbot/web/dash.py"
src = io.open(P, encoding="utf-8").read()


def swap(old, new, tag):
    global src
    if old not in src:
        raise SystemExit(f"не нашёл [{tag}]:\n{old[:130]}")
    src = src.replace(old, new, 1)


# ── 1. хелпер ссылок и CSS ──────────────────────────────────────────────────
swap('''def rows(cur, sql, params, cols):''',
     '''# Незакрытые статусы лида (semantics IS NULL в crm_dict)
OPEN_STATUSES = ["NEW", "32", "IN_PROCESS", "8", "10", "11", "12", "35", "13",
                 "UC_0TYV86"]


def crm_list(uid, statuses=None, date_field=None, d_from=None, d_to=None):
    """Ссылка на список лидов Битрикса с готовым фильтром."""
    p = [f"{B24}/crm/lead/list/?apply_filter=Y", f"ASSIGNED_BY_ID[]={uid}"]
    for s in (statuses or []):
        p.append(f"STATUS_ID[]={s}")
    if date_field and d_from and d_to:
        p.append(f"{date_field}_datesel=RANGE")
        p.append(f"{date_field}_from={d_from}")
        p.append(f"{date_field}_to={d_to}")
    return "&".join(p)


def rows(cur, sql, params, cols):''', "helper")

swap('.hublist a:hover{text-decoration:underline}',
     '''.hublist a:hover{text-decoration:underline}
td a.q{color:inherit;text-decoration:none;border-bottom:1px dotted var(--ink3)}
td a.q:hover{color:var(--blue);border-bottom-color:var(--blue)}
.heat a.q{border-bottom:0}
.heat a.q:hover{color:var(--blue)}''', "css")

# ── 2. heat() умеет ссылку ──────────────────────────────────────────────────
swap('''def heat(value, vmax, text, sub=None):
    """Ячейка-теплокарта: одна синяя шкала, значение всегда подписано."""
    if value is None:
        return '<td class="dim">—</td>'
    a = 0.0 if not vmax else min(1.0, value / vmax)
    return (f'<td class="heat num"><i style="opacity:{a * 0.42:.2f}"></i>'
            f'<span>{text}'
            + (f'<span class="sub2">{sub}</span>' if sub else '')
            + '</span></td>')''',
     '''def heat(value, vmax, text, sub=None, href=None):
    """Ячейка-теплокарта: одна синяя шкала, значение всегда подписано."""
    if value is None:
        return '<td class="dim">—</td>'
    a = 0.0 if not vmax else min(1.0, value / vmax)
    body = (f'{text}' + (f'<span class="sub2">{sub}</span>' if sub else ''))
    if href:
        body = (f'<a class="q" href="{href}" target="_blank" '
                f'rel="noopener">{body}</a>')
    return (f'<td class="heat num"><i style="opacity:{a * 0.42:.2f}"></i>'
            f'<span>{body}</span></td>')''', "heat")

# ── 3. «Сейчас»: ссылки, без «Без звонка» ───────────────────────────────────
swap('''    trs = []
    tot = [0] * 9
    for u, name in mgrs:
        (n, cold, new, tocall, inwork, postponed,
         passport, auc, contract, prepay) = queue.get(u, (0,) * 10)
        # порядок столбцов = порядок накопления итога
        cols = (n, new, tocall, inwork, postponed, passport, auc, contract, prepay)
        for i, v in enumerate(cols):
            tot[i] += v
        sc = sc_by.get(u)
        sc_txt = num(sum(sc) / len(sc), 1) if sc and len(sc) >= 15 else "—"
        cell = lambda v: f'<td class="num">{v if v else "·"}</td>'
        trs.append(
            f'<tr><td class="mname">{e(short(name))}</td>'
            f'<td class="num"><b>{n}</b></td>'
            + "".join(cell(v) for v in cols[1:])
            + f'<td class="num {"hot" if cold > 20 else ""}">{cold}</td>'
              f'<td class="num">{sc_txt}</td></tr>')
    tot_cold = sum(queue.get(u, (0, 0))[1] for u, _ in mgrs)
    tfoot = ("".join(f'<td class="num"><b>{v}</b></td>' for v in tot)
             + f'<td class="num"><b>{tot_cold}</b></td><td class="num">—</td>')''',
     '''    # столбец -> какие статусы показать в CRM по клику
    STATUS_COLS = [None, ["NEW"], ["32"], ["IN_PROCESS"], ["35"], ["8"],
                   ["12"], ["10"], ["11"]]
    trs = []
    tot = [0] * 9
    for u, name in mgrs:
        (n, cold, new, tocall, inwork, postponed,
         passport, auc, contract, prepay) = queue.get(u, (0,) * 10)
        # порядок столбцов = порядок накопления итога
        cols = (n, new, tocall, inwork, postponed, passport, auc, contract, prepay)
        for i, v in enumerate(cols):
            tot[i] += v
        sc = sc_by.get(u)
        sc_txt = num(sum(sc) / len(sc), 1) if sc and len(sc) >= 15 else "—"

        def cell(v, st, bold=False):
            if not v:
                return '<td class="num">·</td>'
            txt = f"<b>{v}</b>" if bold else str(v)
            href = crm_list(u, st or OPEN_STATUSES)
            return (f'<td class="num"><a class="q" href="{href}" '
                    f'target="_blank" rel="noopener">{txt}</a></td>')

        trs.append(
            f'<tr><td class="mname">{e(short(name))}</td>'
            + "".join(cell(v, STATUS_COLS[i], bold=(i == 0))
                      for i, v in enumerate(cols))
            + f'<td class="num">{sc_txt}</td></tr>')
    tfoot = ("".join(f'<td class="num"><b>{v}</b></td>' for v in tot)
             + '<td class="num">—</td>')''', "now-rows")

swap('''  <table><thead><tr><th>Менеджер</th><th>Всего открыто</th>
    <th>Не обработан</th><th>Позвонить</th><th>В работе</th><th>Отложен</th>
    <th>Ждём паспорт</th><th>На торгах</th><th>Ждём договор</th>
    <th>Ждём предоплату</th><th>Без звонка</th><th>Скрипт</th></tr></thead>''',
     '''  <table><thead><tr><th>Менеджер</th><th>Всего открыто</th>
    <th>Не обработан</th><th>Позвонить</th><th>В работе</th><th>Отложен</th>
    <th>Ждём паспорт</th><th>На торгах</th><th>Ждём договор</th>
    <th>Ждём предоплату</th><th>Скрипт</th></tr></thead>''', "now-head")

swap('''    можно сверять с CRM напрямую. Суммы в рублях в карточках лидов
    не заполняются, поэтому показано количество заявок. «Без звонка» —
    открытые заявки, по номеру которых не было ни одного исходящего;
    здесь непригодные номера и PARTNER исключены (набирать нечего),
    звонки считаются по номеру клиента. «Скрипт» — средний балл соответствия
    эталонному скрипту за 14 дней по первым разговорам, прочерк — меньше
    15 разобранных.</p>''',
     '''    можно сверять с CRM напрямую. Суммы в рублях в карточках лидов
    не заполняются, поэтому показано количество заявок.
    <b>Любая цифра — ссылка:</b> открывает список лидов в Битриксе,
    отфильтрованный по этому менеджеру и этому статусу.
    «Скрипт» — средний балл соответствия эталонному скрипту за 14 дней
    по первым разговорам, прочерк — меньше 15 разобранных.</p>''', "now-note")

# ── 4. конверсия и продажи: ссылки с диапазоном дат ─────────────────────────
swap('''def dash_blocks(conn, mgrs, now):
    months = last_months(now, MONTHS_BACK)''',
     '''def month_range(key):
    """'2026-05' -> ('01.05.2026', '31.05.2026') — формат дат фильтра Битрикса."""
    y, m = int(key[:4]), int(key[5:])
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
    return f"01.{m:02d}.{y}", f"{last}.{m:02d}.{y}"


def dash_blocks(conn, mgrs, now):
    months = last_months(now, MONTHS_BACK)''', "month-range")

swap('''            leads, won, inwork = v
            pc = 100.0 * won / leads
            tds.append(heat(pc, cmax, f"{num(pc, 2)}%",
                            f"{won} из {leads}" + (f", {inwork} в работе" if inwork else "")))''',
     '''            leads, won, inwork = v
            pc = 100.0 * won / leads
            d1, d2 = month_range(k)
            tds.append(heat(pc, cmax, f"{num(pc, 2)}%",
                            f"{won} из {leads}" + (f", {inwork} в работе" if inwork else ""),
                            href=crm_list(uid, ["CONVERTED"], "DATE_CREATE", d1, d2)))''',
     "conv-link")

swap('''        tds = [heat(closed.get((uid, k)), cl_max, str(closed.get((uid, k), 0)))
               if closed.get((uid, k)) else '<td class="dim">·</td>' for k in keys]''',
     '''        tds = []
        for k in keys:
            v = closed.get((uid, k))
            if not v:
                tds.append('<td class="dim">·</td>')
                continue
            d1, d2 = month_range(k)
            tds.append(heat(v, cl_max, str(v),
                            href=crm_list(uid, ["CONVERTED"], "DATE_CLOSED", d1, d2)))''',
     "closed-link")

swap('''    «Другие ответственные» —
    конвертации вне пятёрки. Таблица слева отвечает на другой вопрос — какая
    доля заявок месяца доведена до сделки.</p>''',
     '''    «Другие ответственные» —
    конвертации вне пятёрки. Таблица слева отвечает на другой вопрос — какая
    доля заявок месяца доведена до сделки. Цифры кликабельны — открывают
    те же заявки в CRM.</p>''', "closed-note")

io.open(P, "w", encoding="utf-8").write(src)
compile(src, "dash.py", "exec")
print("готово, синтаксис чист")
