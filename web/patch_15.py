"""Запланированные дела и касания за месяц + точный список дел на сегодня.

Просьба Тимофея 24.08:
 1. «Контроль отдела» — под таблицей «Сейчас» вторая таблица по менеджерам:
    сколько дел поставлено в CRM начиная с сегодняшнего дня; каждое 1-е число
    окно сдвигается на начало месяца и счётчики обнуляются.
 2. Туда же — средние касания с клиентами, то же окно, тот же сброс.
 3. На дашборде менеджера цифра «Дела на сегодня» должна вести именно
    на эти лиды, а не на общий список просроченных.

По п.3: фильтр списка лидов Битрикса умеет ASSIGNED_BY_ID и STATUS_ID
(проверено), но фильтра «дело со сроком сегодня» в URL нет — ACTIVITY_COUNTER
даёт все просроченные, это и была ошибка. Поэтому цифра раскрывает точный
список этих лидов прямо на странице, каждая строка — ссылка на карточку.
Список строится из тех же строк, что дали цифру, значит расходиться не может.

Разовый скрипт.
"""
import io

P = "/opt/ropbot/web/dash.py"
src = io.open(P, encoding="utf-8").read()


def swap(old, new, tag):
    global src
    if old not in src:
        raise SystemExit(f"не нашёл [{tag}]:\n{old[:160]}")
    src = src.replace(old, new, 1)


# ── 1. запросы окна месяца и точный список дел ──────────────────────────────
swap('''def now_data(conn):
    """Разбивка открытых заявок по статусам + балл скрипта за 14 дней."""''',
     '''# Точный список лидов, у которых дело со сроком сегодня — те же строки,
# из которых складывается цифра на дашборде менеджера.
TODO_LIST_SQL = """
  SELECT l.id, l.title, min(a.end_time AT TIME ZONE %(tz)s) AS due
    FROM activities a
    JOIN leads l ON l.id = a.owner_id
   WHERE a.owner_type_id = 1
     AND NOT a.completed
     AND a.provider_id = 'CRM_TODO'
     AND a.end_time IS NOT NULL
     AND (a.end_time AT TIME ZONE %(tz)s)::date
         = (now() AT TIME ZONE %(tz)s)::date
     AND a.responsible_id = %(uid)s
   GROUP BY 1, 2
   ORDER BY 3
"""


def todo_list(conn, uid):
    return conn.execute(TODO_LIST_SQL, {"tz": TZ, "uid": uid}).fetchall()


# ── Запланированные дела и касания: окно с текущего месяца ──────────────────
# Решение Тимофея 24.08: считаем с сегодняшнего дня, а каждое 1-е число окно
# сдвигается на начало месяца — счётчики обнуляются сами, без ручной правки.
PLAN_START = dt.date(2026, 8, 24)

PLAN_TODO_SQL = f"""
  SELECT a.responsible_id, count(*)
    FROM activities a
   WHERE a.owner_type_id = 1
     AND a.provider_id = 'CRM_TODO'
     AND (a.created AT TIME ZONE %(tz)s) >= %(frm)s
     AND a.responsible_id IN ({WORKING})
   GROUP BY 1
"""

PLAN_CALL_SQL = f"""
  SELECT c.portal_user_id,
         count(*) FILTER (WHERE c.duration > 0),
         count(DISTINCT c.phone_e164) FILTER (WHERE c.duration > 0)
    FROM calls c
   WHERE c.direction = 'out'
     AND (c.call_start AT TIME ZONE %(tz)s) >= %(frm)s
     AND c.portal_user_id IN ({WORKING})
   GROUP BY 1
"""


def plan_data(conn, now):
    """(дата начала окна, {uid: дел}, {uid: (разговоров, клиентов)})."""
    if "plan" in _CACHE:
        return _CACHE["plan"]
    start = max(now.date().replace(day=1), PLAN_START)
    frm = dt.datetime.combine(start, dt.time())
    todo = dict(conn.execute(PLAN_TODO_SQL, {"tz": TZ, "frm": frm}).fetchall())
    calls = {r[0]: (r[1], r[2]) for r in
             conn.execute(PLAN_CALL_SQL, {"tz": TZ, "frm": frm}).fetchall()}
    _CACHE["plan"] = (start, todo, calls)
    return _CACHE["plan"]


def plan_section(conn, mgrs, now):
    """Таблица «Запланированные дела в этом месяце» — под «Сейчас»."""
    start, todo, calls = plan_data(conn, now)
    trs, t_d, t_t, t_c = [], 0, 0, 0
    for u, name in mgrs:
        d = todo.get(u, 0)
        talks, clients = calls.get(u, (0, 0))
        t_d += d
        t_t += talks
        t_c += clients
        touch = num(talks / clients, 1) if clients else "—"
        trs.append(
            f'<tr><td class="mname">{e(short(name))}</td>'
            f'<td class="num">{"<b>%d</b>" % d if d else "·"}</td>'
            f'<td class="num">{clients or "·"}</td>'
            f'<td class="num">{talks or "·"}</td>'
            f'<td class="num">{touch}</td></tr>')

    avg = num(t_t / t_c, 1) if t_c else "—"
    foot = ('<tfoot><tr><td class="mname">Отдел</td>'
            f'<td class="num"><b>{t_d}</b></td>'
            f'<td class="num"><b>{t_c}</b></td>'
            f'<td class="num"><b>{t_t}</b></td>'
            f'<td class="num"><b>{avg}</b></td></tr></tfoot>')

    since = f"{start.day} {MONTHS[start.month - 1]}"
    return f"""
<h2>Запланированные дела в этом месяце</h2>
<section class="card">
  <table><thead><tr><th>Менеджер</th><th>Поставлено дел</th>
    <th>Клиентов</th><th>Разговоров</th><th>Касаний на клиента</th>
  </tr></thead>
  <tbody>{''.join(trs)}</tbody>{foot}</table>
  <p class="crit-f" style="margin-top:10px">Счёт идёт <b>с {since}</b>
    и обнуляется 1-го числа каждого месяца — цифры за месяц накапливаются
    на глазах, сравнивать менеджеров между собой можно с первого дня.
    <b>Поставлено дел</b> — сколько напоминаний менеджер завёл в карточках
    лидов за это окно (не сколько выполнил). <b>Клиентов</b> — с сколькими
    разными номерами он за это окно поговорил, <b>разговоров</b> — сколько
    состоявшихся исходящих звонков сделал; недозвоны в обе цифры не входят.
    <b>Касаний на клиента</b> — разговоров, делённое на клиентов: 1,0 значит
    «позвонил и забыл», выше — клиента ведут.</p>
</section>
"""


def now_data(conn):
    """Разбивка открытых заявок по статусам + балл скрипта за 14 дней."""''',
     "plan-sql")

# ── 2. таблица на странице РОПа ─────────────────────────────────────────────
swap('''    return now_section(mgrs, queue, sc_by) + f"""
{dash_blocks(conn, mgrs, dt.datetime.now(VLD))}''',
     '''    now = dt.datetime.now(VLD)
    return now_section(mgrs, queue, sc_by) + plan_section(conn, mgrs, now) + f"""
{dash_blocks(conn, mgrs, now)}''',
     "plan-render")

# ── 3. точный список дел на сегодня у менеджера ─────────────────────────────
swap('''    today, overdue = todo_data(conn).get(uid, (0, 0))
    # Счётчик дел в фильтре списка лидов Битрикса
    href = f"{B24}/crm/lead/list/?apply_filter=Y&ASSIGNED_BY_ID[]={uid}&ACTIVITY_COUNTER[]=4"
    num_html = (f'<a class="q" href="{href}" target="_blank" rel="noopener">'
                f'{today}</a>' if today else "0")
    tail = (f'<p class="hero-side">И ещё <b>{overdue}</b> '
            f'{plural(overdue, "лид", "лида", "лидов")} с просроченным делом.</p>'
            if overdue else "")''',
     '''    today, overdue = todo_data(conn).get(uid, (0, 0))
    # Фильтра «дело со сроком сегодня» в URL списка лидов Битрикса нет —
    # ACTIVITY_COUNTER открывал все просроченные. Поэтому цифра раскрывает
    # ровно те лиды, из которых она сложилась; строка — ссылка на карточку.
    lst = ""
    if today:
        items = []
        for lid, title, due in todo_list(conn, uid):
            items.append(
                f'<div class="todo-i"><span class="todo-h">'
                f'{due.strftime("%H:%M")}</span>'
                f'<a href="{crm_link("LEAD", lid)}" target="_blank" '
                f'rel="noopener">{e(title or ("Лид " + str(lid)))}</a></div>')
        lst = ('<details class="todo-d"><summary>Показать список '
               f'({today})</summary><div class="todo-l">'
               + "".join(items) + "</div></details>")
    num_html = f'<span class="hero-num">{today}</span>'
    tail = (f'<p class="hero-side">И ещё <b>{overdue}</b> '
            f'{plural(overdue, "лид", "лида", "лидов")} с просроченным делом.</p>'
            if overdue else "")''',
     "todo-list")

swap('''  <div class="hero"><span class="hero-num">{num_html}</span>
    <span class="hero-of">{plural(today, "лид ждёт", "лида ждут", "лидов ждут")}
      дела сегодня</span></div>
  {tail}
  <p class="crit-f" style="margin-top:14px">Дело — напоминание, которое вы сами
    ставите в карточке лида. Считаются незакрытые дела со сроком на сегодня;
    если на одном лиде несколько дел, лид считается один раз.
    Цифра открывает эти заявки в Битриксе.</p>''',
     '''  <div class="hero">{num_html}
    <span class="hero-of">{plural(today, "лид ждёт", "лида ждут", "лидов ждут")}
      дела сегодня</span></div>
  {lst}
  {tail}
  <p class="crit-f" style="margin-top:14px">Дело — напоминание, которое вы сами
    ставите в карточке лида. Считаются незакрытые дела со сроком на сегодня;
    если на одном лиде несколько дел, лид считается один раз.
    «Показать список» раскрывает ровно эти лиды по времени дела —
    строка ведёт в карточку.</p>''',
     "todo-hero")

# ── 4. стили списка ─────────────────────────────────────────────────────────
swap('''.crit-f{''',
     '''.todo-d{margin-top:12px}
.todo-d summary{cursor:pointer;font-size:14px;color:var(--ink2);
  padding:6px 0;user-select:none}
.todo-l{margin-top:6px;max-height:340px;overflow:auto;
  border-top:1px solid var(--line)}
.todo-i{display:flex;gap:10px;align-items:baseline;padding:6px 0;
  border-bottom:1px solid var(--line);font-size:14px}
.todo-h{flex:0 0 44px;color:var(--ink2);font-variant-numeric:tabular-nums}
.todo-i a{color:var(--ink);text-decoration:none;border-bottom:1px solid var(--line)}
.crit-f{''',
     "todo-css")

io.open(P, "w", encoding="utf-8").write(src)
compile(src, "dash.py", "exec")
print("готово, синтаксис чист")
