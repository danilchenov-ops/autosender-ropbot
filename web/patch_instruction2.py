# -*- coding: utf-8 -*-
"""Патч 09.09.2026: вкладка «Instruction» переделана по новой версии
«Инструкции по работе с лидами ОП АВТО» и согласованной с Тимофеем форме:
строка — менеджер, столбцы — проверки, ячейка — % выполнения, первым —
общий «По инструкции». Правило четырёх звонков — отдельный столбец."""
import re, shutil
P = "/opt/ropbot/web/dash.py"
shutil.copy(P, P + ".bak-instruction2")
s = open(P, encoding="utf-8").read()

start = s.index("# ── вкладка РОПа «Instruction» (06.09.2026)")
end = s.index("\n\n\nROP_PAGES = {")
old_block = s[start:end]
assert "def page_instruction" in old_block

NEW = r'''# ── вкладка РОПа «Instruction» (06.09.2026, переделана 09.09.2026) ───────────
# Насколько менеджеры идут по «Инструкции по работе с лидами ОП АВТО»
# (таблица Тимофея, версия от 09.09.2026 — правило четырёх звонков,
# звонок по договору через 5–10 минут, ежедневные касания в работе).
# Форма согласована с Тимофеем 09.09: строка — менеджер, столбцы — проверки,
# в ячейке одна цифра — % заявок, где правило выполнено; первым столбцом
# общий «По инструкции» (среднее по проверкам, где были случаи). Периоды
# кнопками, клик по ячейке — список нарушений со ссылками в Битрикс.
# Что не измеримо по базе (что говорить клиенту, «обоснованная срочность»,
# Дубль/Спам/Неплатёжеспособный/Не готов) — не проверяем, написано внизу.
INS_NEW_MIN = 30        # новая заявка: первый звонок в первые N рабочих минут
INS_TASK_GRACE_MIN = 60  # открытое окно моложе N минут без задачи — ещё не оценка
INS_SILENCE_WORKMIN = 9 * 60  # «не молчим»: пауза дольше рабочего дня (9–18) — нарушение
INS_DOGOVOR_MIN = 10    # «Договор»/«Оплата»: звонок в первые N минут (регламент 5–10)
INS_TORGI_DAYS = PUSH_DAYS  # «На торгах»: не дольше N дней (как «Толкнуть»)
INS_JUNK_TRIES = 4      # «Недозвон»: минимум попыток дозвона...
INS_JUNK_DAYS = 2       # ...на стольких разных днях
INS_FOUR_CALLS = 4      # правило четырёх звонков: отпускать клиента можно после N дозвонов
INS_WON_GRACE_H = 1     # «ТС куплен»: звонок-поздравление в первый час — не нарушение
INS_MIN_WINDOW = TIMINGS_MIN_WINDOW  # статус-однодневки (< 5 мин) не считаем
INS_DETAIL_CAP = 25     # строк нарушений в раскрытии ячейки

# Столбцы таблицы: (ключ, заголовок, коротко что проверяем, полное правило)
INS_RULES = [
    ("new", "Новая заявка",
     f"звонок за {INS_NEW_MIN} мин",
     f"Первый звонок в первые {INS_NEW_MIN} рабочих минут (9–18 Влд; ночная "
     "заявка отсчитывается с 9 утра). Инструкция: 1-й звонок совершается "
     "сразу после поступления заявки."),
    ("task", "Задача",
     "поставлена на звонок",
     "В статусах «Позвонить», «В работе», «Отложен» стоит задача на следующий "
     "звонок — дело CRM по заявке, созданное с момента постановки статуса "
     "(допуск 15 мин до) или уже стоявшее со сроком после. Инструкция: "
     "обязательно ставим задачу на звонки."),
    ("silence", "Не молчим",
     "пауза ≤ рабочего дня",
     "В статусах «В работе» и «Паспорт» между касаниями нет паузы дольше "
     "рабочего дня (9 рабочих часов): от постановки статуса до первого звонка "
     "и между звонками; у открытых — до сейчас. Инструкция: 1-й звонок через "
     "3–4 часа после разговора, дальше — на следующий день, не ждём молча."),
    ("dog", "Договор / оплата",
     f"звонок за {INS_DOGOVOR_MIN} мин",
     f"После постановки «Договор» и «Оплата обеспечительного» — звонок в первые "
     f"{INS_DOGOVOR_MIN} минут. Инструкция: через 5–10 минут после отправки "
     "договора звоним «Вы получили ссылку?»; после подписания — звоним про "
     "оплату по тому же регламенту."),
    ("torgi", "На торгах",
     f"не дольше {INS_TORGI_DAYS} дней",
     f"Заявка вышла из «На торгах» за {INS_TORGI_DAYS} дней. Инструкция: не "
     "затягиваем с покупкой, «фейковые торги» не дольше 1–2 дней. Висящие "
     f"моложе {INS_TORGI_DAYS} дней — ещё не оценка."),
    ("four", "4 звонка",
     "прежде чем отпустить",
     f"Правило четырёх звонков: клиента из «В работе» / «Паспорт» отпускают "
     f"(переводят в проигрышный статус, «Недозвон», «Не готов») только после "
     f"{INS_FOUR_CALLS} и более дозвонов — состоявшихся исходящих разговоров "
     "по заявке. Инструкция: 1-й звонок через 3–4 часа, 2-й, 3-й, 4-й — по дню; "
     "на четвёртом ставим вопрос ребром."),
    ("junk", "Недозвон",
     f"≥{INS_JUNK_TRIES} попытки, ≥{INS_JUNK_DAYS} дня",
     f"До статуса «Недозвон» было не меньше {INS_JUNK_TRIES} попыток дозвона "
     f"минимум на {INS_JUNK_DAYS} разных днях. Инструкция: 1-й звонок сразу, "
     "2-й через 2–3 часа в тот же день, 3-й на следующий день, 4-й через "
     "2–3 часа; только после четвёртого — «Недозвон»."),
    ("won", "ТС куплен",
     "больше не трогают",
     "После «ТС куплен» исходящих звонков клиенту нет (поздравление в первый "
     "час — не нарушение). Инструкция: поздравить, передать логисту, больше "
     "лид не трогать."),
]

INS_STAGE_SQL = """
SELECT s.owner_id, s.stage_id, s.created_time AS t0, nx.t_end, nx.next_stage,
       nd.semantics AS next_sem,
       l.title, l.phone_e164, l.assigned_by, l.date_create,
       EXISTS (SELECT 1 FROM activities a
                WHERE a.owner_type_id = 1 AND a.owner_id = l.id
                  AND a.provider_id = 'CRM_TODO'
                  AND (a.created BETWEEN s.created_time - interval '15 minutes'
                                     AND coalesce(nx.t_end, now())
                       OR (a.created < s.created_time
                           AND a.end_time > s.created_time))) AS task_ok,
       (SELECT array_agg(c.call_start ORDER BY c.call_start)
          FROM calls c
         WHERE c.direction = 'out'
           AND c.call_start > l.date_create - interval '30 minutes'
           AND ((c.crm_entity_type = 'LEAD' AND c.crm_entity_id = l.id)
                OR (l.phone_e164 IS NOT NULL
                    AND c.phone_e164 = l.phone_e164))) AS calls,
       (SELECT count(*)
          FROM calls c
         WHERE c.direction = 'out' AND c.duration > 0
           AND c.call_start > l.date_create - interval '30 minutes'
           AND c.call_start <= coalesce(nx.t_end, now())
           AND ((c.crm_entity_type = 'LEAD' AND c.crm_entity_id = l.id)
                OR (l.phone_e164 IS NOT NULL
                    AND c.phone_e164 = l.phone_e164))) AS talks
  FROM stage_history s
  JOIN leads l ON l.id = s.owner_id
  LEFT JOIN LATERAL (
      SELECT x.created_time AS t_end, x.stage_id AS next_stage
        FROM stage_history x
       WHERE x.entity_kind = 'lead' AND x.owner_id = s.owner_id
         AND x.created_time > s.created_time
         AND x.stage_id IS DISTINCT FROM s.stage_id
       ORDER BY x.created_time LIMIT 1) nx ON TRUE
  LEFT JOIN crm_dict nd ON nd.kind = 'STATUS' AND nd.status_id = nx.next_stage
 WHERE s.entity_kind = 'lead'
   AND s.stage_id IN ('32', 'IN_PROCESS', '35', '8', '10', '11', '12', '13', 'JUNK')
   AND s.created_time >= %(since)s
   AND l.assigned_by IN (""" + WORKING + """)
   AND coalesce(l.phone_kind, '') NOT IN ('junk', 'none')
"""

INS_NEW_SQL = """
SELECT l.id, l.date_create, l.title, l.phone_e164, l.assigned_by, t.fc
  FROM leads l
  LEFT JOIN LATERAL (
      SELECT min(c.call_start) AS fc
        FROM calls c
       WHERE c.direction = 'out'
         AND c.call_start > l.date_create - interval '5 minutes'
         AND ((c.crm_entity_type = 'LEAD' AND c.crm_entity_id = l.id)
              OR (l.phone_e164 IS NOT NULL
                  AND c.phone_e164 = l.phone_e164))) t ON TRUE
 WHERE l.date_create >= %(since)s
   AND l.assigned_by IN (""" + WORKING + """)
   AND coalesce(l.phone_kind, '') NOT IN ('junk', 'none')
   AND l.source_id IS DISTINCT FROM 'PARTNER'
   AND l.source_id IS DISTINCT FROM 'CALL'
   AND l.status_id NOT IN ('31', '27')
"""

# «Отпустить» клиента = перевод в один из этих статусов (или в проигрышный
# по семантике crm_dict). JUNK — «Недозвон», UC_0TYV86 — «Не готов купить».
INS_DROP_STAGES = ("JUNK", "UC_0TYV86")

INS_CSS = """
.ins-p[hidden]{display:none}
.ins-t{border-collapse:separate;border-spacing:0}
.ins-t th.rule{text-align:center;white-space:normal;min-width:96px;font-weight:600;
  cursor:help;vertical-align:bottom}
.ins-t th.rule small{display:block;font-weight:400;color:var(--ink3);
  font-size:11px;line-height:1.3;margin-top:2px}
.ins-t th.total,.ins-t td.total{border-right:2px solid var(--line)}
.ins-c{cursor:pointer;text-align:center;font-variant-numeric:tabular-nums;
  border-radius:8px;padding:6px 8px}
.ins-c b{font-size:17px;font-weight:700;display:block;line-height:1.15}
.ins-c small{font-size:11px;color:var(--ink3)}
.ins-c.g{background:rgba(12,163,12,.14)}.ins-c.g b{color:#1d8a1d}
.ins-c.y{background:rgba(250,178,25,.20)}.ins-c.y b{color:#a86f00}
.ins-c.r{background:rgba(208,59,59,.16)}.ins-c.r b{color:var(--bad)}
.ins-c.n{cursor:default;background:transparent}.ins-c.n b{color:var(--ink3);font-weight:500}
.ins-c.on{outline:2px solid var(--blue);outline-offset:-2px}
.ins-c:not(.n):hover{filter:brightness(.96)}
.ins-t tfoot td{border-top:2px solid var(--line)}
.ins-d{margin:12px 0 4px;padding:10px 14px;border:1px solid var(--line);
  border-radius:10px;background:var(--bg);font-size:13.5px}
.ins-d h4{margin:0 0 6px;font-size:14px}
.ins-d ul{margin:0;padding-left:18px;line-height:1.5}
.ins-d .why{color:var(--bad)}
.ins-d .ok{color:#1d8a1d}
.ins-legend{font-size:13px;color:var(--ink2);margin:10px 0 0}
.ins-legend i{display:inline-block;width:12px;height:12px;border-radius:3px;
  vertical-align:-1px;margin:0 4px 0 10px}
"""


def _ins_periods(today):
    mon = today - dt.timedelta(days=today.weekday())
    m1 = today.replace(day=1)
    pm1 = (m1 - dt.timedelta(days=1)).replace(day=1)
    m3 = (pm1 - dt.timedelta(days=1)).replace(day=1)
    nxt = today + dt.timedelta(days=1)
    return [
        ("today", "Сегодня", today, nxt),
        ("week", "Текущая неделя", mon, nxt),
        ("lweek", "Прошлая неделя", mon - dt.timedelta(days=7), mon),
        ("month", "Текущий месяц", m1, nxt),
        ("lmonth", "Прошлый месяц", pm1, m1),
        ("m3", f"3 месяца (с {ru_date(m3)})", m3, nxt),
    ]


def _ins_dur(sec):
    sec = int(sec)
    if sec < 3600:
        return f"{max(1, sec // 60)} мин"
    if sec < 86400:
        return f"{sec // 3600} ч"
    d = sec // 86400
    return f"{d} {plural(d, 'день', 'дня', 'дней')}"


def _ins_cases(conn, now, since):
    """[(rule_key, uid, day_vld, ok, lead_id, title, t0, why)] по всем правилам.
    Нерешённые случаи (окно только открылось, торги моложе порога) не входят.
    Один статус может дать случаи в нескольких столбцах (задача + не молчим +
    четыре звонка)."""
    out = []
    grace = dt.timedelta(minutes=INS_TASK_GRACE_MIN)
    min_win = dt.timedelta(minutes=INS_MIN_WINDOW)

    for (lid, t0, title, phone, uid, fc) in conn.execute(
            INS_NEW_SQL, {"since": since}).fetchall():
        wm = _tm_workmin(t0, fc) if fc else _tm_workmin(t0, now)
        if fc is None and wm < INS_NEW_MIN:
            continue                                   # ещё успевает
        ok = fc is not None and wm <= INS_NEW_MIN
        why = ("" if ok else "звонка не было" if fc is None
               else f"первый звонок через {_ins_dur(wm * 60)} рабочего времени")
        out.append(("new", uid, t0.astimezone(VLD).date(), ok, lid, title,
                    t0, why))

    for (lid, st, t0, t_end, nxt, next_sem, title, phone, uid, created,
         task_ok, calls, talks) in conn.execute(
            INS_STAGE_SQL, {"since": since}).fetchall():
        calls = list(calls or [])
        end = t_end or now
        day = t0.astimezone(VLD).date()
        in_win = [c for c in calls if t0 < c <= end]
        short_win = end - t0 < min_win                 # статус-однодневка

        def add(key, ok, why):
            out.append((key, uid, day, ok, lid, title, t0, why))

        # задача на звонок — «Позвонить», «В работе», «Отложен»
        if st in ("32", "IN_PROCESS", "35") and not short_win:
            if task_ok:
                add("task", True, "")
            elif not (t_end is None and now - t0 < grace):
                add("task", False, "задача не поставлена")

        # не молчим — «В работе», «Паспорт»
        if st in ("IN_PROCESS", "8") and not short_win:
            pts = [t0] + in_win + [end]
            gaps = [(_tm_workmin(a, b), a, b) for a, b in zip(pts, pts[1:])]
            worst = max(gaps, key=lambda g: g[0])
            if worst[0] > INS_SILENCE_WORKMIN:
                add("silence", False,
                    f"пауза {_ins_dur((worst[2] - worst[1]).total_seconds())}"
                    + ("" if in_win else ", звонков не было"))
            elif t_end is not None or in_win:
                add("silence", True, "")
            # у открытого окна без звонка и без просроченной паузы — рано судить

        # правило четырёх звонков — отпустили из «В работе»/«Паспорт»
        if st in ("IN_PROCESS", "8") and t_end is not None and (
                nxt in INS_DROP_STAGES or next_sem == "F"):
            if talks >= INS_FOUR_CALLS:
                add("four", True, "")
            else:
                add("four", False,
                    f"отпустили после {talks} "
                    f"{plural(talks, 'дозвона', 'дозвонов', 'дозвонов')}")

        # договор / оплата — звонок за INS_DOGOVOR_MIN минут
        if st in ("10", "11") and not short_win:
            lim = dt.timedelta(minutes=INS_DOGOVOR_MIN)
            if in_win and in_win[0] - t0 <= lim:
                add("dog", True, "")
            elif not in_win and t_end is None and now - t0 < lim:
                pass                                   # ещё успевает
            else:
                what = "договора" if st == "10" else "подписания"
                add("dog", False,
                    f"звонка после {what} не было" if not in_win
                    else f"первый звонок через "
                         f"{_ins_dur((in_win[0] - t0).total_seconds())}")

        if st == "12":
            lim = dt.timedelta(days=INS_TORGI_DAYS)
            if t_end is None and now - t0 < lim:
                continue
            if end - t0 > lim:
                add("torgi", False,
                    f"на торгах {_ins_dur((end - t0).total_seconds())}"
                    + ("" if t_end else ", до сих пор"))
            else:
                add("torgi", True, "")

        if st == "JUNK":
            before = [c for c in calls if c <= t0]
            days = {c.astimezone(VLD).date() for c in before}
            ok = len(before) >= INS_JUNK_TRIES and len(days) >= INS_JUNK_DAYS
            add("junk", ok, "" if ok else f"попыток {len(before)}, дней {len(days)}")

        if st == "13":
            after = [c for c in calls
                     if c > t0 + dt.timedelta(hours=INS_WON_GRACE_H)]
            add("won", not after, "" if not after else
                f"после покупки ещё {len(after)} "
                f"{plural(len(after), 'звонок', 'звонка', 'звонков')}")
    return out


def page_instruction(conn):
    now = dt.datetime.now(VLD)
    today = now.date()
    periods = _ins_periods(today)
    since = min(p[2] for p in periods)
    since_ts = dt.datetime.combine(since, dt.time(0, 0), VLD)

    mgrs = conn.execute(f"""
        SELECT m.portal_user_id, m.name, m.last_name
          FROM managers m
         WHERE m.portal_user_id IN ({WORKING})
         ORDER BY m.last_name, m.name""").fetchall()
    names = {u: human_name(n, ln) or f"id {u}" for u, n, ln in mgrs}
    uids = [u for u, _, _ in mgrs]

    cases = _ins_cases(conn, now, since_ts)

    def cls(pct):
        return "g" if pct >= 80 else "y" if pct >= 50 else "r"

    def detail(did, label, who, plabel, short, full, cs):
        okn, n = sum(1 for c in cs if c[3]), len(cs)
        bad = sorted((c for c in cs if not c[3]), key=lambda c: -c[6].timestamp())
        items = "".join(
            f'<li><span class="dim">'
            f'{c[6].astimezone(VLD).strftime("%d.%m %H:%M")}</span> '
            f'<a href="{crm_link("LEAD", c[4])}" target="_blank" '
            f'rel="noopener">{e(mask_title(c[5] or f"лид {c[4]}"))}</a>'
            f' — <span class="why">{e(c[7])}</span></li>'
            for c in bad[:INS_DETAIL_CAP])
        more = (f'<p class="dim" style="margin:6px 0 0">… и ещё '
                f'{len(bad) - INS_DETAIL_CAP}</p>'
                if len(bad) > INS_DETAIL_CAP else "")
        body = (f"<ul>{items}</ul>{more}" if bad
                else '<p class="ok" style="margin:0">Нарушений нет.</p>')
        return (f'<div class="ins-d" id="{did}" hidden><h4>{e(who)} · '
                f'{e(label)} · {plabel.lower()}: {okn} из {n} по правилу '
                f'({e(short)})</h4><p class="dim" style="margin:0 0 8px">'
                f'{e(full)}</p>{body}</div>')

    head = ('<th style="text-align:left">Менеджер</th>'
            '<th class="rule total" title="Среднее по проверкам, где были случаи">'
            'По инструкции<small>среднее</small></th>'
            + "".join(f'<th class="rule" title="{e(full)}">{e(label)}'
                      f'<small>{e(short)}</small></th>'
                      for _, label, short, full in INS_RULES))

    blocks, btns = [], []
    for pi, (pk, plabel, d0, d1) in enumerate(periods):
        sel = [c for c in cases if d0 <= c[2] < d1]
        cell = {}                                      # (rule, uid) -> [cases]
        for c in sel:
            cell.setdefault((c[0], c[1]), []).append(c)
            cell.setdefault((c[0], "all"), []).append(c)
        trs, details = [], []
        for u in uids + ["all"]:
            who = "Отдел" if u == "all" else names[u]
            tds, pcts = [], []
            for key, label, short, full in INS_RULES:
                cs = cell.get((key, u), [])
                if not cs:
                    tds.append('<td class="ins-c n"><b>—</b></td>')
                    continue
                okn, n = sum(1 for c in cs if c[3]), len(cs)
                pct = 100.0 * okn / n
                pcts.append(pct)
                did = f"insd-{pk}-{key}-{u}"
                tds.append(f'<td class="ins-c {cls(pct)}" data-d="{did}">'
                           f'<b>{round(pct)}%</b><small>{okn} из {n}</small></td>')
                details.append(detail(did, label, who, plabel, short, full, cs))
            if pcts:
                a = sum(pcts) / len(pcts)
                tot = (f'<td class="ins-c total {cls(a)}" style="cursor:default">'
                       f'<b>{round(a)}%</b><small>{len(pcts)} '
                       f'{plural(len(pcts), "проверка", "проверки", "проверок")}'
                       '</small></td>')
            else:
                tot = '<td class="ins-c n total"><b>—</b></td>'
            row = (f'<td class="mname">{e(short_(who)) if u != "all" else "<b>Отдел</b>"}</td>'
                   + tot + "".join(tds))
            if u == "all":
                trs.append(f"<tfoot><tr>{row}</tr></tfoot>")
            else:
                trs.append(f"<tr>{row}</tr>")
        blocks.append(
            f'<div class="ins-p" id="insp-{pk}" data-p="{pk}"'
            f'{"" if pi == 1 else " hidden"}>'
            f'<p class="sub" style="margin:0 0 10px">{plabel}: '
            f'{d0.strftime("%d.%m")}–{(d1 - dt.timedelta(days=1)).strftime("%d.%m")}, '
            f'случаев для оценки <b>{len(sel)}</b>.</p>'
            '<div style="overflow-x:auto"><table class="ins-t"><thead><tr>'
            f'{head}</tr></thead><tbody>'
            + "".join(t for t in trs if not t.startswith("<tfoot"))
            + "</tbody>" + "".join(t for t in trs if t.startswith("<tfoot"))
            + "</table></div>" + "".join(details) + "</div>")
        btns.append(f'<button class="mbtn{" on" if pi == 1 else ""}" '
                    f'data-p="{pk}">{plabel}</button>')

    return (f"<style>{INS_CSS}</style>"
            '<p class="sub" style="max-width:1000px">Как менеджеры идут по '
            '«Инструкции по работе с лидами»: строка — менеджер, столбец — '
            'правило из инструкции, в ячейке — доля заявок, где правило '
            'выполнено. Наведите на заголовок столбца — полное правило. '
            'Клик по ячейке — список нарушений со ссылками в Битрикс.</p>'
            '<h2>По инструкции</h2><section class="card">'
            f'<div class="mbtns" id="ins-btns">{"".join(btns)}</div>'
            + "".join(blocks) +
            '<p class="ins-legend"><i style="background:rgba(12,163,12,.5)"></i>'
            '80% и выше — по инструкции <i style="background:rgba(250,178,25,.6)"></i>'
            '50–79% — через раз <i style="background:rgba(208,59,59,.5)"></i>'
            'ниже 50% — инструкция не работает. «—» — случаев не было.</p>'
            '<p class="vz-note" style="max-width:1000px">Период — по моменту '
            'постановки статуса (для новой заявки — по её приходу), время Влд. '
            'Задача — дело CRM по заявке (то, что менеджер завёл, а не выполнил). '
            'Звонки — исходящие по карточке лида или на его номер; дозвон — '
            'состоявшийся разговор. Статусы короче 5 минут (поставил и тут же '
            'перевёл) не считаются; заявки со спамовым или пустым номером — тоже. '
            'Что по базе не видно — содержание разговора, «обоснованную '
            'срочность», работу с Дублем, Спамом, Неплатёжеспособным и «Не готов '
            'купить» — здесь не оцениваем.</p>'
            '</section>'
            """<script>
(function(){
  var key = "ropbot_ins_period";
  function show(p){
    var ps = document.querySelectorAll('.ins-p');
    for (var i = 0; i < ps.length; i++) ps[i].hidden = ps[i].getAttribute('data-p') !== p;
    var bs = document.querySelectorAll('#ins-btns .mbtn');
    for (var j = 0; j < bs.length; j++) bs[j].className = 'mbtn' + (bs[j].getAttribute('data-p') === p ? ' on' : '');
    try { localStorage.setItem(key, p); } catch (err) {}
  }
  var bs = document.querySelectorAll('#ins-btns .mbtn');
  for (var j = 0; j < bs.length; j++) bs[j].addEventListener('click', function(){ show(this.getAttribute('data-p')); });
  try { var s = localStorage.getItem(key); if (s && document.getElementById('insp-' + s)) show(s); } catch (err) {}
  var cs = document.querySelectorAll('.ins-c[data-d]');
  for (var k = 0; k < cs.length; k++) cs[k].addEventListener('click', function(){
    var id = this.getAttribute('data-d'), d = document.getElementById(id);
    if (!d) return;
    var open = d.hidden;
    var box = this.closest('.ins-p');
    var ds = box.querySelectorAll('.ins-d'); for (var i = 0; i < ds.length; i++) ds[i].hidden = true;
    var on = box.querySelectorAll('.ins-c.on'); for (var i2 = 0; i2 < on.length; i2++) on[i2].classList.remove('on');
    if (open) { d.hidden = false; this.classList.add('on'); }
  });
})();
</script>""")'''

s = s[:start] + NEW + s[end:]
# short_ — короткое имя менеджера (фамилия), как в «Сейчас»
s = s.replace("e(short_(who))", "e(short(who))")
open(P, "w", encoding="utf-8").write(s)
print("ok")
