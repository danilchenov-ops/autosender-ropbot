"""Месячная оценка менеджеров: сводка, лучший и худший, образцовый и провальный разговор.

Логика та же, что в недельном портрете (profile.py), но окно произвольное и
добавлено ранжирование. Метрики счёта дублируют `v_manager_weekly` — при правке
одного править и второе (см. projects/portret-menedzhera.md).

Лучший и худший определяются средним местом по восьми УПРАВЛЯЕМЫМ метрикам.
Результат (конверсия) в ранг не входит: он вызревает 38 дней и относится
к другому периоду — показывается отдельной строкой для проверки выводов.

Запуск:
    python monthly.py --print          — напечатать
    python monthly.py                  — отправить всем руководителям
    python monthly.py --days 30
    python monthly.py --to 460128042   — только одному чату
"""
import datetime as dt
import sys

import tg
from common import db, log

VLD = dt.timezone(dt.timedelta(hours=10))
B24_LEAD = "https://synergosmoto.bitrix24.ru/crm/lead/details/{}/"
MIN_LEADS = 100          # меньше — в сравнение не берём
NAME_W = 10
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]

# (ключ, подпись, меньше — лучше)
RANKED = [
    ("p50", "скорость реакции", True),
    ("fast15", "доля за 15 минут", False),
    ("brosh_pct", "брошенные заявки", True),
    ("reach_pct", "доля дозвонов", False),
    ("cb1h_pct", "перезвон за час", False),
    ("kept_pct", "исполнение обещаний", False),
    ("score", "балл разговора", False),
    ("step_pct", "следующий шаг с датой", False),
]

STATS = """
WITH lw AS (
    SELECT * FROM v_lead_worked WHERE date_create >= %(a)s AND date_create < %(b)s
),
lead_side AS (
    SELECT assigned_by AS uid, manager,
           count(*) AS leads,
           count(*) FILTER (WHERE grade = 'A') AS a_leads,
           count(*) FILTER (WHERE grade = 'B') AS b_leads,
           count(*) FILTER (WHERE grade IS NOT NULL) AS graded,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY touch_min)
               FILTER (WHERE first_contact IS NOT NULL)::int AS p50,
           round(100.0 * count(*) FILTER (WHERE first_contact IS NOT NULL
                                            AND touch_min <= 15) / count(*)) AS fast15,
           count(*) FILTER (WHERE untouched) AS brosh,
           round(100.0 * count(*) FILTER (WHERE untouched) / count(*), 1) AS brosh_pct,
           count(*) FILTER (WHERE worked_elsewhere) AS dupes,
           round(100.0 * count(*) FILTER (WHERE reached) / count(*)) AS reach_pct,
           count(*) FILTER (WHERE NOT reached) AS not_reached,
           count(*) FILTER (WHERE NOT reached AND calls_3d >= 5) AS persisted
    FROM lw GROUP BY 1, 2
),
call_side AS (
    SELECT c.portal_user_id AS uid,
           count(*) FILTER (WHERE c.direction = 'out') AS out_calls,
           round(sum(c.duration) FILTER (WHERE c.duration > 0) / 60.0) AS talk_min,
           count(DISTINCT (c.call_start AT TIME ZONE 'Asia/Vladivostok')::date) AS days
    FROM calls c
    WHERE c.call_start >= %(a)s AND c.call_start < %(b)s AND c.phone_kind <> 'junk'
    GROUP BY 1
),
missed AS (
    SELECT m.portal_user_id AS uid, count(*) AS missed,
           round(100.0 * count(*) FILTER (WHERE cb.delay_min <= 60) / count(*)) AS cb1h_pct
    FROM calls m
    LEFT JOIN LATERAL (
        SELECT EXTRACT(EPOCH FROM (c.call_start - m.call_start)) / 60.0 AS delay_min
        FROM calls c
        WHERE c.phone_number = m.phone_number AND c.direction = 'out'
          AND c.call_start > m.call_start AND c.duration > 0
        ORDER BY c.call_start LIMIT 1) cb ON TRUE
    WHERE m.direction = 'in' AND m.is_missed AND m.phone_kind <> 'junk'
      AND m.call_start >= %(a)s AND m.call_start < %(b)s
    GROUP BY 1
),
promises AS (
    SELECT f.manager_id AS uid, count(*) AS promises,
           count(*) FILTER (WHERE k.kept) AS kept,
           round(100.0 * count(*) FILTER (WHERE k.kept) / count(*)) AS kept_pct
    FROM followups f
    LEFT JOIN LATERAL (
        SELECT true AS kept FROM calls c
        WHERE c.phone_e164 = f.phone_e164 AND c.direction = 'out' AND c.duration > 0
          AND c.call_start >= (f.due_date - 1)::timestamptz
          AND c.call_start <  (f.due_date + 3)::timestamptz LIMIT 1) k ON TRUE
    WHERE f.due_date >= %(a)s::date AND f.due_date < %(b)s::date
    GROUP BY 1
),
quality AS (
    SELECT c.portal_user_id AS uid, count(*) AS scored,
           round(avg(s.card_score), 1) AS score,
           round(avg(s.talk_share), 2) AS talk_share,
           round(avg(s.questions_manager), 1) AS questions,
           round(100.0 * count(*) FILTER (WHERE s.need_identified) / count(*)) AS need_pct,
           round(100.0 * count(*) FILTER (WHERE s.price_named) / count(*)) AS price_pct,
           round(100.0 * count(*) FILTER (WHERE s.next_step_dated) / count(*)) AS step_pct,
           count(*) FILTER (WHERE array_length(s.objections, 1) > 0) AS with_obj,
           round(100.0 * count(*) FILTER (WHERE array_length(s.objections, 1) > 0
                                            AND s.objections_handled)
                 / NULLIF(count(*) FILTER (WHERE array_length(s.objections, 1) > 0), 0)) AS obj_pct
    FROM call_scores s JOIN calls c ON c.id = s.call_id
    WHERE c.call_start >= %(a)s AND c.call_start < %(b)s
    GROUP BY 1
)
SELECT l.uid, l.manager, l.leads, l.a_leads, l.b_leads, l.graded,
       l.p50, l.fast15, l.brosh, l.brosh_pct, l.dupes, l.reach_pct,
       l.not_reached, l.persisted,
       COALESCE(cs.out_calls, 0) AS out_calls, COALESCE(cs.talk_min, 0) AS talk_min,
       COALESCE(cs.days, 0) AS days,
       COALESCE(ms.missed, 0) AS missed, ms.cb1h_pct,
       COALESCE(pr.promises, 0) AS promises, COALESCE(pr.kept, 0) AS kept, pr.kept_pct,
       COALESCE(q.scored, 0) AS scored, q.score, q.talk_share, q.questions,
       q.need_pct, q.price_pct, q.step_pct, q.with_obj, q.obj_pct,
       r.conv_pct
FROM lead_side l
LEFT JOIN call_side cs ON cs.uid = l.uid
LEFT JOIN missed ms    ON ms.uid = l.uid
LEFT JOIN promises pr  ON pr.uid = l.uid
LEFT JOIN quality q    ON q.uid = l.uid
LEFT JOIN v_manager_result r ON r.portal_user_id = l.uid
ORDER BY l.leads DESC
"""

BEST_CALL = """
SELECT c.id, mg.full_name, c.call_start, c.duration, c.crm_entity_id,
       s.score, s.talk_share, s.questions_manager, s.objections, s.summary, s.next_step
FROM call_scores s
JOIN calls c ON c.id = s.call_id
LEFT JOIN managers mg ON mg.portal_user_id = c.portal_user_id
WHERE c.call_start >= %(a)s AND c.call_start < %(b)s
  AND c.direction = 'out' AND c.crm_entity_type = 'LEAD' AND c.crm_entity_id IS NOT NULL
  AND s.score >= 9 AND s.need_identified AND s.price_named AND s.next_step_dated
  AND array_length(s.objections, 1) > 0 AND s.objections_handled
ORDER BY s.score DESC, s.questions_manager DESC, abs(s.talk_share - 0.5) ASC
LIMIT 1
"""

WORST_CALL = """
SELECT c.id, mg.full_name, c.call_start, c.duration, c.crm_entity_id,
       s.score, s.talk_share, s.questions_manager, s.objections, s.summary, s.outcome
FROM call_scores s
JOIN calls c ON c.id = s.call_id
LEFT JOIN managers mg ON mg.portal_user_id = c.portal_user_id
WHERE c.call_start >= %(a)s AND c.call_start < %(b)s
  AND c.direction = 'out' AND c.crm_entity_type = 'LEAD' AND c.crm_entity_id IS NOT NULL
  AND c.duration >= 120
  AND s.outcome NOT IN ('не дозвонились', 'нецелевой')
  AND NOT s.need_identified AND NOT s.price_named AND NOT s.next_step_dated
ORDER BY s.score ASC, c.duration DESC
LIMIT 1
"""


def short(name):
    parts = [p for p in (name or "").split() if not (len(p) <= 3 and p[:1] == "М")]
    return (parts[-1] if parts else name or "?")[:NAME_W]


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} {few}"
    return f"{n} {many}"


def leads_w(n):
    return plural(n, "заявка", "заявки", "заявок")


def calls_w(n):
    return plural(n, "звонок", "звонка", "звонков")


def label(name):
    """Фамилия целиком — для текста, не для таблицы."""
    parts = [p for p in (name or "").split() if not (len(p) <= 3 and p[:1] == "М")]
    return parts[-1] if parts else (name or "?")


def fmt(v, suffix=""):
    if v is None:
        return "—"
    if isinstance(v, float):
        v = round(v, 1)
    return f"{v}{suffix}"


def table(head, rows, widths):
    out = [" ".join(h.ljust(w) if i == 0 else h.rjust(w)
                    for i, (h, w) in enumerate(zip(head, widths)))]
    for r in rows:
        out.append(" ".join(str(c).ljust(w) if i == 0 else str(c).rjust(w)
                            for i, (c, w) in enumerate(zip(r, widths))))
    return "```\n" + "\n".join(out) + "\n```"


def ranks(rows):
    """Место по каждой метрике и среднее место. Пропуски получают последнее место."""
    out = {r["uid"]: {} for r in rows}
    for key, _, asc in RANKED:
        vals = [(r["uid"], r.get(key)) for r in rows]
        known = sorted([v for v in vals if v[1] is not None],
                       key=lambda x: float(x[1]), reverse=not asc)
        place = {}
        prev, prev_place = None, 0
        for i, (uid, val) in enumerate(known, start=1):
            if prev is not None and float(val) == float(prev):
                place[uid] = prev_place          # равные значения — равное место
            else:
                place[uid] = i
                prev, prev_place = val, i
        last = len(rows)
        for uid, val in vals:
            out[uid][key] = place.get(uid, last)
    for uid, d in out.items():
        d["avg"] = round(sum(d[k] for k, _, _ in RANKED) / len(RANKED), 2)
    return out


def strengths(rk, row, top=True):
    """Метрики, где менеджер в первых двух (или в последних двух)."""
    n = len([1 for _ in RANKED])
    picked = []
    for key, label, asc in RANKED:
        place = rk[key]
        if top and place <= 2:
            picked.append((place, key, label))
        if not top and place >= 5:
            picked.append((place, key, label))
    picked.sort(reverse=not top)
    return picked[:n]


def value_of(row, key):
    v = row.get(key)
    if v is None:
        return "—"
    if key == "p50":
        return f"{int(v)} мин"
    if key == "score":
        return f"{float(v):.0f} из 100"
    if key in ("brosh_pct",):
        return f"{float(v):.1f}%"
    return f"{int(v)}%"


def month_title(a, b):
    a_l, b_l = a.astimezone(VLD), (b - dt.timedelta(seconds=1)).astimezone(VLD)
    return (f"{a_l.day} {MONTHS[a_l.month - 1]} — {b_l.day} {MONTHS[b_l.month - 1]}")


def build(a, b, rows, dropped, best, worst):
    live = [r for r in rows if r["leads"] >= MIN_LEADS and r["out_calls"] > 0]
    rk = ranks(live)
    m = [f"*Оценка менеджеров за месяц — {month_title(a, b)}*", ""]

    m.append("*Сводка*")
    m.append(table(
        ["Менеджер", "Лид", "Реак", "Брош", "Дозв", "≤1ч", "Обещ", "Балл"],
        [[short(r["manager"]), r["leads"], fmt(r["p50"]), f'{float(r["brosh_pct"]):.0f}%',
          fmt(r["reach_pct"], "%"), fmt(r["cb1h_pct"], "%"), fmt(r["kept_pct"], "%"),
          fmt(float(r["score"]) if r["score"] is not None else None)]
         for r in live], [NAME_W, 4, 5, 5, 5, 4, 5, 5]))
    m.append("_Реак — медиана рабочих минут до первого контакта. Брош — доля заявок, "
             "по которым не было ни одного разговора ни с кем. Дозв — доля заявок "
             "с состоявшимся разговором. ≤1ч — перезвон на пропущенный за час. "
             "Обещ — исполнение обещаний клиенту в срок. Балл — карточка эталонного скрипта 0–100, как на панелях._")

    m.append("\n*Место по управляемым метрикам*")
    order = sorted(live, key=lambda r: rk[r["uid"]]["avg"])
    m.append(table(
        ["Менеджер"] + ["Реак", "15м", "Брош", "Дозв", "≤1ч", "Обещ", "Балл", "Шаг", "Итог"],
        [[short(r["manager"])] + [rk[r["uid"]][k] for k, _, _ in RANKED]
         + [f'{rk[r["uid"]]["avg"]:.1f}'] for r in order],
        [NAME_W, 4, 3, 4, 4, 3, 4, 4, 3, 5]))
    m.append("_Итог — среднее место по восьми метрикам. Результат (конверсия) в ранг "
             "не входит: он вызревает 38 дней и относится к более раннему периоду._")

    anomalies = []
    for key, lbl, asc in RANKED:
        vals = sorted(((float(r[key]), r) for r in live if r.get(key) is not None),
                      key=lambda x: x[0], reverse=not asc)
        if len(vals) < 3:
            continue
        worst_v, worst_r = vals[-1]
        second_v = vals[-2][0]
        bad = (worst_v >= 2 * second_v) if asc else (worst_v <= 0.5 * second_v)
        if bad and second_v:
            anomalies.append(f"• {label(worst_r['manager'])}: {lbl} — "
                             f"{value_of(worst_r, key)} против "
                             f"{value_of(vals[-2][1], key)} у следующего. "
                             f"Это не отставание, а другой порядок величины.")
    if anomalies:
        m.append("\n*Выбросы*")
        m.extend(anomalies)

    if order:
        top, bot = order[0], order[-1]
        loads = sorted(live, key=lambda r: -r["leads"])
        def load_note(r):
            if r["uid"] == loads[0]["uid"]:
                return " Нагрузка при этом самая высокая в отделе."
            if r["uid"] == loads[-1]["uid"]:
                return " Нагрузка при этом самая низкая в отделе."
            return ""

        m.append(f"\n*Лучший — {label(top['manager'])}*")
        m.append(f"Среднее место {rk[top['uid']]['avg']:.2f} из "
                 f"{len(order)}: {leads_w(top['leads'])}, {calls_w(top['out_calls'])}."
                 + load_note(top))
        for place, key, lbl in strengths(rk[top["uid"]], top, top=True):
            m.append(f"• {lbl} — {place}-е место, {value_of(top, key)}")
        weak = max(((rk[top["uid"]][k], k, lbl) for k, lbl, _ in RANKED))
        m.append(f"Слабое место: {weak[2]} — {weak[0]}-е место, {value_of(top, weak[1])}.")
        if top["conv_pct"] is not None:
            m.append(f"Конверсия по вызревшим заявкам: {float(top['conv_pct']):.1f}%.")
        if len(order) > 1:
            gap = rk[order[1]["uid"]]["avg"] - rk[top["uid"]]["avg"]
            base = rk[top["uid"]]["avg"]
            exact = [r for r in order[1:] if rk[r["uid"]]["avg"] - base < 0.005]
            near = [r for r in order[1:]
                    if 0.005 <= rk[r["uid"]]["avg"] - base < 0.3]
            if exact:
                m.append(f"⚠️ Ровно то же среднее место у: "
                         f"{', '.join(label(r['manager']) for r in exact)}. "
                         f"Выбор одного лучшего здесь условный.")
            if near:
                m.append(f"Вплотную идут {', '.join(label(r['manager']) for r in near)} "
                         f"(+{min(rk[r['uid']]['avg'] - base for r in near):.2f} места) — "
                         f"это в пределах шума.")
            if exact or near:
                m.append("Надёжно различаются верх и низ таблицы, а не соседние места.")
            for r in exact:
                best_m = min((rk[r["uid"]][k], lbl, k) for k, lbl, _ in RANKED)
                worst_m = max((rk[r["uid"]][k], lbl, k) for k, lbl, _ in RANKED)
                m.append(f"• {label(r['manager'])}: сильнее всего — {best_m[1]} "
                         f"({best_m[0]}-е, {value_of(r, best_m[2])}), слабее всего — "
                         f"{worst_m[1]} ({worst_m[0]}-е, {value_of(r, worst_m[2])}).")

        m.append(f"\n*Худший — {label(bot['manager'])}*")
        m.append(f"Среднее место {rk[bot['uid']]['avg']:.2f} из "
                 f"{len(order)}: {leads_w(bot['leads'])}, {calls_w(bot['out_calls'])}."
                 + load_note(bot))
        for place, key, lbl in strengths(rk[bot["uid"]], bot, top=False):
            m.append(f"• {lbl} — {place}-е место, {value_of(bot, key)}")
        strong = min(((rk[bot["uid"]][k], k, lbl) for k, lbl, _ in RANKED))
        m.append(f"Сильное место: {strong[2]} — {strong[0]}-е место, "
                 f"{value_of(bot, strong[1])}.")
        if bot["conv_pct"] is not None:
            m.append(f"Конверсия по вызревшим заявкам: {float(bot['conv_pct']):.1f}% — "
                     f"смотреть вместе с местом в ранге, они могут расходиться.")

    if dropped:
        m.append("\n*Вне сравнения*")
        for r in dropped:
            if r["out_calls"] == 0:
                m.append(f"• *{label(r['manager'])}* — ни одного исходящего звонка "
                         f"за период. Получено {leads_w(r['leads'])}, из них "
                         f"{r['brosh']} без единого разговора с кем бы то ни было, "
                         f"остальные отработали другие менеджеры. Либо телефония на "
                         f"него не заведена, либо он не работает — проверить в первую "
                         f"очередь.")
                if r.get("last_lead"):
                    m.append(f"  Последняя заявка ему — "
                             f"{r['last_lead']:%d.%m.%Y}, поток остановлен.")
                if r.get("since_june"):
                    m.append(f"  С 1 июня ему ушло {leads_w(r['since_june'])}, "
                             f"{r['brosh_june']} из них не набрал никто.")
            else:
                m.append(f"• {label(r['manager'])}: всего {r['leads']} заявок — "
                         f"выборка не сравнима с остальными.")

    if best:
        m.append("\n*Образцовый разговор*")
        m.append(f"{short(best['full_name'])}, "
                 f"{best['call_start'].astimezone(VLD):%d.%m %H:%M}, "
                 f"{best['duration'] // 60} мин {best['duration'] % 60} сек, "
                 f"балл {best['score']} из 10 по чек-листу разбора.")
        m.append(f"Выяснил потребность, назвал цену, отработал возражения "
                 f"({', '.join(best['objections'][:3])}), назначил следующий шаг с датой. "
                 f"Вопросов клиенту: {best['questions_manager']}, "
                 f"доля своей речи {float(best['talk_share']):.0%}.")
        m.append(f"_{best['summary']}_")
        m.append(f"[Карточка и запись]({B24_LEAD.format(best['crm_entity_id'])})")

    if worst:
        m.append("\n*Провальный разговор*")
        m.append(f"{short(worst['full_name'])}, "
                 f"{worst['call_start'].astimezone(VLD):%d.%m %H:%M}, "
                 f"{worst['duration'] // 60} мин {worst['duration'] % 60} сек, "
                 f"балл {worst['score']} из 10 по чек-листу разбора.")
        m.append("Потребность не выяснена, цена не названа, следующий шаг не назначен — "
                 f"при живом разговоре. Доля своей речи "
                 f"{float(worst['talk_share']):.0%}, вопросов клиенту: "
                 f"{worst['questions_manager']}.")
        m.append(f"_{worst['summary']}_")
        m.append(f"[Карточка и запись]({B24_LEAD.format(worst['crm_entity_id'])})")

    if best and worst and best["full_name"] == worst["full_name"]:
        m.append(f"\nОба разговора — {label(best['full_name'])}. Это и есть главный "
                 f"вывод: разница не в таланте, а в том, соблюдён чек-лист или нет.")

    m.append("\n_Мусорные телефоны отброшены, звонки считаются по номеру клиента, "
             "PARTNER вне сравнения. Оценка разговоров — по расшифрованной части "
             "записей, она пока покрывает не все звонки._")
    return "\n".join(m)


def chunks(text, limit=3900):
    """Телеграм режет длинные сообщения — бьём по границам блоков."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for block in text.split("\n\n*"):
        piece = block if not parts and not cur else "\n\n*" + block
        if cur and len(cur) + len(piece) > limit:
            parts.append(cur)
            cur = piece.lstrip("\n")
        else:
            cur += piece
    if cur:
        parts.append(cur)
    return parts


def rows_of(conn, sql, params):
    cur = conn.execute(sql, params)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def main():
    args = sys.argv[1:]
    days = int(args[args.index("--days") + 1]) if "--days" in args else 30
    now = dt.datetime.now(VLD)
    b = now.replace(hour=0, minute=0, second=0, microsecond=0)   # сегодня не берём: день неполный
    a = b - dt.timedelta(days=days)
    params = {"a": a, "b": b}

    with db() as conn:
        rows = rows_of(conn, STATS, params)
        best = rows_of(conn, BEST_CALL, params)
        worst = rows_of(conn, WORST_CALL, params)
        chats = [r[0] for r in conn.execute(
            "SELECT chat_id FROM tg_users WHERE is_boss AND active").fetchall()]

    dropped = [r for r in rows if r["leads"] < MIN_LEADS or r["out_calls"] == 0]
    with db() as conn:
        for r in dropped:
            if r["out_calls"]:
                continue
            extra = conn.execute(
                """SELECT max(date_create AT TIME ZONE 'Asia/Vladivostok'),
                          count(*) FILTER (WHERE date_create >= date '2026-06-01'),
                          count(*) FILTER (WHERE date_create >= date '2026-06-01'
                                             AND untouched)
                   FROM v_lead_worked WHERE assigned_by = %s""", (r["uid"],)).fetchone()
            r["last_lead"], r["since_june"], r["brosh_june"] = extra
    text = build(a, b, rows, dropped,
                 best[0] if best else None, worst[0] if worst else None)

    if "--print" in args:
        print(text)
        print(f"\n[{len(text)} символов, {len(chunks(text))} сообщ.]")
        return
    if "--to" in args:
        chats = [int(args[args.index("--to") + 1])]
    parts = chunks(text)
    for chat in chats:
        try:
            for part in parts:
                tg.send(chat, part)
            log.info("Месячная оценка отправлена в %s (%s сообщ.)", chat, len(parts))
        except Exception as e:  # noqa: BLE001
            log.error("Отправка в %s: %s", chat, str(e)[:200])


if __name__ == "__main__":
    main()
