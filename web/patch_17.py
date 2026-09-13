"""Столбец «Всего открыто» убран из таблицы «Сейчас» (РОП и менеджер).

Просьба Тимофея 24.08. Сумма дублировала раскладку по статусам и мешала
читать строку. Запрос NOW_SQL оставлен как есть — первая цифра просто
не выводится, чтобы не трогать индексы статусов.
"""
import io

P = "/opt/ropbot/web/dash.py"
src = io.open(P, encoding="utf-8").read()


def swap(old, new, tag):
    global src
    if old not in src:
        raise SystemExit(f"не нашёл [{tag}]:\n{old[:160]}")
    src = src.replace(old, new, 1)


swap('''    trs, tot = [], [0] * 9
    for u, name in mgrs:
        cols = queue.get(u, (0,) * 9)
        for i, v in enumerate(cols):
            tot[i] += v''',
     '''    # Первая цифра запроса — «Всего открыто», не показываем (просьба 24.08).
    trs, tot = [], [0] * 8
    for u, name in mgrs:
        cols = queue.get(u, (0,) * 9)[1:]
        for i, v in enumerate(cols):
            tot[i] += v''',
     "now-tot")

swap('''        def cell(v, st, bold=False, _u=u):
            if not v:
                return '<td class="num">·</td>'
            txt = f"<b>{v}</b>" if bold else str(v)
            href = crm_list(_u, st or OPEN_STATUSES)''',
     '''        def cell(v, st, _u=u):
            if not v:
                return '<td class="num">·</td>'
            txt = str(v)
            href = crm_list(_u, st or OPEN_STATUSES)''',
     "now-cell")

swap('''            + "".join(cell(v, STATUS_COLS[i], bold=(i == 0))
                      for i, v in enumerate(cols))''',
     '''            + "".join(cell(v, STATUS_COLS[i + 1])
                      for i, v in enumerate(cols))''',
     "now-cells")

swap('''  <table><thead><tr><th>{e(first_col)}</th>
    {''.join(f'<th>{h}</th>' for h in NOW_HEAD)}<th>Скрипт</th></tr></thead>''',
     '''  <table><thead><tr><th>{e(first_col)}</th>
    {''.join(f'<th>{h}</th>' for h in NOW_HEAD[1:])}<th>Скрипт</th></tr></thead>''',
     "now-head")

swap('''  <p class="crit-f" style="margin-top:10px">Каждый столбец — <b>статус лида
    в Битриксе</b>, цифры сходятся с фильтром в CRM. «Всего открыто» — сумма
    всех незакрытых заявок; остальные столбцы её раскладывают. Фильтров нет —
    можно сверять с CRM напрямую.''',
     '''  <p class="crit-f" style="margin-top:10px">Каждый столбец — <b>статус лида
    в Битриксе</b>, цифры сходятся с фильтром в CRM. Вместе они и есть все
    незакрытые заявки менеджера. Фильтров нет —
    можно сверять с CRM напрямую.''',
     "now-note")

io.open(P, "w", encoding="utf-8").write(src)
compile(src, "dash.py", "exec")
print("готово, синтаксис чист")
