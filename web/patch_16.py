"""Мелкие правки к списку дел на сегодня: телефон в строке и текст при нуле."""
import io

P = "/opt/ropbot/web/dash.py"
src = io.open(P, encoding="utf-8").read()


def swap(old, new, tag):
    global src
    if old not in src:
        raise SystemExit(f"не нашёл [{tag}]:\n{old[:160]}")
    src = src.replace(old, new, 1)


swap('''  SELECT l.id, l.title, min(a.end_time AT TIME ZONE %(tz)s) AS due''',
     '''  SELECT l.id, l.title, l.phone_e164, min(a.end_time AT TIME ZONE %(tz)s) AS due''',
     "todo-sql-phone")

swap('''   GROUP BY 1, 2
   ORDER BY 3
"""''',
     '''   GROUP BY 1, 2, 3
   ORDER BY 4
"""''',
     "todo-sql-group")

swap('''        for lid, title, due in todo_list(conn, uid):
            items.append(
                f'<div class="todo-i"><span class="todo-h">'
                f'{due.strftime("%H:%M")}</span>'
                f'<a href="{crm_link("LEAD", lid)}" target="_blank" '
                f'rel="noopener">{e(title or ("Лид " + str(lid)))}</a></div>')''',
     '''        for lid, title, phone, due in todo_list(conn, uid):
            name = (title or "").strip(" /") or f"Лид {lid}"
            items.append(
                f'<div class="todo-i"><span class="todo-h">'
                f'{due.strftime("%H:%M")}</span>'
                f'<a href="{crm_link("LEAD", lid)}" target="_blank" '
                f'rel="noopener">{e(name)}</a>'
                + (f'<span class="todo-p">{e(mask(phone))}</span>'
                   if phone else "") + '</div>')''',
     "todo-row")

swap('''    «Показать список» раскрывает ровно эти лиды по времени дела —
    строка ведёт в карточку.</p>''',
     '''    {"«Показать список» раскрывает ровно эти лиды по времени дела — "
      "строка ведёт в карточку." if today else
      "Сегодня дел не поставлено — если клиент ждёт звонка, дело лучше завести: "
      "иначе он потеряется."}</p>''',
     "todo-note")

swap('''.todo-h{flex:0 0 44px;''',
     '''.todo-p{margin-left:auto;color:var(--ink2);font-variant-numeric:tabular-nums;
  white-space:nowrap}
.todo-h{flex:0 0 44px;''',
     "todo-css-phone")

io.open(P, "w", encoding="utf-8").write(src)
compile(src, "dash.py", "exec")
print("готово, синтаксис чист")
