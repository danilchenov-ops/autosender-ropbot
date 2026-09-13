import re, shutil
P = "/opt/ropbot/web/dash.py"
shutil.copy(P, P + ".bak-vnimanie")
s = open(P, encoding="utf-8").read()

def rep(old, new, cnt=1):
    global s
    assert s.count(old) == cnt, (s.count(old), old[:80])
    s = s.replace(old, new)

# 1. порядок воронки в NOW_SQL: паспорт → договор → предоплата → торги
rep("""         count(*) FILTER (WHERE l.status_id = '8'),
         count(*) FILTER (WHERE l.status_id = '12'),
         count(*) FILTER (WHERE l.status_id = '10'),
         count(*) FILTER (WHERE l.status_id = '11')
    FROM leads l""",
"""         count(*) FILTER (WHERE l.status_id = '8'),
         count(*) FILTER (WHERE l.status_id = '10'),
         count(*) FILTER (WHERE l.status_id = '11'),
         count(*) FILTER (WHERE l.status_id = '12')
    FROM leads l""")

rep("""STATUS_COLS = [None, ["NEW"], ["32"], ["IN_PROCESS"], ["35"], ["8"],
               ["12"], ["10"], ["11"]]
NOW_HEAD = ["Всего открыто", "Не обработан", "Позвонить", "В работе",
            "Отложен", "Ждём паспорт", "На торгах", "Ждём договор",
            "Ждём предоплату"]
""",
"""STATUS_COLS = [None, ["NEW"], ["32"], ["IN_PROCESS"], ["35"], ["8"],
               ["10"], ["11"], ["12"]]
NOW_HEAD = ["Всего открыто", "Не обработан", "Позвонить", "В работе",
            "Отложен", "Ждём паспорт", "Ждём договор", "Ждём предоплату",
            "На торгах"]

# ── «Внимание» и «Толкнуть» в таблице «Сейчас» (просьба Тимофея 02.09) ──────
# «Внимание» — открытые заявки с составной оценкой от ATTN_SCORE, которые ещё
# не дошли до «На торгах»: сильный клиент, которого ведут медленно.
# «Толкнуть» — заявки, висящие в «На торгах» дольше PUSH_DAYS дней (по последнему
# входу в статус из stage_history; если истории нет — по date_modify лида).
ATTN_SCORE = 60
PUSH_DAYS = 10
ATTN_STATUSES = ["NEW", "32", "IN_PROCESS", "35", "8", "10", "11"]

ATTN_SQL = f\"\"\"
  SELECT l.assigned_by, l.id, l.title, l.phone_e164, m.value, lc.score,
         l.status_id
    FROM leads l
    JOIN lead_composite lc ON lc.lead_id = l.id
    LEFT JOIN lead_marks m ON m.lead_id = l.id
   WHERE l.assigned_by IN ({WORKING})
     AND l.status_semantic = 'P'
     AND l.status_id IN ({','.join("'" + x + "'" for x in ATTN_STATUSES)})
     AND lc.score >= {ATTN_SCORE}
   ORDER BY lc.score DESC, l.date_create
\"\"\"

PUSH_SQL = f\"\"\"
  SELECT l.assigned_by, l.id, l.title, l.phone_e164, m.value,
         extract(epoch FROM now() - coalesce(t.t12, l.date_modify)) / 86400
    FROM leads l
    LEFT JOIN lead_marks m ON m.lead_id = l.id
    LEFT JOIN LATERAL (SELECT max(h.created_time) t12 FROM stage_history h
                        WHERE h.entity_kind = 'lead' AND h.owner_id = l.id
                          AND h.stage_id = '12') t ON TRUE
   WHERE l.assigned_by IN ({WORKING})
     AND l.status_semantic = 'P'
     AND l.status_id = '12'
     AND coalesce(t.t12, l.date_modify) < now() - interval '{PUSH_DAYS} days'
   ORDER BY 6 DESC
\"\"\"


def attn_push(conn):
    \"\"\"({uid: [(id, title, phone, mark, score, status)]},
        {uid: [(id, title, phone, mark, days)]})\"\"\"
    if "attn_push" in _CACHE:
        return _CACHE["attn_push"]
    attn, push = {}, {}
    for uid, *r in conn.execute(ATTN_SQL):
        attn.setdefault(uid, []).append(r)
    for uid, *r in conn.execute(PUSH_SQL):
        push.setdefault(uid, []).append(r)
    _CACHE["attn_push"] = (attn, push)
    return _CACHE["attn_push"]


def attn_push_list(mgrs, attn, push):
    \"\"\"Свёрнутый список заявок из столбцов «Внимание» и «Толкнуть».\"\"\"
    def lead_a(lid, title, phone, mark):
        name = (title or "").strip(" /") or f"Лид {lid}"
        return (f'<a href="{crm_link("LEAD", lid)}" target="_blank" '
                f'rel="noopener">{e(mask_title(name))}</a>'
                + (f' <span class="dim">{e(mark)}</span>' if mark else "")
                + (f'<span class="todo-p">{e(mask(phone))}</span>'
                   if phone else ""))
    blocks, n = [], 0
    for u, name in mgrs:
        a, p = attn.get(u, []), push.get(u, [])
        if not a and not p:
            continue
        n += len(a) + len(p)
        items = []
        for lid, title, phone, mark, score, st in a:
            items.append(f'<div class="todo-i"><span class="todo-h">'
                         f'★ {int(round(score))}</span>{lead_a(lid, title, phone, mark)}'
                         f' <span class="dim">· {e(STATUS_NAMES.get(st, st))}</span></div>')
        for lid, title, phone, mark, days in p:
            d = int(days)
            items.append(f'<div class="todo-i"><span class="todo-h">'
                         f'{d} дн.</span>{lead_a(lid, title, phone, mark)}'
                         f' <span class="dim">· на торгах</span></div>')
        blocks.append(f'<p class="mname" style="margin:10px 0 2px">'
                      f'{e(short(name))}</p>' + "".join(items))
    if not blocks:
        return ""
    return ('<details class="todo-d"><summary>Внимание и Толкнуть — '
            f'список заявок ({n})</summary><div class="todo-l">'
            + "".join(blocks) + "</div></details>")


STATUS_NAMES = {"NEW": "не обработан", "32": "позвонить",
                "IN_PROCESS": "в работе", "35": "отложен", "8": "ждём паспорт",
                "10": "ждём договор", "11": "ждём предоплату", "12": "на торгах"}


def mask_title(name):
    \"\"\"Телефон в названии лида — под маску (как в очереди дня).\"\"\"
    return re.sub(r"(\\+?[78][\\s(\\-]*\\d{3}[\\s)\\-]*\\d{3}[\\s\\-]*\\d{2}[\\s\\-]*\\d{2})",
                  lambda m: mask(m.group(1)), name)
""")

# 2. now_section: сигнатура + ячейки + шапка + подвал + подпись
rep("""def now_section(mgrs, queue, sc_by, first_col="Менеджер", total=True,
                delta=None):""",
"""def now_section(mgrs, queue, sc_by, first_col="Менеджер", total=True,
                delta=None, attn=None, push=None):""")

rep("""    delta = delta or {}
    trs, tot = [], [0] * 8
    dtot = [0] * 8
    for u, name in mgrs:""",
"""    delta = delta or {}
    attn, push = attn or {}, push or {}
    trs, tot = [], [0] * 8
    dtot = [0] * 8
    atot = ptot = 0
    for u, name in mgrs:""")

rep("""        trs.append(
            f'<tr><td class="mname">{e(short(name))}</td>'
            + "".join(row_cells)
            + f'<td class="num">{sc_txt}</td></tr>')
""",
"""        na, np_ = len(attn.get(u, [])), len(push.get(u, []))
        atot += na
        ptot += np_

        def xcell(v, st, cls, _u=u):
            if not v:
                return '<td class="num">·</td>'
            return (f'<td class="num"><a class="q {cls}" '
                    f'href="{crm_list(_u, st)}" target="_blank" '
                    f'rel="noopener">{v}</a></td>')
        trs.append(
            f'<tr><td class="mname">{e(short(name))}</td>'
            + "".join(row_cells)
            + xcell(na, ATTN_STATUSES, "attn")
            + xcell(np_, ["12"], "push")
            + f'<td class="num">{sc_txt}</td></tr>')
""")

rep("""        foot = ('<tfoot><tr><td class="mname">Отдел</td>'
                + "".join(ftot(v, i) for i, v in enumerate(tot))
                + '<td class="num">—</td></tr></tfoot>')
""",
"""        foot = ('<tfoot><tr><td class="mname">Отдел</td>'
                + "".join(ftot(v, i) for i, v in enumerate(tot))
                + f'<td class="num"><b>{atot or "·"}</b></td>'
                + f'<td class="num"><b>{ptot or "·"}</b></td>'
                + '<td class="num">—</td></tr></tfoot>')
    lst = attn_push_list(mgrs, attn, push) if (attn or push) else ""
""")

rep("""    {''.join(f'<th>{h}</th>' for h in NOW_HEAD[1:])}<th>Скрипт</th></tr></thead>
  <tbody>{''.join(trs)}</tbody>{foot}</table>
  <p class="crit-f" style="margin-top:10px">Каждый столбец — <b>статус лида
    в Битриксе</b>, цифры сходятся с фильтром в CRM. Вместе они и есть все
    незакрытые заявки менеджера. Фильтров нет —
    можно сверять с CRM напрямую.""",
"""    {''.join(f'<th>{h}</th>' for h in NOW_HEAD[1:])}<th>Внимание</th>
    <th>Толкнуть</th><th>Скрипт</th></tr></thead>
  <tbody>{''.join(trs)}</tbody>{foot}</table>
  {lst}
  <p class="crit-f" style="margin-top:10px">Столбцы статусов идут в порядке
    воронки. Каждый — <b>статус лида
    в Битриксе</b>, цифры сходятся с фильтром в CRM. Вместе они и есть все
    незакрытые заявки менеджера. Фильтров нет —
    можно сверять с CRM напрямую.
    <b>«Внимание»</b> — заявки с составной оценкой от {ATTN_SCORE}, которые
    ещё не дошли до «На торгах»: сильный клиент, а движения нет.
    <b>«Толкнуть»</b> — заявки, висящие в «На торгах» дольше {PUSH_DAYS} дней.
    Клик по этим двум цифрам открывает список в Битриксе с фильтром по
    менеджеру и статусам (сама оценка и срок в CRM не фильтруются), точный
    перечень заявок — в списке под таблицей.""")

# 3. вызовы
rep("""    return now_section([row], queue, sc_by, total=False, delta=now_delta(conn)) + f\"\"\"""",
"""    attn, push = attn_push(conn)
    return now_section([row], queue, sc_by, total=False, delta=now_delta(conn),
                       attn=attn, push=push) + f\"\"\"""")
rep("""    return now_section(mgrs, queue, sc_by, delta=now_delta(conn)) + plan_section(conn, mgrs, now) + f\"\"\"""",
"""    attn, push = attn_push(conn)
    return now_section(mgrs, queue, sc_by, delta=now_delta(conn),
                       attn=attn, push=push) + plan_section(conn, mgrs, now) + f\"\"\"""")

# 4. CSS подсветка
rep("td a.q{color:inherit;text-decoration:none;border-bottom:1px dotted var(--ink3)}",
    "td a.q{color:inherit;text-decoration:none;border-bottom:1px dotted var(--ink3)}\n"
    "td a.q.attn{color:var(--serious);font-weight:600}\n"
    "td a.q.push{color:var(--bad);font-weight:600}")

if "import re" not in s.split("\n\n")[0] and not re.search(r"^import re$", s, re.M):
    s = s.replace("import datetime as dt", "import datetime as dt\nimport re", 1)
open(P, "w", encoding="utf-8").write(s)
print("ok")
