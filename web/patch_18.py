"""Вкладка «Сообщения» на панели РОПа: СМС с телефонов менеджеров (SMSGate).

Просьба Тимофея 25.08: видеть, кому что отправляется. Правило №1 (решение
Тимофея): новая заявка → СМС-приветствие от закреплённого менеджера,
не чаще одной отправки на номер за месяц; одна отправка может занять 2 СМС.
Запуск позже — пока витрина: сводка по менеджерам за месяц + лента отправок.
Данные — sms_outbox / sms_devices (022_sms.sql). До запуска страница честно
пишет, что рассылка выключена и чего не хватает.

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


# ── 1. вкладка ──────────────────────────────────────────────────────────────
swap('''ROP_TABS = [("menedzhery", "", "Менеджеры"),
            ("etalon", "etalon.html", "Эталон"),''',
     '''ROP_TABS = [("menedzhery", "", "Менеджеры"),
            ("soobshcheniya", "soobshcheniya.html", "Сообщения"),
            ("etalon", "etalon.html", "Эталон"),''',
     "tab")

# ── 2. страница ─────────────────────────────────────────────────────────────
swap('''ROP_PAGES = {"kontrol": page_kontrol, "menedzhery": page_managers,''',
     '''SMS_STATUS_RU = {
    "planned": ("в очереди", "sms-wait"),
    "sent": ("отправлено", "sms-sent"),
    "delivered": ("доставлено", "sms-ok"),
    "failed": ("ошибка", "sms-bad"),
    "skipped": ("пропущено", "sms-skip"),
}


def page_sms(conn):
    """Вкладка «Сообщения»: СМС с телефонов менеджеров через SMSGate."""
    mgrs = dict(conn.execute(f"""
        SELECT m.portal_user_id,
               btrim(m.name || ' ' || coalesce(m.last_name, ''))
          FROM managers m WHERE m.portal_user_id IN ({WORKING})""").fetchall())
    devices = {r[0]: r[1] for r in conn.execute(
        "SELECT portal_user_id, active FROM sms_devices").fetchall()}

    month0 = "date_trunc('month', now() AT TIME ZONE %s)"
    stats = {r[0]: r[1:] for r in conn.execute(f"""
        SELECT portal_user_id,
               count(*) FILTER (WHERE status <> 'skipped'),
               coalesce(sum(parts) FILTER (WHERE status <> 'skipped'), 0),
               count(*) FILTER (WHERE status = 'delivered'),
               count(*) FILTER (WHERE status = 'failed'),
               count(*) FILTER (WHERE status = 'skipped')
          FROM sms_outbox
         WHERE (created_at AT TIME ZONE %s) >= {month0}
         GROUP BY 1""", (TZ, TZ)).fetchall()}

    trs = []
    for u, name in sorted(mgrs.items(), key=lambda kv: kv[1]):
        n, parts, ok, bad, skip = stats.get(u, (0, 0, 0, 0, 0))
        dev = devices.get(u)
        dev_txt = ("<span class=\\"sms-ok\\">подключён</span>" if dev
                   else "<span class=\\"sms-skip\\">выключен</span>" if dev is False
                   else "<span class=\\"sms-bad\\">нет реквизитов</span>")
        trs.append(
            f'<tr><td class="mname">{e(short(name))}</td>'
            f'<td class="num">{n or "·"}</td>'
            f'<td class="num">{parts or "·"}</td>'
            f'<td class="num">{ok or "·"}</td>'
            f'<td class="num">{bad or "·"}</td>'
            f'<td class="num">{skip or "·"}</td>'
            f'<td>{dev_txt}</td></tr>')

    rows_ = conn.execute("""
        SELECT o.created_at AT TIME ZONE %s, o.portal_user_id, o.phone_e164,
               o.lead_id, o.body, o.parts, o.status, o.error, o.rule
          FROM sms_outbox o
         ORDER BY o.id DESC LIMIT 60""", (TZ,)).fetchall()

    if not rows_:
        feed = ('<section class="card"><p class="muted">Отправок ещё не было. '
                'Рассылка выключена: идёт настройка. Как только появится '
                'первая СМС — она встанет сюда.</p></section>')
    else:
        items = []
        for ts, uid, phone, lid, body, parts, st, err, rule in rows_:
            st_txt, st_cls = SMS_STATUS_RU.get(st, (st, ""))
            link = (f' · <a href="{crm_link("LEAD", lid)}" target="_blank" '
                    f'rel="noopener">карточка</a>' if lid else "")
            err_txt = f' · <span class="sms-bad">{e(err or "")}</span>' if err else ""
            items.append(
                f'<div class="sms-i"><div class="sms-m">'
                f'{ts.strftime("%d.%m %H:%M")} · '
                f'<b>{e(short(mgrs.get(uid, str(uid))))}</b> → '
                f'{e(mask(phone))}{link} · {parts} СМС · '
                f'<span class="{st_cls}">{st_txt}</span>{err_txt}</div>'
                f'<div class="sms-b">{e(body)}</div></div>')
        feed = ('<section class="card">' + "".join(items) + "</section>")

    no_dev = [e(short(n)) for u, n in sorted(mgrs.items(), key=lambda kv: kv[1])
              if u not in devices]
    setup = ""
    if no_dev:
        setup = (f'<p class="crit-f" style="margin-top:10px">Без реквизитов '
                 f'SMSGate: {", ".join(no_dev)} — с их телефонов рассылка '
                 f'не пойдёт, пока логин и пароль из приложения не занесены '
                 f'в sms_devices.</p>')

    return f"""
<style>
.sms-i{{padding:10px 0;border-bottom:1px solid var(--line)}}
.sms-i:last-child{{border-bottom:0}}
.sms-m{{font-size:13px;color:var(--ink2);margin-bottom:4px}}
.sms-b{{font-size:14px;white-space:pre-wrap}}
.sms-ok{{color:#2e7d32}}.sms-bad{{color:#c62828}}
.sms-skip,.sms-wait{{color:var(--ink2)}}.sms-sent{{color:var(--ink)}}
</style>
<p class="sub" style="max-width:1000px">СМС уходят с личных телефонов
менеджеров через приложение SMSGate — клиент видит обычный номер своего
менеджера и может ответить или перезвонить напрямую.
<b>Правило:</b> новая заявка → приветствие от закреплённого менеджера,
не чаще одной отправки на номер за месяц.
<b>Рассылка пока выключена</b> — идёт настройка.</p>

<h2>За {MONTHS_NOM[dt.datetime.now(VLD).month - 1]}</h2>
<section class="card">
  <table><thead><tr><th>Менеджер</th><th>Отправок</th><th>СМС</th>
    <th>Доставлено</th><th>Ошибки</th><th>Пропущено</th><th>Телефон</th>
  </tr></thead><tbody>{"".join(trs)}</tbody></table>
  <p class="crit-f" style="margin-top:10px">«Отправок» — сообщений клиентам;
    «СМС» — во сколько частей они уложились (длинный текст занимает две).
    «Пропущено» — правило не дало отправить: на этот номер уже писали
    в последние 30 дней, либо телефон менеджера не подключён.</p>
  {setup}
</section>

<h2>Последние отправки</h2>
{feed}"""


ROP_PAGES = {"kontrol": page_kontrol, "menedzhery": page_managers,
             "soobshcheniya": page_sms,''',
     "page")

# ── 3. сборка ───────────────────────────────────────────────────────────────
swap('''            "rop": [("menedzhery", "index.html"),
                    ("etalon", "etalon.html"),''',
     '''            "rop": [("menedzhery", "index.html"),
                    ("soobshcheniya", "soobshcheniya.html"),
                    ("etalon", "etalon.html"),''',
     "pageset")

io.open(P, "w", encoding="utf-8").write(src)
compile(src, "dash.py", "exec")
print("готово, синтаксис чист")
