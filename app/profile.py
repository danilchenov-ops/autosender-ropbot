"""Недельный портрет менеджера — таблица метрик в Телеграм.

Что меряем и почему — /opt/knowledge/projects/virtualnyy-rop.md.
Коротко: управляемое поведение (скорость, охват, настойчивость, обещания,
механика разговора), а не результат. Результат идёт отдельным блоком и только
по вызревшим лидам — свежая конверсия всегда занижена.

Правила счёта соблюдаются представлениями: мусорные телефоны отброшены,
звонки считаются по номеру клиента, PARTNER исключён.

Запуск:
    python profile.py                 — прошлая полная неделя, Тимофею
    python profile.py --all           — всем руководителям
    python profile.py --week 2026-08-10
    python profile.py --print         — только напечатать, не отправлять
"""
import datetime as dt
import sys

import tg
from common import db, log

TIMOFEY = 460128042
MIN_RESULT_LEADS = 100   # меньше — выборка шумит, сравнивать нельзя
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]
NAME_W = 10


def short(name):
    """«М7 Дмитрий Пономарь» -> «Пономарь»."""
    parts = [p for p in (name or "").split() if not (len(p) <= 3 and p[:1] == "М")]
    return (parts[-1] if parts else name or "?")[:NAME_W]


def pct(a, b):
    return None if not b else round(100.0 * a / b)


def fmt(v, suffix=""):
    if v is None:
        return "—"
    if isinstance(v, float):
        v = round(v, 1)
    return f"{v}{suffix}"


def table(head, rows, widths):
    """Моноширинная таблица под ширину телефона."""
    out = [" ".join(h.ljust(w) if i == 0 else h.rjust(w)
                    for i, (h, w) in enumerate(zip(head, widths)))]
    for r in rows:
        out.append(" ".join(str(c).ljust(w) if i == 0 else str(c).rjust(w)
                            for i, (c, w) in enumerate(zip(r, widths))))
    return "```\n" + "\n".join(out) + "\n```"


def last_full_week(today=None):
    today = today or dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=10))).date()
    this_week = today - dt.timedelta(days=today.weekday())
    return this_week - dt.timedelta(days=7)


def week_title(w):
    end = w + dt.timedelta(days=6)
    if w.month == end.month:
        return f"{w.day}–{end.day} {MONTHS[end.month - 1]}"
    return f"{w.day} {MONTHS[w.month - 1]} – {end.day} {MONTHS[end.month - 1]}"


TRAITS = """
SELECT manager, talks, ball, glubina, kval, konkretika, dovedenie, nastoychivost, video
FROM v_manager_traits WHERE week = %s AND talks >= 10 ORDER BY talks DESC
"""

IDEAL = """
SELECT COALESCE(mg.full_name, i.assigned_by::text)          AS manager,
       count(*)                                             AS leads,
       count(*) FILTER (WHERE NOT i.reached)                 AS not_reached,
       count(*) FILTER (WHERE i.f_persisted)                 AS persisted,
       count(*) FILTER (WHERE i.f_dense)                     AS dense,
       count(*) FILTER (WHERE i.reached)                     AS reached,
       count(*) FILTER (WHERE i.f_deep_first)                AS deep_first,
       count(*) FILTER (WHERE i.f_spread)                    AS spread
FROM v_lead_ideal i
LEFT JOIN managers mg ON mg.portal_user_id = i.assigned_by
WHERE i.week = %s
GROUP BY 1
HAVING count(*) >= 10
ORDER BY count(*) DESC
"""


def fetch(conn, week):
    cols = [d.name for d in conn.execute(
        "SELECT * FROM v_manager_weekly WHERE week = %s LIMIT 0", (week,)).description]
    rows = conn.execute(
        """SELECT * FROM v_manager_weekly
           WHERE week = %s AND (leads > 0 OR out_calls > 0)
           ORDER BY leads DESC, out_calls DESC""", (week,)).fetchall()
    return [dict(zip(cols, r)) for r in rows]


class Sections:
    """Нумерация блоков: часть из них может отсутствовать."""

    def __init__(self):
        self.n = 0

    def __call__(self, title):
        self.n += 1
        return f"\n*{self.n}. {title}*"


def build(week, cur, prev, result, ideal, traits):
    m = [f"*Портрет менеджера — {week_title(week)}*"]
    if not cur:
        return "\n".join(m + ["Данных за эту неделю нет."])
    s = Sections()

    m.append(s("Что досталось"))
    m.append(table(
        ["Менеджер", "Лид", "A", "B", "AB%"], [
            [short(r["manager"]), r["leads"], r["a_leads"], r["b_leads"],
             fmt(pct(r["a_leads"] + r["b_leads"], r["graded"]), "%")]
            for r in cur], [NAME_W, 4, 3, 3, 4]))
    m.append("_A и B — классы скоринга по поведению на сайте. AB% — их доля среди "
             "размеченных заявок: контроль того, что вход у всех сопоставим._")

    m.append(s("Скорость и охват"))
    m.append(table(
        ["Менеджер", "Реак", "≤15м", "Брош", "Дубл"], [
            [short(r["manager"]), fmt(r["p50_touch_min"]),
             fmt(pct(r["touched_15m"], r["leads"]), "%"),
             r["untouched"], r["dupes"]]
            for r in cur], [NAME_W, 5, 5, 4, 4]))
    m.append("_Реак — медиана рабочих минут до первого контакта: исходящего звонка "
             "или принятого входящего (день 10:00–19:00). "
             "Брош — по номеру не было ни одного контакта, настоящая потеря. "
             "Дубл — свою карточку не трогали, клиента вели по соседней._")

    m.append(s("Настойчивость и связь"))
    m.append(table(
        ["Менеджер", "Дозв", "5+поп", "Проп", "≤1ч"], [
            [short(r["manager"]), fmt(pct(r["reached"], r["leads"]), "%"),
             fmt(pct(r["persisted"], r["not_reached"]), "%"),
             r["missed"], fmt(pct(r["cb_1h"], r["missed"]), "%")]
            for r in cur], [NAME_W, 5, 6, 4, 4]))
    m.append("_Дозв — доля лидов, где клиент взял трубку. "
             "5+поп — из недозвонившихся сделали не меньше 5 попыток за 3 дня (норма). "
             "≤1ч — доля пропущенных, на которые перезвонили в течение часа._")

    m.append(s("Активность"))
    has_online = any(r["online_h"] is not None for r in cur)
    has_tm = any(r["tm_hours"] for r in cur)
    head = ["Менеджер", "Звон", "Мин", "Дней"]
    w = [NAME_W, 5, 5, 4]
    if has_tm:
        head.append("Таб,ч")
        w.append(6)
    if has_online:
        head.append("Онл,ч")
        w.append(6)
    rows = []
    for r in cur:
        row = [short(r["manager"]), r["out_calls"], r["talk_min"], r["active_days"]]
        if has_tm:
            row.append(fmt(r["tm_hours"]))
        if has_online:
            row.append(fmt(r["online_h"]))
        rows.append(row)
    m.append(table(head, rows, w))
    tail = "_Мин — минуты разговора, Дней — дней со звонками."
    if has_tm:
        tail += " Таб,ч — часы по табелю Битрикса."
    if has_online:
        tail += (" Онл,ч — часы с открытой сессией в Битриксе: это «в системе», "
                 "а не «работал».")
    else:
        tail += " Время в системе начали снимать — столбец появится со следующей недели."
    m.append(tail + "_")

    if any(r["promises"] for r in cur):
        m.append(s("Обещания клиенту"))
        m.append(table(
            ["Менеджер", "Обещ", "Сдерж", "%"], [
                [short(r["manager"]), r["promises"], r["promises_kept"],
                 fmt(pct(r["promises_kept"], r["promises"]), "%")]
                for r in cur if r["promises"]], [NAME_W, 5, 6, 4]))
        m.append("_Договорённость с конкретной датой, вытащенная из расшифровки, и был ли "
                 "звонок в срок. База пока маленькая — разобрана лишь часть записей._")

    if any(r["scored"] for r in cur):
        m.append(s("Как ведёт разговор"))
        m.append(table(
            ["Менеджер", "N", "Балл", "Потр", "Цена", "Шаг"], [
                [short(r["manager"]), r["scored"], fmt(r["card_score"]),
                 fmt(pct(r["need_ok"], r["scored"]), "%"),
                 fmt(pct(r["price_named"], r["scored"]), "%"),
                 fmt(pct(r["step_dated"], r["scored"]), "%")]
                for r in cur if r["scored"]], [NAME_W, 3, 5, 5, 5, 4]))
        m.append("_Балл — карточка эталонного скрипта, 0–100, та же шкала, что на панели "
                 "менеджера (N — разобранных звонков). Потр/Цена/Шаг — доля разговоров, где "
                 "выявлена потребность, названа цена, назначен следующий шаг с датой._")
    else:
        m.append("\n_Блок «как ведёт разговор» пока пуст: записи расшифровываются, "
                 "оценка копится._")

    if traits:
        m.append(s("Характеристики разговора"))
        m.append(table(
            ["Менеджер", "Глуб", "Квал", "Конкр", "Дов", "Наст", "Видео"], [
                [short(r["manager"]), fmt(r["glubina"], "%"), fmt(r["kval"], "%"),
                 fmt(r["konkretika"], "%"), fmt(r["dovedenie"], "%"),
                 fmt(r["nastoychivost"], "%"), fmt(r["video"], "%")]
                for r in traits], [NAME_W, 5, 5, 5, 4, 5, 5]))
        m.append("_Глубина — вытащил задачу клиента и привязал выгоду к его словам. "
                 "Квалификация — выяснил, кто решает или когда покупка. Конкретика — "
                 "цена и дата/время названы. Доведение — сам предложил конкретный шаг. "
                 "Настойчивость — возражения отработаны, а не приняты (от разговоров "
                 "с возражениями). Видео — предложил показать: связь с покупкой +20 п.п. "
                 "Сводный процент этих черт — балл выше: та же карточка, 0–100._")

    if ideal:
        m.append(s("Стандарт идеальной сделки"))
        m.append(table(
            ["Менеджер", "5+поп", "Плотн", "7+мин", "3+дня"], [
                [short(r["manager"]),
                 fmt(pct(r["persisted"], r["not_reached"]), "%"),
                 fmt(pct(r["dense"], r["leads"]), "%"),
                 fmt(pct(r["deep_first"], r["reached"]), "%"),
                 fmt(pct(r["spread"], r["leads"]), "%")]
                for r in ideal], [NAME_W, 6, 6, 6, 6]))
        m.append("_Четыре действия, у которых нашлась связь с покупкой: 5+ попыток "
                 "при недозвоне (×16,7), 5+ звонков за 3 дня (×6,8), первый разговор "
                 "7+ минут (×5,6), диалог растянут на 3+ дня. Нормы: 80 / 30 / 30 / 25%._")

    if result:
        m.append(s("Результат (лиды 45–135 дней назад)"))
        m.append(table(
            ["Менеджер", "Лид", "Прод", "Конв"], [
                [short(r["manager"]), r["leads"], r["won"],
                 fmt(float(r["conv_pct"]) if r["conv_pct"] is not None else None, "%")]
                for r in result], [NAME_W, 5, 5, 6]))
        m.append("_Свежие недели в результат не берём: медиана цикла сделки 8,6 дня, "
                 "90-й процентиль 38 дней — иначе конверсия занижена у всех. "
                 f"Показаны те, у кого не меньше {MIN_RESULT_LEADS} лидов в окне._")

    notes = observations(cur, prev)
    if notes:
        m.append("\n*Что видно*")
        m.extend("• " + n for n in notes)

    m.append("\n_Считается каждый понедельник. Правила счёта — реестр, analytics-rules._")
    return "\n".join(m)


def observations(cur, prev):
    out = []
    by_prev = {r["portal_user_id"]: r for r in prev}

    react = [r for r in cur if r["p50_touch_min"] is not None and r["leads"] >= 20]
    if len(react) >= 2:
        best = min(react, key=lambda r: r["p50_touch_min"])
        worst = max(react, key=lambda r: r["p50_touch_min"])
        if worst["p50_touch_min"] >= 2 * max(best["p50_touch_min"], 1):
            out.append(
                f"Скорость реакции: {short(best['manager'])} "
                f"{int(best['p50_touch_min'])} мин против "
                f"{int(worst['p50_touch_min'])} у {short(worst['manager'])} — "
                f"разрыв {round(worst['p50_touch_min'] / max(best['p50_touch_min'], 1))}×.")

    lost = sorted((r for r in cur if r["leads"] >= 20),
                  key=lambda r: -(pct(r["untouched"], r["leads"]) or 0))
    if lost and (pct(lost[0]["untouched"], lost[0]["leads"]) or 0) >= 5:
        r = lost[0]
        out.append(f"Брошено без единого контакта: {short(r['manager'])} — "
                   f"{r['untouched']} из {r['leads']} "
                   f"({pct(r['untouched'], r['leads'])}%). Это не дубли и не заявки "
                   f"с входящего: по номеру не было ни одного разговора.")

    tot_np = sum(r["not_reached"] for r in cur)
    tot_p = sum(r["persisted"] for r in cur)
    if tot_np >= 20 and (pct(tot_p, tot_np) or 0) < 20:
        out.append(f"Норма «5 попыток за 3 дня» держится в {pct(tot_p, tot_np)}% "
                   f"случаев недозвона ({tot_p} из {tot_np}) — по всему отделу.")

    for r in cur:
        p = by_prev.get(r["portal_user_id"])
        if not p or not p["p50_touch_min"] or not r["p50_touch_min"] or r["leads"] < 20:
            continue
        d = float(r["p50_touch_min"]) - float(p["p50_touch_min"])
        if abs(d) >= max(60, 0.5 * float(p["p50_touch_min"])):
            out.append(f"{short(r['manager'])}: реакция "
                       f"{'замедлилась' if d > 0 else 'ускорилась'} — "
                       f"{int(p['p50_touch_min'])} → {int(r['p50_touch_min'])} мин "
                       f"к прошлой неделе.")
    return out[:6]


def main():
    args = sys.argv[1:]
    week = last_full_week()
    if "--week" in args:
        week = dt.date.fromisoformat(args[args.index("--week") + 1])
    prev_week = week - dt.timedelta(days=7)

    with db() as conn:
        cur = fetch(conn, week)
        prev = fetch(conn, prev_week)
        cols = [d.name for d in conn.execute(
            "SELECT * FROM v_manager_result LIMIT 0").description]
        result = [dict(zip(cols, r)) for r in conn.execute(
            """SELECT * FROM v_manager_result WHERE leads >= %s
               ORDER BY conv_pct DESC NULLS LAST""", (MIN_RESULT_LEADS,)).fetchall()]
        icur = conn.execute(IDEAL, (week,))
        icols = [d.name for d in icur.description]
        ideal = [dict(zip(icols, r)) for r in icur.fetchall()]
        tcur = conn.execute(TRAITS, (week,))
        tcols = [d.name for d in tcur.description]
        traits = [dict(zip(tcols, r)) for r in tcur.fetchall()]

    text = build(week, cur, prev, result, ideal, traits)

    if "--print" in args:
        print(text)
        return

    chats = [TIMOFEY]
    if "--all" in args:
        with db() as conn:
            chats = [r[0] for r in conn.execute(
                "SELECT chat_id FROM tg_users WHERE is_boss AND active").fetchall()]
    for chat in chats:
        try:
            tg.send(chat, text)
            log.info("Портрет за %s отправлен в %s", week, chat)
        except Exception as e:  # noqa: BLE001
            log.error("Отправка в %s: %s", chat, str(e)[:200])


if __name__ == "__main__":
    main()
