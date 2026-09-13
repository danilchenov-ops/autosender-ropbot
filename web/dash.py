"""Панели менеджеров — статические страницы по секретной ссылке.

Одна ссылка на человека, три вкладки:

    /d/<токен>/               заявки в работе — что делать сегодня
    /d/<токен>/razgovory.html качество разговоров по карточке эталонного скрипта
    /d/<токен>/pravila.html   шесть правил идеальной сделки, факт против нормы

Правила счёта (`/opt/knowledge/…/analytics-rules.md`) соблюдаются:
непригодные номера отброшены, PARTNER исключён, окно звонков открывается
от ПЕРВОЙ карточки номера — иначе дубль выглядит брошенным, хотя клиента ведут.

Запуск (код не запечён в образ, подаётся в контейнер через stdin):
    docker exec -i ropbot-collector-1 python - --token X --page zayavki \
        < /opt/ropbot/web/dash.py
"""
import argparse
import datetime as dt
import io
import tarfile
import time
import html
import re
import sys

from common import db

TZ = "Asia/Vladivostok"
VLD = dt.timezone(dt.timedelta(hours=10))
W_TALKS = 14          # окно вкладки «Разговоры», дней
W_RULES_FROM = 45     # окно вкладки «Правила»: заявки от 45 до 3 дней назад —
W_RULES_TO = 3        # свежим ещё рано, трёхдневное окно правил не закрылось
SILENCE_DAYS = 3      # с какой тишины заявка попадает в список «вернуться»
B24 = "https://synergosmoto.bitrix24.ru"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

# Рабочий состав отдела = у кого есть активный токен. Выключил токен в
# dash_tokens — человек пропал из очередей, сравнений и дашборда РОПа.
WORKING = ("SELECT portal_user_id FROM dash_tokens "
           "WHERE active AND kind = 'manager' AND portal_user_id IS NOT NULL")

MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
              "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_PREP = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле",
               "августе", "сентябре", "октябре", "ноябре", "декабре"]
DOW = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

# Решение Тимофея 23.08: менеджеру пока показываем только «Скрипт».
# Остальные страницы (заявки, разговоры, правила) остаются в коде и вернутся
# в TABS, когда решим их включить.
TABS = [("dashboard", "", "Дашборд"),
        ("skript", "skript.html", "Скрипт"),
        ("kpi", "kpi.html", "KPI")]

# ── карточка разговора (app/card.py) ─────────────────────────────────────────
CRITERIA = [
    ("next_step", 34, "Конкретный следующий шаг",
     "«Жду от вас X, и я сделаю Y». Главное отличие выигранных разговоров: "
     "68% против 41%."),
    ("decision_maker", 22, "Выяснено, кто участвует в решении",
     "Жена, партнёр, отец — кто ещё скажет «да». Второй по силе признак."),
    ("value_to_pain", 20, "Выгода привязана к словам клиента",
     "Не список преимуществ, а ответ на то, что клиент сам назвал."),
    ("date_fixed", 13, "Названы дата и время следующего действия",
     "Не «на неделе», а «в четверг после обеда». Самое пустое поле у всех: "
     "12% против 2%."),
    ("timeline", 11, "Выяснен срок покупки",
     "Когда клиент планирует купить, а не когда ему удобно поговорить."),
]

# ── шесть правил идеальной сделки (реестр «Идеальный менеджер», 21.08.2026) ───
# ключ, заголовок, знаменатель, норма %, во сколько раз чаще покупают, пояснение
RULES = [
    ("persisted", "Звонить, пока не возьмут трубку", "not_reached", 80, "16,7",
     "Пять и больше попыток за три дня по тем, до кого не дозвонились. "
     "Считается по заявкам, где разговора так и не было."),
    ("client_back", "Оставить повод перезвонить самому", "leads", 15, "13,3",
     "Клиент сам позвонил два раза и больше. Не «если что, звоните», "
     "а конкретное обещание, ради которого он наберёт."),
    ("dense", "Пять звонков в первые три дня", "leads", 30, "6,8",
     "Плотность на одну заявку, а не количество звонков за день."),
    ("deep_first", "Первый разговор не короче семи минут", "reached", 30, "5,6",
     "Считается по заявкам, где разговор состоялся. Справка о цене за две "
     "минуты — не первый разговор."),
    ("spread", "Диалог растянут на несколько дней", "leads", 25, None,
     "Между первым и последним разговором три дня и больше. У выигранных "
     "сделок медиана 5,9 дня, у проигранных — ноль."),
]


# ── мелочи ───────────────────────────────────────────────────────────────────

def e(s):
    return html.escape(str(s if s is not None else ""))


def num(v, digits=0):
    if v is None:
        return "—"
    return f"{v:.{digits}f}".replace(".", ",")


def human_name(name, last):
    """«М7 Дмитрий» + «Пономарь» -> «Дмитрий Пономарь». Внутренний код убираем."""
    parts = [p for p in (name or "").split() if not (len(p) <= 3 and p[:1] == "М")]
    if last and last not in parts:
        parts.append(last)
    return " ".join(parts) or (name or "")


def ru_date(d):
    return f"{d.day} {MONTHS[d.month - 1]}"


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def ago(ts, now):
    """«3 дня», «5 часов», «40 минут» — сколько прошло."""
    if ts is None:
        return "—"
    s = (now - ts).total_seconds()
    if s < 3600:
        m = max(1, int(s // 60))
        return f"{m} {plural(m, 'минута', 'минуты', 'минут')}"
    if s < 86400:
        h = int(s // 3600)
        return f"{h} {plural(h, 'час', 'часа', 'часов')}"
    d = int(s // 86400)
    return f"{d} {plural(d, 'день', 'дня', 'дней')}"


def mask(phone):
    p = (phone or "").lstrip("+")
    if len(p) < 11:
        return "номер не записан"
    return f"+{p[0]} ({p[1:4]}) ***-**-{p[-2:]}"


def mmss(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def crm_link(kind, eid):
    if not eid:
        return None
    path = {"LEAD": "lead", "DEAL": "deal", "CONTACT": "contact",
            "COMPANY": "company"}.get(kind or "")
    return f"{B24}/crm/{path}/details/{eid}/" if path else None


# Незакрытые статусы лида (semantics IS NULL в crm_dict)
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


def rows(cur, sql, params, cols):
    return [dict(zip(cols, r)) for r in cur.execute(sql, params).fetchall()]


# ── общая разметка ───────────────────────────────────────────────────────────

CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{color-scheme:light;
--bg:#f4f3f0;--surface:#fcfcfb;--line:#e3e1dc;--ink:#0b0b0b;--ink2:#52514e;
--ink3:#7d7b76;--blue:#2a78d6;--blue-soft:#cde2fb;--good:#0ca30c;
--warn:#fab219;--serious:#ec835a;--bad:#d03b3b;--tick:#0b0b0b}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;
--bg:#121211;--surface:#1a1a19;--line:#383835;--ink:#fff;--ink2:#c3c2b7;
--ink3:#94938a;--blue:#3987e5;--blue-soft:#184f95;--good:#0ca30c;
--warn:#fab219;--serious:#ec835a;--bad:#e66767;--tick:#fff}}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:20px 16px 64px}
.wrap.wide{max-width:1500px;padding:24px 28px 64px}
.wide h1{font-size:26px}
.wide .tiles{grid-template-columns:repeat(4,minmax(0,1fr))}
.cols2{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}
.wide table{font-size:13.5px}
.wide th,.wide td{padding:7px 10px}
.wide tbody tr:hover{background:var(--bg)}
.num{font-variant-numeric:tabular-nums}
.mname{font-weight:600;white-space:nowrap}
.heat{position:relative;text-align:right}
.heat span{position:relative;z-index:1}
.heat i{position:absolute;inset:2px;border-radius:4px;background:var(--blue);
font-style:normal}
.dim{color:var(--ink3)}
.up-today{color:var(--good);font-weight:650;font-size:11.5px;margin-left:3px;
white-space:nowrap;font-variant-numeric:tabular-nums}
.sub2{font-size:11.5px;color:var(--ink3);display:block;line-height:1.2}
.mbtns{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
.mbtn{font:600 12.5px/1 inherit;padding:6px 11px;border-radius:8px;cursor:pointer;
border:1px solid var(--line);background:var(--bg);color:var(--ink2)}
.mbtn.on{background:var(--blue);border-color:var(--blue);color:#fff}
.warnbox{border-left:3px solid var(--warn);padding-left:12px}
tfoot td{font-weight:650;border-top:2px solid var(--line)}
.cols2 h2:first-child{margin-top:0}
.back{display:inline-block;margin-bottom:10px;font-size:13.5px;font-weight:600;
color:var(--ink3);text-decoration:none}
.back:hover{color:var(--blue)}
.hublist a{text-decoration:none;font-size:15.5px}
.hublist a:hover{text-decoration:underline}
td a.q{color:inherit;text-decoration:none;border-bottom:1px dotted var(--ink3)}
td a.q.attn{color:var(--serious);font-weight:600}
td a.q.push{color:var(--bad);font-weight:600}
td a.q.ap.on{border-bottom:2px solid currentColor}
.h2-hint{font:400 13px/1.35 inherit;color:var(--ink3);text-transform:none;letter-spacing:0;margin-left:10px;vertical-align:middle}
th.hint{cursor:help;text-decoration:underline dotted var(--ink3);text-underline-offset:3px;position:relative}
th.hint .tip{display:none;position:absolute;right:0;top:100%;z-index:5;width:280px;padding:8px 10px;background:var(--ink);color:var(--bg);font-weight:400;font-size:12.5px;line-height:1.35;text-align:left;border-radius:8px;box-shadow:0 4px 14px rgba(0,0,0,.25);white-space:normal}
th.hint:hover .tip{display:block}
tr.me td{background:var(--blue-soft);}
tr.me td.mname{font-weight:700}
.ap-row td{padding:0 6px 10px;text-align:left;border-bottom:1px solid var(--line)}
.ap-row .ap-box{margin-top:6px}
.ap-box{margin-top:12px;padding:10px 12px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
td a.q:hover{color:var(--blue);border-bottom-color:var(--blue)}
.heat a.q{border-bottom:0}
.heat a.q:hover{color:var(--blue)}
.blocks{display:grid;grid-template-columns:1fr 1fr;gap:4px 32px}
@media(max-width:1100px){.blocks{grid-template-columns:1fr}}
.blk{padding:14px 0;border-top:1px solid var(--line)}
.blk h3{font-size:14px;margin:0 0 8px;letter-spacing:.01em}
.blk h3 b{color:var(--blue)}
.dlg{border-left:3px solid var(--blue);padding:8px 14px;background:var(--bg);
border-radius:0 10px 10px 0;margin:0 0 8px;font-size:14px;line-height:1.55}
.dlg .who{font-weight:700;color:var(--blue);margin-right:4px}
.dlg .who.k{color:var(--ink3)}
.verdict{font-size:13.5px;margin:10px 0 0;line-height:1.55}
.verdict b.good-t{color:var(--good)}
.verdict b.bad-t{color:var(--bad)}
@media(max-width:1100px){.cols2{grid-template-columns:1fr}
.wrap.wide{max-width:720px;padding:20px 16px 64px}}
h1{font-size:22px;line-height:1.25;margin:0 0 2px;letter-spacing:-.01em}
h2{font-size:15px;margin:24px 0 12px;color:var(--ink2);font-weight:600;
letter-spacing:.02em;text-transform:uppercase}
h2:first-of-type{margin-top:0}
.sub{color:var(--ink2);font-size:14px;margin:0 0 16px}
.tabs{display:flex;gap:6px;margin:0 0 20px;border-bottom:1px solid var(--line)}
.tab{display:block;padding:9px 14px;font-size:14px;font-weight:600;color:var(--ink2);
text-decoration:none;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab:hover{color:var(--ink)}
.tab.on{color:var(--ink);border-bottom-color:var(--blue)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;
padding:18px 16px;margin:0 0 16px}
.hero{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.hero-num{font-size:56px;font-weight:650;line-height:1;letter-spacing:-.03em;
font-variant-numeric:tabular-nums}
.hero-of{color:var(--ink3);font-size:20px}
.hero-side{color:var(--ink2);font-size:14px;margin-top:10px}
.delta{font-weight:600}
.delta.up{color:var(--good)}.delta.down{color:var(--bad)}
.tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.tiles.t3{grid-template-columns:repeat(3,1fr)}
.tile{background:var(--surface);border:1px solid var(--line);border-radius:12px;
padding:12px}
.tile.inner{background:var(--bg);border:0;padding:10px 12px}
.tile b{display:block;font-size:22px;line-height:1.2;font-variant-numeric:tabular-nums}
.tile span{font-size:12px;color:var(--ink2);line-height:1.3;display:block;margin-top:2px}
.tiles-in{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px}
.crit{padding:14px 0;border-top:1px solid var(--line)}
.crit:first-of-type{border-top:0;padding-top:0}
.crit-head{display:flex;justify-content:space-between;align-items:baseline;gap:6px 10px;
flex-wrap:wrap}
.crit-t{font-weight:600;font-size:15px;flex:1 1 60%;min-width:0}
.crit-v{font-variant-numeric:tabular-nums;font-weight:650}
.bar{position:relative;height:12px;background:var(--bg);border-radius:6px;
margin:9px 0 6px;overflow:hidden}
.bar-full{position:absolute;left:0;top:0;bottom:0;background:var(--blue);border-radius:6px}
.bar-part{position:absolute;top:0;bottom:0;background:var(--blue-soft)}
.bar-tick{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--tick);opacity:.55}
.bar-norm{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--good)}
.todo-d{margin-top:12px}
.todo-d summary{cursor:pointer;font-size:14px;color:var(--ink2);
  padding:6px 0;user-select:none}
.todo-l{margin-top:6px;max-height:340px;overflow:auto;
  border-top:1px solid var(--line)}
.todo-i{display:flex;gap:10px;align-items:baseline;padding:6px 0;
  border-bottom:1px solid var(--line);font-size:14px}
.todo-p{margin-left:auto;color:var(--ink2);font-variant-numeric:tabular-nums;
  white-space:nowrap}
.todo-h{flex:0 0 44px;color:var(--ink2);font-variant-numeric:tabular-nums}
.todo-i a{color:var(--ink);text-decoration:none;border-bottom:1px solid var(--line)}
.crit-f{font-size:13px;color:var(--ink2);margin:0}
.crit-w{font-size:12px;color:var(--ink3)}
.chart{width:100%;height:auto;display:block}
.grid{stroke:var(--line);stroke-width:1}
.ref{stroke:var(--ink3);stroke-width:1.5;stroke-dasharray:5 4}
.ax{fill:var(--ink3);font-size:11px}
.col-bar{fill:var(--blue)}
.hit{fill:transparent}
.col:hover .col-bar{fill:var(--ink)}
.call{background:var(--surface);border:1px solid var(--line);border-radius:12px;
padding:14px;margin-bottom:10px}
.call-head{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;
flex-wrap:wrap}
.call-when{font-weight:600;font-size:14px}
.chip{font-size:12px;font-weight:650;white-space:nowrap;padding:3px 9px;border-radius:20px;
border:1px solid currentColor}
.chip.good{color:var(--good)}.chip.warn{color:var(--warn)}.chip.bad{color:var(--bad)}
.lead{display:flex;gap:12px;align-items:flex-start;padding:11px 0;
border-top:1px solid var(--line)}
.lead:first-child{border-top:0}
.lead-badge{flex:0 0 46px;font-size:12px;font-weight:700;text-align:center;
padding:4px 0;border-radius:8px;background:var(--bg);color:var(--ink2);line-height:1.25}
.lead-badge.crown{background:var(--good);color:#fff}
.lead-badge.a{background:var(--blue);color:#fff}
.lead-badge.b{background:var(--blue-soft);color:var(--ink)}
.lead-badge.s80{background:var(--bad);color:#fff}
.lead-badge.s60{background:var(--serious);color:#fff}
.dismiss{display:block;margin-top:6px;margin-left:auto;font:600 12px/1 inherit;
color:var(--ink3);background:none;border:1px solid var(--line);border-radius:8px;
padding:5px 9px;cursor:pointer}
.dismiss:hover{color:var(--bad);border-color:var(--bad)}
.dismiss:disabled{opacity:.4;cursor:default}
.lead-body{flex:1 1 auto;min-width:0}
.lead-t{font-size:14.5px;font-weight:600;line-height:1.35}
.lead-m{font-size:13px;color:var(--ink2);margin-top:2px}
.lead-w{flex:0 0 auto;font-size:13px;font-variant-numeric:tabular-nums;
color:var(--ink2);text-align:right;white-space:nowrap}
.lead-w b{display:block;font-size:14px;color:var(--ink)}
.hot,.lead-w b.hot{color:var(--bad);font-weight:650}
.sm{font-size:13.5px;margin:8px 0 0;color:var(--ink2)}
.next{color:var(--ink)}
.ok{color:var(--good);font-weight:700}.no{color:var(--bad);font-weight:700}
.muted{color:var(--ink3)}
a{color:var(--blue)}
details summary{cursor:pointer;color:var(--ink3)}
details[open] summary{margin-bottom:6px}
table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:5px 6px;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{color:var(--ink3);font-weight:600}
/* качество разговоров: чарты */
.qa-lead{display:flex;gap:18px;align-items:baseline;flex-wrap:wrap;margin-bottom:2px}
.qa-lead b{font-size:34px;line-height:1;font-variant-numeric:tabular-nums}
.qa-note{font-size:13px;color:var(--ink2);line-height:1.55;margin:10px 0 0}
.qa-warn{font-size:13px;color:var(--serious);line-height:1.5;margin:8px 0 0}
.qa-grid2{display:grid;grid-template-columns:1.15fr .85fr;gap:16px;align-items:start}
@media(max-width:1100px){.qa-grid2{grid-template-columns:1fr}}
.qa-radars{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
gap:10px}
.qa-rad{text-align:center}
.qa-rad figcaption{font-size:12.5px;font-weight:600;margin-top:2px}
.qa-rad .sub2{font-weight:400}
.qa-hm{display:grid;gap:2px;font-size:12px;align-items:center}
.qa-hm .hh{font-size:11px;color:var(--ink3);text-align:center;line-height:1.15;
padding-bottom:2px;align-self:end}
.qa-hm .hn{font-weight:600;font-size:13px;white-space:nowrap}
.qa-cell{border-radius:5px;padding:6px 2px;text-align:center;
font-variant-numeric:tabular-nums;color:var(--ink)}
.qa-cell.na{color:var(--ink3);background:var(--bg)}
.qa-tbl td.q{text-align:left;color:var(--ink2);font-size:12.5px}
.qa-cards{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
@media(max-width:1100px){.qa-cards{grid-template-columns:1fr}}
.qa-face{width:44px;height:44px;border-radius:50%;background:var(--blue-soft);
color:var(--blue);font-weight:700;font-size:16px;display:flex;align-items:center;
justify-content:center;flex:0 0 44px}
.qa-chead{display:flex;gap:12px;align-items:center;margin-bottom:10px}
.qa-chead .nm{font-weight:650;font-size:15px}
.qa-blocks{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:10px 0}
.qa-b{background:var(--bg);border-radius:9px;padding:8px;text-align:center}
.qa-b b{display:block;font-size:17px;font-variant-numeric:tabular-nums}
.qa-b span{font-size:11px;color:var(--ink3)}
.qa-lists{display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:13px}
@media(max-width:700px){.qa-lists{grid-template-columns:1fr}}
.qa-lists h4{margin:0 0 4px;font-size:12px;text-transform:uppercase;
letter-spacing:.03em;color:var(--ink3)}
.qa-lists li{margin-bottom:3px;line-height:1.4}
.qa-lists ul{margin:0;padding-left:16px}
.qa-quote{border-left:3px solid var(--line);padding:4px 10px;margin:8px 0 0;
font-size:13px;color:var(--ink2);line-height:1.5}
.qa-quote.best{border-left-color:var(--good)}
.qa-quote.worst{border-left-color:var(--bad)}
.qa-quote b{color:var(--ink);font-weight:600}
/* недельный план работы */
.adv{border-top:1px solid var(--line);padding:16px 0 4px}
.adv:first-of-type{border-top:0;padding-top:2px}
.adv-h{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px}
.adv-h .nm{font-weight:650;font-size:15.5px}
.adv-h .meta{font-size:12.5px;color:var(--ink3)}
.adv-h .thin{font-size:11.5px;color:var(--serious);font-weight:600}
.adv-talk{background:var(--bg);border-radius:10px;padding:10px 12px;
font-size:13.5px;line-height:1.55;margin:0 0 10px}
.adv-talk b{color:var(--ink)}
.adv-sec{font-size:11.5px;text-transform:uppercase;letter-spacing:.03em;
color:var(--ink3);margin:10px 0 4px;font-weight:600}
.adv-list{margin:0;padding-left:0;list-style:none;font-size:13.5px}
.adv-list li{line-height:1.5;margin-bottom:7px;padding-left:26px;position:relative}
.adv-list li .n{position:absolute;left:0;top:1px;width:19px;height:19px;
border-radius:50%;background:var(--blue);color:#fff;font-size:11.5px;
font-weight:700;text-align:center;line-height:19px}
.adv-list li.good .n{background:var(--good)}
.adv-list li .code{font-weight:700}
.adv-obj{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
gap:8px;font-size:13px}
.adv-o{background:var(--bg);border-radius:9px;padding:9px 11px;line-height:1.45}
.adv-o .top{display:flex;justify-content:space-between;gap:8px;
align-items:baseline;margin-bottom:3px}
.adv-o .lbl{font-weight:650}
.adv-o .pc{font-variant-numeric:tabular-nums;font-weight:700}
.adv-o .pc.bad{color:var(--bad)}.adv-o .pc.mid{color:var(--warn)}
.adv-o .pc.ok{color:var(--good)}
.adv-o .tip{color:var(--ink2);font-size:12.5px}
.adv-bad{font-size:12.5px;color:var(--ink2);margin:6px 0 0}
.adv-bad b{color:var(--ink)}
.adv-when{font-size:12.5px;color:var(--ink3);margin:0 0 12px}
.foot{font-size:13px;color:var(--ink2);line-height:1.6}
.foot li{margin-bottom:6px}
/* эталон: кружки-донаты */
.dn{position:relative;width:78px;margin:0 auto}
.dn svg{width:78px;height:78px;transform:rotate(-90deg);display:block}
.dn-bg{fill:none;stroke:var(--line);stroke-width:7}
.dn-fg{fill:none;stroke-width:7;stroke-linecap:round}
.dn-t{position:absolute;inset:0;display:flex;flex-direction:column;
align-items:center;justify-content:center;text-align:center;pointer-events:none}
.dn-t b{font-size:14.5px;line-height:1.1;font-variant-numeric:tabular-nums}
.dn-t span{font-size:10px;color:var(--ink3);margin-top:1px}
.dn-tr{position:absolute;top:0;right:2px;font-style:normal;font-size:12px;font-weight:700}
.dn-tr.up{color:var(--good)}.dn-tr.down{color:var(--bad)}.dn-tr.flat{color:var(--ink3)}
.dn.none svg{opacity:.35}
.dn.none .dn-t b{color:var(--ink3);font-weight:600}
.et-grid{display:grid;gap:10px 8px;align-items:center;margin:6px 0 2px}
.et-h{font-size:12px;color:var(--ink2);text-align:center;line-height:1.3;align-self:end}
.et-h b{display:block;font-size:12.5px;color:var(--ink)}
.et-h .bestof{color:var(--ink3);font-size:11px;display:block;margin-top:1px}
.et-name{font-weight:600;font-size:14px}
.et-name .star{display:block;font-size:11px;color:var(--blue)}
.et-name .who2{display:block;font-size:11px;color:var(--ink3);font-weight:400}
.et-legend{font-size:13px;color:var(--ink2);margin:2px 0 14px;line-height:1.55}
.gap-i{padding:9px 0;border-top:1px solid var(--line);font-size:14px;line-height:1.5}
.gap-i:first-child{border-top:0;padding-top:2px}
.gap-price{color:var(--ink2);font-size:13px;display:block}
.gap-n{display:inline-block;width:20px;height:20px;border-radius:50%;
background:var(--blue);color:#fff;font-size:12px;font-weight:700;text-align:center;
line-height:20px;margin-right:7px}
.et-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
@media(max-width:1100px){.et-cards{grid-template-columns:1fr}}
@media(max-width:900px){.dn{width:62px}.dn svg{width:62px;height:62px}
.dn-t b{font-size:12.5px}}
@media(max-width:480px){.hero-num{font-size:46px}
.tiles,.tiles.t3{grid-template-columns:1fr 1fr}
.tab{padding:9px 10px;font-size:13.5px}}
"""


def shell(tab_key, mgr, built, body, tabs=None, wide=False):
    tabs = tabs or TABS
    nav = "".join(
        f'<a class="tab{" on" if k == tab_key else ""}" href="{href or "./"}">{t}</a>'
        for k, href, t in tabs)
    title = dict((k, t) for k, _, t in tabs)[tab_key]
    nav_block = f'<nav class="tabs">{nav}</nav>' if len(tabs) > 1 else ""
    return f"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<meta name="referrer" content="no-referrer">
<title>{e(title)} · {e(mgr)}</title>
<style>{CSS}</style>
</head><body><div class="wrap{" wide" if wide else ""}">
<a class="back" id="backlink" hidden href="#">← Все панели</a>
<h1>{e(mgr)}</h1>
<p class="sub">Обновлено {e(built)}</p>
{nav_block}
{body}
<script>
// Ссылка «Все панели» видна только тому браузеру, который открывал хаб:
// адрес хаба хранится локально, в разметку страниц он не попадает.
try {{
  var h = localStorage.getItem("ropbot_hub");
  if (h && location.pathname.indexOf(h) !== 0) {{
    var b = document.getElementById("backlink");
    b.href = h; b.hidden = false;
  }}
}} catch (err) {{}}
</script>
</div></body></html>"""


def bullet(pct_full, pct_part=None, tick=None, norm=None):
    w_full = max(0.0, min(100.0, pct_full or 0))
    w_part = max(0.0, min(100.0 - w_full, pct_part or 0))
    out = [f'<div class="bar"><div class="bar-full" style="width:{w_full:.1f}%"></div>']
    if w_part:
        out.append(f'<div class="bar-part" style="left:{w_full:.1f}%;width:{w_part:.1f}%"></div>')
    if tick is not None:
        out.append(f'<div class="bar-tick" style="left:{max(0, min(100, tick)):.1f}%"></div>')
    if norm is not None:
        out.append(f'<div class="bar-norm" style="left:{max(0, min(100, norm)):.1f}%"></div>')
    out.append("</div>")
    return "".join(out)


# ── вкладка «Заявки» ─────────────────────────────────────────────────────────

QUEUE_SQL = """
SELECT l.id, l.date_create, l.phone_e164, l.form_model, l.ip_city, l.form_text,
       l.title, m.value AS mark, lc.score AS comp, lc.crown,
       t.out_calls, t.out_own, t.talks, t.last_talk, t.last_out
  FROM leads l
  LEFT JOIN lead_marks m     ON m.lead_id = l.id
  LEFT JOIN lead_composite lc ON lc.lead_id = l.id
  -- окно звонков открываем от ПЕРВОЙ карточки этого номера: иначе дубль
  -- выглядит брошенным, хотя клиента ведут по соседней заявке (правило 7)
  LEFT JOIN LATERAL (SELECT min(l2.date_create) d FROM leads l2
                      WHERE l2.phone_e164 = l.phone_e164) f ON TRUE
  LEFT JOIN LATERAL (
      SELECT count(*) FILTER (WHERE c.direction = 'out')                  AS out_calls,
             count(*) FILTER (WHERE c.direction = 'out'
                                AND c.portal_user_id = l.assigned_by)     AS out_own,
             count(*) FILTER (WHERE c.duration > 0)                       AS talks,
             max(c.call_start) FILTER (WHERE c.duration > 0)              AS last_talk,
             max(c.call_start) FILTER (WHERE c.direction = 'out')         AS last_out
        FROM calls c
       WHERE c.phone_e164 = l.phone_e164
         AND c.call_start >= f.d - interval '30 minutes') t ON TRUE
 WHERE l.assigned_by = %(uid)s
   AND l.status_semantic = 'P'
   AND coalesce(l.phone_kind, '') NOT IN ('junk', 'none')
   AND l.source_id IS DISTINCT FROM 'PARTNER'
"""
QUEUE_COLS = ("id date_create phone_e164 form_model ip_city form_text title mark "
              "comp crown out_calls out_own talks last_talk last_out").split()


def grade_of(mark):
    return mark[0] if mark and mark[0] in "ABCD" else None


def prio(r):
    if r["crown"]:
        return 0
    return {"A": 1, "B": 2, "C": 3, "D": 4}.get(grade_of(r["mark"]), 5)


def badge(r):
    if r["crown"]:
        return '<div class="lead-badge crown">♛<br>живой</div>'
    g = grade_of(r["mark"])
    approx = "≈" if r["mark"] and r["mark"].endswith("≈") else ""
    if g == "A":
        return f'<div class="lead-badge a">⚡<br>A{approx}</div>'
    if g == "B":
        return f'<div class="lead-badge b">★<br>B{approx}</div>'
    if g:
        return f'<div class="lead-badge">{g}{approx}</div>'
    return '<div class="lead-badge">без<br>класса</div>'


MARKS = "⚡★♛♕♔❗≈"
FIELD_RX = {
    "бюджет": re.compile(r"Бюджет:\s*([^\n]{1,40})", re.I),
    "срок": re.compile(r"Срок:\s*([^\n]{1,30})", re.I),
    "руль": re.compile(r"Руль:\s*([^\n]{1,15})", re.I),
}


def describe(r):
    """Человеческая строка заявки: имя, марка, город, бюджет, срок.

    Название лида в Битриксе имеет вид «★ 44197 / Василий / Kia» — значок,
    номер, имя, марка. Значок ставит marker.py, здесь он лишний.
    """
    title = (r["title"] or "").lstrip(MARKS + " ").strip()
    # телефон в названии («Пропущенный вызов 7901...») не показываем открытым:
    # на странице номера всегда в маске, полный — в карточке Битрикса
    title = re.sub(r"\+?\d[\d\s()-]{8,}\d", "", title).strip(" ,·-")
    parts = [p.strip() for p in title.split("/")]
    name = parts[1] if len(parts) > 1 else ""
    model = (r["form_model"] or "").strip() or (parts[2] if len(parts) > 2 else "")

    head = " · ".join(x for x in (name, model) if x)
    if not head:
        # «44190 /  /» — форма пришла без имени и марки, показывать нечего
        if title.lower().startswith("venyoo"):
            head = "Чат на сайте"
        elif "Квиз" in (r["form_text"] or ""):
            head = "Заявка из квиза"
        elif "/" in title:
            head = "Заявка с сайта"
        else:
            head = title or "Заявка"

    text = r["form_text"] or ""
    meta = []
    city = (r["ip_city"] or "").strip()
    if city and "not found" not in city.lower():
        meta.append(city)
    for label, rx in FIELD_RX.items():
        m = rx.search(text)
        if m:
            meta.append(f"{label} {m.group(1).strip().rstrip('.,')}")
    return head, meta


def lead_row(r, now, right_label, right_value, hot=False):
    what, meta = describe(r)
    link = crm_link("LEAD", r["id"])
    others = ""
    if (r["out_calls"] or 0) > 0 and (r["out_own"] or 0) == 0:
        others = ' · <span class="hot">звонил другой менеджер</span>'
    line2 = " · ".join([e(mask(r["phone_e164"]))] + [e(m) for m in meta])
    return (f'<div class="lead">{badge(r)}<div class="lead-body">'
            f'<div class="lead-t">{e(what)}</div>'
            f'<div class="lead-m">{line2}'
            + (f' · <a href="{link}" target="_blank" rel="noopener">карточка</a>' if link else '')
            + f'{others}</div></div>'
            f'<div class="lead-w"><b class="{"hot" if hot else ""}">{right_value}</b>'
            f'{right_label}</div></div>')


def page_queue(conn, uid, now):
    rs = rows(conn.cursor(), QUEUE_SQL, {"uid": uid}, QUEUE_COLS)
    for r in rs:
        r["date_create"] = r["date_create"].astimezone(VLD)
        for k in ("last_talk", "last_out"):
            if r[k]:
                r[k] = r[k].astimezone(VLD)

    cold = [r for r in rs if not r["out_calls"]]
    push = [r for r in rs if r["out_calls"] and not r["talks"]]
    work = [r for r in rs if r["talks"]]
    silent = [r for r in work
              if r["last_talk"] and (now - r["last_talk"]).days >= SILENCE_DAYS]
    crowns = sum(1 for r in rs if r["crown"])

    cold.sort(key=lambda r: (prio(r), r["date_create"]))
    push.sort(key=lambda r: (prio(r), r["date_create"]))
    silent.sort(key=lambda r: r["last_talk"])

    def block(title, note, items, label, value, hot=lambda r: False, cap=40):
        if not items:
            return (f'<h2>{title}</h2><section class="card">'
                    f'<p class="muted">Пусто — и это хорошо.</p></section>')
        shown = items[:cap]
        tail = ("" if len(items) <= cap else
                f'<p class="crit-f" style="margin-top:12px">Показаны первые {cap} '
                f'из {len(items)}, остальные — в CRM.</p>')
        return (f'<h2>{title}</h2><section class="card">'
                f'<p class="crit-f" style="margin-bottom:10px">{note}</p>'
                + "".join(lead_row(r, now, label, value(r), hot(r)) for r in shown)
                + tail + '</section>')

    body = [
        '<div class="tiles">'
        f'<div class="tile"><b>{len(rs)}</b><span>заявок в работе</span></div>'
        f'<div class="tile"><b>{len(cold)}</b><span>без единого звонка</span></div>'
        f'<div class="tile"><b>{len(push)}</b><span>не дозвонились</span></div>'
        f'<div class="tile"><b>{crowns}</b><span>♛ живых лидов</span></div>'
        '</div>',

        block("Ни одного звонка", "Сверху — ♛ живые лиды и класс A: по ним "
              "покупают в 6–11 раз чаще, чем по классу D.",
              cold, "ждёт", lambda r: ago(r["date_create"], now),
              hot=lambda r: (now - r["date_create"]).days >= 1),

        block("Звонили, но не дозвонились",
              "Норма — пять попыток за три дня. При одной попытке покупает "
              "0,50% заявок, при пяти — 2,51%, при десяти и больше — 8,77%.",
              push, "попыток",
              lambda r: f'{r["out_calls"]} из 5',
              hot=lambda r: (r["out_calls"] or 0) < 5),

        block(f"Разговор был, тишина от {SILENCE_DAYS} дней",
              "Медиана цикла сделки — 8,6 дня. Тишина дольше недели обычно "
              "означает, что следующий шаг не был назначен с датой.",
              silent, "тишина", lambda r: ago(r["last_talk"], now),
              hot=lambda r: (now - r["last_talk"]).days >= 7),

        '<h2>Как читать</h2><section class="card"><ul class="foot">'
        '<li>♛ — составная оценка 60 и выше: поведение на сайте, разбор разговоров '
        'и отклик клиента вместе. ⚡ A и ★ B — класс по поведению до звонка.</li>'
        '<li>Заявки с непригодным номером в список не попадают — набирать нечего. '
        'PARTNER исключён.</li>'
        '<li>Звонки считаются по номеру клиента от первой его карточки, а не по '
        'этой заявке: если клиента вели по соседнему дублю, тут это видно.</li>'
        f'<li>В работе — статус «в работе» в CRM. Всего разговоров ведётся '
        f'{len(work)}, из них тишина от {SILENCE_DAYS} дней у {len(silent)}.</li>'
        '</ul></section>',
    ]
    return "".join(body)


# ── вкладка «Разговоры» ──────────────────────────────────────────────────────

TALKS_SQL = """
  SELECT c.id, (c.call_start AT TIME ZONE %(tz)s), c.duration, c.direction,
         c.phone_e164, c.crm_entity_type, c.crm_entity_id,
         s.card_score, s.raw, s.outcome, s.next_step, s.summary
    FROM call_scores s
    JOIN calls c    ON c.id = s.call_id
   WHERE s.card_score IS NOT NULL AND s.raw IS NOT NULL
     AND c.portal_user_id IN (SELECT portal_user_id FROM dash_tokens
                               WHERE active AND kind = 'manager')
     AND coalesce(c.phone_kind, '') NOT IN ('junk', 'none')
     AND (c.call_start AT TIME ZONE %(tz)s)
         >= (now() AT TIME ZONE %(tz)s) - make_interval(days => %(w)s)
"""
TALKS_COLS = ("call_id ts duration direction phone_e164 crm_entity_type crm_entity_id "
              "card_score raw outcome next_step summary").split()


def levels(r):
    """Уровень 0/1/2 по каждому критерию — та же арифметика, что в app/card.py."""
    return {
        "next_step": 2 if r.get("next_step_specific") else (
            1 if r.get("next_step_proposed") else 0),
        "decision_maker": 2 if r.get("decision_maker_identified") else 0,
        "value_to_pain": 2 if r.get("linked_to_client_pain") else (
            1 if (r.get("value_props_n") or 0) >= 2 else 0),
        "date_fixed": 2 if r.get("date_time_fixed") else 0,
        "timeline": 2 if r.get("timeline_discussed") else 0,
    }


def card_rates(rs):
    n = len(rs)
    full = {k: 0 for k, *_ in CRITERIA}
    part = {k: 0 for k, *_ in CRITERIA}
    for r in rs:
        lv = levels(r["raw"])
        for k in full:
            if lv[k] == 2:
                full[k] += 1
            elif lv[k] == 1:
                part[k] += 1
    if not n:
        return {k: (None, None) for k in full}
    return {k: (100.0 * full[k] / n, 100.0 * part[k] / n) for k in full}


def avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def daily_svg(days, dept_avg):
    if not days:
        return "<p class='muted'>Пока нет разобранных разговоров.</p>"
    W, H = 520, 215
    pad_l, pad_b, pad_t = 28, 28, 10
    plot_w, plot_h = W - pad_l - 8, H - pad_b - pad_t
    n = len(days)
    step = plot_w / n
    bw = min(26.0, step * 0.62)
    out = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" '
           f'aria-label="Средний балл карточки по дням">']
    for v in (0, 25, 50, 75, 100):
        y = pad_t + plot_h * (1 - v / 100)
        out.append(f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{W - 8}" y2="{y:.1f}"/>')
        out.append(f'<text class="ax" x="{pad_l - 6}" y="{y + 3.5:.1f}" text-anchor="end">{v}</text>')
    if dept_avg is not None:
        y = pad_t + plot_h * (1 - dept_avg / 100)
        out.append(f'<line class="ref" x1="{pad_l}" y1="{y:.1f}" x2="{W - 8}" y2="{y:.1f}"/>')
    for i, (d, cnt, val) in enumerate(days):
        x = pad_l + step * i + (step - bw) / 2
        h = plot_h * (val / 100)
        y = pad_t + plot_h - h
        out.append(f'<g class="col"><title>{e(ru_date(d))} · {cnt} разгов. · '
                   f'балл {num(val, 1)}</title>'
                   f'<rect class="hit" x="{pad_l + step * i:.1f}" y="{pad_t}" '
                   f'width="{step:.1f}" height="{plot_h}"/>'
                   f'<rect class="col-bar" x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                   f'height="{max(h, 2):.1f}" rx="4"/></g>')
        if n <= 12 or i % 2 == 0:   # без последней: налезала на предпоследнюю
            out.append(f'<text class="ax" x="{pad_l + step * i + step / 2:.1f}" '
                       f'y="{H - 8}" text-anchor="middle">{d.day}.{d.month:02d}</text>')
    out.append("</svg>")
    return "".join(out)


def chip(score):
    if score >= 50:
        return f'<span class="chip good">● сильный · {num(score)}</span>'
    if score >= 25:
        return f'<span class="chip warn">◐ средний · {num(score)}</span>'
    return f'<span class="chip bad">○ слабый · {num(score)}</span>'


def call_card(r):
    lv = levels(r["raw"])
    miss = [t for k, w, t, _ in CRITERIA if lv[k] == 0]
    got = [t for k, w, t, _ in CRITERIA if lv[k] == 2]
    link = crm_link(r["crm_entity_type"], r["crm_entity_id"])
    ts = r["ts"]
    head = (f'{e(ru_date(ts.date()))}, {ts.strftime("%H:%M")} · {mmss(r["duration"])} · '
            f'{"исходящий" if r["direction"] == "out" else "входящий"}')
    p = [f'<article class="call"><div class="call-head"><div>'
         f'<div class="call-when">{head}</div>'
         f'<div class="muted sm">{e(mask(r["phone_e164"]))}'
         + (f' · <a href="{link}" target="_blank" rel="noopener">карточка в Битриксе</a>'
            if link else '')
         + f'</div></div>{chip(float(r["card_score"]))}</div>']
    if got:
        p.append(f'<p class="sm"><span class="ok">✓</span> '
                 f'{e(", ".join(t.lower() for t in got))}</p>')
    if miss:
        p.append(f'<p class="sm"><span class="no">✗</span> нет: '
                 f'{e(", ".join(t.lower() for t in miss))}</p>')
    if r["next_step"]:
        p.append(f'<p class="sm next">Следующий шаг: {e(r["next_step"])}</p>')
    if r["summary"]:
        p.append(f'<details class="sm"><summary>О чём был разговор</summary>'
                 f'<p>{e(r["summary"])}</p></details>')
    p.append("</article>")
    return "".join(p)


def page_talks(conn, uid):
    cur = conn.cursor()
    p = {"tz": TZ, "uid": uid, "w": W_TALKS}
    me = rows(cur, TALKS_SQL + " AND c.portal_user_id = %(uid)s ORDER BY c.call_start DESC",
              p, TALKS_COLS)
    dept = rows(cur, TALKS_SQL + " ORDER BY c.call_start DESC", p, TALKS_COLS)

    days = [(d, c, float(v)) for d, c, v in conn.execute("""
        SELECT ts::date, count(*), round(avg(card_score), 1) FROM (
            SELECT (c.call_start AT TIME ZONE %(tz)s) AS ts, s.card_score
              FROM call_scores s JOIN calls c ON c.id = s.call_id
             WHERE s.card_score IS NOT NULL AND c.portal_user_id = %(uid)s
               AND coalesce(c.phone_kind, '') NOT IN ('junk', 'none')
               AND (c.call_start AT TIME ZONE %(tz)s)
                   >= (now() AT TIME ZONE %(tz)s) - make_interval(days => %(w)s)) x
         GROUP BY 1 ORDER BY 1""", p).fetchall()]

    def win(user, a, b):
        q = TALKS_SQL.replace("make_interval(days => %(w)s)", "make_interval(days => %(a)s)")
        q += ("AND (c.call_start AT TIME ZONE %(tz)s) < "
              "(now() AT TIME ZONE %(tz)s) - make_interval(days => %(b)s) ")
        if user:
            q += "AND c.portal_user_id = %(uid)s "
        r = conn.execute(f"SELECT avg(card_score), count(*) FROM ({q}) x",
                         {"tz": TZ, "uid": uid, "a": a, "b": b}).fetchone()
        return float(r[0]) if r[0] is not None and r[1] >= 15 else None

    me7, prev7 = win(True, 7, 0), win(True, 14, 7)
    n = len(me)
    my_avg = avg([float(r["card_score"]) for r in me])
    dept_avg = avg([float(r["card_score"]) for r in dept])
    strong = sum(1 for r in me if float(r["card_score"]) >= 50)
    weak = sum(1 for r in me if float(r["card_score"]) < 25)

    delta = ""
    if me7 is not None and prev7 is not None:
        d = me7 - prev7
        cls = "up" if d > 0 else ("down" if d < 0 else "")
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "=")
        delta = (f'<br>За последние 7 дней у вас <b>{num(me7, 1)}</b> — '
                 f'<span class="delta {cls}">{arrow} {num(abs(d), 1)}</span> '
                 f'к предыдущей неделе')

    table = "".join(f'<tr><td>{e(ru_date(d))}</td><td>{c}</td><td>{num(v, 1)}</td></tr>'
                    for d, c, v in days)
    calls = "".join(call_card(r) for r in me[:40])

    return f"""
<section class="card">
  <div class="hero"><span class="hero-num">{num(my_avg, 1)}</span>
    <span class="hero-of">из 100 баллов</span></div>
  <p class="hero-side">Последние {W_TALKS} дней. По отделу {num(dept_avg, 1)}{delta}</p>
  <div class="tiles-in">
    <div class="tile inner"><b>{n}</b><span>разговоров разобрано</span></div>
    <div class="tile inner"><b>{num(100 * strong / n if n else None)}%</b>
      <span>сильных, от 50 баллов</span></div>
    <div class="tile inner"><b>{num(100 * weak / n if n else None)}%</b>
      <span>слабых, меньше 25</span></div>
  </div>
</section>

<h2>По дням</h2>
<section class="card">{daily_svg(days, dept_avg)}
  <p class="crit-f">Средний балл за день. Пунктир — среднее по отделу за период.
    В днях с 5–10 разговорами один плохой звонок двигает столбик заметно —
    смотреть на уровень, а не на скачки.</p>
  <details class="sm"><summary>Показать таблицей</summary>
    <table><thead><tr><th>День</th><th>Разговоров</th><th>Балл</th></tr></thead>
    <tbody>{table}</tbody></table></details>
</section>

<h2>Разговоры</h2>
{calls or '<section class="card"><p class="muted">Разобранных разговоров за период нет.</p></section>'}

<h2>Как это считается</h2>
<section class="card"><ul class="foot">
  <li>Карточка собрана из разбора 386 расшифровок: сравнили 76 выигранных разговоров
      с 98 проигранными, зеркальных по месяцу, источнику и менеджеру. Веса критериев
      пропорциональны разрыву между группами.</li>
  <li>Проверка на отложенной выборке дала AUC 0,737 против 0,692 на обучающей —
      карточка не подогнана под данные, на которых её строили.</li>
  <li>В разбор попадают разговоры от 90 секунд за последние три недели.
      Звонки на непригодные номера отброшены.</li>
  <li><b>Инструмент для планёрок и отбора записей на прослушивание, а не для премий:</b>
      каждый третий проигранный разговор набирает больше 50 баллов.</li>
  <li>Балл ставит не модель на глаз — модель отвечает на конкретные вопросы
      по расшифровке, арифметику считает код.</li>
</ul></section>"""


# ── вкладка «Правила» ────────────────────────────────────────────────────────

RULES_SQL = """
  SELECT assigned_by,
         count(*)                                   AS leads,
         count(*) FILTER (WHERE NOT reached)        AS not_reached,
         count(*) FILTER (WHERE reached)            AS reached,
         count(*) FILTER (WHERE untouched)          AS untouched,
         count(*) FILTER (WHERE calls_own > 0)      AS own_called,
         count(*) FILTER (WHERE f_persisted)        AS persisted,
         count(*) FILTER (WHERE f_dense)            AS dense,
         count(*) FILTER (WHERE f_deep_first)       AS deep_first,
         count(*) FILTER (WHERE f_client_back)      AS client_back,
         count(*) FILTER (WHERE f_spread)           AS spread
    FROM v_lead_ideal
   WHERE assigned_by IN (SELECT portal_user_id FROM dash_tokens
                          WHERE active AND kind = 'manager')
     AND date_create >= now() - make_interval(days => %(a)s)
     AND date_create <  now() - make_interval(days => %(b)s)
   GROUP BY 1
"""
RULES_COLS = ("uid leads not_reached reached untouched own_called persisted dense "
              "deep_first client_back spread").split()


def page_rules(conn, uid):
    cur = conn.cursor()
    p = {"a": W_RULES_FROM, "b": W_RULES_TO, "uid": uid}
    rs = rows(cur, RULES_SQL, p, RULES_COLS)
    mine = next((r for r in rs if r["uid"] == uid), None)
    if not mine or not mine["leads"]:
        return ('<section class="card"><p class="muted">За период нет заявок, '
                'считать нечего.</p></section>')

    # В сравнение берём только тех, кто реально работает заявки: 100+ заявок и
    # собственные исходящие больше чем по половине. Иначе среднее по отделу
    # тянет вниз человек с нулём звонков, и все выглядят лучше, чем есть.
    peers = [r for r in rs if r["leads"] >= 100 and r["own_called"] >= 0.5 * r["leads"]]
    total = {k: sum(r[k] for r in peers) for k in RULES_COLS if k != "uid"}

    def pct(r, key, denom):
        d = r[denom]
        return None if not d else 100.0 * r[key] / d

    blocks = []
    for key, title, denom, norm, lift, why in RULES:
        mv, dv = pct(mine, key, denom), pct(total, key, denom)
        base = {"leads": "заявок", "reached": "заявок с разговором",
                "not_reached": "заявок без разговора"}[denom]
        lift_s = (f' Такие заявки покупают в {lift} раза чаще.' if lift else '')
        blocks.append(
            f'<div class="crit"><div class="crit-head">'
            f'<span class="crit-t">{e(title)}</span>'
            f'<span class="crit-v">{num(mv)}%<span class="crit-w"> · норма {norm}%'
            f'</span></span></div>{bullet(mv, tick=dv, norm=norm)}'
            f'<p class="crit-f">{e(why)}{lift_s} '
            f'<span class="crit-w">Считается от {base}: {mine[denom]} шт. '
            f'По отделу {num(dv)}%.</span></p></div>')

    # Правило 6 — приоритет: медиана рабочих минут до контакта по классам
    pr = conn.execute("""
        SELECT grade, count(*),
               percentile_cont(0.5) WITHIN GROUP (ORDER BY touch_min)
                   FILTER (WHERE NOT untouched)
          FROM v_lead_ideal
         WHERE assigned_by = %(uid)s AND grade IN ('A','B','C','D')
           AND date_create >= now() - make_interval(days => %(a)s)
         GROUP BY 1 ORDER BY 1""", p).fetchall()
    prio_rows = "".join(
        f'<tr><td>{"⚡ A" if g == "A" else "★ B" if g == "B" else g}</td>'
        f'<td>{c}</td><td>{num(float(m)) if m is not None else "—"}</td></tr>'
        for g, c, m in pr)
    a_row = next((r for r in pr if r[0] == "A"), None)
    cd = [float(r[2]) for r in pr if r[0] in ("C", "D") and r[2] is not None]
    verdict = "Данных мало, судить рано"
    if a_row and a_row[2] is not None and cd:
        am, cdm = float(a_row[2]), sum(cd) / len(cd)
        if am <= 15:
            verdict = "Норма выполняется"
        elif am < cdm * 0.7:
            verdict = (f"Приоритет есть: A обрабатывается в {num(cdm / am, 1)} раза "
                       f"быстрее C и D. Но до нормы далеко — {num(am)} "
                       f"{plural(round(am), 'минута', 'минуты', 'минут')} против пятнадцати")
        elif am > cdm:
            verdict = (f"Обратный приоритет: A обрабатывается медленнее прочих "
                       f"классов — {num(am)} против {num(cdm)} "
                       f"{plural(round(cdm), 'минуты', 'минут', 'минут')}")
        else:
            verdict = (f"Приоритета нет: A идёт наравне с остальными — {num(am)} "
                       f"{plural(round(am), 'минута', 'минуты', 'минут')} против нормы "
                       f"в пятнадцать")

    return f"""
<section class="card">
  <p class="crit-f">Пять правил ниже выведены не из мнений, а из признаков,
  у которых в нашей базе нашлась связь с покупкой: 33 190 заявок, 355 покупок.
  Зелёный штрих на полосе — норма, серый — среднее по отделу.
  Окно: заявки, созданные от {W_RULES_FROM} до {W_RULES_TO} дней назад
  ({mine['leads']} шт.) — по свежим трёхдневное окно правил ещё не закрылось.</p>
</section>

<section class="card">{''.join(blocks)}</section>

<h2>Шестое правило: сначала ⚡</h2>
<section class="card">
  <p class="crit-f" style="margin-bottom:10px">Медиана <b>рабочих</b> минут
  до первого контакта по классам заявки. Норма — 15 минут по классу A.</p>
  <table><thead><tr><th>Класс</th><th>Заявок</th><th>Минут до контакта</th></tr></thead>
  <tbody>{prio_rows or '<tr><td colspan="3">нет размеченных заявок</td></tr>'}</tbody></table>
  <p class="crit-f" style="margin-top:10px"><b>{e(verdict)}.</b>
  Класс A покупает в 6–11 раз чаще класса D, поэтому порядок обзвона важнее
  скорости обзвона.</p>
</section>

<h2>Оговорки</h2>
<section class="card"><ul class="foot">
  <li>Множители — связь, а не доказанная причина: менеджеры и сами упорнее
      с перспективными клиентами.</li>
  <li>Классы есть только с 1 августа и покрывают чуть больше половины заявок —
      шестое правило считается вслепую по остальным.</li>
  <li>Среднее по отделу — по тем, кто реально работает заявки: от 100 заявок
      за период и собственные исходящие больше чем по половине из них.
      Иначе среднее тянет вниз человек с нулём звонков.</li>
  <li>Звонки считаются по номеру клиента, а не по карточке: дубли не портят счёт.
      Заявки с непригодным номером и PARTNER исключены.</li>
</ul></section>"""


# ── сборка ───────────────────────────────────────────────────────────────────

# ── вкладка «Скрипт»: карточка оценки v2 (реестр, «Версия 2», 22.08) ─────────
# ключ, вес, название, слабая сторона (2-е лицо), как исправить
SCRIPT_ITEMS = [
    ("next_step", 25, "Конкретный следующий шаг",
     "Не договариваешься о конкретном следующем шаге",
     "Заканчивать разговор формулой «жду от вас X, и я сделаю Y». Главное "
     "отличие выигранных разговоров: 68% против 41%."),
    ("qual", 18, "Квалификация до расчёта: авто · бюджет · срок",
     "Считаешь расчёт без квалификации",
     "До расчёта знать три вещи: какой автомобиль, какой бюджет, когда покупка. "
     "Выяснено меньше двух из трёх — весь балл разговора умножается на 0,7."),
    ("contract", 15, "Предложен договор",
     "Не предлагаешь договор",
     "Предложить прямо: «отправлю договор — посмотрите». Второе по силе отличие "
     "победителей: 60% против 36%."),
    ("safety", 12, "Проговорена безопасность сделки",
     "Не проговариваешь безопасность сделки",
     "Не ждать вопроса: как защищена предоплата, юрлицо, реквизиты; сильнее "
     "всего — показать вживую по видео. Доверие — главное возражение купивших, "
     "вдвое чаще цены."),
    ("decision_maker", 10, "Выяснено, кто участвует в решении",
     "Не выясняешь, кто участвует в решении",
     "Спросить прямо: «С кем будете советоваться?» Жена, партнёр, отец — тот, "
     "кто в итоге скажет «да»."),
    ("value_to_pain", 10, "Выгода привязана к словам клиента",
     "Перечисляешь преимущества вместо ответа на слова клиента",
     "Не список плюсов компании, а ответ на то, что клиент сам назвал важным."),
    ("date_fixed", 10, "Названы дата и время следующего шага",
     "Не называешь дату и время следующего шага",
     "Не «на неделе», а «в четверг после обеда». Разрыв шестикратный: "
     "12% против 2%."),
]

STRONG = {
    "next_step": "Договариваешься о конкретном следующем шаге",
    "qual": "Квалифицируешь до расчёта: авто, бюджет, срок",
    "contract": "Предлагаешь договор",
    "safety": "Проговариваешь безопасность сделки",
    "decision_maker": "Выясняешь, кто участвует в решении",
    "value_to_pain": "Привязываешь выгоду к словам клиента",
    "date_fixed": "Называешь дату и время следующего шага",
}


def qual_n(raw):
    """Сколько из трёх квалификационных вещей известно: авто, бюджет, срок."""
    g = raw.get
    return ((1 if g("car") else 0) + (1 if g("budget_rub") else 0)
            + (1 if g("timeline_discussed") else 0))


def script_credits(r):
    """Зачёт по пунктам v2. r — строка fetch_scored (raw + флаги расшифровки)."""
    g = r["raw"].get
    return {
        "next_step": 1.0 if g("next_step_specific") else (
            0.5 if g("next_step_proposed") else 0.0),
        "qual": qual_n(r["raw"]) / 3.0,
        "contract": 1.0 if r["tx_contract"] else 0.0,
        "safety": 1.0 if (r["tx_safety"] or r["tx_video"]) else 0.0,
        "decision_maker": 1.0 if g("decision_maker_identified") else 0.0,
        "value_to_pain": 1.0 if g("linked_to_client_pain") else (
            0.5 if (g("value_props_n") or 0) >= 2 else 0.0),
        "date_fixed": 1.0 if (g("date_time_fixed") or g("next_step_dated")) else 0.0,
    }


def script_score(r):
    cr = script_credits(r)
    total = sum(w * cr[k] for k, w, *_ in SCRIPT_ITEMS)
    if qual_n(r["raw"]) < 2:      # требование 1 карточки v2
        total *= 0.7
    return total


def script_rates(rs):
    acc = {k: [0.0, 0] for k, *_ in SCRIPT_ITEMS}
    for r in rs:
        for k, v in script_credits(r).items():
            acc[k][0] += v
            acc[k][1] += 1
    return {k: (100.0 * s / n if n else None) for k, (s, n) in acc.items()}


FETCH_COLS = ("uid ts duration phone_e164 crm_entity_type crm_entity_id raw "
              "next_step summary tx_contract tx_video tx_safety is_first").split()


_CACHE = {}          # живёт в пределах одного процесса сборки


def fetch_scored(conn, frm):
    """Разобранные разговоры рабочего состава с балла v2 и флагом первого
    содержательного разговора. Договор/видео/безопасность — регулярки по
    расшифровке (безопасность — прокси до расширения промта разбора)."""
    ck = ("scored", frm)
    if ck in _CACHE:
        return _CACHE[ck]
    rs = rows(conn.cursor(), """
        SELECT c.portal_user_id AS uid, (c.call_start AT TIME ZONE %(tz)s) AS ts,
               c.duration, c.phone_e164, c.crm_entity_type, c.crm_entity_id,
               s.raw, s.next_step, s.summary,
               coalesce(t.text ~* 'договор', false)          AS tx_contract,
               coalesce(t.text ~* 'видео|трансляц', false)   AS tx_video,
               coalesce(t.text ~* 'безопасн|гарант|защит|юрлиц|реквизит|юридическ',
                        false)                               AS tx_safety,
               NOT EXISTS (SELECT 1 FROM calls c2
                            WHERE c2.phone_e164 = c.phone_e164
                              AND c2.duration >= 60
                              AND c2.call_start < c.call_start) AS is_first
          FROM call_scores s
          JOIN calls c ON c.id = s.call_id
          LEFT JOIN transcripts t ON t.call_id = c.id
         WHERE s.raw IS NOT NULL
           AND coalesce(c.phone_kind, '') NOT IN ('junk', 'none')
           AND c.portal_user_id IN (SELECT portal_user_id FROM dash_tokens
                                     WHERE active AND kind = 'manager')
           AND (c.call_start AT TIME ZONE %(tz)s) >= %(frm)s
         ORDER BY c.call_start DESC""",
        {"tz": TZ, "frm": frm}, FETCH_COLS)
    for r in rs:
        r["score"] = script_score(r)
    _CACHE[ck] = rs
    return rs


# Образцовый разговор v2. Формулы — из выигранных разговоров отдела
# (эталонный скрипт, версия 2), цифры — измеренные разрывы WON/LOST.
IDEAL_TALK = [
    ("Повод и открытие",
     [("М", "Добрый день, Сергей! Автосалон, меня зовут Дмитрий. Вы оставляли "
            "заявку на Honda Freed — посмотрел её перед звонком, есть пара "
            "хороших вариантов. Удобно пару минут?")],
     "Короткий повод и сразу к делу — без длинной презентации."),
    ("Задача клиента",
     [("М", "Чтобы не гонять вас по каталогу: для чего берёте машину? Кто "
            "будет ездить, что сейчас не устраивает?"),
      ("К", "Жене на работу и детей возить. Сейчас седан — тесно с колясками.")],
     "Не «какая комплектация», а какая задача. К словам клиента дальше "
     "привяжется выгода (вес 10)."),
    ("Квалификация до расчёта: авто · бюджет · срок",
     [("М", "По деньгам на какую сумму рассчитываете?"),
      ("К", "До миллиона трёхсот."),
      ("М", "И когда планируете покупку — в этом месяце или присматриваетесь?")],
     "До расчёта знать три вещи: автомобиль, бюджет, срок (вес 18, по 6 за "
     "каждую). Выяснено меньше двух — весь балл разговора умножается на 0,7. "
     "Не анкетой в лоб, а по ходу разговора: побеждает «знает», а не «спросил»."),
    ("Кто участвует в решении",
     [("М", "Решать будете вместе с женой? Тогда пришлю подборку так, чтобы "
            "удобно было показать ей.")],
     "Вес 10. У победителей состав решения известен в 32% против 14%."),
    ("Расчёт и цена — после квалификации",
     [("М", "Под вашу задачу и бюджет: с доставкой и оформлением этот Freed "
            "выходит 1 250 — 1 350, зависит от аукциона.")],
     "Цену менеджер называет сам, когда знает задачу и рамки, — если клиент "
     "вынужден спрашивать «сколько стоит», разговор идёт не туда."),
    ("Выгода — в ответ на слова клиента",
     [("М", "Раз тесно с колясками — у Freed двери-купе и ровный пол, коляска "
            "встаёт не складывая. Под вашу задачу это главное.")],
     "Вес 10. Не список плюсов, а ответ на то, что клиент назвал: 34% против 18%."),
    ("Безопасность — проактивно, лучше с видео",
     [("М", "И сразу про надёжность, не дожидаясь вопроса: работаем по договору "
            "от юрлица, предоплата защищена. Могу прямо сейчас включить "
            "видеозвонок с нашей стоянки или прислать видеообзор машины.")],
     "Вес 12, полный зачёт — когда проактивно или с видео. Доверие — главное "
     "возражение купивших, вдвое чаще цены; видео-приём: 38% против 18%."),
    ("Предложение договора",
     [("М", "Чтобы всё было прозрачно — отправлю вам договор, посмотрите "
            "спокойно все условия до какой-либо оплаты.")],
     "Вес 15. Второе по силе отличие победителей: договор предлагают 60% "
     "против 36% (p = 0,001)."),
    ("Финал: следующий шаг с датой",
     [("М", "Тогда так: сегодня пришлю подборку и договор в WhatsApp, вы "
            "вечером посмотрите с женой — а я позвоню в четверг в 19:00, "
            "обсудим. Договорились?")],
     "Самый тяжёлый пункт (вес 25): формула «жду от вас X — я сделаю Y», "
     "68% против 41%. Плюс дата и время (вес 10) — самое пустое поле у всех."),
]


def month_bounds(now_l):
    m0 = now_l.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev0 = (m0 - dt.timedelta(days=1)).replace(day=1)
    monday = (now_l - dt.timedelta(days=now_l.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return m0, prev0, monday - dt.timedelta(days=7), monday


def script_stat(rows_scored, frm, to=None):
    xs = [s for ts, s in rows_scored if ts >= frm and (to is None or ts < to)]
    if not xs:
        return None, 0, None
    ok = sum(1 for s in xs if s >= 50)
    return sum(xs) / len(xs), len(xs), 100.0 * ok / len(xs)


def page_script(conn, uid):
    now_l = dt.datetime.now(VLD).replace(tzinfo=None)
    m0, prev0, w0, w1 = month_bounds(now_l)

    all_rows = fetch_scored(conn, min(prev0, w0))
    dept = [r for r in all_rows if r["is_first"]]
    me = [r for r in dept if r["uid"] == uid]
    if not me:
        return ('<section class="card"><p class="muted">Пока нет разобранных '
                'первых разговоров — балл считать не из чего.</p></section>')

    me_sc = [(r["ts"], r["score"]) for r in me]
    dept_sc = [(r["ts"], r["score"]) for r in dept]

    cur_avg, cur_n, cur_ok = script_stat(me_sc, m0)
    prev_avg, prev_n, prev_ok = script_stat(me_sc, prev0, m0)
    wk_avg, wk_n, _ = script_stat(me_sc, w0, w1)
    d_cur, _, d_ok = script_stat(dept_sc, m0)

    delta = ""
    if cur_avg is not None and prev_avg is not None and prev_n >= 15:
        d = cur_avg - prev_avg
        cls = "up" if d > 0 else ("down" if d < 0 else "")
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "=")
        delta = (f' <span class="delta {cls}">{arrow} {num(abs(d), 1)}</span> '
                 f'к прошлому месяцу')

    rates_me = script_rates(me)
    rates_dept = script_rates(dept)

    weak_html, strong_html = [], []
    losses = sorted(((w * (100 - mv) / 100.0, k, w, weak, fix, mv, rates_dept[k])
                     for k, w, _, weak, fix in SCRIPT_ITEMS
                     if (mv := rates_me[k]) is not None), reverse=True)
    for loss, k, w, weak, fix, mv, dv in losses[:4]:
        weak_html.append(
            f'<div class="crit"><div class="crit-head">'
            f'<span class="crit-t">{e(weak)}</span>'
            f'<span class="crit-v">{num(mv)}%<span class="crit-w"> · отдел '
            f'{num(dv)}%</span></span></div>{bullet(mv, tick=dv)}'
            f'<p class="crit-f">{e(fix)} <span class="crit-w">Пункт стоит {w} '
            f'{plural(w, "балл", "балла", "баллов")}, недобираешь '
            f'{num(loss, 1)}.</span></p></div>')

    weak_keys = {k for _, k, *_ in losses[:4]}
    strong = sorted(((w, k, mv, rates_dept[k])
                     for k, w, *_ in SCRIPT_ITEMS
                     if k not in weak_keys and (mv := rates_me[k]) is not None
                     and (mv >= (rates_dept[k] or 0) or mv >= 60)), reverse=True)
    if not strong:
        strong = sorted(((w, k, mv, rates_dept[k]) for k, w, *_ in SCRIPT_ITEMS
                         if k not in weak_keys and (mv := rates_me[k]) is not None),
                        key=lambda x: -x[2])[:2]
    for w, k, mv, dv in strong[:4]:
        strong_html.append(
            f'<div class="crit"><div class="crit-head">'
            f'<span class="crit-t">{e(STRONG[k])}</span>'
            f'<span class="crit-v">{num(mv)}%<span class="crit-w"> · отдел '
            f'{num(dv)}%</span></span></div>{bullet(mv, tick=dv)}'
            f'<p class="crit-f crit-w">Вес {w} из 100 — сейчас приносит '
            f'{num(w * mv / 100, 1)} {plural(round(w * mv / 100), "балл", "балла", "баллов")}. '
            f'Держи уровень.</p></div>')

    blocks = []
    for i, (title, lines, why) in enumerate(IDEAL_TALK, 1):
        dlg = "".join(
            f'<p class="dlg"><span class="who{"" if who == "М" else " k"}">'
            f'{"Менеджер:" if who == "М" else "Клиент:"}</span>{e(txt)}</p>'
            for who, txt in lines)
        blocks.append(f'<div class="blk"><h3><b>{i}.</b> {e(title)}</h3>{dlg}'
                      f'<p class="crit-f">{e(why)}</p></div>')

    week_ago = now_l - dt.timedelta(days=7)
    cand = [r for r in me if r["ts"] >= week_ago and (r["duration"] or 0) >= 180]

    def review(r, best):
        cr = script_credits(r)
        got = sorted(((w, t) for k, w, t, *_ in SCRIPT_ITEMS if cr[k] >= 0.99),
                     reverse=True)
        miss = sorted(((w, t, fix) for k, w, t, _, fix in SCRIPT_ITEMS
                       if cr[k] <= 0.01), reverse=True)
        link = crm_link(r["crm_entity_type"], r["crm_entity_id"])
        ts = r["ts"]
        got_pts = sum(w for w, _ in got[:3])
        miss_pts = sum(w for w, _, _ in miss[:3])
        penalty = qual_n(r["raw"]) < 2
        if best:
            head, cls = "Лучший первый разговор недели", "good-t"
            verdict = (f'<b class="{cls}">Почему лучший:</b> закрыты самые ценные '
                       f'пункты скрипта — {e(", ".join(t.lower() for _, t in got[:3]))} '
                       f'(вместе {got_pts} из 100 по весу).')
            if miss:
                verdict += (f' Дожать до эталона: {e(miss[0][1].lower())} — '
                            f'ещё {miss[0][0]} {plural(miss[0][0], "балл", "балла", "баллов")}.')
        else:
            head, cls = "Худший первый разговор недели", "bad-t"
            verdict = (f'<b class="{cls}">Почему худший:</b> потеряны самые '
                       f'дорогие пункты — '
                       f'{e(", ".join(t.lower() for _, t, _ in miss[:3]))} '
                       f'(минус {miss_pts} из 100 по весу).'
                       + (f' Плюс не выяснены авто/бюджет/срок — балл умножен '
                          f'на 0,7.' if penalty else '')
                       + (f' Что удалось: '
                          f'{e(", ".join(t.lower() for _, t in got[:2]))}.'
                          if got else ' Не зачтён ни один пункт скрипта.'))
        fixes = "".join(f'<p class="sm next"><b>Обратить внимание:</b> {e(f)}</p>'
                        for _, _, f in miss[:2])
        return (f'<div><h2>{head}</h2><article class="call">'
                f'<div class="call-head"><div>'
                f'<div class="call-when">{e(ru_date(ts.date()))}, '
                f'{ts.strftime("%H:%M")} · {mmss(r["duration"])} · '
                f'{e(mask(r["phone_e164"]))}</div>'
                f'<div class="muted sm">'
                + (f'<a href="{link}" target="_blank" rel="noopener">карточка '
                   f'в Битриксе</a>' if link else '')
                + f'</div></div>{chip(r["score"])}</div>'
                f'<p class="verdict">{verdict}</p>{fixes}'
                + (f'<details class="sm"><summary>О чём был разговор</summary>'
                   f'<p>{e(r["summary"])}</p></details>' if r["summary"] else '')
                + '</article></div>')

    reviews = ""
    if cand:
        best = max(cand, key=lambda r: r["score"])
        worst = min(cand, key=lambda r: r["score"])
        parts = [review(best, True)]
        if worst is not best:
            parts.append(review(worst, False))
        reviews = ('<div class="cols2">' + "".join(parts) + '</div>'
                   '<p class="crit-f" style="margin:-6px 0 16px">Разбор обновляется '
                   'автоматически. Берутся только <b>первые</b> содержательные '
                   'разговоры с клиентом от 3 минут за последние 7 дней — '
                   'повторные звонки по скрипту не оцениваются.</p>')

    mn = MONTHS_PREP[m0.month - 1]
    pn = MONTHS_PREP[prev0.month - 1]

    return f"""
<section class="card">
  <div class="hero"><span class="hero-num">{num(cur_avg, 1)}</span>
    <span class="hero-of">из 100 — работа по скрипту в {e(mn)}</span></div>
  <p class="hero-side">{cur_n} первых разговоров разобрано, по отделу
    {num(d_cur, 1)}.{delta}<br>
    Считаются только первые содержательные разговоры с клиентом — эталонный
    скрипт про первый контакт. Балл пересчитывается автоматически в течение дня.</p>
  <div class="tiles" style="margin-top:16px">
    <div class="tile"><b>{num(prev_avg, 1)}</b><span>{e(MONTHS_NOM[prev0.month - 1])}: {prev_n} {plural(prev_n, "разговор", "разговора", "разговоров")}{" — мало, история разборов копится с августа" if prev_n < 15 else ""}</span></div>
    <div class="tile"><b>{num(wk_avg, 1)}</b><span>прошлая неделя, {wk_n} разговоров</span></div>
    <div class="tile"><b>{num(cur_ok)}%</b><span>успешных разговоров в {e(mn)} (от 50 баллов)</span></div>
    <div class="tile"><b>{num(prev_ok)}%</b><span>успешных в {e(pn)} · отдел сейчас {num(d_ok)}%</span></div>
  </div>
</section>

<div class="cols2"><div>
<h2>Слабые места — начни сверху</h2>
<section class="card">{''.join(weak_html)}
  <p class="crit-f" style="margin-top:14px">Отсортировано по значимости: наверху
    пункт, на котором теряется больше всего балла (вес × недобор). Штрих —
    средний уровень отдела. Периоды: этот и прошлый месяц, только первые
    разговоры.</p>
</section>
</div><div>
<h2>Сильные места — не отпускай</h2>
<section class="card">{''.join(strong_html)}
  <p class="crit-f" style="margin-top:14px">Наверху — самое важное для продажи
    (по весу пункта в карточке эталонного скрипта, версия 2).</p>
</section>
</div></div>

{reviews}

<h2>Как звучит идеальный разговор — скрипт v2</h2>
<section class="card">
  <div class="blocks">{''.join(blocks)}</div>
  <p class="crit-f" style="margin-top:14px">Пример учебный, имена условные, но
    каждая формула взята из выигранных разговоров нашего отдела: скрипт собран
    из разбора 386 расшифровок и дополнен требованиями руководителя 22.08
    (квалификация до расчёта, безопасность проактивно, обязательное предложение
    договора, ветка для отложенного клиента).</p>
</section>

<h2>Как это считается</h2>
<section class="card"><ul class="foot">
  <li>Карточка оценки v2: следующий шаг 25 · квалификация 18 · договор 15 ·
      безопасность 12 · состав решения 10 · привязка выгоды 10 · дата и время 10.
      Выяснено меньше 2 из 3 квалификационных — балл × 0,7.</li>
  <li>Оценивается <b>первый содержательный разговор</b> с клиентом (от 90 секунд,
      раньше по этому номеру разговоров не было). Повторные звонки в балл
      не входят: в них скрипт уже проговорён.</li>
  <li>«Договор» и «видео» считаются по расшифровке автоматически;
      «безопасность» — пока приближённо, по словам-маркерам, до расширения
      разбора. Отложенный клиент (ветка Б скрипта) пока оценивается общей
      карточкой.</li>
  <li><b>Инструмент для роста, а не для премий:</b> сильный разговор по скрипту
      не гарантирует сделку, и наоборот.</li>
</ul></section>"""


# ── страницы РОПа ────────────────────────────────────────────────────────────
# Лента «Контроль» с экрана убрана (решение Тимофея 23.08). Страница и журнал
# rop_control живы — вернуть можно строкой ("kontrol", "kontrol.html", "Контроль").
# «Слепок работы» убран с экрана решением Тимофея 26.08.2026. Код page_slepok
# жив; вернуть — строкой ("slepok", "slepok.html", "Слепок работы") здесь
# и парой ("slepok", "slepok.html") в PAGE_SET ("all" и "now").
ROP_TABS = [("menedzhery", "", "Менеджеры"),
            ("soobshcheniya", "soobshcheniya.html", "Сообщения"),
            ("etalon", "etalon.html", "Эталон"),
            ("kachestvo", "kachestvo.html", "Качество"),
            ("zamery", "zamery.html", "Замеры"),
            ("puls", "puls.html", "Пульс"),
            ("taymingi", "taymingi.html", "KPI"),
            ("visual", "visual.html", "Visual"),
            ("instruction", "instruction.html", "Instruction")]

KONTROL_SQL = """
  SELECT r.reason, r.entered_at, r.score_at_entry,
         l.id, l.title, l.form_model, l.form_text, l.ip_city, l.phone_e164,
         l.assigned_by, l.date_closed, l.date_create,
         coalesce(lc.score, r.score_at_entry) AS score,
         m.name AS mname, m.last_name AS mlast,
         d.name AS status_name,
         t.talks, t.talk_min, t.last_out, t.in_after_out, r.id AS rid
    FROM rop_control r
    JOIN leads l ON l.id = r.lead_id
    LEFT JOIN lead_composite lc ON lc.lead_id = l.id
    LEFT JOIN managers m ON m.portal_user_id = l.assigned_by
    LEFT JOIN crm_dict d ON d.kind = 'STATUS' AND d.status_id = l.status_id
    LEFT JOIN LATERAL (
        SELECT count(*) FILTER (WHERE c.duration > 0)                     AS talks,
               round(coalesce(sum(c.duration) FILTER (WHERE c.duration > 0), 0)
                     / 60.0)                                              AS talk_min,
               max(c.call_start) FILTER (WHERE c.direction = 'out')       AS last_out,
               count(*) FILTER (WHERE c.direction = 'in' AND c.call_start >
                   coalesce((SELECT max(c2.call_start) FROM calls c2
                              WHERE c2.phone_e164 = l.phone_e164
                                AND c2.direction = 'out'), l.date_create)) AS in_after_out
          FROM calls c
          JOIN (SELECT min(date_create) d FROM leads
                 WHERE phone_e164 = l.phone_e164) f ON TRUE
         WHERE c.phone_e164 = l.phone_e164
           AND c.call_start >= f.d - interval '30 minutes') t ON TRUE
   WHERE r.resolved_at IS NULL
   ORDER BY coalesce(lc.score, r.score_at_entry) DESC
"""
KONTROL_COLS = ("reason entered_at score_at_entry id title form_model form_text "
                "ip_city phone_e164 assigned_by date_closed date_create score "
                "mname mlast status_name talks talk_min last_out in_after_out rid").split()

WEEK_SQL = """
  SELECT r.manager_at_entry,
         count(*) FILTER (WHERE r.reason = 'closed_alive'
                            AND r.entered_at >= now() - interval '7 days') AS closed_alive,
         count(*) FILTER (WHERE r.resolution = 'returned'
                            AND r.resolved_at >= now() - interval '7 days') AS returned,
         count(*) FILTER (WHERE r.resolution IN ('confirmed_dead', 'dismissed')
                            AND r.resolved_at >= now() - interval '7 days') AS dead,
         count(*) FILTER (WHERE r.resolution = 'expired'
                            AND r.resolved_at >= now() - interval '7 days') AS expired
    FROM rop_control r
   GROUP BY 1
"""


def score_badge(score):
    s = int(round(float(score)))
    cls = "s80" if s >= 80 else "s60"
    return f'<div class="lead-badge {cls}">{s}<br>балл</div>'


def kontrol_row(r, now):
    what, meta = describe(r)
    link = crm_link("LEAD", r["id"])
    mgr = short(f"{r['mname'] or ''} {r['mlast'] or ''}".strip() or "?")
    if r["reason"] == "closed_alive":
        bits = [e(mgr), f'закрыта в «{e(r["status_name"] or "?")}»',
                f'{r["talks"] or 0} разгов., {num(r["talk_min"])} мин']
        right_v, right_l, hot = ago(r["date_closed"], now), "назад", float(r["score"]) >= 80
    else:
        silence = ago(r["last_out"] or r["date_create"], now)
        bits = [e(mgr), f'{r["talks"] or 0} разгов.']
        if r["in_after_out"]:
            bits.append(f'<span class="hot">клиент перезванивал ×{r["in_after_out"]}</span>')
        right_v, right_l, hot = silence, "тишина", bool(r["in_after_out"])
    line2 = " · ".join([e(mask(r["phone_e164"]))] + bits
                       + ([f'<a href="{link}" target="_blank" rel="noopener">карточка</a>']
                          if link else []))
    return (f'<div class="lead">{score_badge(r["score"])}<div class="lead-body">'
            f'<div class="lead-t">{e(what)}</div>'
            f'<div class="lead-m">{line2}</div></div>'
            f'<div class="lead-w"><b class="{"hot" if hot else ""}">{right_v}</b>'
            f'{right_l}'
            f'<button class="dismiss" data-rid="{r["rid"]}" '
            f'title="Убрать из ленты навсегда: закрыто верно, сбоем не считать">'
            f'&#10005; убрать</button></div></div>')


def short(name):
    parts = [p for p in (name or "").split() if not (len(p) <= 3 and p[:1] == "М")]
    return parts[-1] if parts else (name or "?")


def page_kontrol(conn):
    now = dt.datetime.now(VLD)
    rs = rows(conn.cursor(), KONTROL_SQL, {}, KONTROL_COLS)
    for r in rs:
        for k in ("entered_at", "date_closed", "date_create", "last_out"):
            if r[k] is not None:
                r[k] = r[k].astimezone(VLD)
    closed = [r for r in rs if r["reason"] == "closed_alive"]
    cold = [r for r in rs if r["reason"] == "going_cold"]

    week = {u: (ca, ret, dd, ex) for u, ca, ret, dd, ex
            in conn.execute(WEEK_SQL).fetchall()}
    mgr_names = dict(conn.execute(
        "SELECT portal_user_id, btrim(name || ' ' || coalesce(last_name, '')) "
        "FROM managers"))
    returned7 = sum(v[1] for v in week.values())
    expired7 = sum(v[3] for v in week.values())
    week_rows = "".join(
        f'<tr><td>{e(short(mgr_names.get(u, str(u))))}</td>'
        f'<td class="{"hot" if ca else ""}">{ca}</td><td>{ret}</td>'
        f'<td>{dd}</td><td>{ex}</td></tr>'
        for u, (ca, ret, dd, ex) in sorted(
            week.items(), key=lambda x: -x[1][0]) if any((ca, ret, dd, ex)))

    def block(title, note, items, cap=30):
        if not items:
            return (f'<h2>{title}</h2><section class="card">'
                    f'<p class="muted">Пусто — сбоев нет.</p></section>')
        shown = items[:cap]
        tail = ("" if len(items) <= cap else
                f'<p class="crit-f" style="margin-top:12px">Показаны первые {cap} '
                f'из {len(items)} — по убыванию балла.</p>')
        return (f'<h2>{title} · {len(items)}</h2><section class="card">'
                f'<p class="crit-f" style="margin-bottom:10px">{note}</p>'
                + "".join(kontrol_row(r, now) for r in shown) + tail + '</section>')

    return f"""
<div class="tiles">
  <div class="tile"><b>{len(closed)}</b><span>закрыли живого</span></div>
  <div class="tile"><b>{len(cold)}</b><span>горячий стынет</span></div>
  <div class="tile"><b>{returned7}</b><span>возвращено за 7 дней</span></div>
  <div class="tile"><b>{expired7}</b><span>пропущено за 7 дней</span></div>
</div>

{block("Закрыли живого",
       "Заявка с составной оценкой от 60 закрыта в отказ за последние 7 дней, "
       "и по клиенту нигде больше не работают. Историческая конверсия этой "
       "полосы — до 17%: часть этих отказов — потерянные продажи.", closed)}

{block("Горячий стынет",
       "Разговор был, оценка от 60, но по номеру клиента нет ни одного "
       "исходящего уже больше 48 часов. Красная строка — клиент перезванивал "
       "сам, а мы молчим.", cold)}

<h2>Счёт недели по менеджерам</h2>
<section class="card">
  <table><thead><tr><th>Менеджер</th><th>Закрыл живых</th><th>Возвращено</th>
    <th>Убрано</th><th>Пропущено</th></tr></thead>
  <tbody>{week_rows or '<tr><td colspan="5">за неделю событий нет</td></tr>'}</tbody></table>
  <p class="crit-f" style="margin-top:10px">«Убрано» — РОП подтвердил, что
    сбоя нет: кнопкой на этой странице или отметкой «Мёртвый, закрыт верно»
    в Битриксе. «Пропущено» — карточка провисела в ленте 5 дней без реакции.</p>
</section>

<h2>Как работает лента</h2>
<section class="card"><ul class="foot">
  <li><b>«✕ убрать»</b> — единственная кнопка: подтверждает, что сбоя нет,
      карточка исчезает сразу и навсегда, в счёте недели идёт в «Убрано».</li>
  <li>Остальные решения — в Битриксе, лента подхватывает их сама в течение
      15 минут: <b>вернуть</b> — переоткройте лид; <b>передать</b> — смените
      ответственного; <b>дожать</b> — пусть менеджер позвонит. Отметка
      «Мёртвый, закрыт верно» в поле «Контроль РОПа» равнозначна кнопке.</li>
  <li>Карточка без реакции 5 дней уходит в «пропущено». Повторно та же заявка
      попадает в ленту не раньше чем через неделю; убранная — никогда.</li>
  <li>Дубли отсечены: если по клиенту работают в другой карточке — это
      не потеря, в ленту не попадает. Телефоны непригодные и PARTNER исключены.</li>
</ul></section>

<script>
document.addEventListener("click", async function (e) {{
  var b = e.target.closest(".dismiss");
  if (!b) return;
  b.disabled = true;
  try {{
    var token = location.pathname.split("/")[2];
    var r = await fetch("/act", {{method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{token: token, rid: +b.dataset.rid}})}});
    var j = await r.json();
    if (j.ok) {{
      var row = b.closest(".lead");
      row.style.opacity = "0.35";
      setTimeout(function () {{ row.remove(); }}, 250);
    }} else {{ b.disabled = false; }}
  }} catch (err) {{ b.disabled = false; }}
}});
</script>"""


MONTHS_BACK = 4          # сколько месяцев показываем в дашборде

# Все заявки и все продажи, БЕЗ фильтра источника и типа телефона —
# решение Тимофея 23.08: «учитывать все покупки независимо от источника».
# Продажа = лид дошёл до стадии «ТС куплен» (stage_id='13') — решение
# Тимофея 01.09.2026, заменило прежнее «продажа = конвертация лида» (23.08):
# конвертаций в разы больше покупок, и с учётом руководителя они не сходились.
SOLD_CTE = """
  WITH sold AS (SELECT lead_id AS owner_id, t13 FROM v_sales WHERE counted)
"""

CONV_SQL = SOLD_CTE + """
  SELECT l.assigned_by,
         to_char(date_trunc('month', l.date_create AT TIME ZONE %(tz)s), 'YYYY-MM'),
         count(*),
         count(*) FILTER (WHERE sold.owner_id IS NOT NULL),
         count(*) FILTER (WHERE l.status_semantic = 'P')
    FROM leads l
    LEFT JOIN sold ON sold.owner_id = l.id
   WHERE l.assigned_by IN (SELECT portal_user_id FROM dash_tokens
                            WHERE active AND kind = 'manager')
     AND l.date_create >= date_trunc('month', now()) - make_interval(months => %(back)s)
   GROUP BY 1, 2
"""

# Продажи по месяцу, когда лид попал в стадию «ТС куплен».
CLOSED_SQL = SOLD_CTE + """
  SELECT l.assigned_by,
         to_char(date_trunc('month', sold.t13 AT TIME ZONE %(tz)s), 'YYYY-MM'),
         count(*)
    FROM sold JOIN leads l ON l.id = sold.owner_id
   WHERE sold.t13 >= date_trunc('month', now()) - make_interval(months => %(back)s)
   GROUP BY 1, 2
"""

CALLS_SQL = """
  SELECT uid, to_char(date_trunc('month', ts), 'YYYY-MM') AS m,
         extract(isodow from ts)::int AS dow,
         count(*) FILTER (WHERE extract(hour from ts) >= 18)      AS evening,
         count(*)                                                 AS total,
         count(DISTINCT ts::date)                                 AS days
    FROM (SELECT c.portal_user_id AS uid,
                 (c.call_start AT TIME ZONE %(tz)s) AS ts
            FROM calls c
           WHERE c.direction = 'out' AND c.duration > 0
             AND c.portal_user_id IN (SELECT portal_user_id FROM dash_tokens
                                       WHERE active AND kind = 'manager')
             AND c.call_start >= date_trunc('month', now())
                                 - make_interval(months => %(back)s)) x
   GROUP BY 1, 2, 3
"""


def last_months(now, n):
    """[('2026-05', 'май'), ...] — n последних календарных месяцев."""
    out = []
    y, m = now.year, now.month
    for _ in range(n):
        out.append((f"{y:04d}-{m:02d}", MONTHS_NOM[m - 1]))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def heat(value, vmax, text, sub=None, href=None):
    """Ячейка-теплокарта: одна синяя шкала, значение всегда подписано."""
    if value is None:
        return '<td class="dim">—</td>'
    a = 0.0 if not vmax else min(1.0, value / vmax)
    body = (f'{text}' + (f'<span class="sub2">{sub}</span>' if sub else ''))
    if href:
        body = (f'<a class="q" href="{href}" target="_blank" '
                f'rel="noopener">{body}</a>')
    return (f'<td class="heat num"><i style="opacity:{a * 0.42:.2f}"></i>'
            f'<span>{body}</span></td>')


def month_range(key):
    """'2026-05' -> ('01.05.2026', '31.05.2026') — формат дат фильтра Битрикса."""
    y, m = int(key[:4]), int(key[5:])
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)).day
    return f"01.{m:02d}.{y}", f"{last}.{m:02d}.{y}"


def dash_blocks(conn, mgrs, now):
    months = last_months(now, MONTHS_BACK)
    keys = [k for k, _ in months]
    cur_key = keys[-1]
    p = {"tz": TZ, "back": MONTHS_BACK - 1}

    # ── конверсия ────────────────────────────────────────────────────────────
    conv = {}
    for uid, m, leads, won, inwork in conn.execute(CONV_SQL, p).fetchall():
        conv[(uid, m)] = (leads, won, inwork)
    convs = [100.0 * w / l for (l, w, _) in conv.values() if l >= 50]
    cmax = max(convs) if convs else 1.0

    conv_rows = []
    for uid, name in mgrs:
        tds = []
        for k in keys:
            v = conv.get((uid, k))
            if not v or not v[0]:
                tds.append('<td class="dim">—</td>')
                continue
            leads, won, inwork = v
            pc = 100.0 * won / leads
            d1, d2 = month_range(k)
            tds.append(heat(pc, cmax, f"{num(pc, 2)}%",
                            f"{won} из {leads}" + (f", {inwork} в работе" if inwork else ""),
                            href=crm_list(uid, ["13"], "DATE_CREATE", d1, d2)))
        conv_rows.append(f'<tr><td class="mname">{e(short(name))}</td>{"".join(tds)}</tr>')

    closed = {(u, m): n for u, m, n in conn.execute(CLOSED_SQL, p).fetchall()}
    cl_max = max(closed.values() or [1])
    cl_rows = []
    for uid, name in mgrs:
        tds = []
        for k in keys:
            v = closed.get((uid, k))
            if not v:
                tds.append('<td class="dim">·</td>')
                continue
            d1, d2 = month_range(k)
            tds.append(heat(v, cl_max, str(v),
                            href=crm_list(uid, ["13"], "DATE_MODIFY", d1, d2)))
        cl_rows.append(f'<tr><td class="mname">{e(short(name))}</td>{"".join(tds)}</tr>')
    five = {u for u, _ in mgrs}
    oth = ["".join(f'<td class="num">{sum(n for (u, m), n in closed.items() if m == k and u not in five) or "·"}</td>'
                   for k in keys)]
    cl_rows.append(f'<tr><td class="mname dim">Другие ответственные</td>{oth[0]}</tr>')
    cl_tot = "".join(f'<td class="num"><b>{sum(n for (u, m), n in closed.items() if m == k)}</b></td>'
                     for k in keys)

    tot_tds = []
    for k in keys:
        l = sum(v[0] for (u, mm), v in conv.items() if mm == k)
        w = sum(v[1] for (u, mm), v in conv.items() if mm == k)
        tot_tds.append(f'<td class="num">{num(100.0 * w / l, 2) if l else "—"}%'
                       f'<span class="sub2">{w} из {l}</span></td>')

    # ── звонки: вечера и выходные ────────────────────────────────────────────
    ev = {}       # (uid, m, dow) -> (evening, total)
    wknd = {}     # (uid, m) -> [дней, звонков]
    for uid, m, dow, evening, total, days in conn.execute(CALLS_SQL, p).fetchall():
        ev[(uid, m, dow)] = (evening, total)
        if dow >= 6:
            a = wknd.setdefault((uid, m), [0, 0])
            a[0] += days
            a[1] += total

    emax = max([v[0] for v in ev.values()] or [1])
    tabs, tables = [], []
    for k, label in months:
        on = " on" if k == cur_key else ""
        tabs.append(f'<button class="mbtn{on}" data-m="{k}">{label}</button>')
        rows = []
        for uid, name in mgrs:
            tds, tot = [], 0
            for d in range(1, 8):
                n_ev = ev.get((uid, k, d), (0, 0))[0]
                tot += n_ev
                tds.append(heat(n_ev, emax, str(n_ev)) if n_ev
                           else '<td class="dim">·</td>')
            rows.append(f'<tr><td class="mname">{e(short(name))}</td>{"".join(tds)}'
                        f'<td class="num"><b>{tot}</b></td></tr>')
        hid = "" if k == cur_key else " hidden"
        tables.append(
            f'<div class="mtab" data-m="{k}"{hid}><table><thead><tr><th>Менеджер</th>'
            + "".join(f"<th>{d}</th>" for d in DOW)
            + '<th>всего</th></tr></thead><tbody>'
            + "".join(rows) + '</tbody></table></div>')

    wmax = max([v[0] for v in wknd.values()] or [1])
    wk_rows = []
    for uid, name in mgrs:
        tds = []
        for k in keys:
            a = wknd.get((uid, k))
            if not a:
                tds.append('<td class="dim">—</td>')
            else:
                tds.append(heat(a[0], wmax, f"{a[0]} дн.", f"{a[1]} звон."))
        wk_rows.append(f'<tr><td class="mname">{e(short(name))}</td>{"".join(tds)}</tr>')

    head = "".join(f"<th>{lbl}</th>" for _, lbl in months)
    cur_lbl = months[-1][1]

    return f"""
<div class="cols2"><div>
<h2>Конверсия по месяцам</h2>
<section class="card">
  <table><thead><tr><th>Менеджер</th>{head}</tr></thead>
  <tbody>{''.join(conv_rows)}</tbody>
  <tfoot><tr><td class="mname">Отдел</td>{''.join(tot_tds)}</tr></tfoot></table>
  <p class="crit-f warnbox" style="margin-top:12px">Все заявки и все продажи,
    включая PARTNER и городские номера. <b>Продажа = лид дошёл до стадии «ТС куплен»</b>
    (решение руководителя 01.09), засчитана заявке месяца её создания.
    <b>{e(cur_lbl.capitalize())} ещё не вызрел</b> — медиана цикла 8,6 дня,
    90-й процентиль 38 дней: под цифрой видно, сколько заявок ещё в работе.
    Сравнивать между людьми — можно, с прошлым месяцем — нет.</p>
</section>
</div><div>

<h2>Продажи по месяцам</h2>
<section class="card">
  <table><thead><tr><th>Менеджер</th>{head}</tr></thead>
  <tbody>{''.join(cl_rows)}</tbody>
  <tfoot><tr><td class="mname">Отдел</td>{cl_tot}</tr></tfoot></table>
  <p class="crit-f" style="margin-top:12px">Продажа засчитывается менеджеру
    в момент, когда лид попал в стадию «ТС куплен» — по месяцу этого перехода,
    независимо от того, когда пришла заявка. «Другие ответственные» —
    продажи вне пятёрки. Таблица слева отвечает на другой вопрос — какая
    доля заявок месяца доведена до сделки. Цифры кликабельны — открывают
    те же заявки в CRM.</p>
</section>

<h2>Работа в выходные</h2>
<section class="card">
  <table><thead><tr><th>Менеджер</th>{head}</tr></thead>
  <tbody>{''.join(wk_rows)}</tbody></table>
  <p class="crit-f warnbox" style="margin-top:12px">Субботы и воскресенья, в которые
    менеджер провёл хотя бы один исходящий разговор, и сколько всего разговоров.
    <b>Считается по звонкам, а не по онлайну:</b> учёт присутствия в CRM включён
    только 20 августа, истории по нему пока нет. Как накопится — добавим отдельной
    строкой.</p>
</section>
</div></div>

<h2>Звонки после 18:00 · {e(cur_lbl)}</h2>
<section class="card">
  <div class="mbtns">{''.join(tabs)}</div>
  {''.join(tables)}
  <p class="crit-f" style="margin-top:12px">Состоявшиеся исходящие разговоры,
    начатые в 18:00 и позже по владивостокскому времени. Две трети заявок приходят,
    когда офис закрыт, — вечерние звонки закрывают этот разрыв.</p>
</section>

<script>
document.addEventListener("click", function (e) {{
  var b = e.target.closest(".mbtn");
  if (!b) return;
  var m = b.dataset.m;
  document.querySelectorAll(".mbtn").forEach(function (x) {{
    x.classList.toggle("on", x.dataset.m === m);
  }});
  document.querySelectorAll(".mtab").forEach(function (x) {{
    x.hidden = x.dataset.m !== m;
  }});
  var h = document.querySelector("h2 + section .mbtns");
  if (h) {{
    var t = h.closest("section").previousElementSibling;
    if (t) t.innerHTML = "Звонки после 18:00 · " + b.textContent;
  }}
}});
</script>"""


NOW_SQL = f"""
  SELECT l.assigned_by, count(*),
         count(*) FILTER (WHERE l.status_id = 'NEW'),
         count(*) FILTER (WHERE l.status_id = '32'),
         count(*) FILTER (WHERE l.status_id = 'IN_PROCESS'),
         count(*) FILTER (WHERE l.status_id = '35'),
         count(*) FILTER (WHERE l.status_id = '8'),
         count(*) FILTER (WHERE l.status_id = '10'),
         count(*) FILTER (WHERE l.status_id = '11'),
         count(*) FILTER (WHERE l.status_id = '12')
    FROM leads l
   WHERE l.assigned_by IN ({WORKING})
     AND l.status_semantic = 'P'
   GROUP BY 1
"""

# Сегодняшний прирост по статусам: сколько лидов менеджера СЕГОДНЯ вошло
# в этот статус (по истории stage_history, время Влд). Показывается зелёной
# стрелкой рядом с абсолютной цифрой «сейчас».
DELTA_TODAY_SQL = f"""
  SELECT l.assigned_by, h.stage_id, count(DISTINCT h.owner_id)
    FROM stage_history h
    JOIN leads l ON l.id = h.owner_id
   WHERE h.entity_kind = 'lead'
     AND l.assigned_by IN ({WORKING})
     AND (h.created_time AT TIME ZONE '{TZ}')::date
         = (now() AT TIME ZONE '{TZ}')::date
   GROUP BY 1, 2
"""


def delta_today(conn):
    """{uid: {stage_id: сколько вошло сегодня}}."""
    if "delta_today" in _CACHE:
        return _CACHE["delta_today"]
    d = {}
    for uid, stage, n in conn.execute(DELTA_TODAY_SQL):
        d.setdefault(uid, {})[stage] = n
    _CACHE["delta_today"] = d
    return d


# столбец -> какие статусы открыть в CRM по клику (None = все незакрытые)
STATUS_COLS = [None, ["NEW"], ["32"], ["IN_PROCESS"], ["35"], ["8"],
               ["10"], ["11"], ["12"]]
NOW_HEAD = ["Всего открыто", "Не обработан", "Позвонить", "В работе",
            "Отложен", "Паспорт", "Договор", "Оплата обеспечительного",
            "На торгах"]

# ── «Внимание» и «Толкнуть» в таблице «Сейчас» (просьба Тимофея 02.09) ──────
# «Внимание» — открытые заявки с составной оценкой от ATTN_SCORE, которые ещё
# не дошли до «На торгах»: сильный клиент, которого ведут медленно.
# «Толкнуть» — заявки, висящие в «На торгах» дольше PUSH_DAYS дней (по последнему
# входу в статус из stage_history; если истории нет — по date_modify лида).
ATTN_SCORE = 60
PUSH_DAYS = 10
ATTN_STATUSES = ["NEW", "32", "IN_PROCESS", "35", "8", "10", "11"]

ATTN_SQL = f"""
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
"""

PUSH_SQL = f"""
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
"""


def attn_push(conn):
    """({uid: [(id, title, phone, mark, score, status)]},
        {uid: [(id, title, phone, mark, days)]})"""
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
    """Свёрнутый список заявок из столбцов «Внимание» и «Толкнуть»."""
    def lead_a(lid, title, phone, mark):
        name = (title or "").strip(" /") or f"Лид {lid}"
        if len(name) > 70:
            name = name[:68].rstrip() + "…"
        mark = mark if mark and mark.lower() != "нет данных" else ""
        return (f'<a href="{crm_link("LEAD", lid)}" target="_blank" '
                f'rel="noopener">{e(mask_title(name))}</a>'
                + (f' <span class="dim">{e(mark)}</span>' if mark else "")
                + (f'<span class="todo-p">{e(mask(phone))}</span>'
                   if phone else ""))
    blocks = []
    for u, name in mgrs:
        a, p = attn.get(u, []), push.get(u, [])
        if a:
            items = [f'<div class="todo-i"><span class="todo-h">'
                     f'★ {int(round(score))}</span>{lead_a(lid, title, phone, mark)}'
                     f' <span class="dim">· {e(STATUS_NAMES.get(st, st))}</span></div>'
                     for lid, title, phone, mark, score, st in a]
            blocks.append(
                f'<div class="ap-box" id="ap-{u}-attn" hidden>'
                f'<p class="mname" style="margin:0 0 4px">{e(short(name))} — '
                f'Внимание: {len(a)} '
                f'{plural(len(a), "заявка", "заявки", "заявок")} '
                f'с оценкой от {ATTN_SCORE}</p>'
                f'<div class="todo-l">{"".join(items)}</div></div>')
        if p:
            items = [f'<div class="todo-i"><span class="todo-h">'
                     f'{int(days)} дн.</span>{lead_a(lid, title, phone, mark)}'
                     f'</div>' for lid, title, phone, mark, days in p]
            blocks.append(
                f'<div class="ap-box" id="ap-{u}-push" hidden>'
                f'<p class="mname" style="margin:0 0 4px">{e(short(name))} — '
                f'Толкнуть: {len(p)} '
                f'{plural(len(p), "заявка", "заявки", "заявок")} '
                f'в «На торгах» дольше {PUSH_DAYS} дней</p>'
                f'<div class="todo-l">{"".join(items)}</div></div>')
    if not blocks:
        return ""
    return ('<div id="ap-store" hidden>' + "".join(blocks) + '</div>'
            + """<script>
document.querySelectorAll("a.q.ap").forEach(function (a) {
  a.addEventListener("click", function (ev) {
    ev.preventDefault();
    var id = a.dataset.ap, box = document.getElementById(id);
    if (!box) return;
    var tr = a.closest("tr"), was = a.classList.contains("on");
    document.querySelectorAll(".ap-row").forEach(function (r) {
      var b = r.querySelector(".ap-box");
      if (b) { b.hidden = true; document.getElementById("ap-store").appendChild(b); }
      r.remove();
    });
    document.querySelectorAll("a.q.ap").forEach(function (x) { x.classList.remove("on"); });
    if (was) return;
    var row = document.createElement("tr");
    row.className = "ap-row";
    var td = document.createElement("td");
    td.colSpan = tr.children.length;
    td.appendChild(box); box.hidden = false;
    row.appendChild(td);
    tr.parentNode.insertBefore(row, tr.nextSibling);
    a.classList.add("on");
  });
});
</script>""")


STATUS_NAMES = {"NEW": "не обработан", "32": "позвонить",
                "IN_PROCESS": "в работе", "35": "отложен", "8": "паспорт",
                "10": "договор", "11": "оплата обеспечительного", "12": "на торгах"}


def mask_title(name):
    """Телефон в названии лида — под маску (как в очереди дня)."""
    return re.sub(r"(\+?[78][\s(\-]*\d{3}[\s)\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2})",
                  lambda m: mask(m.group(1)), name)


# Лиды с незакрытым «Делом» (CRM_TODO): срок сегодня и срок уже прошёл
TODO_SQL = f"""
  SELECT a.responsible_id,
         count(DISTINCT a.owner_id) FILTER (
             WHERE (a.end_time AT TIME ZONE %(tz)s)::date
                   = (now() AT TIME ZONE %(tz)s)::date),
         count(DISTINCT a.owner_id) FILTER (
             WHERE (a.end_time AT TIME ZONE %(tz)s)::date
                   < (now() AT TIME ZONE %(tz)s)::date)
    FROM activities a
   WHERE a.owner_type_id = 1
     AND NOT a.completed
     AND a.provider_id = 'CRM_TODO'
     AND a.end_time IS NOT NULL
     AND a.responsible_id IN ({WORKING})
   GROUP BY 1
"""


def todo_data(conn):
    """{uid: (дел на сегодня, просрочено)} — считается раз на всю сборку."""
    if "todo" in _CACHE:
        return _CACHE["todo"]
    d = {r[0]: (r[1], r[2]) for r in conn.execute(TODO_SQL, {"tz": TZ}).fetchall()}
    _CACHE["todo"] = d
    return d


# Точный список лидов, у которых дело со сроком сегодня — те же строки,
# из которых складывается цифра на дашборде менеджера.
TODO_LIST_SQL = """
  SELECT l.id, l.title, l.phone_e164, min(a.end_time AT TIME ZONE %(tz)s) AS due
    FROM activities a
    JOIN leads l ON l.id = a.owner_id
   WHERE a.owner_type_id = 1
     AND NOT a.completed
     AND a.provider_id = 'CRM_TODO'
     AND a.end_time IS NOT NULL
     AND (a.end_time AT TIME ZONE %(tz)s)::date
         = (now() AT TIME ZONE %(tz)s)::date
     AND a.responsible_id = %(uid)s
   GROUP BY 1, 2, 3
   ORDER BY 4
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
    лидов за это окно (не сколько выполнил). <b>Клиентов</b> — со сколькими
    разными номерами он за это окно поговорил, <b>разговоров</b> — сколько
    состоявшихся исходящих звонков сделал; недозвоны в обе цифры не входят.
    <b>Касаний на клиента</b> — разговоров, делённое на клиентов: 1,0 значит
    «позвонил и забыл», выше — клиента ведут.</p>
</section>
"""


def now_data(conn):
    """Разбивка открытых заявок по статусам + балл скрипта за 14 дней."""
    if "now" in _CACHE:
        return _CACHE["now"]
    queue = {r[0]: r[1:] for r in conn.execute(NOW_SQL).fetchall()}
    sc_by = {}
    for r in fetch_scored(conn, dt.datetime.now(VLD).replace(tzinfo=None)
                          - dt.timedelta(days=14)):
        if r["is_first"]:
            sc_by.setdefault(r["uid"], []).append(r["score"])
    _CACHE["now"] = (queue, sc_by)
    return queue, sc_by


def now_delta(conn):
    return delta_today(conn)


def now_section(mgrs, queue, sc_by, first_col="Менеджер", total=True,
                delta=None, attn=None, push=None, me=None):
    """Таблица «Сейчас». Одним кодом для РОПа (все) и менеджера (одна строка).

    delta — {uid: {stage_id: сколько вошло сегодня}}: рядом с абсолютной
    цифрой рисуем зелёный прирост «↑N» (просьба Тимофея 25.08). Стрелка —
    это сегодняшнее движение по статусу, а не только текущий остаток.
    """
    delta = delta or {}
    attn, push = attn or {}, push or {}
    trs, tot = [], [0] * 8
    dtot = [0] * 8
    atot = ptot = 0
    for u, name in mgrs:
        cols = queue.get(u, (0,) * 9)[1:]
        for i, v in enumerate(cols):
            tot[i] += v
        md = delta.get(u, {})
        sc = sc_by.get(u)
        sc_txt = num(sum(sc) / len(sc), 1) if sc and len(sc) >= 15 else "—"

        def cell(v, st, _u=u, _md=md, _i=None):
            add = sum(_md.get(sid, 0) for sid in (st or []))
            up = (f'<span class="up-today" title="сегодня прибавилось '
                  f'{add}">↑{add}</span>' if add else "")
            if not v and not add:
                return '<td class="num">·</td>'
            href = crm_list(_u, st or OPEN_STATUSES)
            body = (f'<a class="q" href="{href}" target="_blank" '
                    f'rel="noopener">{v}</a>' if v else
                    '<span class="dim">0</span>')
            return f'<td class="num">{body}{up}</td>'

        row_cells = []
        for i, v in enumerate(cols):
            st = STATUS_COLS[i + 1]
            row_cells.append(cell(v, st))
            dtot[i] += sum(md.get(sid, 0) for sid in (st or []))
        na, np_ = len(attn.get(u, [])), len(push.get(u, []))
        atot += na
        ptot += np_

        def xcell(v, kind, _u=u):
            # цифра раскрывает точный список этих заявок под таблицей:
            # в Битриксе по оценке и сроку не отфильтровать (Тимофей, 02.09)
            if not v:
                return '<td class="num">·</td>'
            return (f'<td class="num"><a class="q ap {kind}" href="#" '
                    f'data-ap="ap-{_u}-{kind}" title="показать список">'
                    f'{v}</a></td>')
        trs.append(
            f'<tr{" class=\"me\"" if u == me else ""}>'
            f'<td class="mname">{e(short(name))}</td>'
            + "".join(row_cells)
            + xcell(na, "attn")
            + xcell(np_, "push")
            + f'<td class="num">{sc_txt}</td></tr>')

    foot = ""
    if total:
        def ftot(v, i):
            up = (f'<span class="up-today">↑{dtot[i]}</span>'
                  if dtot[i] else "")
            return f'<td class="num"><b>{v}</b>{up}</td>'
        foot = ('<tfoot><tr><td class="mname">Отдел</td>'
                + "".join(ftot(v, i) for i, v in enumerate(tot))
                + f'<td class="num"><b>{atot or "·"}</b></td>'
                + f'<td class="num"><b>{ptot or "·"}</b></td>'
                + '<td class="num">—</td></tr></tfoot>')
    lst = attn_push_list(mgrs, attn, push) if (attn or push) else ""

    return f"""
<h2>Сейчас <span class="h2-hint">Клик по цифре открывает в CRM фильтр
  с этими лидами; «Внимание» и «Толкнуть» раскрывают список прямо здесь.</span></h2>
<section class="card">
  <table><thead><tr><th>{e(first_col)}</th>
    {''.join(f'<th>{h}</th>' for h in NOW_HEAD[1:])}<th class="hint">Внимание<span class="tip">Заявки с составной оценкой от {ATTN_SCORE} (поведение на сайте, разбор разговоров, история клиента), которые ещё не дошли до «На торгах». Сильный клиент — а по воронке не движется. Клик по цифре — список этих заявок.</span></th>
    <th class="hint">Толкнуть<span class="tip">Заявки, которые висят в статусе «На торгах» дольше {PUSH_DAYS} дней. Пора дожать клиента или честно закрыть. Клик по цифре — список этих заявок.</span></th><th>Скрипт</th></tr></thead>
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
    Клик по этим двум цифрам раскрывает под таблицей точный список этих
    заявок со ссылками на карточки (в Битриксе по оценке и сроку
    не отфильтровать). Суммы в рублях в карточках лидов
    не заполняются, поэтому показано количество заявок.
    <b>Любая цифра — ссылка:</b> открывает этот список заявок в Битриксе.
    <b>Зелёная стрелка ↑</b> — сколько заявок вошло в этот статус
    <b>сегодня</b> (по истории статусов, время Влд): движение за день рядом
    с общим остатком. «Скрипт» — средний балл соответствия эталонному скрипту
    за 14 дней по первым разговорам, прочерк — меньше 15 разобранных.</p>
</section>
"""


# ── «Начни с этих» — очередь дня на дашборде менеджера ───────────────────────
# Решение Тимофея 23.08: свежая заявка важнее всего — клиент только что
# оставил её и, скорее всего, ещё у телефона. Дальше — горячие ♛, по которым
# тишина (менеджер видит их за сутки до ленты РОПа), затем сильные классы
# без единого звонка, затем недозвоны. Не больше QUEUE_CAP строк: это план
# на ближайший час, а не отчёт. Страница пересобирается каждую минуту.
QUEUE_CAP = 10
FRESH_H = 24      # «свежая» — моложе суток, разговора ещё не было
FRESH_HOT_MIN = 120  # без единой попытки дольше двух часов — подсветка
COOL_H = 24       # горячий «остывает» после суток без исходящего
COOL_HOT_H = 36   # ...а с 36 часов горит: на 48-м часу попадёт к РОПу


def queue_day(rs, now):
    """Раскладка открытых заявок по четырём корзинам очереди."""
    fresh, cooling, waiting, retry = [], [], [], []
    for r in rs:
        talks, outc = r["talks"] or 0, r["out_calls"] or 0
        age_h = (now - r["date_create"]).total_seconds() / 3600
        quiet = r["last_out"] or r["last_talk"]
        if talks == 0 and age_h < FRESH_H:
            fresh.append(r)
        elif ((r["crown"] or (r["comp"] or 0) >= 60) and talks and quiet
              and (now - quiet).total_seconds() / 3600 >= COOL_H):
            cooling.append(r)
        elif talks == 0 and outc == 0 and grade_of(r["mark"]) in ("A", "B"):
            waiting.append(r)
        elif talks == 0 and 0 < outc < 5 and grade_of(r["mark"]) in ("A", "B"):
            retry.append(r)
    fresh.sort(key=lambda r: r["date_create"], reverse=True)
    cooling.sort(key=lambda r: r["last_out"] or r["last_talk"])
    waiting.sort(key=lambda r: (prio(r), r["date_create"]))
    retry.sort(key=lambda r: (prio(r), r["date_create"]))
    return fresh, cooling, waiting, retry


def queue_block(conn, uid, now):
    rs = rows(conn.cursor(), QUEUE_SQL, {"uid": uid}, QUEUE_COLS)
    for r in rs:
        r["date_create"] = r["date_create"].astimezone(VLD)
        for k in ("last_talk", "last_out"):
            if r[k]:
                r[k] = r[k].astimezone(VLD)
    fresh, cooling, waiting, retry = queue_day(rs, now)

    items = []
    for r in fresh:
        outc = r["out_calls"] or 0
        if outc:
            items.append(lead_row(r, now, "попыток", f"{outc} из 5"))
        else:
            hot = (now - r["date_create"]).total_seconds() >= FRESH_HOT_MIN * 60
            items.append(lead_row(r, now, "как пришла",
                                  ago(r["date_create"], now), hot))
    for r in cooling:
        quiet = r["last_out"] or r["last_talk"]
        hot = (now - quiet).total_seconds() / 3600 >= COOL_HOT_H
        items.append(lead_row(r, now, "тишина", ago(quiet, now), hot))
    for r in waiting:
        items.append(lead_row(r, now, "ждёт", ago(r["date_create"], now)))
    for r in retry:
        items.append(lead_row(r, now, "попыток", f'{r["out_calls"]} из 5'))

    total = len(items)
    if not items:
        body = ('<p class="muted">Пусто: свежих заявок нет, горячие '
                'не остывают, сильные классы обзвонены. Дальше — таблица '
                'ниже, столбец «Не обработан».</p>')
    else:
        tail = ("" if total <= QUEUE_CAP else
                f'<p class="crit-f" style="margin-top:12px">В очереди '
                f'{total}, показаны первые {QUEUE_CAP} — остальные '
                f'подтянутся по мере разбора.</p>')
        body = "".join(items[:QUEUE_CAP]) + tail
    return (
        '<h2>Начни с этих</h2><section class="card">'
        '<p class="crit-f" style="margin-bottom:10px">Свежие — первыми: '
        'клиент только что оставил заявку и ещё у телефона. Дальше горячие ♛, '
        'по которым тишина от суток (на вторые сутки их увидит РОП), затем '
        '⚡A и ★B без единого звонка и недозвоны — норма пять попыток за три '
        'дня. Список обновляется каждую минуту: позвонил — строка уйдёт сама.</p>'
        + body + "</section>")


def page_mgr_dash(conn, uid):
    """Вкладка «Дашборд» менеджера: та же таблица «Сейчас», что у РОПа —
    весь отдел, своя строка подсвечена (решение Тимофея 08.09: раньше
    менеджер видел только себя). «Дела на сегодня» — по-прежнему свои.

    Очередь «Начни с этих» (queue_block) выключена решением Тимофея 23.08 —
    «выглядит не очень, потом придумаю». Код оставлен, вернуть — одной строкой.
    """
    queue, sc_by = now_data(conn)
    row = conn.execute(f"""
        SELECT m.portal_user_id, btrim(m.name || ' ' || coalesce(m.last_name, ''))
          FROM managers m WHERE m.portal_user_id = %s""", (uid,)).fetchone()
    if not row:
        return ('<section class="card"><p class="muted">Нет данных '
                'по менеджеру.</p></section>')
    today, overdue = todo_data(conn).get(uid, (0, 0))
    # Фильтра «дело со сроком сегодня» в URL списка лидов Битрикса нет —
    # ACTIVITY_COUNTER открывал все просроченные. Поэтому цифра раскрывает
    # ровно те лиды, из которых она сложилась; строка — ссылка на карточку.
    lst = ""
    if today:
        items = []
        for lid, title, phone, due in todo_list(conn, uid):
            name = (title or "").strip(" /") or f"Лид {lid}"
            items.append(
                f'<div class="todo-i"><span class="todo-h">'
                f'{due.strftime("%H:%M")}</span>'
                f'<a href="{crm_link("LEAD", lid)}" target="_blank" '
                f'rel="noopener">{e(name)}</a>'
                + (f'<span class="todo-p">{e(mask(phone))}</span>'
                   if phone else "") + '</div>')
        lst = ('<details class="todo-d"><summary>Показать список '
               f'({today})</summary><div class="todo-l">'
               + "".join(items) + "</div></details>")
    num_html = f'<span class="hero-num">{today}</span>'
    tail = (f'<p class="hero-side">И ещё <b>{overdue}</b> '
            f'{plural(overdue, "лид", "лида", "лидов")} с просроченным делом.</p>'
            if overdue else "")

    attn, push = attn_push(conn)
    mgrs = conn.execute(f"""
        SELECT m.portal_user_id, btrim(m.name || ' ' || coalesce(m.last_name, ''))
          FROM managers m WHERE m.portal_user_id IN ({WORKING}) ORDER BY 2""").fetchall()
    if uid not in {m[0] for m in mgrs}:
        mgrs = [row] + mgrs
    return now_section(mgrs, queue, sc_by, delta=now_delta(conn),
                       attn=attn, push=push, me=uid) \
        + funnel_for_dash(conn, dt.datetime.now(VLD)) + f"""
<h2>Дела на сегодня</h2>
<section class="card" style="max-width:460px">
  <div class="hero">{num_html}
    <span class="hero-of">{plural(today, "лид ждёт", "лида ждут", "лидов ждут")}
      дела сегодня</span></div>
  {lst}
  {tail}
  <p class="crit-f" style="margin-top:14px">Дело — напоминание, которое вы сами
    ставите в карточке лида. Считаются незакрытые дела со сроком на сегодня;
    если на одном лиде несколько дел, лид считается один раз.
    {"«Показать список» раскрывает ровно эти лиды по времени дела — "
      "строка ведёт в карточку." if today else
      "Сегодня дел не поставлено — если клиент ждёт звонка, дело лучше завести: "
      "иначе он потеряется."}</p>
</section>""" + mgr_gaps_block(conn, uid)


# ── вкладка РОПа «Слепок работы» ─────────────────────────────────────────────
# По десять последних заявок каждого менеджера, взятых в работу: вся история
# клиента одной строкой слева направо — заявка, звонки с сутью разговора,
# смены статуса, открытые дела и паузы между всем этим. Красное — тишина.
# История статусов лидов заполнена бэкфиллом app/stagehist_fix.py (25.08.2026):
# Битрикс для лидов отдаёт STATUS_ID, старый crm_sync ждал STAGE_ID.

SLEPOK_DAYS = 120      # глубина отбора заявок, дней
SLEPOK_TRY = 30        # звонок короче (сек) — попытка дозвона, не разговор
SLEPOK_RED_H = 48      # пауза (часов), с которой подсветка красным
SLEPOK_FIRST_MIN = 60  # первый отклик дольше (мин от начала рабочего дня) — красный

SLEPOK_CSS = """
.sl-legend{font-size:13px;color:var(--ink2);margin:4px 0 6px;max-width:1100px}
.sl-mgr{margin:28px 0 10px}
.sl-row{border-top:1px solid var(--line);padding:12px 2px}
.sl-row:first-of-type{border-top:none}
.sl-head{display:flex;align-items:baseline;gap:8px 12px;flex-wrap:wrap}
.sl-who{font-weight:600}
.sl-who a{color:inherit;text-decoration:none}
.sl-who a:hover{color:var(--blue)}
.sl-meta{color:var(--ink3);font-size:13px}
.sl-res{margin-left:auto;text-align:right;font-size:12.5px;color:var(--ink3);white-space:nowrap}
.sl-res b{font-size:13.5px;font-weight:600}
.sl-res .ok{color:var(--good)}
.sl-res .fail{color:var(--ink2)}
.sl-res .open{color:var(--blue)}
.sl-flow{display:flex;flex-wrap:wrap;align-items:center;row-gap:7px;margin-top:8px;font-size:13px;line-height:1.35}
.ev{border:1px solid var(--line);background:var(--surface);border-radius:8px;padding:2.5px 9px;white-space:nowrap;max-width:430px;overflow:hidden;text-overflow:ellipsis}
.ev a{color:inherit;text-decoration:none}
.ev-start{border-color:var(--blue);color:var(--blue);background:transparent}
.ev-st{color:var(--ink2);background:transparent;border-style:dashed}
.ev-try{color:var(--ink3)}
.ev-in{border-color:var(--warn)}
.ev-todo{background:transparent}
.ev-todo.late{border-color:var(--bad);color:var(--bad)}
.ev-fin{color:var(--ink2);font-weight:600}
.ev-fin.ok{border-color:var(--good);color:var(--good)}
.ev .dur{color:var(--ink3)}
.gap{color:var(--ink3);padding:0 8px;white-space:nowrap;font-size:12.5px}
.gap.long{color:var(--bad);font-weight:650}
.sl-none{color:var(--ink3);font-size:14px}
"""

SLEPOK_LEADS_SQL = f"""
SELECT * FROM (
  SELECT l.id, l.assigned_by, l.date_create, l.date_closed, l.status_id,
         l.status_semantic, l.title, l.form_model, l.form_text, l.ip_city,
         l.phone_e164,
         d.name AS status_name, m.value AS mark,
         coalesce(lc.crown, false) AS crown,
         row_number() OVER (PARTITION BY l.assigned_by
                            ORDER BY l.date_create DESC) AS rn
    FROM leads l
    LEFT JOIN crm_dict d        ON d.kind = 'STATUS' AND d.status_id = l.status_id
    LEFT JOIN lead_marks m      ON m.lead_id = l.id
    LEFT JOIN lead_composite lc ON lc.lead_id = l.id
   WHERE l.assigned_by IN ({WORKING})
     AND l.phone_kind IN ('mobile', 'landline')
     AND l.status_id NOT IN ('31', '27')   -- спам и дубль работой не считаем
     AND l.date_create <  now() - interval '48 hours'
     AND l.date_create >= now() - interval '{SLEPOK_DAYS} days'
     AND (l.status_id <> 'NEW' OR EXISTS (
          SELECT 1 FROM calls c
           WHERE c.phone_e164 = l.phone_e164 AND c.direction = 'out'
             AND c.call_start >= l.date_create - interval '30 minutes'))
) x WHERE rn <= 10
"""

SLEPOK_CALLS_SQL = """
SELECT l.id AS lead_id, c.call_start AS t, c.direction, c.duration,
       cs.outcome, cs.next_step, cs.summary
  FROM leads l
  JOIN calls c
    ON c.phone_e164 = l.phone_e164
   AND c.call_start >= l.date_create - interval '30 minutes'
   AND (coalesce(l.status_semantic, '') NOT IN ('S', 'F')
        OR l.date_closed IS NULL
        OR c.call_start <= l.date_closed + interval '1 hour')
  LEFT JOIN call_scores cs ON cs.call_id = c.id
 WHERE l.id = ANY(%s)
 ORDER BY c.call_start"""

SLEPOK_STAGES_SQL = """
SELECT sh.owner_id AS lead_id, sh.created_time AS t, sh.stage_id,
       coalesce(d.name, sh.stage_id) AS name
  FROM stage_history sh
  LEFT JOIN crm_dict d ON d.kind = 'STATUS' AND d.status_id = sh.stage_id
 WHERE sh.entity_kind = 'lead' AND sh.owner_id = ANY(%s)
   AND coalesce(sh.stage_id, '') <> ''
 ORDER BY sh.created_time"""

SLEPOK_TODOS_SQL = """
SELECT a.owner_id AS lead_id, a.created AS t, a.end_time, a.subject
  FROM activities a
 WHERE a.provider_id = 'CRM_TODO' AND a.owner_type_id = 1
   AND NOT a.completed AND a.owner_id = ANY(%s)
 ORDER BY a.created"""

SL_OUTCOME = {"договорились": "договорились", "думает": "клиент думает",
              "отказ": "отказ", "нецелевой": "нецелевой"}


def sl_when(t, now):
    lt = t.astimezone(VLD)
    s = f"{DOW[lt.weekday()]} {lt.day:02d}.{lt.month:02d}"
    if lt.year != now.year:
        s += f".{lt.year % 100:02d}"
    return f"{s} {lt.strftime('%H:%M')}"


def sl_dur_text(seconds):
    m = seconds / 60
    if m < 90:
        return f"{max(1, int(m))} мин"
    h = seconds / 3600
    if h < 48:
        return f"{int(round(h))} ч"
    return f"{round(seconds / 86400)} дн"


def sl_night(t):
    lt = t.astimezone(VLD)
    return lt.hour < 9 or lt.hour >= 19


def sl_work_start(t):
    """Начало рабочего дня для заявки: ночной и вечерней отсчёт с 9 утра."""
    lt = t.astimezone(VLD)
    if lt.hour >= 19:
        lt = (lt + dt.timedelta(days=1)).replace(hour=9, minute=0, second=0,
                                                 microsecond=0)
    elif lt.hour < 9:
        lt = lt.replace(hour=9, minute=0, second=0, microsecond=0)
    return lt


def sl_gap(prev_t, t):
    s = (t - prev_t).total_seconds()
    if s < 45 * 60:
        return '<span class="gap">→</span>'
    cls = " long" if s >= SLEPOK_RED_H * 3600 else ""
    return f'<span class="gap{cls}">— {e(sl_dur_text(s))} —</span>'


def sl_first_gap(created, t):
    """Первый отклик: время всегда написано, красный — от начала рабочего дня."""
    s = (t - created).total_seconds()
    eff = (t - sl_work_start(created)).total_seconds()
    cls = " long" if eff >= SLEPOK_FIRST_MIN * 60 else ""
    return f'<span class="gap{cls}">— {e(sl_dur_text(max(s, 60)))} —</span>'


def sl_phrase(r):
    txt = ((r.get("next_step") or "").strip()
           or SL_OUTCOME.get((r.get("outcome") or "").strip(), ""))
    if len(txt) > 58:
        txt = txt[:55].rstrip() + "…"
    return txt


def sl_chip(ev0, now, link):
    if ev0["kind"] == "tries":
        n = f' ×{ev0["n"]}' if ev0["n"] > 1 else ""
        return (f'<span class="ev ev-try" title="попытки дозвона без разговора">'
                f'↗ недозвон{n}</span>')
    if ev0["kind"] == "inmiss":
        return ('<span class="ev ev-in" title="входящий звонок без разговора">'
                '↙ клиент звонил</span>')
    if ev0["kind"] == "talk":
        arrow = "↗" if ev0["direction"] == "out" else "↙"
        phrase = sl_phrase(ev0)
        tip = (ev0.get("summary") or "").strip()
        body = f'{arrow} <span class="dur">{mmss(ev0["duration"])}</span>'
        if phrase:
            body += f" · {e(phrase)}"
        if link:
            body = f'<a href="{link}" target="_blank" rel="noopener">{body}</a>'
        title = f' title="{e(tip)}"' if tip else ""
        return f'<span class="ev"{title}>{body}</span>'
    if ev0["kind"] == "stage":
        return f'<span class="ev ev-st">→ {e(ev0["name"])}</span>'
    if ev0["kind"] == "final":
        if ev0["sem"] == "S":
            return '<span class="ev ev-fin ok">✓ успех</span>'
        return f'<span class="ev ev-fin">✕ {e(ev0["name"] or "закрыта")}</span>'
    if ev0["kind"] == "todo":
        due = ev0.get("due")
        late = due is not None and due < now
        when = sl_when(due, now) if due else "без срока"
        tip = (ev0.get("subject") or "").strip()
        return (f'<span class="ev ev-todo{" late" if late else ""}" '
                f'title="{e(tip)}">&#128197; дело на {e(when)}'
                + (" · просрочено" if late else "") + "</span>")
    return ""


def sl_row(r, calls, stages, todos, now):
    created = r["date_create"]
    sem = r["status_semantic"] or ""
    closed = r["date_closed"] if sem in ("S", "F") else None

    evs, prev_stage = [], None
    for s0 in stages:
        if (s0["stage_id"] == "NEW"
                and s0["t"] <= created + dt.timedelta(minutes=3)):
            prev_stage = "NEW"
            continue
        if s0["stage_id"] == prev_stage:
            continue
        prev_stage = s0["stage_id"]
        evs.append({"kind": "stage", "t": s0["t"], "name": s0["name"],
                    "stage_id": s0["stage_id"]})
    for c in calls:
        evs.append(dict(c, kind="call"))
    for td in todos:
        evs.append({"kind": "todo", "t": td["t"], "due": td["end_time"],
                    "subject": td["subject"]})
    evs.sort(key=lambda x: x["t"])

    # хвостовой статус совпадает с текущим — вместо него финальная плашка
    while evs and evs[-1]["kind"] == "stage" and evs[-1]["stage_id"] == r["status_id"]:
        evs.pop()
    if closed:
        evs.append({"kind": "final", "t": closed, "sem": sem,
                    "name": r["status_name"]})
        evs.sort(key=lambda x: x["t"])

    packed, talks, tries = [], 0, 0
    for ev0 in evs:
        if ev0["kind"] != "call":
            packed.append(ev0)
            continue
        dur = ev0["duration"] or 0
        if ev0["direction"] == "out" and dur < SLEPOK_TRY:
            tries += 1
            last = packed[-1] if packed else None
            if last and last["kind"] == "tries":
                last["n"] += 1
                last["t_last"] = ev0["t"]
            else:
                packed.append({"kind": "tries", "t": ev0["t"],
                               "t_last": ev0["t"], "n": 1})
        elif dur >= SLEPOK_TRY:
            talks += 1
            packed.append(dict(ev0, kind="talk"))
        else:
            packed.append(dict(ev0, kind="inmiss"))

    link = crm_link("LEAD", r["id"])
    bits = [f'<span class="ev ev-start">заявка {e(sl_when(created, now))}'
            + (" &#127769;" if sl_night(created) else "") + "</span>"]
    prev = created
    for i, ev0 in enumerate(packed):
        bits.append(sl_first_gap(created, ev0["t"]) if i == 0
                    else sl_gap(prev, ev0["t"]))
        bits.append(sl_chip(ev0, now, link))
        prev = ev0.get("t_last", ev0["t"])
    if not closed and (now - prev).total_seconds() >= 24 * 3600:
        s = (now - prev).total_seconds()
        cls = " long" if s >= SLEPOK_RED_H * 3600 else ""
        bits.append(f'<span class="gap{cls}">— тишина {e(sl_dur_text(s))} —</span>')

    # шапка строки
    what, meta = describe(r)
    grade = grade_of(r["mark"])
    sym = "♛" if r["crown"] else {"A": "⚡", "B": "★"}.get(grade, "")
    if sem == "S":
        res = '<b class="ok">✓ успех</b>'
    elif sem == "F":
        res = f'<b class="fail">✕ {e(r["status_name"] or "закрыта")}</b>'
    else:
        res = f'<b class="open">● {e(r["status_name"] or "в работе")}</b>'
    life = ((closed or now) - created).total_seconds()
    sub = (f'{sl_dur_text(life)} · {talks} '
           f'{plural(talks, "разговор", "разговора", "разговоров")}')
    who = (f'<a href="{link}" target="_blank" rel="noopener">{e(what)}</a>'
           if link else e(what))
    meta_txt = " · ".join([mask(r["phone_e164"])] + meta)
    head = ('<div class="sl-head">'
            + (f'<span>{sym}</span>' if sym else "")
            + f'<span class="sl-who">{who}</span>'
            + f'<span class="sl-meta">{e(meta_txt)}</span>'
            + f'<span class="sl-res">{res}<br>{e(sub)}</span></div>')
    return (f'<div class="sl-row">{head}'
            f'<div class="sl-flow">{"".join(bits)}</div></div>')


def page_slepok(conn):
    now = dt.datetime.now(VLD)
    leads = rows(conn, SLEPOK_LEADS_SQL, (),
                 ["id", "assigned_by", "date_create", "date_closed", "status_id",
                  "status_semantic", "title", "form_model", "form_text",
                  "ip_city", "phone_e164", "status_name", "mark", "crown", "rn"])
    ids = [r["id"] for r in leads]
    calls, stages, todos = {}, {}, {}
    if ids:
        def grp(sql, cols):
            d = {}
            for r in rows(conn, sql, (ids,), cols):
                d.setdefault(r["lead_id"], []).append(r)
            return d
        calls = grp(SLEPOK_CALLS_SQL, ["lead_id", "t", "direction", "duration",
                                       "outcome", "next_step", "summary"])
        stages = grp(SLEPOK_STAGES_SQL, ["lead_id", "t", "stage_id", "name"])
        todos = grp(SLEPOK_TODOS_SQL, ["lead_id", "t", "end_time", "subject"])

    mgrs = conn.execute(f"""
        SELECT m.portal_user_id, btrim(m.name || ' ' || coalesce(m.last_name, ''))
          FROM managers m WHERE m.portal_user_id IN ({WORKING}) ORDER BY 2""").fetchall()
    by_mgr = {}
    for r in leads:
        by_mgr.setdefault(r["assigned_by"], []).append(r)

    out = [f"<style>{SLEPOK_CSS}</style>",
           '<p class="sl-legend">По десять последних заявок, взятых в работу: '
           'вся история клиента одной строкой слева направо. ↗ исходящий '
           'звонок, ↙ входящий, пунктирная плашка — смена статуса в CRM, '
           '&#128197; — незакрытое дело. Между событиями написано, сколько '
           'прошло времени; <b style="color:var(--bad)">красное</b> — пауза '
           f'дольше {SLEPOK_RED_H} часов, а у первого отклика — дольше часа '
           'от начала рабочего дня. &#127769; — заявка пришла в нерабочее '
           'время. Клик по имени или звонку — карточка в Битриксе.</p>']
    for uid, name in mgrs:
        items = by_mgr.get(uid, [])
        out.append(f'<h2 class="sl-mgr">{e(human_name(name, None))}</h2>')
        if not items:
            out.append('<section class="card"><p class="sl-none">Взятых '
                       'в работу заявок за последние месяцы не нашлось.'
                       '</p></section>')
            continue
        out.append('<section class="card">'
                   + "".join(sl_row(r, calls.get(r["id"], []),
                                    stages.get(r["id"], []),
                                    todos.get(r["id"], []), now)
                             for r in items)
                   + "</section>")

    out.append(f"""
<h2>Как это устроено</h2>
<section class="card"><ul class="foot">
  <li>Берутся десять последних заявок каждого менеджера не моложе 48 часов
      (иначе истории ещё нет), по которым была хоть какая-то работа:
      исходящий звонок или смена статуса. Спам, дубли и непригодные номера
      отброшены.</li>
  <li>Подпись у звонка — договорённость из разбора разговора моделью;
      наведите курсор, чтобы увидеть краткое содержание. Разбираются разговоры
      от 90 секунд за последние недели — у остальных подписи нет, только
      длительность.</li>
  <li>Подряд идущие безответные наборы свёрнуты в одну плашку
      «недозвон ×N». Звонок короче {SLEPOK_TRY} секунд — попытка,
      не разговор.</li>
  <li>Дела показаны только незакрытые. История смен статусов накоплена
      с конца апреля 2026 — у более старых заявок статусы могут
      отсутствовать.</li>
  <li>Итог справа — текущий статус заявки, сколько она живёт и сколько
      состоялось разговоров. Звонки соседней заявки того же клиента могут
      попадать в строку: телефон один.</li>
</ul></section>""")
    return "".join(out)


def page_managers(conn):
    queue, sc_by = now_data(conn)
    mgrs = conn.execute(f"""
        SELECT m.portal_user_id, btrim(m.name || ' ' || coalesce(m.last_name, ''))
          FROM managers m WHERE m.portal_user_id IN ({WORKING}) ORDER BY 2""").fetchall()

    now = dt.datetime.now(VLD)
    attn, push = attn_push(conn)
    return now_section(mgrs, queue, sc_by, delta=now_delta(conn),
                       attn=attn, push=push) + plan_section(conn, mgrs, now) + f"""
{dash_blocks(conn, mgrs, now)}

<h2>Что смотреть в первую очередь</h2>
<section class="card"><ul class="foot">
  <li>Сбои по живым заявкам — закрытые с высокой оценкой и остывающие —
      живут на вкладке «Контроль», там же кнопка «убрать».</li>
  <li>Большой «Отложен» или «Паспорт» — заявки уходят из виду; клик
      по цифре открывает их список в CRM.</li>
  <li>Балл скрипта по отделу держится около 48–50: рост даёт финал разговора —
      следующий шаг с датой, а не вежливость в начале.</li>
</ul></section>"""


def page_hub(conn):
    rs = conn.execute("""
        SELECT t.token, t.kind,
               btrim(coalesce(m.name, '') || ' ' || coalesce(m.last_name, ''))
          FROM dash_tokens t
          LEFT JOIN managers m ON m.portal_user_id = t.portal_user_id
         WHERE t.active AND t.kind IN ('rop', 'manager')
         ORDER BY t.kind DESC, 3""").fetchall()
    rops = [r for r in rs if r[1] == "rop"]
    mgrs = [r for r in rs if r[1] == "manager"]

    def link(token, label):
        return (f'<div class="lead"><div class="lead-body">'
                f'<div class="lead-t"><a href="/d/{token}/">{e(label)}</a></div>'
                f'</div></div>')

    out = ['<div class="cols2"><div><h2>Контроль отдела</h2>'
           '<section class="card hublist">']
    for t, _, _ in rops:
        out.append(link(t, "Дашборд отдела — статусы, конверсия, продажи"))
    out.append('</section></div><div><h2>Менеджеры</h2>'
               '<section class="card hublist">')
    out += [link(t, human_name(n, None) or t[:8]) for t, _, n in mgrs]
    out.append('</section></div></div><section class="card"><p class="crit-f">'
               'Каждая ссылка — личный кабинет. У менеджеров одна страница '
               '«Скрипт», у РОПа — «Контроль» и «Менеджеры». '
               'Ссылки секретные: кому переслал — тот и видит, поэтому эту '
               'страницу лучше не пересылать целиком.</p></section>'
               '<script>'
               '// Запоминаем адрес хаба — на других страницах появится '
               '«← Все панели».\n'
               'try { localStorage.setItem("ropbot_hub", location.pathname); } '
               'catch (err) {}'
               '</script>')
    return "".join(out)


ADMIN_TABS = [("hub", "", "Все панели")]


# ── вкладка РОПа «Эталон» + личные разрывы менеджера ─────────────────────────
# Данные — manager_snapshot (app/snapshot.py, крон ежедневно). Берётся страта
# 'std' (нормировка на состав классов отдела), когда посчитана, иначе 'all'.
# Эталон по каждому параметру — лучший менеджер этого параметра (n >= 10).
# Веса и цена разрывов — этап 3 слепка (projects/slepok-menedzherov.md):
# в цене — ПОЛОВИНА фактической разницы конверсий вне выборки, вилка ×0,5…×2
# от неё. Самоотбор не вычищен, истинную цену покажет только эксперимент.

ET_STAR = 8829   # Жернов — эталон отдела (этап 3, 25.08.2026): устойчивый
                 # топ-2 конверсии вызревших в двух периодах + лучший процесс.

ET_LEVERS = [    # (код, подпись, ед., больше=лучше, вес)
    ("P3",  "5+ звонков за 3 дня", "%", True, 35),
    ("H3",  "5+ попыток по недозвонам", "%", True, 30),
    ("D1",  "Конкретный следующий шаг", "%", True, 20),
    ("T4P", "Техники продаж", "%", True, 15),
]
ET_DISC = [
    ("C1",  "Первый отклик", "раб.мин", False, 0),
    ("H2",  "Брошено 0–1 звонком", "%", False, 0),
    ("GI2", "Закрыто без разговора", "%", False, 0),
    ("GI3", "Просроченные дела", "лидов", False, 0),
    ("D2",  "Договорённость выполнена", "%", True, 0),
    ("GO1", "Реакция на ♛", "раб.ч", False, 0),
]
ET_MONITOR = [
    ("R2",  "3+ разговора", "%", True, 0),
    ("G1",  "Первый разговор", "мин", True, 0),
    ("REZ", "Конверсия вызревших", "%", True, 0),
]
ET_PERIOD = {"GI3": "mom", "REZ": "mat"}        # остальные — окно 30 дней
ET_PRICE = {"P3": 0.0325, "H3": 0.021}          # Δ конверсии в цене, доля
ET_MIN_N = 10                                   # меньше — «мало данных»


def et_data(conn):
    """(дата снимка, дата сравнения, тек, прош, {uid: имя})."""
    if "etalon" in _CACHE:
        return _CACHE["etalon"]
    last = conn.execute("SELECT max(snap_date) FROM manager_snapshot").fetchone()[0]
    prev = None
    if last:
        prev = conn.execute(
            "SELECT max(snap_date) FROM manager_snapshot WHERE snap_date <= %s",
            (last - dt.timedelta(days=7),)).fetchone()[0]
    names = {r[0]: human_name(r[1], r[2]) for r in conn.execute(f"""
        SELECT m.portal_user_id, m.name, m.last_name FROM managers m
         WHERE m.portal_user_id IN ({WORKING})""").fetchall()}

    def load(day):
        d = {}
        if not day:
            return d
        for uid, period, param, grade, v, n in conn.execute(
                """SELECT portal_user_id, period, param, grade, value, denom
                     FROM manager_snapshot
                    WHERE snap_date = %s AND grade IN ('all','std')""", (day,)):
            if period != ET_PERIOD.get(param, "30d"):
                continue
            key = (uid, param)
            if grade == "std" or key not in d:
                d[key] = (float(v), float(n or 0))
        return d

    _CACHE["etalon"] = (last, prev, load(last), load(prev), names)
    return _CACHE["etalon"]


def et_best(cur, names, param, up):
    """Эталон параметра: (значение, uid лучшего) при выборке n >= ET_MIN_N."""
    vals = [(cur[(u, param)][0], u) for u in names
            if (u, param) in cur and cur[(u, param)][1] >= ET_MIN_N]
    if not vals:
        return None, None
    return max(vals) if up else min(vals)


def et_rel(v, best, up):
    """Насколько близко к эталону, 0..1."""
    if v is None or best is None:
        return None
    if up:
        return 1.0 if best <= 0 else max(0.0, min(1.0, v / best))
    return 1.0 if v <= best else max(0.0, min(1.0, (best + 0.5) / (v + 0.5)))


def et_fmt(param, v):
    if v is None:
        return "—"
    if param == "G1":
        return num(v / 60, 1)
    if param in ("C1", "GI3"):
        return num(v, 0)
    if param == "REZ":
        return num(v, 2)
    return num(v, 1)


def et_trend(up, v, pv):
    """Стрелка к снимку недельной давности, с поправкой на направление."""
    if v is None or pv is None:
        return ""
    ch = (v - pv) / max(abs(pv), 1e-9)
    better = ch > 0.03 if up else ch < -0.03
    worse = ch < -0.03 if up else ch > 0.03
    if better:
        return '<i class="dn-tr up" title="лучше, чем неделю назад">▲</i>'
    if worse:
        return '<i class="dn-tr down" title="хуже, чем неделю назад">▼</i>'
    return '<i class="dn-tr flat" title="без изменений">·</i>'


def donut(rel, text, sub="", trend="", full=False):
    """Кружок-донат: заливка — близость к эталону, цвет — статус."""
    if rel is None:
        return (f'<div class="dn none"><svg viewBox="0 0 60 60" aria-hidden="true">'
                f'<circle class="dn-bg" cx="30" cy="30" r="24"></circle></svg>'
                f'<div class="dn-t"><b>{text}</b><span>{e(sub)}</span></div></div>')
    color = ("var(--blue)" if full else
             "var(--good)" if rel >= 0.85 else
             "var(--warn)" if rel >= 0.55 else "var(--bad)")
    dash = max(5.0, min(1.0, rel) * 150.8)
    return (f'<div class="dn"><svg viewBox="0 0 60 60" aria-hidden="true">'
            f'<circle class="dn-bg" cx="30" cy="30" r="24"></circle>'
            f'<circle class="dn-fg" cx="30" cy="30" r="24" '
            f'style="stroke:{color};stroke-dasharray:{dash:.1f} 150.8"></circle>'
            f'</svg><div class="dn-t"><b>{text}</b><span>{e(sub)}</span></div>'
            f'{trend}</div>')


def et_gaps(cur, names, uid):
    """Топ-3 разрыва менеджера по значимым параметрам, с ценой где честно."""
    out = []
    for param, label, unit, up, weight in ET_LEVERS:
        mine = cur.get((uid, param))
        if not mine or mine[1] < ET_MIN_N:
            continue
        best, bu = et_best(cur, names, param, up)
        if best is None or bu == uid:
            continue
        rel = et_rel(mine[0], best, up)
        if rel is None or rel >= 0.9:
            continue
        g = dict(param=param, label=label, unit=unit, v=mine[0], n=mine[1],
                 best=best, who=names.get(bu, ""), score=weight * (1 - rel))
        if param in ET_PRICE:
            mid = mine[1] * (best - mine[0]) / 100.0 * ET_PRICE[param]
            g["deals"] = (mid / 2, mid, mid * 2)
        out.append(g)
    out.sort(key=lambda x: -x["score"])
    return out[:3]


def et_price_text(g):
    if "deals" not in g:
        return ("цена в сделках не оценивается: карточкам разговоров меньше "
                "месяца")
    lo, mid, hi = g["deals"]
    if hi < 0.05:
        return "цена разрыва меньше 0,1 сделки в месяц"
    return (f"≈{num(mid, 1)} сделки в месяц, "
            f"осторожная вилка {num(lo, 1)}–{num(hi, 1)}")


def et_block(cur, prv, names, order, plist, title, note):
    cols = f"minmax(150px,180px) repeat({len(plist)},1fr)"
    head = ['<div></div>']
    for param, label, unit, up, _w in plist:
        best, bu = et_best(cur, names, param, up)
        bname = ((names.get(bu) or "").split() or [""])[0] if bu else ""
        bestof = (f'<span class="bestof">эталон {et_fmt(param, best)} · '
                  f'{e(bname)}</span>'
                  if best is not None else '<span class="bestof">нет данных</span>')
        head.append(f'<div class="et-h"><b>{e(label)}</b>{e(unit)}{bestof}</div>')
    rows = []
    for uid in order:
        star = ('<span class="star">★ эталон отдела</span>'
                if uid == ET_STAR else "")
        rows.append(f'<div class="et-name">{e(names[uid])}{star}</div>')
        for param, label, unit, up, _w in plist:
            mine = cur.get((uid, param))
            if not mine:
                rows.append(donut(None, "—", "нет данных"))
                continue
            v, n = mine
            best, bu = et_best(cur, names, param, up)
            rel = et_rel(v, best, up)
            sub = "мало данных" if n < ET_MIN_N else unit
            pv = prv.get((uid, param), (None,))[0] if prv else None
            rows.append(donut(rel if n >= ET_MIN_N else None,
                              et_fmt(param, v), sub,
                              et_trend(up, v, pv), full=(bu == uid)))
    return (f'<h2>{e(title)}</h2><section class="card">'
            f'<div class="et-grid" style="grid-template-columns:{cols}">'
            + "".join(head) + "".join(rows)
            + f'</div><p class="et-legend" style="margin:12px 0 0">{note}</p>'
            '</section>')


def page_etalon(conn):
    last, prev, cur, prv, names = et_data(conn)
    if not last:
        return ('<section class="card"><p class="muted">Снимков слепка ещё '
                'нет — таблица manager_snapshot пуста.</p></section>')
    # порядок: эталон первым, дальше по близости к эталонам рычагов
    def mean_rel(uid):
        rs = []
        for param, _l, _u, up, _w in ET_LEVERS:
            m = cur.get((uid, param))
            b, _ = et_best(cur, names, param, up)
            r = et_rel(m[0], b, up) if m and m[1] >= ET_MIN_N else None
            if r is not None:
                rs.append(r)
        return sum(rs) / len(rs) if rs else 0
    order = sorted(names, key=lambda u: (u != ET_STAR, -mean_rel(u)))

    intro = (f'<p class="et-legend">Каждый кружок — параметр менеджера за '
             f'последние 30 дней против <b>эталона</b> — лучшего в отделе по '
             f'этому параметру (его кружок синий и полный). Заливка — '
             f'насколько близко к эталону, стрелка — динамика к снимку '
             f'недельной давности. Числа нормированы на состав классов '
             f'A/B/C/D, где классы уже накоплены. Эталон отдела по результату '
             f'и процессу — <b>{e(names.get(ET_STAR, ""))}</b>. Снимок за '
             f'{ru_date(last)}, обновляется каждый день.</p>')

    blocks = et_block(cur, prv, names, order, ET_LEVERS,
                      "Рычаги результата",
                      "Эти параметры проверены на связь с покупкой вне выборки "
                      "(этап 3 слепка): плотность и настойчивость дают "
                      "конверсию в разы, следующий шаг и техники подтверждены "
                      "на зеркальной выборке разговоров. «Техники продаж» — "
                      "пока прокси по имеющимся полям, новые поля разборщика "
                      "копятся с 25.08.")
    blocks += et_block(cur, prv, names, order, ET_DISC,
                       "Дисциплина",
                       "Порядок в работе: скорость, брошенные, гигиена CRM, "
                       "обещания. Связь с конверсией на вызревших заявках не "
                       "подтверждена (у скорости — даже перевёрнута, вероятен "
                       "конфаундер источника), поэтому цену разрывов здесь не "
                       "считаем — но это лицо отдела перед клиентом.")
    blocks += et_block(cur, prv, names, order, ET_MONITOR,
                       "Следствия интереса и результат",
                       "Растянутость диалога и длина разговора — следствие "
                       "интереса клиента, а не действие менеджера: наблюдаем, "
                       "не давим. Конверсия — по вызревшим заявкам 45–135 "
                       "дней, сегодняшняя работа отразится в ней через "
                       "полтора месяца.")

    cards = []
    for uid in order:
        gaps = et_gaps(cur, names, uid)
        if not gaps:
            body = ('<p class="sm">Заметных разрывов с эталоном по значимым '
                    'параметрам нет.</p>')
        else:
            body = "".join(
                f'<div class="gap-i"><span class="gap-n">{i+1}</span>'
                f'<b>{e(g["label"])}</b>: у него {et_fmt(g["param"], g["v"])}'
                f'{e(g["unit"])} — эталон {et_fmt(g["param"], g["best"])}'
                f'{e(g["unit"])} ({e((g["who"].split() or [""])[0])})'
                f'<span class="gap-price">{et_price_text(g)}</span></div>'
                for i, g in enumerate(gaps))
        cards.append(f'<section class="card"><div class="et-name" '
                     f'style="margin-bottom:6px">{e(names[uid])}</div>'
                     + body + '</section>')

    outro = ('<p class="foot">Как это считается: снимок '
             '<code>manager_snapshot</code> раз в день; только пригодные '
             'номера, без PARTNER, окно звонков от первой карточки номера, '
             'дубли менеджеру не в вину. Цена разрыва — консервативно: '
             'половина фактической разницы конверсий вне выборки, вилка от '
             'четверти до полной; самоотбор не вычищен, истинную цену '
             'покажет только эксперимент. Это инструмент роста, не рейтинг '
             'и не основа премий.</p>')

    return (intro + blocks
            + '<h2>Разрывы и цена</h2><div class="et-cards">'
            + "".join(cards) + '</div>' + outro)


def mgr_gaps_block(conn, uid):
    """Личные топ-3 разрыва на дашборде менеджера. Не рейтинг: только свои
    точки роста и пример эталона."""
    try:
        last, prev, cur, prv, names = et_data(conn)
    except Exception:
        return ""
    if not last or uid not in names:
        return ""
    gaps = et_gaps(cur, names, uid)
    if not gaps:
        return ""
    items = []
    for i, g in enumerate(gaps):
        who = e((g["who"].split() or ["лучшего"])[0]) if g["who"] else "лучшего"
        items.append(
            f'<div class="gap-i"><span class="gap-n">{i+1}</span>'
            f'<b>{e(g["label"])}</b>: у {who} — '
            f'{et_fmt(g["param"], g["best"])}{e(g["unit"])}, у тебя — '
            f'{et_fmt(g["param"], g["v"])}{e(g["unit"])}.'
            f'<span class="gap-price">{et_price_text(g)}</span></div>')
    return ('<h2>Три точки роста</h2><section class="card" '
            'style="max-width:560px">' + "".join(items)
            + '<p class="crit-f" style="margin-top:12px">Сравнение с лучшим '
            'в отделе по каждому параметру за 30 дней, с поправкой на '
            'качество достающихся заявок. Это инструмент роста — не рейтинг '
            'и не премии.</p></section>')



# ── вкладка РОПа «Качество разговоров» (QA-оценка) ───────────────────────────
# Данные — call_qa (app/qa.py + промт разборщика). Баллы 0–10 по 16 метрикам,
# блоки A 25% / B 30% / C 35% / D 10%. A3 (доля речи и монолог) считается кодом
# по ролям, остальное — модель с цитатой-доказательством на каждую метрику.
#
# Почему радар разнесён на малые множители, а не один график на всех: палитра
# прогнана валидатором — шесть цветных полигонов не различаются ни при
# дальтонизме, ни в тёмной теме. Каждому свой радар, серый контур — отдел,
# шкалы одинаковые: сравнение остаётся, читаемость появляется.

QA_DAYS = 30
QA_MIN_CALLS = 5           # меньше — «мало данных», в рейтинг не берём
QA_BLOCKS = [("a", "Структура", 0.25), ("b", "Диалог", 0.30),
             ("c", "Результат", 0.35), ("d", "Эмоции", 0.10)]
QA_METRICS = [
    ("a1", "A1", "Этапы звонка"), ("a2", "A2", "Инициатива"),
    ("a3", "A3", "Доля речи"), ("a4", "A4", "Монолог"),
    ("b1", "B1", "Потребность"), ("b2", "B2", "Выгоды"),
    ("b3", "B3", "Слушание"), ("b4", "B4", "Возражения"),
    ("b5", "B5", "Чистота речи"),
    ("c1", "C1", "Закрытие"), ("c2", "C2", "Следующий шаг"),
    ("c3", "C3", "Квалификация"), ("c4", "C4", "Цена"),
    ("d1", "D1", "Подстройка"), ("d2", "D2", "Негатив"), ("d3", "D3", "Тон"),
]
QA_LONG = {"a1": "соблюдение этапов звонка", "a2": "инициатива в разговоре",
           "a3": "доля речи менеджера, норма 40–60%",
           "a4": "длина самого долгого монолога, порог 90 секунд",
           "b1": "выявление потребности", "b2": "презентация через выгоды",
           "b3": "активное слушание", "b4": "отработка возражений",
           "b5": "чистота речи", "c1": "попытка закрытия",
           "c2": "качество следующего шага", "c3": "квалификация",
           "c4": "работа с ценой",
           "d1": "подстройка под клиента", "d2": "реакция на негатив",
           "d3": "энергия и тон"}

QA_SQL = f"""
  SELECT q.portal_user_id AS uid, count(*) AS n,
         {", ".join(f"avg(q.{k}) AS {k}" for k, _s, _t in QA_METRICS)},
         avg(q.block_a) AS block_a, avg(q.block_b) AS block_b,
         avg(q.block_c) AS block_c, avg(q.block_d) AS block_d,
         avg(q.integral) AS integral, stddev_samp(q.integral) AS sd,
         100.0 * count(*) FILTER (WHERE q.c2 >= 4)
               / NULLIF(count(*) FILTER (WHERE q.c2 IS NOT NULL), 0) AS conv,
         avg(q.talk_share) AS talk_share, avg(q.max_mono_sec) AS mono
    FROM call_qa q
   WHERE q.call_start >= now() - interval '{QA_DAYS} days'
     AND q.portal_user_id IN ({WORKING})
   GROUP BY 1
"""

QA_EDGE_SQL = f"""
  SELECT uid, kind, call_id, integral, quotes, call_start, crm_id FROM (
    SELECT q.portal_user_id AS uid, q.call_id, q.integral, q.quotes,
           q.call_start, c.crm_entity_id AS crm_id,
           row_number() OVER (PARTITION BY q.portal_user_id
                              ORDER BY q.integral DESC NULLS LAST) AS rn_hi,
           row_number() OVER (PARTITION BY q.portal_user_id
                              ORDER BY q.integral ASC NULLS LAST) AS rn_lo
      FROM call_qa q JOIN calls c ON c.id = q.call_id
     WHERE q.call_start >= now() - interval '{QA_DAYS} days'
       AND q.portal_user_id IN ({WORKING}) AND q.integral IS NOT NULL
  ) t, LATERAL (SELECT CASE WHEN rn_hi = 1 THEN 'best'
                            WHEN rn_lo = 1 THEN 'worst' END AS kind) k
   WHERE k.kind IS NOT NULL
"""


def qa_fetch(conn):
    if "qa" in _CACHE:
        return _CACHE["qa"]
    cur = conn.execute(QA_SQL)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        for c in cols:
            if c not in ("uid", "n") and r[c] is not None:
                r[c] = float(r[c])
    names = {r[0]: human_name(r[1], r[2]) for r in conn.execute(f"""
        SELECT m.portal_user_id, m.name, m.last_name FROM managers m
         WHERE m.portal_user_id IN ({WORKING})""").fetchall()}
    edges = {}
    for uid, kind, call_id, integral, quotes, when, crm_id in conn.execute(
            QA_EDGE_SQL):
        edges[(uid, kind)] = dict(call_id=call_id, integral=float(integral),
                                  quotes=quotes or {}, when=when, crm=crm_id)
    period = conn.execute(
        f"""SELECT min(call_start) AT TIME ZONE '{TZ}',
                   max(call_start) AT TIME ZONE '{TZ}', count(*)
              FROM call_qa
             WHERE call_start >= now() - interval '{QA_DAYS} days'""").fetchone()
    _CACHE["qa"] = (rows, names, edges, period)
    return _CACHE["qa"]


def qa_dept(rows):
    """Среднее по отделу, взвешенное по числу звонков."""
    out = {}
    keys = [k for k, _s, _t in QA_METRICS] + ["block_a", "block_b", "block_c",
                                              "block_d", "integral"]
    for k in keys:
        num = sum(r[k] * r["n"] for r in rows if r.get(k) is not None)
        den = sum(r["n"] for r in rows if r.get(k) is not None)
        out[k] = num / den if den else None
    return out


def qa_mix(v, weak=False):
    """Цвет ячейки тепловой карты: расходящаяся шкала вокруг 5 из 10.

    Слоты статуса (плохо/хорошо) с нейтральной серединой на фоне поверхности —
    не радуга. color-mix держит обе темы одной формулой.
    """
    if v is None:
        return "var(--bg)"
    d = max(-1.0, min(1.0, (v - 5.0) / 5.0))
    pct = int(round(abs(d) * 62))
    if pct < 4:
        return "var(--bg)"
    tone = "var(--good)" if d > 0 else "var(--bad)"
    return f"color-mix(in oklab, {tone} {pct}%, var(--surface))"


def qa_radar(vals, ref, size=170, label=None):
    """Радар по четырём блокам: серый контур — отдел, синий — менеджер."""
    cx = cy = size / 2
    r = size / 2 - 26
    n = len(QA_BLOCKS)
    import math
    def pt(i, frac):
        a = -math.pi / 2 + 2 * math.pi * i / n
        rr = r * max(0.0, min(1.0, frac))
        return cx + rr * math.cos(a), cy + rr * math.sin(a)
    grid = []
    for ring in (0.25, 0.5, 0.75, 1.0):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                       (pt(i, ring) for i in range(n)))
        grid.append(f'<polygon class="grid" fill="none" points="{pts}"></polygon>')
    for i in range(n):
        x, y = pt(i, 1.0)
        grid.append(f'<line class="grid" x1="{cx}" y1="{cy}" x2="{x:.1f}" '
                    f'y2="{y:.1f}"></line>')
    def poly(source, cls, style):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in
                       (pt(i, (source.get("block_" + k) or 0) / 10.0)
                        for i, (k, _t, _w) in enumerate(QA_BLOCKS)))
        return f'<polygon class="{cls}" points="{pts}" style="{style}"></polygon>'
    body = "".join(grid)
    body += poly(ref, "ref", "fill:none;stroke:var(--ink3);stroke-width:1.5;"
                             "stroke-dasharray:4 3")
    body += poly(vals, "me", "fill:color-mix(in oklab,var(--blue) 22%,transparent);"
                             "stroke:var(--blue);stroke-width:2;"
                             "stroke-linejoin:round")
    for i, (k, t, _w) in enumerate(QA_BLOCKS):
        x, y = pt(i, 1.22)
        anchor = ("middle" if i in (0, 2) else
                  "start" if i == 1 else "end")
        v = vals.get("block_" + k)
        body += (f'<text class="ax" x="{x:.1f}" y="{y + 3:.1f}" '
                 f'text-anchor="{anchor}">{e(t)} {num(v, 1)}</text>')
    title = f'<title>{e(label)}</title>' if label else ""
    pad = 46          # место под подписи блоков по краям
    return (f'<svg class="chart" viewBox="{-pad} {-14} {size + pad * 2} '
            f'{size + 28}" role="img" style="max-width:{size + pad * 2}px;'
            f'margin:0 auto">{title}{body}</svg>')


def qa_bars(rows, names, dept, w=620):
    """Интегральный балл по менеджерам + линия среднего по отделу."""
    if not rows:
        return ""
    rows = sorted(rows, key=lambda r: -(r["integral"] or 0))
    bh, gap, top, left = 26, 10, 16, 162
    h = top + len(rows) * (bh + gap) + 26
    inner = w - left - 46
    mx = 10.0
    avg = dept["integral"] or 0
    ax = left + inner * avg / mx
    body = [f'<line class="ref" x1="{ax:.1f}" y1="{top - 8}" x2="{ax:.1f}" '
            f'y2="{h - 24}"></line>',
            f'<text class="ax" x="{ax:.1f}" y="{h - 8}" text-anchor="middle">'
            f'среднее по отделу {num(avg, 1)}</text>']
    for i, r in enumerate(rows):
        y = top + i * (bh + gap)
        v = r["integral"] or 0
        bw = max(3.0, inner * v / mx)
        lead = (i == 0 and r["n"] >= QA_MIN_CALLS)
        fill = "var(--good)" if lead else "var(--blue)"
        body.append(
            f'<g><title>{e(names.get(r["uid"], ""))}: {num(v, 2)} из 10, '
            f'{r["n"]} {plural(r["n"], "звонок", "звонка", "звонков")}</title>'
            f'<text class="ax" x="{left - 8}" y="{y + bh * 0.68:.0f}" '
            f'text-anchor="end" style="font-size:12.5px;fill:var(--ink)">'
            f'{e(names.get(r["uid"], ""))}</text>'
            f'<rect x="{left}" y="{y}" width="{bw:.1f}" height="{bh}" rx="4" '
            f'style="fill:{fill}"></rect>'
            f'<text class="ax" x="{left + bw + 7:.1f}" y="{y + bh * 0.68:.0f}" '
            f'style="fill:var(--ink);font-variant-numeric:tabular-nums">'
            f'{num(v, 2)}</text></g>')
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" role="img">'
            f'<title>Интегральный балл по менеджерам</title>'
            + "".join(body) + '</svg>')


def qa_scatter(rows, names, w=560, h=330):
    """Балл × стабильность. Ось Y перевёрнута: чем выше, тем ровнее."""
    pts = [r for r in rows if r["integral"] is not None and r["sd"] is not None]
    if not pts:
        return ('<p class="qa-note">Стабильность появится, когда у менеджеров '
                'наберётся хотя бы по два оценённых разговора.</p>')
    pad_l, pad_b, pad_t, pad_r = 44, 40, 22, 14
    xs = [r["integral"] for r in pts]
    ys = [r["sd"] for r in pts]
    x0, x1 = min(min(xs) - .6, 3), max(max(xs) + .6, 7)
    y0, y1 = 0, max(max(ys) * 1.25, 1.0)
    def px(v):
        return pad_l + (w - pad_l - pad_r) * (v - x0) / (x1 - x0)
    def py(v):  # перевёрнутая: маленький разброс — вверху
        return pad_t + (h - pad_t - pad_b) * (v - y0) / (y1 - y0)
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    body = [
        f'<line class="grid" x1="{pad_l}" y1="{h - pad_b}" x2="{w - pad_r}" '
        f'y2="{h - pad_b}"></line>',
        f'<line class="grid" x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" '
        f'y2="{h - pad_b}"></line>',
        f'<line class="ref" x1="{px(mx):.1f}" y1="{pad_t}" x2="{px(mx):.1f}" '
        f'y2="{h - pad_b}"></line>',
        f'<line class="ref" x1="{pad_l}" y1="{py(my):.1f}" x2="{w - pad_r}" '
        f'y2="{py(my):.1f}"></line>',
        f'<text class="ax" x="{w - pad_r - 4}" y="{pad_t + 14}" '
        f'text-anchor="end" style="font-weight:700;fill:var(--good)">'
        f'Лидеры: балл выше среднего, разброс ниже</text>',
        f'<text class="ax" x="{(w + pad_l) / 2:.0f}" y="{h - 6}" '
        f'text-anchor="middle">интегральный балл →</text>',
        f'<text class="ax" x="12" y="{(h - pad_b + pad_t) / 2:.0f}" '
        f'text-anchor="middle" transform="rotate(-90 12 '
        f'{(h - pad_b + pad_t) / 2:.0f})">← разброс между звонками</text>',
    ]
    placed = []
    for r in sorted(pts, key=lambda z: z["integral"]):
        x, y = px(r["integral"]), py(r["sd"])
        dy = -12
        while any(abs(x - ox) < 62 and abs(y + dy - oy) < 13
                  for ox, oy in placed):
            dy = 20 if dy < 0 else dy + 14
        placed.append((x, y + dy))
        good = r["integral"] >= mx and r["sd"] <= my
        col = "var(--good)" if good else "var(--blue)"
        nm = (names.get(r["uid"], "") or "").split()
        body.append(
            f'<g><title>{e(names.get(r["uid"], ""))}: балл {num(r["integral"], 2)}, '
            f'разброс {num(r["sd"], 2)}, {r["n"]} '
            f'{plural(r["n"], "звонок", "звонка", "звонков")}</title>'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" style="fill:{col};'
            f'stroke:var(--surface);stroke-width:2"></circle>'
            f'<text class="ax" x="{x:.1f}" y="{y + dy:.1f}" text-anchor="middle" '
            f'style="fill:var(--ink);font-size:11.5px">'
            f'{e(nm[-1] if nm else "")}</text></g>')
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" role="img">'
            f'<title>Балл против стабильности</title>' + "".join(body) + '</svg>')


def qa_edges(rows, names, dept):
    """Топ-3 сильных и топ-3 слабых метрики относительно отдела."""
    res = {}
    for r in rows:
        diffs = []
        for k, short, title in QA_METRICS:
            v, d = r.get(k), dept.get(k)
            if v is None or d is None:
                continue
            diffs.append((v - d, k, short, title, v))
        diffs.sort(reverse=True)
        res[r["uid"]] = (diffs[:3], list(reversed(diffs))[:3])
    return res


def qa_quote_for(edge, key):
    q = (edge or {}).get("quotes") or {}
    return q.get(key.upper())


def page_kachestvo(conn):
    rows, names, edges, period = qa_fetch(conn)
    if not rows:
        return ('<section class="card"><p class="muted">Оценок пока нет: '
                'карточка качества считается на разговорах, разобранных после '
                '25.08.2026.</p></section>')
    dept = qa_dept(rows)
    ranked = sorted([r for r in rows if r["n"] >= QA_MIN_CALLS],
                    key=lambda r: -(r["integral"] or 0))
    thin = [r for r in rows if r["n"] < QA_MIN_CALLS]
    edge_metrics = qa_edges(rows, names, dept)
    d0, d1, total = period
    counts = [r["n"] for r in rows]
    spread = (max(counts) / max(1, min(counts))) if counts else 1

    head = (f'<p class="sub" style="margin-bottom:14px">Период: '
            f'{ru_date(d0.date())} — {ru_date(d1.date())} · '
            f'{total} {plural(total, "разговор", "разговора", "разговоров")} · '
            f'{len(rows)} {plural(len(rows), "менеджер", "менеджера", "менеджеров")}'
            f' · окно {QA_DAYS} дней</p>')

    warn = ""
    if spread > 3:
        warn = ('<p class="qa-warn">Разница в числе разговоров между '
                'менеджерами больше чем втрое — для части сравнений данных '
                'недостаточно, смотрите на число звонков в таблице.</p>')

    # ── рейтинг ────────────────────────────────────────────────────────────
    trs = []
    for i, r in enumerate(ranked + thin):
        lead = (i == 0 and r in ranked)
        mark = ' style="background:color-mix(in oklab,var(--good) 12%,transparent)"'
        sd = num(r["sd"], 2) if r["sd"] is not None else "—"
        note = "" if r["n"] >= QA_MIN_CALLS else             '<span class="sub2">мало данных</span>'
        trs.append(
            f'<tr{mark if lead else ""}>'
            f'<td class="mname">{"🏆 " if lead else ""}{e(names.get(r["uid"], ""))}'
            f'{note}</td>'
            f'<td><b>{num(r["integral"], 2)}</b></td>'
            f'<td>{num(r["block_a"], 1)}</td><td>{num(r["block_b"], 1)}</td>'
            f'<td>{num(r["block_c"], 1)}</td><td>{num(r["block_d"], 1)}</td>'
            f'<td>{sd}</td><td>{num(r["conv"], 0)}%</td><td>{r["n"]}</td></tr>')
    table = (
        '<h2>Рейтинг менеджеров</h2><section class="card">'
        '<table class="qa-tbl"><thead><tr><th>Менеджер</th><th>Интеграл</th>'
        '<th>A структура</th><th>B диалог</th><th>C результат</th>'
        '<th>D эмоции</th><th>Разброс</th><th>Следующий шаг</th>'
        '<th>Звонков</th></tr></thead><tbody>' + "".join(trs) + '</tbody></table>'
        '<p class="qa-note">Интеграл = A×0,25 + B×0,30 + C×0,35 + D×0,10. '
        'Разброс — стандартное отклонение интеграла между звонками, меньше '
        'значит ровнее. «Следующий шаг» — доля звонков, где договорённость '
        'хотя бы намечена (C2 ≥ 4).</p>' + warn + '</section>')

    # ── радары ────────────────────────────────────────────────────────────
    rad = "".join(
        f'<figure class="qa-rad" style="margin:0">'
        + qa_radar(r, dept, label=names.get(r["uid"], ""))
        + f'<figcaption>{e(names.get(r["uid"], ""))}'
          f'<span class="sub2">интеграл {num(r["integral"], 2)} · '
          f'{r["n"]} {plural(r["n"], "звонок", "звонка", "звонков")}</span>'
          f'</figcaption></figure>'
        for r in ranked + thin)
    radars = (
        '<h2>Портрет по блокам</h2><section class="card">'
        f'<div class="qa-radars">{rad}</div>'
        '<p class="qa-note">Синий — менеджер, пунктирный серый — среднее по '
        'отделу, шкала у всех одна (0–10). Шесть цветных полигонов на одном '
        'радаре неразличимы при дальтонизме и в тёмной теме — проверено '
        'валидатором палитры, поэтому портреты разнесены.</p></section>')

    # ── столбцы и разброс ─────────────────────────────────────────────────
    charts = (
        '<h2>Балл и стабильность</h2><div class="qa-grid2">'
        '<section class="card">' + qa_bars(rows, names, dept)
        + '<p class="qa-note">Зелёный — лидер периода. Пунктир — среднее по '
          'отделу.</p></section>'
        '<section class="card">' + qa_scatter(rows, names)
        + '<p class="qa-note">Правый верхний угол — высокий балл при ровном '
          'качестве от звонка к звонку.</p></section></div>')

    # ── тепловая карта ────────────────────────────────────────────────────
    order = ranked + thin
    cols = f"minmax(120px,150px) repeat({len(QA_METRICS)},1fr)"
    cells = ['<div></div>']
    for k, short, title in QA_METRICS:
        cells.append(f'<div class="hh" title="{e(QA_LONG[k])}"><b>{short}</b><br>'
                     f'{e(title)}</div>')
    for r in order:
        cells.append(f'<div class="hn">{e(names.get(r["uid"], ""))}</div>')
        for k, short, title in QA_METRICS:
            v = r.get(k)
            cls = "qa-cell" + ("" if v is not None else " qa-cell na")
            cells.append(
                f'<div class="{cls}" style="background:{qa_mix(v)}" '
                f'title="{e(names.get(r["uid"], ""))} · {short} '
                f'{e(QA_LONG[k])}: {num(v, 1)}">{num(v, 1)}</div>')
    heat = ('<h2>Метрики по менеджерам</h2><section class="card">'
            f'<div class="qa-hm" style="grid-template-columns:{cols}">'
            + "".join(cells) + '</div>'
            '<p class="qa-note">Зелёное — выше середины шкалы, красное — ниже, '
            'серое — метрика в этих звонках не применялась (N/A) и в среднее '
            'не входит. A3 и A4 считает код по разметке ролей, а не модель: '
            'A3 — доля речи (десятка внутри нормы 40–60%), A4 — самый длинный '
            'монолог (десятка до 90 секунд, дальше минус балл за каждые 20). '
            'По отделу доля речи менеджера сейчас около '
            f'{num((dept.get("a3") is not None) and 100 * (sum((r.get("talk_share") or 0) * r["n"] for r in rows) / max(1, sum(r["n"] for r in rows))) or 0, 0)}% — '
            'это системная особенность разговоров, а не провал одного человека; '
            'на исход сделки доля речи, по нашим прошлым замерам, не влияет.'
            '</p></section>')

    # ── карточки ──────────────────────────────────────────────────────────
    cards = []
    for r in order:
        uid = r["uid"]
        nm = names.get(uid, "")
        initials = "".join(p[0] for p in nm.split()[:2]).upper()
        strong, weak = edge_metrics.get(uid, ([], []))
        best, worst = edges.get((uid, "best")), edges.get((uid, "worst"))
        blocks = "".join(
            f'<div class="qa-b"><b>{num(r.get("block_" + k), 1)}</b>'
            f'<span>{e(t)}</span></div>' for k, t, _w in QA_BLOCKS)

        def li(items, sign):
            out = []
            for diff, k, short, title, v in items:
                q = qa_quote_for(best if sign > 0 else worst, k)
                qt = (f'<span class="sub2">«{e(str(q)[:110])}»</span>'
                      if q else "")
                out.append(f'<li><b>{short}</b> {e(QA_LONG[k])} — '
                           f'{num(v, 1)} '
                           f'({"+" if diff >= 0 else "−"}{num(abs(diff), 1)} '
                           f'к отделу){qt}</li>')
            return "".join(out) or "<li>—</li>"

        q_best = next((qa_quote_for(best, k) for k, _s, _t in QA_METRICS
                       if qa_quote_for(best, k)), None)
        q_worst = next((qa_quote_for(worst, k) for k in ("c2", "b4", "c1", "a2")
                        if qa_quote_for(worst, k)), None)
        quotes = ""
        if best and q_best:
            link = crm_link("LEAD", best["crm"]) if best.get("crm") else None
            a = (f' · <a href="{link}" target="_blank" rel="noopener">карточка</a>'
                 if link else "")
            quotes += (f'<p class="qa-quote best"><b>Лучший звонок '
                       f'{num(best["integral"], 1)}:</b> «{e(str(q_best)[:180])}»'
                       f'{a}</p>')
        if worst and q_worst:
            link = crm_link("LEAD", worst["crm"]) if worst.get("crm") else None
            a = (f' · <a href="{link}" target="_blank" rel="noopener">карточка</a>'
                 if link else "")
            quotes += (f'<p class="qa-quote worst"><b>Слабый звонок '
                       f'{num(worst["integral"], 1)}:</b> «{e(str(q_worst)[:180])}»'
                       f'{a}</p>')
        cards.append(
            f'<section class="card"><div class="qa-chead">'
            f'<div class="qa-face">{e(initials)}</div><div>'
            f'<div class="nm">{e(nm)}</div>'
            f'<span class="sub2">интеграл {num(r["integral"], 2)} · разброс '
            f'{num(r["sd"], 2) if r["sd"] is not None else "—"} · {r["n"]} '
            f'{plural(r["n"], "звонок", "звонка", "звонков")} · следующий шаг '
            f'{num(r["conv"], 0)}%</span></div></div>'
            f'<div class="qa-blocks">{blocks}</div>'
            f'<div class="qa-lists"><div><h4>Сильные стороны</h4><ul>'
            f'{li(strong, 1)}</ul></div><div><h4>Зоны роста</h4><ul>'
            f'{li(weak, -1)}</ul></div></div>{quotes}</section>')
    cards_html = ('<h2>Карточки менеджеров</h2><div class="qa-cards">'
                  + "".join(cards) + '</div>')

    # ── вывод ─────────────────────────────────────────────────────────────
    concl = (qa_conclusion(ranked, thin, names, dept, edge_metrics)
             + qa_plan_block(conn, order, names))

    foot = ('<p class="foot">Как это считается: каждый разговор длиннее минуты '
            'разбирается моделью тем же проходом, что и договорённости, — '
            'по 16 метрикам с цитатой-доказательством на каждую. Баллы блоков '
            'и интеграл складывает код, доля речи и длина монолога считаются '
            'механически по разметке ролей. Метрика, которая в звонке не '
            'применима, помечается N/A и в среднее не входит. Инструмент для '
            'разбора и обучения, не для премий: у модели своя погрешность, '
            'а запись одноканальная.</p>')
    return (head + table + radars + charts + heat + cards_html + concl + foot)


def qa_advice_rows(conn):
    """Последний недельный срез плана: (понедельник среза, {uid: payload})."""
    if "qa_advice" in _CACHE:
        return _CACHE["qa_advice"]
    week = conn.execute("SELECT max(week_start) FROM qa_advice").fetchone()[0]
    rows = {}
    if week:
        for uid, payload, n in conn.execute(
                """SELECT portal_user_id, payload, n_calls FROM qa_advice
                    WHERE week_start = %s""", (week,)):
            rows[uid] = payload
    _CACHE["qa_advice"] = (week, rows)
    return _CACHE["qa_advice"]


def qa_pc_class(v):
    return "bad" if v < 55 else ("mid" if v < 75 else "ok")


def qa_plan_block(conn, order, names):
    """Детальный план работы по каждому менеджеру. Пересчитывается раз
    в неделю (app/qa_advice.py, крон в ночь на понедельник) — рекомендация
    не должна меняться каждый день."""
    week, rows = qa_advice_rows(conn)
    if not week or not rows:
        return ""
    nxt = week + dt.timedelta(days=7)
    cards = []
    for r in order:                      # order — строки агрегата, не uid
        uid = r["uid"] if isinstance(r, dict) else r
        p = rows.get(uid)
        if not p:
            continue
        thin = ('<span class="thin">мало данных, читать как предварительное</span>'
                if p.get("thin") else "")
        head = (f'<div class="adv-h"><span class="nm">{e(p.get("name", ""))}'
                f'</span><span class="meta">{p.get("n", 0)} '
                f'{plural(p.get("n", 0), "звонок", "звонка", "звонков")} '
                f'с оценкой · интеграл {num(p.get("integral"), 2)} · '
                f'следующий шаг {num(p.get("conv"), 0)}%</span>{thin}</div>')

        talk = (f'<p class="adv-talk">{qa_md(p["talk_text"])}</p>'
                if p.get("talk_text") else "")

        gaps = p.get("gaps") or []
        gaps_html = ""
        if gaps:
            items = "".join(
                f'<li><span class="n">{i + 1}</span>'
                f'<span class="code">{e(g["code"])} {e(g["title"])}.</span> '
                f'{qa_md(g.get("text", ""))}</li>'
                for i, g in enumerate(gaps))
            gaps_html = (f'<div class="adv-sec">Над чем работать</div>'
                         f'<ul class="adv-list">{items}</ul>')

        cats = p.get("obj_cats") or []
        obj_html = ""
        if cats:
            cells = "".join(
                f'<div class="adv-o"><div class="top">'
                f'<span class="lbl">{e(c["label"])}</span>'
                f'<span class="pc {qa_pc_class(c["handled"])}">'
                f'{c["handled"]}%</span></div>'
                f'<div class="tip">{e(c["tip"])}</div>'
                f'<div class="tip" style="margin-top:3px">прозвучало '
                f'{c["n"]} {plural(c["n"], "раз", "раза", "раз")} '
                f'за 30 дней</div></div>'
                for c in cats)
            bad = p.get("obj_bad") or []
            bad_txt = ""
            if bad:
                bad_txt = ('<p class="adv-bad">Чаще всего остаётся без ответа: '
                           + ", ".join(f'<b>«{e(b["text"])}»</b> ({b["n"]})'
                                       for b in bad) + '.</p>')
            obj_html = (f'<div class="adv-sec">Возражения: доля разговоров, '
                        f'где отработал</div><div class="adv-obj">{cells}</div>'
                        f'{bad_txt}')

        strong = p.get("strong") or []
        strong_html = ""
        if strong:
            items = "".join(
                f'<li class="good"><span class="n">✓</span>'
                f'<span class="code">{e(s["code"])} {e(s["title"])}</span> — '
                f'{num(s["v"], 1)} против {num(s["dept"], 1)} по отделу</li>'
                for s in strong)
            strong_html = (f'<div class="adv-sec">На чём держится</div>'
                           f'<ul class="adv-list">{items}</ul>')

        worst = p.get("worst") or {}
        wq = worst.get("quotes") or {}
        q = next((wq[k] for k in ("C2", "B4", "C1", "A2", "B1") if wq.get(k)),
                 None)
        qhtml = ""
        if q:
            link = crm_link("LEAD", worst["crm"]) if worst.get("crm") else None
            a = (f' · <a href="{link}" target="_blank" rel="noopener">'
                 f'послушать в карточке</a>' if link else "")
            qhtml = (f'<p class="qa-quote worst"><b>Разобрать на планёрке '
                     f'(звонок на {num(worst.get("integral"), 1)}):</b> '
                     f'«{e(str(q)[:200])}»{a}</p>')

        cards.append(f'<div class="adv">{head}{talk}{gaps_html}{obj_html}'
                     f'{strong_html}{qhtml}</div>')
    if not cards:
        return ""
    return (
        '<h2>Кому что тренировать</h2><section class="card">'
        f'<p class="adv-when">Срез за 30 дней на {ru_date(week)}, '
        f'пересчитывается по понедельникам — следующий {ru_date(nxt)}. '
        'Внутри недели рекомендации не меняются: это план тренировки, '
        'а не лента. Возражения считаются по всем разобранным разговорам '
        'за 30 дней, поэтому их больше, чем звонков с полной QA-оценкой.</p>'
        + "".join(cards) + '</section>')


def qa_md(s):
    """Минимальная разметка: **жирный** -> <b>, остальное экранируется."""
    out, bold = [], False
    for i, part in enumerate(str(s).split("**")):
        out.append(("<b>" + e(part) + "</b>") if i % 2 else e(part))
    return "".join(out)


def qa_conclusion(ranked, thin, names, dept, edge_metrics):
    if not ranked:
        return ('<h2>Вывод</h2><section class="card"><p class="qa-note">'
                'Оценённых разговоров пока слишком мало, чтобы называть '
                'лидера.</p></section>')
    lead = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    ln, sn = names.get(lead["uid"], ""), names.get(second["uid"], "") if second else ""
    gap = (lead["integral"] - second["integral"]) if second else None
    strong, _ = edge_metrics.get(lead["uid"], ([], []))
    s_txt = ", ".join(QA_LONG[k] for _d, k, _s, _t, _v in strong[:2]) or "процесс"
    close = ""
    if second:
        close = (f'Ближайший преследователь — {e(sn)} ({num(second["integral"], 2)}), '
                 f'отставание {num(gap, 2)} балла: ')
        if gap is not None and gap < 0.3:
            close += 'разрыв в пределах погрешности, лидерство условное. '
        else:
            close += 'разрыв заметный, но одного периода мало. '
    rec = []
    for r in ranked + thin:
        _s, weak = edge_metrics.get(r["uid"], ([], []))
        if not weak:
            continue
        topics = ", ".join(QA_LONG[k] for _d, k, _sh, _t, _v in weak[:2])
        note = " (мало данных)" if r["n"] < QA_MIN_CALLS else ""
        rec.append(f'<li><b>{e(names.get(r["uid"], ""))}</b>{note} — {topics}</li>')
    return (
        '<h2>Вывод</h2><section class="card">'
        f'<p class="qa-note" style="font-size:14px">Лидер периода — '
        f'<b>{e(ln)}</b> с интегральным баллом {num(lead["integral"], 2)} из 10 '
        f'на {lead["n"]} {plural(lead["n"], "звонке", "звонках", "звонках")}; '
        f'сильнее всего у него {s_txt}. {close}'
        'Средний балл по отделу — '
        f'{num(dept["integral"], 2)}; самый слабый блок у всех — '
        f'{e(min(QA_BLOCKS, key=lambda b: dept.get("block_" + b[0]) or 99)[1].lower())}, '
        'туда и стоит вкладывать обучение.</p>'
        '</section>')

# ── вкладка РОПа «Замеры» ────────────────────────────────────────────────────
# Памятка Тимофея (решение 26.08.2026): какие процессы и замеры идут фоном,
# с какого числа и когда к каждому вернуться. Обновляется руками при каждом
# запуске или остановке процесса — держать в актуальном состоянии!

ZAMERY = [
    ("Новый менеджер: М8 Егор Егоров", "07.09.2026",
     "Шестой менеджер отдела, первый рабочий день 07.09. С этого дня получает "
     "заявки по кругу распределения и виден во всех таблицах панелей. До этой "
     "даты заявок и разговоров нет — его строки в окнах 14/60 дней неполные.",
     "Конец сентября — первое честное сравнение с отделом (наберётся окно). "
     "Ссылка на дашборд ему не выдана: не зарегистрирован в боте."),
    ("Пульс Синергосмото — ежедневный срез бизнеса", "26.08.2026",
     "Каждое утро в 09:30 Влд задача Клода снимает вчерашний день по всей "
     "воронке: расход, показы и клики Директа по каждой кампании; визиты и "
     "цели Метрики по autosender.ru; лиды, сделки, выигрыши и звонки из "
     "Битрикса. Копится в схеме pulse (история с 28.05, пропущенные дни "
     "дособираются задним числом). Автодетект фиксирует изменения настроек "
     "кампаний и аномалии в журнал событий; ручные события (цены, люди, "
     "акции) Тимофей диктует в чат. Итог — вкладка «Пульс» и артефакт.",
     "Раз в неделю (пн) — пересчёт «до/после» вокруг ручных событий. Главный "
     "резерв по 90 дням: заявки пт–вс конвертируются в 2–3,5 раза хуже "
     "будних при 40% объёма (~700–800 тыс ₽/мес)."),
    ("Слепок работы менеджеров → эталон", "25.08.2026",
     "Каждый день в 06:15 Влд снимается снимок работы каждого менеджера: "
     "24 параметра (скорость отклика, настойчивость, плотность, гигиена CRM, "
     "разговоры, портрет «кто ведёт»), с поправкой на классы заявок A–D. "
     "Копится в таблице manager_snapshot. Вкладка «Эталон» сравнивает всех "
     "с лучшим по каждому параметру и оценивает цену разрывов в сделках; "
     "эталон отдела по конверсии вызревших — Жернов. Визуальная лента "
     "«Слепок работы» с экрана убрана 26.08, код жив.",
     "Через 2–3 недели — смотреть динамику стрелок на «Эталоне»: сдвигаются "
     "ли отстающие параметры. Итоговый замер эффекта — конец сентября."),
    ("QA-оценка каждого разговора", "25.08.2026",
     "К каждому разбираемому разговору модель отвечает на 16 вопросов "
     "качества (структура, диалог, результат, эмоции) с цитатой-"
     "доказательством; баллы складывает код. Едет в том же запросе, что и "
     "обычный разбор (+0,04 ₽ к 0,24 ₽). Вкладка «Качество». Архив задним "
     "числом не пересчитывается — картина наполняется с 25.08.",
     "Первый недельный план «кому что тренировать» — понедельник 31.08, "
     "09:10 Влд, дальше каждый понедельник."),
    ("Портрет разговора: новые поля разборщика", "25.08.2026",
     "Разборщик начал извлекать инициативу (кто задаёт вопросы и предлагает "
     "шаг), уступки без встречного обязательства («прожали»), отработку "
     "первого отказа, применённые техники. Поля копятся только на новых "
     "разговорах; до накопления «Эталон» использует прокси-оценки.",
     "Через 2–3 недели — переключить параметры Т с прокси на честные поля "
     "(отметка в реестре slepok-menedzherov.md)."),
    ("Скоринг заявок до звонка (классы A–D)", "20.08.2026",
     "Модель по поведению на сайте ставит класс шанса продажи; класс и значки "
     "⚡/★ пишутся в CRM всем заявкам. Вне выборки разрыв A против D — 6×.",
     "Сверка точности — 2 сентября; итоговый вывод по классам — конец "
     "сентября. Открыто: ТЗ веб-мастеру на передачу ClientID (покрытие "
     "73% → ~95%)."),
    ("Составная оценка заявки и корона ♛", "21.08.2026",
     "После разговора складывается оценка 0–100 (поведение + разговор + "
     "отклик клиента), пишется в поле «Скоринг»; ♛ ≥60 — живой лид.",
     "Пересобрать веса, когда накопится ~1000 разборов с карточкой "
     "(ориентир — октябрь)."),
    ("Балл скрипта v2 по первым разговорам", "23.08.2026",
     "Страница «Скрипт» менеджера и сводная РОПа считают карточку эталонного "
     "скрипта только по первому содержательному разговору с клиентом.",
     "История разборов с августа — плитка «прошлый месяц» наполнится "
     "с сентября."),
    ("Два разговора Светлане", "23.08.2026",
     "По будням в 10:00 Влд в телеграм уходит лучший и худший первый разговор "
     "недели со ссылками на карточки; без повторов.",
     "Если перестанут приходить — лог bestworst.log."),
    ("Журнал «Контроль» (закрыли живого / горячий стынет)", "22.08.2026",
     "Сбои по живым заявкам пишутся в журнал rop_control; сама лента с экрана "
     "убрана 23.08, журнал копится.",
     "~22 сентября — сверка won: сколько возвращённых заявок дошло до сделки."),
    ("Дела и касания месяца", "24.08.2026",
     "На «Контроле отдела» — поставленные дела, клиенты, разговоры, касания "
     "на клиента; окно с 24.08, обнуляется 1-го числа.",
     "1 сентября окно начнёт полный месяц — тогда цифры сравнимы."),
    ("Починка истории статусов лидов", "25.08.2026",
     "Найден баг: Битрикс отдаёт для лидов STATUS_ID, коллектор писал пусто. "
     "История за 120 дней дозаполнена; крон каждые полчаса дозаполняет "
     "свежее, пока не пересобран образ коллектора.",
     "После ближайшей пересборки образа — убрать крон-строку stagehist "
     "(sync исправлен и справится сам)."),
    ("Бэкфилл расшифровок и добор разбора", "идёт",
     "Старые звонки расшифровываются в фоне (~до середины сентября). После "
     "простоя ProxyAPI 25.08 973 разбора возвращены в очередь, добираются "
     "по ~600 в день.",
     "Проверить через пару дней, что очередь llm_queue пуста и баллы "
     "«Скрипт»/«Качество» наполнились."),
    ("Сторож баланса ProxyAPI", "26.08.2026",
     "Каждые 15 минут проверяет, жив ли разбор; при остановке (кончился "
     "баланс) шлёт алерт Тимофею в @ironbossAS_bot, не чаще раза в 3 часа.",
     "Пришёл алерт — пополнить баланс; разбор продолжится сам."),
]

ZAMERY_CSS = """
.zm{border-top:1px solid var(--line);padding:13px 2px}
.zm:first-of-type{border-top:none}
.zm-h{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.zm-t{font-weight:600}
.zm-d{color:var(--ink3);font-size:13px;white-space:nowrap}
.zm-w{margin:5px 0 0;font-size:14px;color:var(--ink2);max-width:1000px}
.zm-b{margin:5px 0 0;font-size:13.5px;max-width:1000px}
.zm-b b{color:var(--blue)}
"""


def page_zamery(conn):
    items = []
    for title, since, what, back in ZAMERY:
        since_txt = since if since == "идёт" else f"с {since}"
        items.append(
            f'<div class="zm"><div class="zm-h"><span class="zm-t">{e(title)}'
            f'</span><span class="zm-d">{e(since_txt)}</span></div>'
            f'<p class="zm-w">{e(what)}</p>'
            f'<p class="zm-b"><b>Вернуться:</b> {e(back)}</p></div>')
    return (f"<style>{ZAMERY_CSS}</style>"
            '<p class="sub" style="max-width:1000px">Памятка: какие замеры и '
            'процессы идут фоном, с какого числа и когда к каждому вернуться. '
            'Страница обновляется вместе с запуском и остановкой процессов.</p>'
            '<section class="card">' + "".join(items) + "</section>")


SMS_STATUS_RU = {
    "planned": ("в очереди", "sms-wait"),
    "sent": ("отправлено", "sms-sent"),
    "delivered": ("доставлено", "sms-ok"),
    "no_report": ("ушла, отчёта нет", "sms-ok"),
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
               count(*) FILTER (WHERE status IN ('delivered', 'no_report')),
               count(*) FILTER (WHERE status = 'failed'),
               count(*) FILTER (WHERE status = 'skipped')
          FROM sms_outbox
         WHERE (created_at AT TIME ZONE %s) >= {month0}
         GROUP BY 1""", (TZ, TZ)).fetchall()}

    trs = []
    for u, name in sorted(mgrs.items(), key=lambda kv: kv[1]):
        n, parts, ok, bad, skip = stats.get(u, (0, 0, 0, 0, 0))
        dev = devices.get(u)
        dev_txt = ("<span class=\"sms-ok\">подключён</span>" if dev
                   else "<span class=\"sms-skip\">выключен</span>" if dev is False
                   else "<span class=\"sms-bad\">нет реквизитов</span>")
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
<b>Правило:</b> новая заявка → через 5 минут приветствие от закреплённого
менеджера, круглосуточно, не чаще одной отправки на номер за 30 дней.
Если менеджер успел поговорить с клиентом раньше — СМС не уходит
(в ленте это «пропущено»). <b>Рассылка включена 27.08</b> — пока шлют
только подключённые телефоны, остальные видны в ленте как пропуски.</p>

<h2>За {MONTHS_NOM[dt.datetime.now(VLD).month - 1]}</h2>
<section class="card">
  <table><thead><tr><th>Менеджер</th><th>Отправок</th><th>СМС</th>
    <th>Ушло</th><th>Ошибки</th><th>Пропущено</th><th>Телефон</th>
  </tr></thead><tbody>{"".join(trs)}</tbody></table>
  <p class="crit-f" style="margin-top:10px">«Отправок» — сообщений клиентам;
    «СМС» — во сколько частей они уложились (длинный текст занимает две).
    «Ушло» — телефон отправил сообщение; часть операторов не возвращает
    отчёт о доставке, такие тоже считаются здесь.
    «Пропущено» — правило не дало отправить: на этот номер уже писали
    в последние 30 дней, либо телефон менеджера не подключён.</p>
  {setup}
</section>

<h2>Последние отправки</h2>
{feed}"""



# ── вкладка РОПа «Тайминги» (под пин-кодом) ──────────────────────────────────
# Воронка договорной стадии по шагам: «Паспорт» → «Договор» →
# «Оплата обеспечительного» → «На торгах».
# Три окна-вкладки: 60 дней / текущая неделя (с понедельника) / текущий месяц
# (с 1-го числа) — недельное и месячное окна обнуляются сами. Внизу — те же
# шаги только по выигранным. Наверху — «как надо» по образцу лучшего.
# Содержимое зашифровано пин-кодом (PBKDF2 + HMAC-поток), расшифровка
# в браузере через WebCrypto — открытым текстом данных в HTML нет. Только https.

TIMINGS_DAYS = 60             # окно вкладки «60 дней»
TIMINGS_PIN = "7788"          # решение Тимофея 28.08.2026 (был 5542)
TIMINGS_WON_DAYS = 150        # окно блока «только выигранные» на вкладке 60 дней
# Балл менеджера в шаге: 70% скорость первого звонка + 30% охват звонком.
# Веса выведены из продаж (апрель–август 2026, 179 окон со звонком, статусы
# 8/10/11 существуют с апреля — глубже истории нет): corr(продажа, скорость)
# = 0.38, звонок в первые 15 мин → 66% продаж, позже суток → 10%; количество
# звонков сверх 1–2 в рамках статуса продажу не добавляет (corr −0.15),
# поэтому звонковая часть меряет охват (был ли звонок), а не число звонков.
TIMINGS_W_SPEED = 0.7
TIMINGS_W_CALLS = 0.3

TIMINGS_MIN_WINDOW = 5        # окна короче стольких минут («статус-однодневки»,
                              # поставил и тут же перевёл) в средние не входят

# Шаги воронки. Окно шага — от постановки статуса до ЛЮБОГО следующего;
# «довёл» — следующий статус оказался целевым или дальше по воронке
# (заявка, перескочившая шаг, тоже считается доведённой).
TIMINGS_SLICES = [
    {"st": "8", "head": "Шаг 1 · «Паспорт» → «Договор»",
     "succ": ("10", "11", "12", "13", "CONVERTED"), "target": "до договора"},
    {"st": "10", "head": "Шаг 2 · «Договор» → «Оплата обеспечительного»",
     "succ": ("11", "12", "13", "CONVERTED"), "target": "до предоплаты"},
    {"st": "11", "head": "Шаг 3 · «Оплата обеспечительного» → «На торгах»",
     "succ": ("12", "13", "CONVERTED"), "target": "до торгов"},
]

# Вкладки-окна страницы: ключ -> (файл, подпись)
TIMINGS_PERIODS = [("base", "taymingi.html", "60 дней"),
                   ("week", "taymingi-w.html", "Текущая неделя"),
                   ("month", "taymingi-m.html", "Текущий месяц")]

TIMINGS_SQL = """
SELECT l.id, ev.t0, nx.t_end, nx.next_stage, d.name AS next_name,
       l.title, l.phone_e164, l.assigned_by,
       m.name AS mname, m.last_name AS mlast,
       t.first_call, t.calls, t.talks
  FROM (SELECT owner_id, min(created_time) AS t0
          FROM stage_history
         WHERE entity_kind = 'lead' AND stage_id = %(st)s
           AND created_time >= %(since)s AND created_time < %(until)s
         GROUP BY owner_id) ev
  JOIN leads l ON l.id = ev.owner_id
  LEFT JOIN LATERAL (
      SELECT s.created_time AS t_end, s.stage_id AS next_stage
        FROM stage_history s
       WHERE s.entity_kind = 'lead' AND s.owner_id = l.id
         AND s.created_time > ev.t0 AND ({endcond})
       ORDER BY s.created_time LIMIT 1) nx ON TRUE
  LEFT JOIN crm_dict d ON d.kind = 'STATUS' AND d.status_id = nx.next_stage
  LEFT JOIN managers m ON m.portal_user_id = l.assigned_by
  LEFT JOIN LATERAL (
      SELECT min(c.call_start) AS first_call,
             count(*) AS calls,
             count(*) FILTER (WHERE c.duration >= 30) AS talks
        FROM calls c
       WHERE c.direction = 'out' AND c.call_start > ev.t0
         AND c.call_start <= coalesce(nx.t_end, now())
         AND ((c.crm_entity_type = 'LEAD' AND c.crm_entity_id = l.id)
              OR (l.phone_e164 IS NOT NULL
                  AND c.phone_e164 = l.phone_e164))) t ON TRUE
 WHERE TRUE{won}
 ORDER BY ev.t0 DESC
"""

TORGI_SQL = """
SELECT l.id, l.date_create, ev.t12, l.title, l.phone_e164, l.assigned_by,
       m.name AS mname, m.last_name AS mlast, t.calls, t.talks
  FROM (SELECT owner_id, min(created_time) AS t12
          FROM stage_history
         WHERE entity_kind = 'lead' AND stage_id = '12'
           AND created_time >= %(since)s AND created_time < %(until)s
         GROUP BY owner_id) ev
  JOIN leads l ON l.id = ev.owner_id
  LEFT JOIN managers m ON m.portal_user_id = l.assigned_by
  LEFT JOIN LATERAL (
      SELECT count(*) AS calls,
             count(*) FILTER (WHERE c.duration >= 30) AS talks
        FROM calls c
       WHERE c.direction = 'out'
         AND c.call_start BETWEEN l.date_create - interval '30 minutes'
                              AND ev.t12
         AND ((c.crm_entity_type = 'LEAD' AND c.crm_entity_id = l.id)
              OR (l.phone_e164 IS NOT NULL
                  AND c.phone_e164 = l.phone_e164))) t ON TRUE
 ORDER BY ev.t12 DESC
"""

TIMINGS_CSS = """
.tm-sum{font-size:15px;margin:0 0 10px;max-width:900px}
.tm-sum b{font-size:16px}
.tm-note{font-size:13px;color:var(--ink2);margin:10px 0 0;max-width:900px}
.tm-best-row{background:rgba(46,125,50,.09)}
.tm-badge{display:inline-block;font-size:11px;font-weight:600;color:#2e7d32;
  border:1px solid #2e7d32;border-radius:10px;padding:0 8px;margin-left:6px;
  vertical-align:1px}
.tm-badge2,.tm-badge3{display:inline-block;font-size:11px;font-weight:600;
  border-radius:10px;padding:0 8px;margin-left:6px;vertical-align:1px}
.tm-badge2{color:#1565c0;border:1px solid #1565c0}
.tm-badge3{color:#c62828;border:1px solid #c62828}
.tm-eff{background:rgba(21,101,192,.08)}
.tm-eff2{background:rgba(198,40,40,.07)}
.tm-imp{display:block;font-size:10px;font-weight:400;line-height:1.1;text-transform:none;letter-spacing:0}
.tm-imp.i1{color:#c62828}.tm-imp.i2{color:#1565c0}
.tm-imp.i0{color:var(--ink2)}
.tm-badge4{display:inline-block;font-size:11px;font-weight:600;color:#b26a00;border:1px solid #e0a800;border-radius:10px;padding:0 8px;margin-left:6px;vertical-align:1px}
.tm-bad{color:#c62828}
.tm-good{color:#2e7d32;font-weight:600}
.tm-kv{color:var(--ink2)}
.tm-kv b{color:var(--ink)}
.tm-plan{margin:12px 0 2px;padding-left:20px;max-width:900px}
.tm-plan li{margin:0 0 10px;font-size:14px}
.tm-mini{margin:10px 0;font-size:13px;max-width:560px}
details.tm-m{border-top:1px solid var(--line);padding:7px 0}
details.tm-m summary{cursor:pointer;list-style:none;display:flex;gap:14px;
  flex-wrap:wrap;align-items:baseline;font-size:14px}
details.tm-m summary::-webkit-details-marker{display:none}
details.tm-m summary:before{content:"\\25B8";color:var(--ink2)}
details.tm-m[open] summary:before{content:"\\25BE"}
.tm-name{font-weight:600;min-width:130px}
details.tm-m table{margin:8px 0 4px;font-size:13px}
details.tm-raw{margin-top:14px}
details.tm-raw>summary{cursor:pointer;color:var(--ink2);font-size:14px}
.tm-lock{max-width:360px;margin:40px auto;text-align:center}
.tm-lock input{font-size:22px;letter-spacing:8px;text-align:center;width:150px;
  padding:8px;border:1px solid var(--line);border-radius:8px}
.tm-lock button{font-size:15px;padding:8px 22px;margin-left:8px;cursor:pointer;
  border:1px solid var(--line);border-radius:8px;background:none}
"""


_DEPT5 = []


def _tm_dept5(conn):
    """portal_user_id отдела продаж ([5]) — в таймингах показываем только их.
    Заявки, висящие на РОПе/админах (нераспределённые), в эти таблицы
    не попадают. Добавлено 29.08.2026: Светлана с 5 шальными заявками
    вышла в «лучшие» Шага 0."""
    if not _DEPT5:
        _DEPT5.append({r[0] for r in conn.execute(
            "SELECT portal_user_id FROM managers WHERE department = '[5]'")})
    return _DEPT5[0]


def _tm_min_txt(mins):
    if mins is None:
        return "—"
    if mins < 1:
        return "меньше минуты"
    if mins < 120:
        return f"{int(round(mins))} мин"
    if mins < 2880:
        return num(mins / 60, 1) + " ч"
    return num(mins / 1440, 1) + " дн"


def _tm_median(vals):
    vals = sorted(vals)
    if not vals:
        return None
    n = len(vals)
    return (vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2)


def _tm_rows(conn, st, succ, since, endcond="s.stage_id <> %(st)s", won=False,
             until=None):
    until = until or dt.datetime.now(VLD) + dt.timedelta(days=1)
    cols = ("lead_id t0 t_end next_stage next_name title phone uid mname mlast "
            "first_call calls talks").split()
    rows = [dict(zip(cols, r)) for r in conn.execute(
        TIMINGS_SQL.format(endcond=endcond,
                           won=(" AND EXISTS (SELECT 1 FROM stage_history sw WHERE"
                                     " sw.entity_kind='lead' AND sw.owner_id=l.id"
                                     " AND sw.stage_id='13')") if won else ""),
        {"st": st, "since": since, "until": until}).fetchall()]
    rows = [r for r in rows if r["uid"] in _tm_dept5(conn)]
    for r in rows:
        r["first_min"] = ((r["first_call"] - r["t0"]).total_seconds() / 60
                          if r["first_call"] else None)
        r["window_min"] = ((r["t_end"] - r["t0"]).total_seconds() / 60
                           if r["t_end"] else None)
        # окно короче TIMINGS_MIN_WINDOW минут не могло вместить звонок —
        # такие строки видны в развороте, но в средние не входят
        r["valid"] = (r["window_min"] is None
                      or r["window_min"] >= TIMINGS_MIN_WINDOW)
        r["reached"] = r["next_stage"] in succ
        r["mgr"] = human_name(r["mname"], r["mlast"]) or f"id {r['uid']}"
    return rows


def _tm_speed_pts(fmin):
    """Очки за скорость первого звонка — шкала из градиента продаж."""
    if fmin is None:
        return 0.0
    if fmin <= 15:
        return 100.0
    if fmin <= 60:
        return 70.0
    if fmin <= 240:
        return 45.0
    if fmin <= 1440:
        return 30.0
    return 10.0


def _tm_mgr_stats(rr):
    """Показатели менеджера по его строкам шага."""
    vv = [r for r in rr if r["valid"]]
    firsts = [r["first_min"] for r in vv if r["first_min"] is not None]
    return {
        "n": len(rr),
        "valid_n": len(vv),
        "batch": len(rr) - len(vv),
        "med_first": _tm_median(firsts),
        "avg_calls": (sum(r["calls"] for r in vv) / len(vv)) if vv else None,
        "no_call": sum(1 for r in vv
                       if r["first_call"] is None and r["t_end"] is not None),
        "reached": sum(1 for r in rr if r["reached"]),
        "h_step": _tm_median([(r["t_end"] - r["t0"]).total_seconds() / 3600
                              for r in vv if r["reached"] and r["t_end"]]),
        # балл: по закрытым окнам и окнам, где звонок уже был, — свежее
        # окно без звонка ещё не провал и в балл не входит
        "score": (lambda sc: sum(sc) / len(sc) if sc else None)(
            [TIMINGS_W_SPEED * _tm_speed_pts(r["first_min"])
             + TIMINGS_W_CALLS * (100.0 if r["first_call"] else 0.0)
             for r in vv if r["t_end"] is not None or r["first_call"]]),
    }


def _tm_by_mgr(rows):
    by = {}
    for r in rows:
        by.setdefault(r["mgr"], []).append(r)
    return {name: _tm_mgr_stats(rr) for name, rr in by.items()}


def _tm_rank_table(stats, target, etalon=None):
    """Рейтинг: строка на менеджера, лучший подсвечен."""
    def sort_key(kv):
        s = kv[1]
        return (-(s["score"] if s["score"] is not None else -1),
                s["med_first"] if s["med_first"] is not None else 9e9)

    items = sorted(stats.items(), key=sort_key)
    if not items:
        return ('<p class="tm-kv">Пока пусто — в этом окне таких заявок '
                'ещё не было.</p>')
    podium = [n for n, s in items
              if s["valid_n"] >= 3 and s["batch"] * 2 <= s["n"]
              and s["score"]][:6]
    best = podium[0] if podium else None
    head = ("<tr><th>Менеджер</th><th>Балл</th><th>Заявок</th>"
            '<th class="tm-eff2">'
            + (f'<span class="tm-imp i0">{e(etalon)}</span>' if etalon else '')
            + '<span class="tm-imp i1">важность 1</span>'
            'Звонит через</th>'
            '<th class="tm-eff"><span class="tm-imp i2">важность 2</span>'
            'Звонков за стадию</th>'
            "<th>Заявок вообще без звонка</th>"
            "<th>Довёл до этапа</th><th>Шаг занял (медиана)</th>"
            "<th>Задним числом</th></tr>")
    trs = []
    for name, s in items:
        cls = ' class="tm-best-row"' if name == best else ""
        place = podium.index(name) + 1 if name in podium else 0
        badge = {1: '<span class="tm-badge">лучший</span>',
                 2: '<span class="tm-badge2">2 место</span>',
                 3: '<span class="tm-badge3">3 место</span>',
                 4: '<span class="tm-badge4">Застенчивый</span>',
                 5: '<span class="tm-badge4">Продавец-наблюдатель</span>',
                 6: '<span class="tm-badge4">Лидофоб</span>'}.get(place, "")
        nc = (f'<span class="tm-bad">{s["no_call"]}</span>'
              if s["no_call"] else "0")
        pct = round(100 * s["reached"] / s["n"]) if s["n"] else 0
        sc = s["score"]
        sc_cls = ("tm-good" if sc is not None and sc >= 60
                  else "tm-bad" if sc is not None and sc < 30 else "")
        sc_td = (f'<td class="num"><span class="{sc_cls}"><b>'
                 f'{round(sc) if sc is not None else "—"}</b></span></td>')
        pc = ("tm-good" if pct >= 50 else "tm-bad" if pct <= 20 else "")
        trs.append(
            f'<tr{cls}><td class="mname">{e(name)}{badge}</td>'
            + sc_td +
            f'<td class="num">{s["n"]}</td>'
            f'<td class="tm-eff2">{_tm_min_txt(s["med_first"])}</td>'
            f'<td class="num tm-eff">{num(s["avg_calls"], 1) if s["avg_calls"] is not None else "—"}</td>'
            f'<td class="num">{nc}</td>'
            f'<td class="num"><span class="{pc}">{s["reached"]} из {s["n"]} '
            f'({pct}%)</span></td>'
            f'<td>{_tm_min_txt(s["h_step"] * 60) if s["h_step"] is not None else "—"}</td>'
            f'<td class="num">{s["batch"] or "0"}</td>'
            "</tr>")
    return f'<table><thead>{head}</thead><tbody>{"".join(trs)}</tbody></table>'


def _tm_details(rows, now):
    """Разворот: каждая заявка каждого менеджера."""
    if not rows:
        return ""
    by = {}
    for r in rows:
        by.setdefault(r["mgr"], []).append(r)
    blocks = []
    for name, rr in sorted(by.items(), key=lambda kv: -len(kv[1])):
        s = _tm_mgr_stats(rr)
        trs = []
        for r in sorted(rr, key=lambda x: x["t0"], reverse=True):
            title = re.sub(r"\+?\d[\d\s()-]{8,}\d", "", r["title"] or "").strip(" ,·-")
            link = crm_link("LEAD", r["lead_id"])
            client = (f'<a href="{link}" target="_blank" rel="noopener">'
                      f'{e(title) or "лид " + str(r["lead_id"])}</a> '
                      f'<span class="tm-kv">{e(mask(r["phone"]))}</span>')
            if not r["valid"]:
                first = (f'<span class="tm-kv">статус сменился через '
                         f'{max(1, int(r["window_min"]))} мин — не считается</span>')
            elif r["first_call"] is None:
                first = ('<span class="tm-kv">пока нет</span>' if r["t_end"] is None
                         else '<span class="tm-bad">не звонил</span>')
            else:
                first = f'через {_tm_min_txt(r["first_min"])}'
            calls_txt = (f'{r["calls"]}' + (f' <span class="tm-kv">(разг. '
                         f'{r["talks"]})</span>' if r["calls"] else ""))
            if r["t_end"] is not None:
                nxt = (f'{e(r["next_name"] or "?")} '
                       f'<span class="tm-kv">{r["t_end"].astimezone(VLD).strftime("%d.%m")}'
                       f' · в статусе {e(ago(r["t0"], r["t_end"]))}</span>')
            else:
                nxt = f'<span class="tm-kv">ещё там ({e(ago(r["t0"], now))})</span>'
            trs.append(
                f'<tr><td class="tm-kv">{r["t0"].astimezone(VLD).strftime("%d.%m %H:%M")}</td>'
                f'<td>{client}</td><td>{first}</td>'
                f'<td class="num">{calls_txt}</td><td>{nxt}</td></tr>')
        blocks.append(
            f'<details class="tm-m"><summary>'
            f'<span class="tm-name">{e(name)}</span>'
            f'<span class="tm-kv">заявок <b>{s["n"]}</b></span>'
            f'<span class="tm-kv">звонит через <b>{_tm_min_txt(s["med_first"])}'
            f'</b></span>'
            + (f'<span class="tm-bad">без звонка {s["no_call"]}</span>'
               if s["no_call"] else "")
            + '</summary>'
            f'<table><thead><tr><th>Статус поставлен</th><th>Клиент</th>'
            f'<th>Первый звонок</th><th>Звонков</th><th>Дальше</th></tr></thead>'
            f'<tbody>{"".join(trs)}</tbody></table></details>')
    return ('<details class="tm-raw"><summary>Развернуть все заявки '
            'по менеджерам</summary>' + "".join(blocks) + "</details>")




S0_SQL = """
SELECT l.id, l.date_create AS t0, nx.t_end, nx.next_stage, d.name AS next_name,
       l.title, l.phone_e164, l.assigned_by,
       m.name AS mname, m.last_name AS mlast,
       coalesce(t.calls, 0) AS calls, coalesce(t.talks, 0) AS talks,
       coalesce(t.dogovor, 0) AS dogovor, t.fc, t.lc,
       q.bq, q.ta, q.ss
  FROM leads l
  LEFT JOIN managers m ON m.portal_user_id = l.assigned_by
  LEFT JOIN LATERAL (
      SELECT s.created_time AS t_end, s.stage_id AS next_stage
        FROM stage_history s
       WHERE s.entity_kind = 'lead' AND s.owner_id = l.id
         AND s.created_time > l.date_create
         AND (s.stage_id IN ('8', '10', '11', '12', '13')
              OR s.stage_semantic IN ('F', 'S'))
       ORDER BY s.created_time LIMIT 1) nx ON TRUE
  LEFT JOIN crm_dict d ON d.kind = 'STATUS' AND d.status_id = nx.next_stage
  LEFT JOIN LATERAL (
      SELECT min(c.call_start) AS fc, max(c.call_start) AS lc,
             count(*) AS calls,
             count(*) FILTER (WHERE c.duration >= 30) AS talks,
             count(*) FILTER (WHERE c.duration >= 30 AND tr.text ~*
               '(заключ|оформ|подпи[сш]|отправ|вышл|пришл|состав|подготов)[а-яё]*[^.!?]{0,60}договор|договор[а-яё]*[^.!?]{0,60}(заключ|оформ|подпи[сш]|отправ|вышл|пришл|состав|подготов)'
             ) AS dogovor
        FROM calls c
        LEFT JOIN transcripts tr ON tr.call_id = c.id
       WHERE c.direction = 'out' AND c.call_start > l.date_create
         AND c.call_start <= coalesce(nx.t_end, now())
         AND ((c.crm_entity_type = 'LEAD' AND c.crm_entity_id = l.id)
              OR (l.phone_e164 IS NOT NULL
                  AND c.phone_e164 = l.phone_e164))) t ON TRUE
  LEFT JOIN LATERAL (
      SELECT sc.raw->>'budget_q' AS bq,
             (sc.raw->>'timeline_asked')::boolean AS ta,
             (sc.raw->>'safety_shown')::boolean AS ss
        FROM calls c2
        JOIN call_scores sc ON sc.call_id = c2.id
       WHERE c2.direction = 'out' AND c2.call_start > l.date_create
         AND c2.call_start <= coalesce(nx.t_end, now())
         AND c2.duration >= 90 AND sc.raw ? 'budget_q'
         AND ((c2.crm_entity_type = 'LEAD' AND c2.crm_entity_id = l.id)
              OR (l.phone_e164 IS NOT NULL
                  AND c2.phone_e164 = l.phone_e164))
       ORDER BY c2.call_start LIMIT 1) q ON TRUE
 WHERE l.date_create >= %(since)s AND l.date_create < %(until)s
   AND coalesce(l.phone_kind, '') NOT IN ('junk', 'none')
   AND l.source_id IS DISTINCT FROM 'PARTNER'
   AND l.source_id IS DISTINCT FROM 'CALL'
   AND l.status_id IS DISTINCT FROM '31'
 ORDER BY l.date_create DESC
"""


def _tm_qual(r):
    """Метки квалификации первого содержательного разговора."""
    if not r["bq"]:
        return '<span class="tm-kv">—</span>'
    bud = {"manager_asked": ('<span class="tm-good" title="менеджер спросил '
                             'бюджет">Б</span>'),
           "client_stated": ('<span title="бюджет назвал клиент сам">Б</span>'),
           }.get(r["bq"], '<span class="tm-bad" title="бюджет не установлен">Б</span>')
    tl = ('<span class="tm-good" title="спросил срок покупки">С</span>'
          if r["ta"] else '<span class="tm-bad" title="срок не спрашивал">С</span>')
    sf = ('<span class="tm-good" title="предложил трансляцию или видеосвязь">'
          'В</span>' if r["ss"] else
          '<span class="tm-bad" title="безопасность не показал">В</span>')
    return f'{bud} {tl} {sf}'


def _tm_step0_block(conn, now, since, until=None):
    """Шаг 0: заявка → «Паспорт». Балл: 60% звонки + 40% скорость.

    Проверено на 150 днях (9849 заявок со звонком): до паспорта доходит
    2–3% — главный фильтр это качество лида, но настойчивость работает:
    5+ звонков удваивают шанс продвижения (5% против 2% при 1–2), скорость
    чуть слабее (4% в первый час против 2%), интервал между звонками
    с исходом не связан — показывается справочно.
    """
    cols = ("lead_id t0 t_end next_stage next_name title phone uid "
            "mname mlast calls talks dogovor fc lc bq ta ss").split()
    until = until or now + dt.timedelta(days=1)
    rows = [dict(zip(cols, r)) for r in conn.execute(
        S0_SQL, {"since": since, "until": until}).fetchall()]
    rows = [r for r in rows if r["uid"] in _tm_dept5(conn)]
    for r in rows:
        r["first_min"] = ((r["fc"] - r["t0"]).total_seconds() / 60
                          if r["fc"] else None)
        r["gap"] = ((r["lc"] - r["fc"]).total_seconds() / 60 / (r["calls"] - 1)
                    if r["calls"] >= 2 else None)
        r["window_min"] = ((r["t_end"] - r["t0"]).total_seconds() / 60
                           if r["t_end"] else None)
        r["valid"] = (r["window_min"] is None
                      or r["window_min"] >= TIMINGS_MIN_WINDOW)
        r["fwd"] = r["next_stage"] in ("8", "10", "11", "12", "13",
                                       "CONVERTED")
        t0v = r["t0"].astimezone(VLD)
        # скорость первого звонка меряем только по будним заявкам 10–18:
        # ночную и выходную заявку никто не обязан брать мгновенно
        r["day_ok"] = t0v.weekday() < 5 and 10 <= t0v.hour < 18
        r["mgr"] = human_name(r["mname"], r["mlast"]) or f"id {r['uid']}"

    head = ('<h2>Шаг 0 · Заявка → «Паспорт»</h2>'
            '<section class="card">'
            f'<p class="tm-sum">Живые заявки: <b>{len(rows)}</b>. Работа '
            'до первого содержательного статуса: сколько звонков, с каким '
            'интервалом и как быстро — пока заявка не переведена '
            'в «Паспорт» (перескок сразу в договор и дальше тоже считается).</p>')
    if not rows:
        return head + '<p class="tm-kv">Пока пусто.</p></section>'

    by = {}
    for r in rows:
        by.setdefault(r["mgr"], []).append(r)

    def st(rr):
        vv = [r for r in rr if r["valid"]]
        firsts = [r["first_min"] for r in vv
                  if r["first_min"] is not None and r["day_ok"]]
        gaps = [r["gap"] for r in vv if r["gap"] is not None]

        def w_score(r):
            cp = {0: 0, 1: 40, 2: 40, 3: 70, 4: 70}.get(r["calls"], 100)
            if r["day_ok"]:
                return 0.6 * cp + 0.4 * _tm_speed_pts(r["first_min"])
            return cp          # ночь/выходной: скорость не судим
        return {"n": len(rr),
                "valid_n": len(vv),
                "batch": len(rr) - len(vv),
                "med_first": _tm_median(firsts),
                "avg_calls": (sum(r["calls"] for r in vv) / len(vv))
                             if vv else None,
                "med_gap": _tm_median(gaps),
                "no_call": sum(1 for r in vv
                               if not r["calls"] and r["t_end"] is not None),
                "dg_base": sum(1 for r in vv if r["talks"]),
                "dg": sum(1 for r in vv if r["dogovor"]),
                "q_base": sum(1 for r in vv if r["bq"]),
                "q_bud": sum(1 for r in vv if r["bq"] and r["bq"] != "no"),
                "q_ask": sum(1 for r in vv if r["bq"] == "manager_asked"),
                "q_tl": sum(1 for r in vv if r["ta"]),
                "q_sf": sum(1 for r in vv if r["ss"]),
                "fwd": sum(1 for r in rr if r["fwd"]),
                # 60% настойчивость, 40% скорость (только будни 10–18)
                "score": (lambda sc: sum(sc) / len(sc) if sc else None)(
                    [w_score(r) for r in vv
                     if r["t_end"] is not None or r["calls"]])}

    items = sorted(((n, st(rr)) for n, rr in by.items()),
                   key=lambda kv: -(kv[1]["score"]
                                    if kv[1]["score"] is not None else -1))
    podium = [n for n, v in items
              if v["valid_n"] >= 3 and v["batch"] * 2 <= v["n"]
              and v["score"]][:6]
    best = podium[0] if podium else None
    trs = []
    for name, v in items:
        cls = ' class="tm-best-row"' if name == best else ""
        place = podium.index(name) + 1 if name in podium else 0
        badge = {1: '<span class="tm-badge">лучший</span>',
                 2: '<span class="tm-badge2">2 место</span>',
                 3: '<span class="tm-badge3">3 место</span>',
                 4: '<span class="tm-badge4">Застенчивый</span>',
                 5: '<span class="tm-badge4">Продавец-наблюдатель</span>',
                 6: '<span class="tm-badge4">Лидофоб</span>'}.get(place, "")
        sc = v["score"]
        sc_cls = ("tm-good" if sc is not None and sc >= 60
                  else "tm-bad" if sc is not None and sc < 30 else "")
        pct = round(100 * v["fwd"] / v["n"]) if v["n"] else 0
        dgp = (round(100 * v["dg"] / v["dg_base"])
               if v["dg_base"] else None)
        dg_cls = ("tm-good" if dgp is not None and dgp >= 50
                  and v["dg_base"] >= 3
                  else "tm-bad" if dgp is not None and dgp < 25
                  and v["dg_base"] >= 3 else "")
        dg_td = (f'<td class="num"><span class="{dg_cls}">{v["dg"]} из '
                 f'{v["dg_base"]} ({dgp}%)</span></td>'
                 if dgp is not None else '<td class="num">—</td>')

        def qtd(key, good, bad, sub=None):
            if not v["q_base"]:
                return '<td class="num dim">—</td>'
            pv = round(100 * v[key] / v["q_base"])
            cls = ("tm-good" if pv >= good else "tm-bad" if pv < bad else "")
            extra = (f'<span class="sub2">сам {sub}%</span>'
                     if sub is not None else "")
            return (f'<td class="num"><span class="{cls}">{pv}%</span>'
                    f'{extra}</td>')

        ask_pct = (round(100 * v["q_ask"] / v["q_base"])
                   if v["q_base"] else None)
        q_tds = (qtd("q_bud", 70, 40, sub=ask_pct)
                 + qtd("q_tl", 60, 30) + qtd("q_sf", 40, 15))
        trs.append(
            f'<tr{cls}><td class="mname">{e(name)}{badge}</td>'
            f'<td class="num"><span class="{sc_cls}"><b>'
            f'{round(sc) if sc is not None else "—"}</b></span></td>'
            f'<td class="num">{v["n"]}</td>'
            f'<td class="num tm-eff2">{num(v["avg_calls"], 1) if v["avg_calls"] is not None else "—"}</td>'
            f'<td class="tm-eff">{_tm_min_txt(v["med_first"])}</td>'
            f'<td>{_tm_min_txt(v["med_gap"])}</td>'
            + dg_td + q_tds
            + (f'<td class="num"><span class="tm-bad">{v["no_call"]}</span></td>'
               if v["no_call"] else '<td class="num">0</td>')
            + f'<td class="num">{v["fwd"]} из {v["n"]} ({pct}%)</td>'
            f'<td class="num">{v["batch"] or "0"}</td></tr>')

    det = []
    for name, rr in sorted(by.items(), key=lambda kv: -len(kv[1])):
        v = st(rr)
        lead_trs = []
        for r in sorted(rr, key=lambda x: x["t0"], reverse=True)[:80]:
            title = re.sub(r"\+?\d[\d\s()-]{8,}\d", "",
                           r["title"] or "").strip(" ,\u00b7-")
            link = crm_link("LEAD", r["lead_id"])
            client = (f'<a href="{link}" target="_blank" rel="noopener">'
                      f'{e(title) or "лид " + str(r["lead_id"])}</a> '
                      f'<span class="tm-kv">{e(mask(r["phone"]))}</span>')
            if not r["valid"]:
                first = (f'<span class="tm-kv">статус сменился через '
                         f'{max(1, int(r["window_min"]))} мин — '
                         'не считается</span>')
            elif not r["calls"]:
                first = ('<span class="tm-kv">пока нет</span>'
                         if r["t_end"] is None
                         else '<span class="tm-bad">не звонил</span>')
            else:
                first = f'через {_tm_min_txt(r["first_min"])}'
            if r["t_end"] is not None:
                nxt = (f'{e(r["next_name"] or "?")} '
                       f'<span class="tm-kv">{r["t_end"].astimezone(VLD).strftime("%d.%m")}</span>')
            else:
                nxt = f'<span class="tm-kv">в работе ({e(ago(r["t0"], now))})</span>'
            dg = ('<span class="tm-good">предлагал</span>' if r["dogovor"]
                  else '<span class="tm-bad">нет</span>' if r["talks"]
                  else '<span class="tm-kv">—</span>')
            lead_trs.append(
                f'<tr><td class="tm-kv">{r["t0"].astimezone(VLD).strftime("%d.%m %H:%M")}</td>'
                f'<td>{client}</td><td>{first}</td>'
                f'<td class="num">{r["calls"]}'
                + (f' <span class="tm-kv">(разг. {r["talks"]})</span>'
                   if r["calls"] else "")
                + f'</td><td>{dg}</td><td>{_tm_qual(r)}</td>'
                + f'<td>{_tm_min_txt(r["gap"])}</td><td>{nxt}</td></tr>')
        more = ('' if len(rr) <= 80 else
                f'<p class="tm-kv">Показаны последние 80 из {len(rr)}.</p>')
        det.append(
            f'<details class="tm-m"><summary>'
            f'<span class="tm-name">{e(name)}</span>'
            f'<span class="tm-kv">заявок <b>{v["n"]}</b></span>'
            f'<span class="tm-kv">звонков ср. <b>{num(v["avg_calls"], 1) if v["avg_calls"] is not None else "—"}</b></span>'
            + (f'<span class="tm-bad">без звонка {v["no_call"]}</span>'
               if v["no_call"] else "")
            + '</summary>'
            '<table><thead><tr><th>Заявка</th><th>Клиент</th>'
            '<th>Первый звонок</th><th>Звонков</th><th>Договор</th>'
            '<th>Квалификация</th><th>Интервал ср.</th>'
            '<th>Дальше</th></tr></thead>'
            f'<tbody>{"".join(lead_trs)}</tbody></table>' + more
            + '</details>')

    return (
        head
        + '<table><thead><tr><th>Менеджер</th><th>Балл</th><th>Заявок</th>'
        '<th class="tm-eff2"><span class="tm-imp i1">важность 1</span>'
        'Звонков в среднем</th>'
        '<th class="tm-eff"><span class="tm-imp i2">важность 2</span>'
        'Первый звонок (медиана)</th>'
        '<th>Интервал между звонками (медиана)</th>'
        '<th>Предлагал договор</th>'
        '<th>Бюджет</th><th>Сроки</th><th>Безопасность</th>'
        '<th>Без звонка вообще</th><th>Довёл до этапа</th>'
        '<th>Задним числом</th></tr></thead><tbody>'
        + "".join(trs) + "</tbody></table>"
        '<p class="tm-note">Окно — от создания заявки до «Паспорт» '
        '(перескок дальше по воронке считается) либо до закрытия заявки; '
        'исключены непригодные номера, PARTNER, источник «Звонок» и спам. '
        'До паспорта доходит лишь 2–3% заявок — главный фильтр это качество '
        'лида, поэтому балл меряет усилие: <b>60% звонки</b> (5+ звонков '
        'удваивают шанс продвижения), <b>40% скорость</b> первого звонка — '
        'скорость считается только по заявкам будних дней 10:00–18:00 '
        '(Влд), ночные и выходные в неё не входят. '
        'Интервал между звонками показан справочно — с исходом на этом '
        'участке он не связан. <b>«Предлагал договор»</b> — по расшифровкам '
        'разговоров от 30 секунд: считается заявка, где менеджер хотя бы раз '
        'предложил заключить, оформить, подготовить или выслать договор '
        '(поиск по фразам в тексте). Зелёное — от 50%, красное — меньше 25%. '
        'В балл пока не входит. <b>«Бюджет» / «Сроки» / «Безопасность»</b> — '
        'по первому содержательному разговору (от 90 секунд) в этом же окне, '
        'разбором расшифровки. Бюджет: менеджер спросил сам или клиент назвал '
        'свою денежную рамку (цена машины с сайта и суммы менеджера не в счёт; '
        'мелким шрифтом — доля, где спросил сам менеджер). Сроки: менеджер '
        'спросил, когда планируется покупка. Безопасность: менеджер предложил '
        'онлайн-трансляцию офиса или созвон по видеосвязи (видеообзор машины '
        'не считается). Считается только по разговорам с 01.09.2026, задним '
        'числом не пересчитывалось — колонки наполняются постепенно. '
        'В балл не входят.</p>'
        + '<details class="tm-raw"><summary>Развернуть все заявки '
        'по менеджерам</summary>' + "".join(det) + "</details></section>")


S4_SQL = """
SELECT l.id, ev.t0, nx.t_end, nx.next_stage, d.name AS next_name,
       l.title, l.phone_e164, l.assigned_by,
       m.name AS mname, m.last_name AS mlast,
       t.calls_ts, coalesce(t.talks, 0) AS talks
  FROM (SELECT owner_id, min(created_time) AS t0
          FROM stage_history
         WHERE entity_kind = 'lead' AND stage_id = '12'
           AND created_time >= %(since)s AND created_time < %(until)s
         GROUP BY owner_id) ev
  JOIN leads l ON l.id = ev.owner_id
  LEFT JOIN LATERAL (
      SELECT s.created_time AS t_end, s.stage_id AS next_stage
        FROM stage_history s
       WHERE s.entity_kind = 'lead' AND s.owner_id = l.id
         AND s.created_time > ev.t0
         AND (s.stage_id = '13' OR s.stage_semantic IN ('F', 'S'))
       ORDER BY s.created_time LIMIT 1) nx ON TRUE
  LEFT JOIN crm_dict d ON d.kind = 'STATUS' AND d.status_id = nx.next_stage
  LEFT JOIN managers m ON m.portal_user_id = l.assigned_by
  LEFT JOIN LATERAL (
      SELECT array_agg(c.call_start ORDER BY c.call_start) AS calls_ts,
             count(*) FILTER (WHERE c.duration >= 30) AS talks
        FROM calls c
       WHERE c.direction = 'out' AND c.call_start > ev.t0
         AND c.call_start <= coalesce(nx.t_end, now())
         AND ((c.crm_entity_type = 'LEAD' AND c.crm_entity_id = l.id)
              OR (l.phone_e164 IS NOT NULL
                  AND c.phone_e164 = l.phone_e164))) t ON TRUE
 ORDER BY ev.t0 DESC
"""


def _tm_step4_block(conn, now, since, until=None):
    """Шаг 4: «На торгах» → «Успешная сделка». Звонки и их интервалы.

    Рейтинг сознательно не назначается — решение Тимофей примет сам,
    посмотрев на цифры (28.08.2026).
    """
    cols = ("lead_id t0 t_end next_stage next_name title phone uid "
            "mname mlast calls_ts talks").split()
    until = until or now + dt.timedelta(days=1)
    rows = [dict(zip(cols, r)) for r in conn.execute(
        S4_SQL, {"since": since, "until": until}).fetchall()]
    rows = [r for r in rows if r["uid"] in _tm_dept5(conn)]
    for r in rows:
        ts = r["calls_ts"] or []
        r["calls"] = len(ts)
        r["first_min"] = ((ts[0] - r["t0"]).total_seconds() / 60
                          if ts else None)
        gaps = [(b - a).total_seconds() / 60 for a, b in zip(ts, ts[1:])]
        r["gap"] = sum(gaps) / len(gaps) if gaps else None
        r["window_min"] = ((r["t_end"] - r["t0"]).total_seconds() / 60
                           if r["t_end"] else None)
        r["valid"] = (r["window_min"] is None
                      or r["window_min"] >= TIMINGS_MIN_WINDOW)
        r["won"] = r["next_stage"] == "13"
        r["mgr"] = human_name(r["mname"], r["mlast"]) or f"id {r['uid']}"

    head = ('<h2>Шаг 4 · «На торгах» → «Успешная сделка»</h2>'
            '<section class="card">'
            f'<p class="tm-sum">Заявки, вышедшие «На торги»: '
            f'<b>{len(rows)}</b>. Как менеджер ведёт клиента после торгов: '
            'когда звонит первый раз, сколько звонков делает и с каким '
            'интервалом.</p>')
    if not rows:
        return head + '<p class="tm-kv">Пока пусто.</p></section>'

    by = {}
    for r in rows:
        by.setdefault(r["mgr"], []).append(r)

    def st(rr):
        vv = [r for r in rr if r["valid"]]
        firsts = [r["first_min"] for r in vv if r["first_min"] is not None]
        gaps = [r["gap"] for r in vv if r["gap"] is not None]
        return {"n": len(rr),
                "batch": len(rr) - len(vv),
                "med_first": _tm_median(firsts),
                "avg_calls": (sum(r["calls"] for r in vv) / len(vv))
                             if vv else None,
                "med_gap": _tm_median(gaps),
                "no_call": sum(1 for r in vv
                               if not r["calls"] and r["t_end"] is not None),
                "won": sum(1 for r in rr if r["won"]),
                "valid_n": len(vv),
                # балл шага 4: 70% звонки (шкала по Жернову: 0/1/2/3-4/5+ →
                # 0/40/60/80/100), 30% скорость. Гипотеза Тимофея 28.08,
                # подтверждена данными: предоплата внесена, скорость первого
                # звонка с продажей не коррелирует (corr −0.10), а наличие
                # сопровождающих звонков даёт 94% доходимости против 82%.
                "score": (lambda sc: sum(sc) / len(sc) if sc else None)(
                    [0.3 * _tm_speed_pts(r["first_min"])
                     + 0.7 * {0: 0, 1: 40, 2: 60, 3: 80, 4: 80}.get(
                           r["calls"], 100)
                     for r in vv
                     if r["t_end"] is not None or r["calls"]])}

    items = sorted(((n, st(rr)) for n, rr in by.items()),
                   key=lambda kv: -(kv[1]["score"]
                                    if kv[1]["score"] is not None else -1))
    podium = [n for n, v in items
              if v["valid_n"] >= 3 and v["batch"] * 2 <= v["n"]
              and v["score"]][:6]
    best = podium[0] if podium else None
    trs = []
    for name, v in items:
        cls = ' class="tm-best-row"' if name == best else ""
        place = podium.index(name) + 1 if name in podium else 0
        badge = {1: '<span class="tm-badge">лучший</span>',
                 2: '<span class="tm-badge2">2 место</span>',
                 3: '<span class="tm-badge3">3 место</span>',
                 4: '<span class="tm-badge4">Застенчивый</span>',
                 5: '<span class="tm-badge4">Продавец-наблюдатель</span>',
                 6: '<span class="tm-badge4">Лидофоб</span>'}.get(place, "")
        sc = v["score"]
        sc_cls = ("tm-good" if sc is not None and sc >= 60
                  else "tm-bad" if sc is not None and sc < 30 else "")
        trs.append(
            f'<tr{cls}><td class="mname">{e(name)}{badge}</td>'
            f'<td class="num"><span class="{sc_cls}"><b>'
            f'{round(sc) if sc is not None else "—"}</b></span></td>'
            f'<td class="num">{v["n"]}</td>'
            f'<td>{_tm_min_txt(v["med_first"])}</td>'
            f'<td class="num tm-eff2">{num(v["avg_calls"], 1) if v["avg_calls"] is not None else "—"}</td>'
            f'<td class="tm-eff">{_tm_min_txt(v["med_gap"])}</td>'
            + (f'<td class="num"><span class="tm-bad">{v["no_call"]}</span></td>'
               if v["no_call"] else '<td class="num">0</td>')
            + f'<td class="num">{v["won"]} из {v["n"]}</td>'
            f'<td class="num">{v["batch"] or "0"}</td></tr>')

    det = []
    for name, rr in sorted(by.items(), key=lambda kv: -len(kv[1])):
        v = st(rr)
        lead_trs = []
        for r in sorted(rr, key=lambda x: x["t0"], reverse=True):
            title = re.sub(r"\+?\d[\d\s()-]{8,}\d", "",
                           r["title"] or "").strip(" ,\u00b7-")
            link = crm_link("LEAD", r["lead_id"])
            client = (f'<a href="{link}" target="_blank" rel="noopener">'
                      f'{e(title) or "лид " + str(r["lead_id"])}</a> '
                      f'<span class="tm-kv">{e(mask(r["phone"]))}</span>')
            if not r["valid"]:
                first = (f'<span class="tm-kv">статус сменился через '
                         f'{max(1, int(r["window_min"]))} мин — '
                         'не считается</span>')
            elif not r["calls"]:
                first = ('<span class="tm-kv">пока нет</span>'
                         if r["t_end"] is None
                         else '<span class="tm-bad">не звонил</span>')
            else:
                first = f'через {_tm_min_txt(r["first_min"])}'
            if r["t_end"] is not None:
                nxt = (f'{e(r["next_name"] or "?")} '
                       f'<span class="tm-kv">{r["t_end"].astimezone(VLD).strftime("%d.%m")}'
                       f' · на торгах {e(ago(r["t0"], r["t_end"]))}</span>')
            else:
                nxt = f'<span class="tm-kv">ещё там ({e(ago(r["t0"], now))})</span>'
            lead_trs.append(
                f'<tr><td class="tm-kv">{r["t0"].astimezone(VLD).strftime("%d.%m %H:%M")}</td>'
                f'<td>{client}</td><td>{first}</td>'
                f'<td class="num">{r["calls"]}'
                + (f' <span class="tm-kv">(разг. {r["talks"]})</span>'
                   if r["calls"] else "")
                + f'</td><td>{_tm_min_txt(r["gap"])}</td><td>{nxt}</td></tr>')
        det.append(
            f'<details class="tm-m"><summary>'
            f'<span class="tm-name">{e(name)}</span>'
            f'<span class="tm-kv">заявок <b>{v["n"]}</b></span>'
            f'<span class="tm-kv">первый звонок <b>{_tm_min_txt(v["med_first"])}'
            f'</b></span>'
            + (f'<span class="tm-bad">без звонка {v["no_call"]}</span>'
               if v["no_call"] else "")
            + '</summary>'
            '<table><thead><tr><th>На торгах с</th><th>Клиент</th>'
            '<th>Первый звонок</th><th>Звонков</th><th>Интервал ср.</th>'
            '<th>Дальше</th></tr></thead>'
            f'<tbody>{"".join(lead_trs)}</tbody></table></details>')

    return (
        head
        + '<table><thead><tr><th>Менеджер</th><th>Балл</th><th>Заявок</th>'
        '<th>Первый звонок (медиана)</th>'
        '<th class="tm-eff2"><span class="tm-imp i1">важность 1</span>'
        'Звонков в среднем</th>'
        '<th class="tm-eff"><span class="tm-imp i2">важность 2</span>'
        'Интервал между звонками (медиана)</th>'
        '<th>Без звонка вообще</th><th>Дошло до сделки</th>'
        '<th>Задним числом</th></tr></thead><tbody>'
        + "".join(trs) + "</tbody></table>"
        '<p class="tm-note">Окно — от выхода на торги до «ТС куплен» / '
        '«Успешная сделка» либо до закрытия заявки; если заявка ещё на '
        'торгах — до сегодня. «Интервал между звонками» — средняя пауза '
        'между соседними звонками по заявке (у кого меньше двух звонков — '
        'интервала нет), по менеджеру — медиана. Окна короче '
        f'{TIMINGS_MIN_WINDOW} минут («задним числом») в средние не входят. '
        'Балл этой стадии — наоборот к шагам 1–3: <b>70% звонки, 30% '
        'скорость</b> (ориентир — Жернов, он продаёт больше всех: ~5 '
        'звонков с интервалом ~4 часа). Данные подтверждают: после '
        'предоплаты скорость первого звонка с продажей не коррелирует, '
        'а сопровождение звонками даёт 94% доходимости против 82% '
        'у заявок без единого звонка. Зачётные столбцы подсвечены.</p>'
        + '<details class="tm-raw"><summary>Развернуть все заявки '
        'по менеджерам</summary>' + "".join(det) + "</details></section>")


def _tm_torgi_block(conn, now, since, label, until=None):
    """Срез: сколько исходящих звонков ушло на заявку от создания до торгов."""
    cols = "lead_id t0 t12 title phone uid mname mlast calls talks".split()
    until = until or now + dt.timedelta(days=1)
    rows = [dict(zip(cols, r)) for r in conn.execute(
        TORGI_SQL, {"since": since, "until": until}).fetchall()]
    rows = [r for r in rows if r["uid"] in _tm_dept5(conn)]
    for r in rows:
        r["days"] = (r["t12"] - r["t0"]).total_seconds() / 86400
        r["mgr"] = human_name(r["mname"], r["mlast"]) or f"id {r['uid']}"

    head = ('<h2>Сколько звонков — до торгов</h2><section class="card">'
            f'<p class="tm-sum">Заявки, вышедшие «На торги» {label}: '
            f'<b>{len(rows)}</b>. Сколько исходящих звонков менеджер '
            'сделал по каждой — от создания заявки до выхода на торги.</p>')
    if not rows:
        return head + ('<p class="tm-kv">Пока пусто — в этом окне на торги '
                       'ещё никто не вышел.</p></section>')

    by = {}
    for r in rows:
        by.setdefault(r["mgr"], []).append(r)

    def st(rr):
        return {"n": len(rr),
                "med_calls": _tm_median([r["calls"] for r in rr]),
                "avg_calls": sum(r["calls"] for r in rr) / len(rr),
                "med_talks": _tm_median([r["talks"] for r in rr]),
                "med_days": _tm_median([r["days"] for r in rr])}

    items = sorted(((n, st(rr)) for n, rr in by.items()),
                   key=lambda kv: (kv[1]["med_days"] if kv[1]["med_days"]
                                   is not None else 9e9))
    # места — самый быстрый медианный путь до торгов при видимых звонках
    podium = [n for n, v in items
              if v["n"] >= 3 and v["med_days"] is not None
              and (v["med_calls"] or 0) > 0][:6]
    best = podium[0] if podium else None
    trs = []
    for name, v in items:
        zero = ' <span class="tm-bad">(звонков не видно)</span>' \
            if v["med_calls"] == 0 else ""
        cls = ' class="tm-best-row"' if name == best else ""
        place = podium.index(name) + 1 if name in podium else 0
        badge = {1: '<span class="tm-badge">лучший</span>',
                 2: '<span class="tm-badge2">2 место</span>',
                 3: '<span class="tm-badge3">3 место</span>',
                 4: '<span class="tm-badge4">Застенчивый</span>',
                 5: '<span class="tm-badge4">Продавец-наблюдатель</span>',
                 6: '<span class="tm-badge4">Лидофоб</span>'}.get(place, "")
        trs.append(
            f'<tr{cls}><td class="mname">{e(name)}{badge}</td>'
            f'<td class="num">{v["n"]}</td>'
            f'<td class="num">{num(v["med_calls"], 1)}{zero}</td>'
            f'<td class="num">{num(v["avg_calls"], 1)}</td>'
            f'<td class="num">{num(v["med_talks"], 1)}</td>'
            f'<td>{num(v["med_days"], 1)} дн</td></tr>')

    det = []
    for name, rr in sorted(by.items(), key=lambda kv: -len(kv[1])):
        v = st(rr)
        lead_trs = []
        for r in sorted(rr, key=lambda x: x["t12"], reverse=True):
            title = re.sub(r"\+?\d[\d\s()-]{8,}\d", "",
                           r["title"] or "").strip(" ,·-")
            link = crm_link("LEAD", r["lead_id"])
            client = (f'<a href="{link}" target="_blank" rel="noopener">'
                      f'{e(title) or "лид " + str(r["lead_id"])}</a> '
                      f'<span class="tm-kv">{e(mask(r["phone"]))}</span>')
            lead_trs.append(
                f'<tr><td class="tm-kv">{r["t0"].astimezone(VLD).strftime("%d.%m")}</td>'
                f'<td>{client}</td>'
                f'<td class="num">{r["calls"]}'
                + (f' <span class="tm-kv">(разг. {r["talks"]})</span>'
                   if r["calls"] else "")
                + f'</td><td>{num(r["days"], 1)} дн</td>'
                f'<td class="tm-kv">{r["t12"].astimezone(VLD).strftime("%d.%m")}</td></tr>')
        det.append(
            f'<details class="tm-m"><summary>'
            f'<span class="tm-name">{e(name)}</span>'
            f'<span class="tm-kv">на торгах <b>{v["n"]}</b></span>'
            f'<span class="tm-kv">звонков <b>{num(v["med_calls"], 1)}</b></span>'
            f'<span class="tm-kv">путь <b>{num(v["med_days"], 1)} дн</b></span>'
            '</summary>'
            '<table><thead><tr><th>Заявка</th><th>Клиент</th><th>Звонков</th>'
            '<th>Путь до торгов</th><th>На торгах с</th></tr></thead>'
            f'<tbody>{"".join(lead_trs)}</tbody></table></details>')

    return (
        head
        + '<table><thead><tr><th>Менеджер</th><th>Заявок на торгах</th>'
        '<th>Звонков на заявку (медиана)</th><th>В среднем</th>'
        '<th>Из них разговоров (медиана)</th><th>Путь до торгов (медиана)</th>'
        '</tr></thead><tbody>' + "".join(trs) + "</tbody></table>"
        '<p class="tm-note">Звонки — исходящие по номеру клиента или '
        'привязанные к заявке, от создания заявки до первого попадания '
        'в «На торгах». «Разговор» — от 30 секунд. «Звонков не видно» — '
        'по заявкам менеджера в базе телефонии нет исходящих: либо звонит '
        'мимо CRM (с личного без привязки), либо ведёт клиента в переписке — '
        'стоит спросить его самого. Места — по самой быстрой медиане пути '
        'до торгов (от 3 заявок, при видимых звонках).</p>'
        + '<details class="tm-raw"><summary>Развернуть все заявки '
        'по менеджерам</summary>' + "".join(det) + "</details></section>")


def _tm_best_card(rows, target, label):
    """«Как надо»: образец по дисциплине телефона на окне договор → торги.

    Образец выбираем по скорости реального первого звонка среди менеджеров,
    которые ведут статусы в реальном времени (задним числом — не больше
    четверти заявок): долю доведённых между менеджерами сравнивать нельзя,
    у них разные привычки работы со статусами.
    """
    stats = _tm_by_mgr(rows)
    cand = [(n, s) for n, s in stats.items()
            if s["valid_n"] >= 8 and s["batch"] * 2 <= s["n"]
            and s["med_first"] is not None]
    if not cand:
        return ""
    cand2 = [(n, s2) for n, s2 in cand if s2["score"] is not None]
    name, s = max(cand2 or cand, key=lambda kv: kv[1].get("score") or 0)
    pct = round(100 * s["reached"] / s["n"])

    vv = [r for r in rows if r["valid"]]
    dept_first = _tm_median([r["first_min"] for r in vv
                             if r["first_min"] is not None])
    dept_closed = [r for r in vv if r["t_end"] is not None]
    dept_ncall = sum(1 for r in dept_closed if r["first_call"] is None)

    return (
        '<h2>Как надо: образец лучшего</h2><section class="card">'
        f'<p class="tm-sum">Образец {label} — <b>{e(name)}</b> '
        '(критерий — балл за телефон, окно от «Договор» до торгов '
        'либо покупки): после смены статуса он набирает клиента через '
        f'<b>{_tm_min_txt(s["med_first"])}</b> (медиана), держит в среднем '
        f'{num(s["avg_calls"], 1)} звонка на заявку и доводит {e(target)} '
        f'<b>{s["reached"]} из {s["n"]}</b> ({pct}%)'
        + (f' — типично за {_tm_min_txt(s["h_step"] * 60)}'
           if s["h_step"] is not None else "") + ".</p>"
        f'<p class="tm-sum">Для сравнения, по отделу в целом первый звонок '
        f'после «Договор» — медиана <b>{_tm_min_txt(dept_first)}</b>, '
        f'а <span class="tm-bad">{dept_ncall} из {len(dept_closed)}</span> '
        'заявок, прошедших этот участок, не получили ни одного звонка.</p>'
        '<ol class="tm-plan">'
        "<li><b>Сменил статус — сразу набери клиента.</b> Короткий звонок "
        "в первые 15 минут: «договор отправил, посмотрите, что подписываем "
        "и когда». Образец звонит, пока клиент ещё в диалоге.</li>"
        "<li><b>Не дозвонился — повторяй в тот же день.</b> Отправленный "
        "договор или счёт без разговора — письмо в пустоту.</li>"
        "<li><b>Веди до денег, а не до отправки.</b> Договор → предоплата → "
        "торги: смена статуса — не конец задачи. У образца участок "
        "закрывается за часы, у остальных заявка висит днями и остывает.</li>"
        "<li><b>Ставь статусы в момент события, а не пачкой в конце.</b> "
        "Иначе ни этот отчёт, ни РОП не видят, где заявка застряла.</li>"
        "</ol></section>")



# ── от заявки до первого звонка, рабочее время 9–18 Влд ──────────────────────
TIMINGS_WORK_FROM = 9         # рабочее окно для счёта времени реакции
TIMINGS_WORK_TO = 18          # (по Владивостоку, все дни недели)

FT_SQL = """
SELECT l.id, l.date_create, l.title, l.phone_e164, l.assigned_by,
       m.name AS mname, m.last_name AS mlast, t.fc
  FROM leads l
  LEFT JOIN managers m ON m.portal_user_id = l.assigned_by
  LEFT JOIN LATERAL (
      SELECT min(c.call_start) AS fc
        FROM calls c
       WHERE c.direction = 'out'
         AND c.call_start > l.date_create - interval '5 minutes'
         AND ((c.crm_entity_type = 'LEAD' AND c.crm_entity_id = l.id)
              OR (l.phone_e164 IS NOT NULL
                  AND c.phone_e164 = l.phone_e164))) t ON TRUE
 WHERE l.date_create >= %(since)s AND l.date_create < %(until)s
   AND (l.date_create AT TIME ZONE 'Asia/Vladivostok')::time >= '09:00'
   AND (l.date_create AT TIME ZONE 'Asia/Vladivostok')::time < '18:00'
   AND coalesce(l.phone_kind, '') NOT IN ('junk', 'none')
   AND l.source_id IS DISTINCT FROM 'PARTNER'
   AND l.source_id IS DISTINCT FROM 'CALL'
   AND l.status_id IS DISTINCT FROM '31'
 ORDER BY l.date_create DESC
"""


def _tm_workmin(a, b, w_from=TIMINGS_WORK_FROM, w_to=TIMINGS_WORK_TO):
    """Рабочие минуты между двумя моментами: окно 9–18 Влд, все дни
    (Instruction для новой заявки передаёт своё окно 10–18)."""
    a, b = a.astimezone(VLD), b.astimezone(VLD)
    if b <= a:
        return 0.0
    total, day, guard = 0.0, a.date(), 0
    while day <= b.date() and guard < 400:
        ws = dt.datetime.combine(day, dt.time(w_from, 0), VLD)
        we = dt.datetime.combine(day, dt.time(w_to, 0), VLD)
        lo, hi = max(a, ws), min(b, we)
        if hi > lo:
            total += (hi - lo).total_seconds() / 60
        day += dt.timedelta(days=1)
        guard += 1
    return total


def _tm_first_touch_block(conn, now, since, label, until=None):
    """Срез: от создания заявки до первого исходящего, рабочее время."""
    cols = "lead_id t0 title phone uid mname mlast fc".split()
    until = until or now + dt.timedelta(days=1)
    rows = [dict(zip(cols, r)) for r in conn.execute(
        FT_SQL, {"since": since, "until": until}).fetchall()]
    rows = [r for r in rows if r["uid"] in _tm_dept5(conn)]
    for r in rows:
        r["wmin"] = _tm_workmin(r["t0"], r["fc"]) if r["fc"] else None
        r["mgr"] = human_name(r["mname"], r["mlast"]) or f"id {r['uid']}"

    head = ('<h2>От заявки до первого звонка</h2><section class="card">'
            f'<p class="tm-sum">Живые заявки, пришедшие в рабочее время '
            f'({TIMINGS_WORK_FROM}:00–{TIMINGS_WORK_TO}:00 Влд) {label}: '
            f'<b>{len(rows)}</b>. '
            'Сколько <b>рабочего</b> времени прошло от создания заявки до '
            'первого исходящего звонка клиенту: часы тикают только внутри '
            'рабочего окна — заявка в 17:50, взятая утром в 9:05, это '
            '15 минут, а не ночь. Ночные и вечерние заявки сюда не входят '
            'вовсе.</p>')
    if not rows:
        return head + '<p class="tm-kv">Пока пусто.</p></section>'

    by = {}
    for r in rows:
        by.setdefault(r["mgr"], []).append(r)

    def st(rr):
        ww = [r["wmin"] for r in rr if r["wmin"] is not None]
        return {"n": len(rr), "called": len(ww),
                "med": _tm_median(ww),
                "avg": sum(ww) / len(ww) if ww else None,
                "fast": sum(1 for w in ww if w <= 15),
                "no_call": len(rr) - len(ww)}

    items = sorted(((n, st(rr)) for n, rr in by.items()),
                   key=lambda kv: (kv[1]["med"] if kv[1]["med"] is not None
                                   else 9e9))
    # места — по самой быстрой медиане при вменяемой выборке
    podium = [n for n, v in items
              if v["called"] >= 3 and v["med"] is not None][:6]
    best = podium[0] if podium else None
    trs = []
    for name, v in items:
        fast_pct = round(100 * v["fast"] / v["called"]) if v["called"] else 0
        cls = ' class="tm-best-row"' if name == best else ""
        place = podium.index(name) + 1 if name in podium else 0
        badge = {1: '<span class="tm-badge">лучший</span>',
                 2: '<span class="tm-badge2">2 место</span>',
                 3: '<span class="tm-badge3">3 место</span>',
                 4: '<span class="tm-badge4">Застенчивый</span>',
                 5: '<span class="tm-badge4">Продавец-наблюдатель</span>',
                 6: '<span class="tm-badge4">Лидофоб</span>'}.get(place, "")
        trs.append(
            f'<tr{cls}><td class="mname">{e(name)}{badge}</td>'
            f'<td class="num">{v["n"]}</td>'
            f'<td>{_tm_min_txt(v["med"])}</td>'
            f'<td>{_tm_min_txt(v["avg"])}</td>'
            f'<td class="num">{v["fast"]} ({fast_pct}%)</td>'
            + (f'<td class="num"><span class="tm-bad">{v["no_call"]}</span></td>'
               if v["no_call"] else '<td class="num">0</td>')
            + "</tr>")

    det = []
    for name, rr in sorted(by.items(), key=lambda kv: -len(kv[1])):
        v = st(rr)
        lead_trs = []
        for r in sorted(rr, key=lambda x: x["t0"], reverse=True)[:80]:
            title = re.sub(r"\+?\d[\d\s()-]{8,}\d", "",
                           r["title"] or "").strip(" ,\u00b7-")
            link = crm_link("LEAD", r["lead_id"])
            client = (f'<a href="{link}" target="_blank" rel="noopener">'
                      f'{e(title) or "лид " + str(r["lead_id"])}</a> '
                      f'<span class="tm-kv">{e(mask(r["phone"]))}</span>')
            if r["wmin"] is None:
                w = '<span class="tm-bad">не звонил</span>'
            else:
                w = f'через {_tm_min_txt(r["wmin"])}'
            lead_trs.append(
                f'<tr><td class="tm-kv">{r["t0"].astimezone(VLD).strftime("%d.%m %H:%M")}</td>'
                f'<td>{client}</td><td>{w}</td></tr>')
        more = ('' if len(rr) <= 80 else
                f'<p class="tm-kv">Показаны последние 80 из {len(rr)}.</p>')
        det.append(
            f'<details class="tm-m"><summary>'
            f'<span class="tm-name">{e(name)}</span>'
            f'<span class="tm-kv">заявок <b>{v["n"]}</b></span>'
            f'<span class="tm-kv">медиана <b>{_tm_min_txt(v["med"])}</b></span>'
            + (f'<span class="tm-bad">без звонка {v["no_call"]}</span>'
               if v["no_call"] else "")
            + '</summary>'
            '<table><thead><tr><th>Заявка</th><th>Клиент</th>'
            '<th>Первый звонок (раб. время)</th></tr></thead>'
            f'<tbody>{"".join(lead_trs)}</tbody></table>' + more
            + '</details>')

    return (
        head
        + '<table><thead><tr><th>Менеджер</th><th>Заявок</th>'
        '<th>Первый звонок, медиана</th><th>Среднее</th>'
        '<th>За 15 раб. минут</th><th>Без звонка вообще</th>'
        '</tr></thead><tbody>' + "".join(trs) + "</tbody></table>"
        '<p class="tm-note">Считаются заявки с пригодным номером; исключены '
        'PARTNER, источник «Звонок» (клиент сам дозвонился — это уже контакт) '
        'и спам. Менеджер — текущий ответственный. Звонок вне рабочего окна '
        'даёт ноль минут задержки. «Без звонка вообще» — исходящих нет '
        'до сих пор. «Лучший» — самая быстрая медиана первого звонка '
        '(при выборке от 3 заявок со звонком).</p>'
        + '<details class="tm-raw"><summary>Развернуть все заявки '
        'по менеджерам</summary>' + "".join(det) + "</details></section>")



# ── вкладка РОПа «Visual» (под пин-кодом) ────────────────────────────────────
# Гонка менеджеров по неделям (решение Тимофея 29.08.2026): баллы недели +
# серый пунктир «эталон» и график «% от личного эталона» (см. visual_daily.py,
# крон 23:55 Влд). Старт 15.07.2026, прошлое досчитано visual_backfill.py.
VISUAL_START = dt.date(2026, 7, 15)  # решение Тимофея 29.08: старт задним числом
VISUAL_COLORS = ["#1565c0", "#2e7d32", "#c62828", "#e0a800",
                 "#6a1b9a", "#00838f", "#5d4037"]

VISUAL_CSS = """
.vz-chip{display:inline-block;width:12px;height:12px;border-radius:3px;
  margin-right:8px;vertical-align:-1px}
.vz-warn{color:var(--warn);font-weight:600}
.vz-panels{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
  gap:18px 22px}
@media(max-width:1100px){.vz-panels{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:700px){.vz-panels{grid-template-columns:1fr}}
.vz-fact{background:rgba(21,101,192,.06)}
.vz-row{cursor:pointer}
.vz-row:hover{background:var(--bg)}
.vz-more{color:var(--ink3);font-size:11px}
.vz-det td{background:var(--bg);font-size:13.5px;line-height:1.55;
  padding:10px 14px}
.vz-note{font-size:13px;color:var(--ink2);max-width:900px;margin:10px 0 0}
a.gd-c{color:inherit;text-decoration:none;display:inline-block;border-bottom:1px dotted var(--ink3);cursor:pointer}
a.gd-c .sub2{display:block}
a.gd-c.on{border-bottom:2px solid var(--blue)}
.gd{margin-top:14px;padding:14px 16px;border:1px solid var(--line);border-radius:10px;background:var(--bg);max-width:820px}
.gd-h{font-weight:700;font-size:15px;margin-bottom:6px}
.gd-p{margin:6px 0;font-size:14px}
.gd-t{width:auto;min-width:520px;margin:8px 0 2px;font-size:13.5px}
.gd-t th{font-size:12px;text-align:right;color:var(--ink3)}
.gd-t td:first-child,.gd-t th:first-child{text-align:left}
.gd-t td.num{white-space:nowrap}
.gd-t b.gd-bad{color:var(--bad)}
.gd-t b.gd-ok{color:var(--good)}
.gd-note{font-size:12px;color:var(--ink3);margin:2px 0 0}
.gd-do{font-weight:700;margin-top:10px;font-size:13px;color:var(--ink2);text-transform:uppercase;letter-spacing:.04em}
.gd-ul{margin:6px 0 0;padding-left:18px;font-size:13.5px;line-height:1.45}
.gd-ul li{margin:4px 0}
.vz-start{font-size:15px;padding:14px;background:rgba(21,101,192,.06);
  border-radius:10px;max-width:640px;margin:14px 0}
"""


FUNNEL_SQL = """
WITH lead_rank AS (
  SELECT l.id, l.assigned_by AS uid,
         greatest(
           coalesce((SELECT max(CASE s.stage_id WHEN '8' THEN 1
                                 WHEN '10' THEN 2 WHEN '11' THEN 3
                                 WHEN '12' THEN 4 ELSE 0 END)
                       FROM stage_history s
                      WHERE s.entity_kind = 'lead' AND s.owner_id = l.id), 0),
           CASE WHEN EXISTS (SELECT 1 FROM v_sales v
                              WHERE v.lead_id = l.id AND v.counted)
                THEN 5 ELSE 0 END) AS r
    FROM leads l
   WHERE l.date_create >= %(since)s AND l.date_create < %(until)s
     AND coalesce(l.phone_kind, '') NOT IN ('junk', 'none')
     AND l.source_id IS DISTINCT FROM 'PARTNER'
     AND l.source_id IS DISTINCT FROM 'CALL'
     AND l.status_id IS DISTINCT FROM '31')
SELECT uid, count(*) AS n,
       count(*) FILTER (WHERE r >= 1) AS s1,
       count(*) FILTER (WHERE r >= 2) AS s2,
       count(*) FILTER (WHERE r >= 3) AS s3,
       count(*) FILTER (WHERE r >= 4) AS s4,
       count(*) FILTER (WHERE r >= 5) AS s5
  FROM lead_rank GROUP BY uid
"""

SALES_SQL = """
SELECT portal_user_id, count(*)
  FROM v_sales
 WHERE counted AND t13 >= %(since)s AND t13 < %(until)s
 GROUP BY 1
"""

FUNNEL_STEPS = [("s1", "Паспорт", "n"),
                ("s2", "Договор", "s1"),
                ("s3", "Оплата обеспечительного", "s2"),
                ("s4", "На торгах", "s3"),
                ("s5", "Продажа", "s4")]



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
        line = (f'<p class="gd-p">Прошли дальше: <b>{me["reached"]} из {me["n"]}</b>'
                + (f' ({p_me:.0f}%)' if p_me is not None else "")
                + (f'. В отделе {p_dep:.0f}%' if p_dep is not None else ""))
        if best and best != uid and best in d["by"]:
            pb = _gd_pct(d["by"][best])
            line += f', лучший в отделе — {e(short(names.get(best, "")))}: {pb:.0f}%'
        body.append(line + ".</p>")
        # Таблица «Вы / Лучший в отделе / Идеально» простыми словами
        # (просьба Тимофея 09.09: без «медиан», в три колонки).
        # «Идеально» — как было у выигранных сделок за 150 дней.
        bst = d["by"].get(best) if best and best != uid else None
        bname = short(names.get(best, "")) if bst else None

        def tr(label, k, fmt, better_low=True, only=None):
            mv, bv, wv = me.get(k), (bst or {}).get(k), won.get(k)
            if mv is None and wv is None:
                return ""
            cls = ""
            if mv is not None and wv is not None:
                worse = (mv > wv * 1.25) if better_low else (mv < wv * 0.8)
                cls = "gd-bad" if worse else "gd-ok"
            f = lambda x: fmt(x) if x is not None else "—"
            return (f'<tr><td>{label}</td><td class="num"><b class="{cls}">'
                    f'{f(mv)}</b></td><td class="num">{f(bv)}</td>'
                    f'<td class="num">{f(wv)}</td></tr>')

        rows_ = [tr("Первый звонок после смены статуса", "med_first", _tm_min_txt)]
        # число звонков сравниваем только там, где оно связано с исходом
        # (шаг 0 и торги); в договорной стадии решает охват и скорость
        if key in ("s1", "s5"):
            rows_.append(tr("Звонков на заявку", "avg_calls",
                            lambda x: f"{x:.1f}", better_low=False))
            rows_.append(tr("Пауза между звонками", "med_gap", _tm_min_txt))
        nc_b = f'{bst["no_call"]}' if bst else "—"
        rows_.append(f'<tr><td>Заявок без единого звонка</td><td class="num">'
                     f'<b class="{"gd-bad" if me["no_call"] else "gd-ok"}">'
                     f'{me["no_call"]}</b></td><td class="num">{nc_b}</td>'
                     f'<td class="num">0</td></tr>')
        body.append('<table class="gd-t"><thead><tr><th></th><th>Вы</th>'
                    f'<th>Лучший в отделе{(" — " + e(bname)) if bname else ""}</th>'
                    '<th>Идеально</th></tr></thead><tbody>'
                    + "".join(r for r in rows_ if r) + "</tbody></table>"
                    '<p class="gd-note">«Идеально» — как было у заявок, '
                    'которые дошли до продажи.</p>')
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


def _vz_funnel_one(conn, names, colors, since, until, label):
    """Воронка по менеджерам за одно окно: таблица + подпись."""
    rows = {r[0]: dict(zip("n s1 s2 s3 s4 s5".split(), r[1:]))
            for r in conn.execute(FUNNEL_SQL,
                                  {"since": since, "until": until}).fetchall()
            if r[0] in names}
    if not rows:
        return ('<p class="tm-kv">В этом окне заявок нет.</p>')
    dept = {k: sum(v[k] for v in rows.values())
            for k in "n s1 s2 s3 s4 s5".split()}
    fact = {u: n for u, n in conn.execute(
        SALES_SQL, {"since": since, "until": until}).fetchall()
        if u in names}

    def pct(a, b):
        return 100.0 * a / b if b else None

    def cell(v, key, prev_key, dept_pct, u):
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
                      for k, _, pk in FUNNEL_STEPS)
        skvoz = pct(v["s5"], v["n"])
        sk_txt = f"{skvoz:.2f}%" if skvoz is not None else "—"
        trs.append(
            f'<tr><td class="mname"><span class="vz-chip" '
            f'style="background:{colors[u]}"></span>{e(names[u])}</td>'
            f'<td class="num">{v["n"]}</td>' + tds +
            f'<td class="num"><b>{sk_txt}</b></td>'
            f'<td class="num vz-fact"><b>{fact.get(u, 0)}</b></td></tr>')
    def dcell(k, pk):
        p = pct(dept[k], dept[pk])
        sub = f"{p:.0f}%" if p is not None else "—"
        return (f'<td class="num">{dept[k]}'
                f'<span class="sub2">{sub}</span></td>')

    dtds = "".join(dcell(k, pk) for k, _, pk in FUNNEL_STEPS)
    dsk = pct(dept["s5"], dept["n"])
    trs.append(f'<tr><td class="mname"><b>Отдел</b></td>'
               f'<td class="num"><b>{dept["n"]}</b></td>' + dtds +
               f'<td class="num"><b>'
               f'{f"{dsk:.2f}%" if dsk is not None else "—"}</b></td>'
               f'<td class="num vz-fact"><b>{sum(fact.values())}</b></td>'
               "</tr>")

    head = "".join(f"<th>{t}</th>" for _, t, _ in FUNNEL_STEPS)
    return (f'<p class="tm-sum">Заявки, созданные {e(label)}: '
            f'<b>{dept["n"]}</b>. Крупно — сколько заявок дошло до стадии, '
            'мелким — <b>конверсия из предыдущей стадии</b>. Последняя '
            'колонка — <b>факт продаж этого периода</b> (любые заявки, в том '
            'числе пришедшие раньше): её и сверяйте с отчётом, остальные '
            'колонки — про судьбу заявок именно этого окна.</p>'
            '<table><thead><tr><th>Менеджер</th><th>Заявок</th>'
            + head + '<th>Сквозная</th>'
            '<th class="vz-fact">Продано<span class="sub2">в этот период'
            '</span></th></tr></thead><tbody>'
            + "".join(trs) + "</tbody></table>")


def _vz_funnel(conn, names, colors, now):
    """Воронка с переключателем окна (считаем все окна сразу, статика)."""
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = day0 + dt.timedelta(days=1)
    wk0 = day0 - dt.timedelta(days=now.weekday())
    m0 = day0.replace(day=1)                      # начало текущего месяца
    pm0 = (m0 - dt.timedelta(days=1)).replace(day=1)   # начало прошлого
    periods = [
        ("Текущий месяц", m0, tomorrow,
         f"с 1 {MONTHS[m0.month - 1]} {m0.year}"),
        ("Прошлый месяц", pm0, m0,
         f"за {MONTHS_NOM[pm0.month - 1]} {pm0.year}"),
        ("3 месяца", day0 - dt.timedelta(days=90), tomorrow, "за 90 дней"),
        ("6 месяцев", day0 - dt.timedelta(days=180), tomorrow, "за 180 дней"),
        ("Эта неделя", wk0, tomorrow, f"с понедельника ({ru_date(wk0)})"),
        ("Прошлая неделя", wk0 - dt.timedelta(days=7), wk0,
         f"за неделю с {ru_date(wk0 - dt.timedelta(days=7))}"),
    ]
    btns, secs = [], []
    for i, (title, since, until, label) in enumerate(periods):
        on = i == 0
        btns.append(f'<button class="mbtn{" on" if on else ""}" '
                    f'data-fn="{i}">{e(title)}</button>')
        secs.append(f'<div class="fnper" id="fn-{i}"'
                    + ("" if on else " hidden") + ">"
                    + _vz_funnel_one(conn, names, colors, since, until, label)
                    + "</div>")
    return ('<h2>Аналитика по % конверсии <span class="h2-hint">Клик '
            'по цифре стадии — подсказка, как улучшить показатель.</span></h2>'
            '<section class="card">'
            f'<div class="mbtns">{"".join(btns)}</div>'
            + "".join(secs)
            + guide_block(conn, names, now) +
            '<p class="vz-note">Стадия считается по максимальной достигнутой: '
            'перескок через шаг засчитывается всем пройденным. Зелёное '
            'и красное — отклонение от конверсии отдела на этом же переходе '
            'больше чем на пятую часть (подсвечивается, если на входе '
            'в переход хотя бы 8 заявок). Исключены непригодные номера, '
            'PARTNER, источник «Звонок» и спам. <b>Оговорка:</b> заявки '
            'последних двух-трёх недель ещё в пути — медиана цикла 9 дней, '
            'хвост до месяца, поэтому в коротких окнах нижние стадии почти '
            'пустые: неделя показывает вход в воронку, месяц и три — '
            'реальную проходимость.</p>'
            """<script>
(function(){
  var bs = document.querySelectorAll('.mbtn[data-fn]');
  for (var i = 0; i < bs.length; i++) bs[i].addEventListener('click', function(){
    for (var j = 0; j < bs.length; j++) bs[j].classList.remove('on');
    var ds = document.querySelectorAll('.fnper');
    for (var j = 0; j < ds.length; j++) ds[j].hidden = true;
    this.classList.add('on');
    document.getElementById('fn-' + this.getAttribute('data-fn')).hidden = false;
  });
})();
</script></section>""")


FUNNEL_DASH_TTL_MIN = 14


def funnel_for_dash(conn, now):
    """«Воронка по менеджерам» с Visual — под таблицей «Сейчас» на дашборде
    менеджера (просьба Тимофея 09.09.2026: пусть каждый видит конверсию
    свою и коллег). Тот же код _vz_funnel, все шесть окон; считается один
    раз на сборку (_CACHE), дальше — одна и та же разметка всем менеджерам.
    Стиль VISUAL_CSS подмешивается сюда же, потому что у дашборда его нет."""
    if "funnel_dash" in _CACHE:
        return _CACHE["funnel_dash"]
    # Шесть окон воронки — ~26 с, а дашборд собирается каждую минуту (3 с).
    # Поэтому готовая разметка лежит в dash_cache и пересчитывается только
    # если старше FUNNEL_DASH_TTL_MIN минут — то есть раз в четверть часа
    # её обновляет 15-минутный build.sh, а минутный build_now берёт готовую.
    conn.execute("""CREATE TABLE IF NOT EXISTS dash_cache (
        key text PRIMARY KEY, html text NOT NULL, built_at timestamptz NOT NULL)""")
    row = conn.execute(
        "SELECT html FROM dash_cache WHERE key = 'funnel_dash' "
        "AND built_at > now() - make_interval(mins => %s)",
        (FUNNEL_DASH_TTL_MIN,)).fetchone()
    if row:
        _CACHE["funnel_dash"] = row[0]
        return row[0]
    mgrs = conn.execute(f"""
        SELECT m.portal_user_id, m.name, m.last_name
          FROM managers m
         WHERE m.portal_user_id IN ({WORKING})
         ORDER BY m.portal_user_id""").fetchall()
    names = {u: human_name(n, ln) or f"id {u}" for u, n, ln in mgrs}
    colors = {u: VISUAL_COLORS[i % len(VISUAL_COLORS)]
              for i, (u, _, _) in enumerate(mgrs)}
    html_ = (f"<style>{VISUAL_CSS}</style>"
             + _vz_funnel(conn, names, colors, now))
    conn.execute("""INSERT INTO dash_cache (key, html, built_at)
        VALUES ('funnel_dash', %s, now())
        ON CONFLICT (key) DO UPDATE SET html = EXCLUDED.html, built_at = now()""",
                 (html_,))
    _CACHE["funnel_dash"] = html_
    return html_


def page_visual(conn):
    now = dt.datetime.now(VLD)
    today = now.date()
    wd = today.weekday()

    mgrs = conn.execute(f"""
        SELECT m.portal_user_id, m.name, m.last_name
          FROM managers m
         WHERE m.portal_user_id IN ({WORKING})
         ORDER BY m.portal_user_id""").fetchall()
    names = {u: human_name(n, ln) or f"id {u}" for u, n, ln in mgrs}
    colors = {u: VISUAL_COLORS[i % len(VISUAL_COLORS)]
              for i, (u, _, _) in enumerate(mgrs)}

    rows = conn.execute(
        "SELECT day, portal_user_id, pts, "
        "coalesce((parts->>'ideal')::float, 0), "
        "coalesce((parts->>'s0')::float, 0), "
        "coalesce((parts->>'s123')::float, 0), "
        "coalesce((parts->>'s4')::float, 0) "
        "FROM visual_points WHERE day >= %s", (VISUAL_START,)).fetchall()

    def monday(d):
        return d - dt.timedelta(days=d.weekday())

    w0 = monday(VISUAL_START)
    n_w = (monday(today) - w0).days // 7 + 1
    weeks = [w0 + dt.timedelta(days=7 * i) for i in range(n_w)]
    wk = {u: [0.0] * n_w for u in names}
    wid = {u: [0.0] * n_w for u in names}
    daily = {u: {} for u in names}          # day -> (pts, s0, s123, s4)
    for day, uid, pts_v, idl, c0, c123, c4 in rows:
        if uid not in names:
            continue
        i = (monday(day) - w0).days // 7
        if 0 <= i < n_w:
            wk[uid][i] += float(pts_v)
            wid[uid][i] += float(idl)
            daily[uid][day] = (float(pts_v), float(c0), float(c123), float(c4))

    sales = {u: [0] * n_w for u in names}
    # продажа = переход лида в стадию «ТС куплен» (13) — решение Тимофея
    # 01.09.2026: сделки WON у половины отдела расходятся с его учётом.
    for uid, wstart, cnt in conn.execute("""
        SELECT portal_user_id,
               (date_trunc('week', t13 AT TIME ZONE 'Asia/Vladivostok'))::date,
               count(*)
          FROM v_sales
         WHERE counted AND t13 >= %s
         GROUP BY 1, 2""", (w0,)).fetchall():
        if uid in names:
            i = (wstart - w0).days // 7
            if 0 <= i < n_w:
                sales[uid][i] = cnt

    # ── балл к этому дню недели против своей нормы, по составляющим ──
    cur_i = n_w - 1
    cw0 = weeks[cur_i]
    COMP = [(1, "Новые заявки (шаг 0)",
             "скорость и настойчивость по свежим заявкам — KPI, «От заявки "
             "до первого звонка» и Шаг 0"),
            (2, "Середина воронки (шаги 1–3)",
             "звонок сразу после смены статуса — KPI, шаги 1–3; это "
             "сильнее всего связано с продажами"),
            (3, "Сопровождение торгов (шаг 4)",
             "звонки по открытым торгам — KPI, Шаг 4")]
    stat = {}
    for u in names:
        def part_sum(week_start, idx):
            return sum(v[idx] for d, v in daily[u].items()
                       if week_start <= d and (d - week_start).days <= wd)
        cur = [part_sum(cw0, 0), part_sum(cw0, 1),
               part_sum(cw0, 2), part_sum(cw0, 3)]
        prevs = []
        for k in (1, 2, 3):
            i = cur_i - k
            if i < 0:
                continue
            pw = weeks[i]
            if not any(pw <= d < pw + dt.timedelta(days=7)
                       for d in daily[u]):
                continue
            prevs.append([part_sum(pw, 0), part_sum(pw, 1),
                          part_sum(pw, 2), part_sum(pw, 3)])
        norm = ([sum(p[j] for p in prevs) / len(prevs) for j in range(4)]
                if prevs else None)
        dev = (100.0 * (cur[0] - norm[0]) / norm[0]
               if norm and norm[0] else None)
        pct = 100.0 * wk[u][cur_i] / wid[u][cur_i] if wid[u][cur_i] else None
        stat[u] = (cur, norm, dev, pct)

    def verdict(dev, norm):
        if norm is None or norm[0] < 30:
            return ('<span class="dim">мало данных</span>', "")
        if dev >= 10:
            return ('<span class="tm-good">&#9650; выше темпа</span>', "")
        if dev >= -10:
            return ('<span class="tm-good">&#9679; в норме</span>', "")
        if dev >= -30:
            return ('<span class="vz-warn">&#9660; ниже темпа</span>', "warn")
        return ('<span class="tm-bad">&#9660; принять меры</span>', "bad")

    def detail(u):
        cur, norm, dev, pct = stat[u]
        if norm is None:
            return "Нормы ещё нет — мало прошлых недель."
        lines = []
        for j, nm, hint in COMP:
            c, n = cur[j], norm[j]
            if n < 10:
                continue
            d_ = 100.0 * (c - n) / n
            if d_ <= -25:
                lines.append((d_, f"<b>{nm}: {round(c)} против своей нормы "
                              f"{round(n)} ({round(d_):+d}%).</b> "
                              f"Промониторить: {hint}."))
        if not lines:
            return ("Провальной составляющей нет — отставание размазано "
                    "или темп в норме. Смотреть общую активность дня.")
        lines.sort(key=lambda t: t[0])
        return "<br>".join(t[1] for t in lines)

    order = sorted(names, key=lambda u: -(stat[u][3]
                                          if stat[u][3] is not None else -1))
    trs = []
    for u in order:
        cur, norm, dev, pct = stat[u]
        vtxt, flag = verdict(dev, norm)
        trs.append(
            f'<tr class="vz-row" data-u="{u}"><td class="mname">'
            f'<span class="vz-chip" style="background:{colors[u]}"></span>'
            f'{e(names[u])}</td>'
            f'<td class="num">{round(pct) if pct is not None else "—"}%</td>'
            f'<td class="num"><b>{round(cur[0])}</b></td>'
            f'<td class="num">{round(norm[0]) if norm else "—"}</td>'
            f'<td class="num">{("%+d%%" % round(dev)) if dev is not None else "—"}</td>'
            f'<td class="num">{sales[u][cur_i]}</td>'
            f"<td>{vtxt} <span class=\"vz-more\">&#9662;</span></td></tr>"
            f'<tr class="vz-det" id="vzd-{u}" hidden><td colspan="7">'
            f"{detail(u)}</td></tr>")

    # ── график по неделям: менеджеры линиями ──
    W, H = 980, 430
    L, R, T, B = 46, 170, 24, 40
    pw_, ph = W - L - R, H - T - B
    ymax = max([v for u in names for v in wk[u]] + [10.0]) * 1.15

    def x(i):
        return L + pw_ * i / max(n_w - 1, 1)

    def y(v):
        return T + ph - ph * v / ymax

    g = []
    for i, ws in enumerate(weeks):
        g.append(f'<line x1="{x(i):.1f}" y1="{T}" x2="{x(i):.1f}" '
                 f'y2="{T+ph}" stroke="var(--line)" stroke-width="1"/>')
        g.append(f'<text x="{x(i):.1f}" y="{H-14}" font-size="11" '
                 f'fill="var(--ink2)" text-anchor="middle">'
                 f'{ws.strftime("%d.%m")}</text>')
    for k in range(6):
        v = ymax * k / 5
        g.append(f'<line x1="{L}" y1="{y(v):.1f}" x2="{L+pw_}" '
                 f'y2="{y(v):.1f}" stroke="var(--line)" stroke-width="1" '
                 f'stroke-dasharray="{"" if k == 0 else "3 4"}"/>')
        g.append(f'<text x="{L-8}" y="{y(v)+4:.1f}" font-size="11" '
                 f'fill="var(--ink2)" text-anchor="end">{round(v)}</text>')

    ends = sorted(((wk[u][-1], u) for u in names), reverse=True)
    label_y, prev = {}, None
    for v, u in ends:
        yy = y(v)
        if prev is not None and prev + 17 > yy:
            yy = prev + 17
        label_y[u] = yy
        prev = yy
    for u in names:
        c = colors[u]
        pts_attr = " ".join(f"{x(i):.1f},{y(v):.1f}"
                            for i, v in enumerate(wk[u]))
        if n_w > 1:
            g.append(f'<polyline points="{pts_attr}" fill="none" '
                     f'stroke="{c}" stroke-width="2.5" '
                     'stroke-linejoin="round" stroke-linecap="round"/>')
        for i, v in enumerate(wk[u]):
            pctw = (f", {round(100 * v / wid[u][i])}% от эталона"
                    if wid[u][i] else "")
            g.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="4.5" '
                     f'fill="{c}" stroke="var(--surface)" stroke-width="2">'
                     f'<title>{e(short(names[u]))}, неделя с '
                     f'{weeks[i].strftime("%d.%m")}: {round(v)} баллов'
                     f'{pctw}, продаж {sales[u][i]}</title></circle>')
        g.append(f'<text x="{x(n_w-1)+10:.1f}" y="{label_y[u]+4:.1f}" '
                 f'font-size="12.5" font-weight="600" fill="{c}">'
                 f'{e(short(names[u]))} · {round(wk[u][-1])}</text>')
    svg = (f'<svg viewBox="0 0 {W} {H}" '
           f'style="width:100%;max-width:1100px" role="img">'
           + "".join(g) + "</svg>")

    # ── путь к эталону: % от личного эталона по неделям, цель пунктиром ──
    VZ_TARGET = 35            # уровень «зелёного»: лучшие недели отдела
    pctw = {u: [(100.0 * wk[u][i] / wid[u][i]) if wid[u][i] else 0.0
                for i in range(n_w)] for u in names}
    H2 = 380
    ph2 = H2 - T - B
    ymax2 = max([v for u in names for v in pctw[u]] + [VZ_TARGET + 5.0]) * 1.15

    def y2(v):
        return T + ph2 - ph2 * v / ymax2

    g2 = []
    for i, ws in enumerate(weeks):
        g2.append(f'<line x1="{x(i):.1f}" y1="{T}" x2="{x(i):.1f}" '
                  f'y2="{T+ph2}" stroke="var(--line)" stroke-width="1"/>')
        g2.append(f'<text x="{x(i):.1f}" y="{H2-14}" font-size="11" '
                  f'fill="var(--ink2)" text-anchor="middle">'
                  f'{ws.strftime("%d.%m")}</text>')
    for k in range(6):
        v = ymax2 * k / 5
        g2.append(f'<line x1="{L}" y1="{y2(v):.1f}" x2="{L+pw_}" '
                  f'y2="{y2(v):.1f}" stroke="var(--line)" stroke-width="1" '
                  f'stroke-dasharray="{"" if k == 0 else "3 4"}"/>')
        g2.append(f'<text x="{L-8}" y="{y2(v)+4:.1f}" font-size="11" '
                  f'fill="var(--ink2)" text-anchor="end">{round(v)}%</text>')
    g2.append(f'<line x1="{L}" y1="{y2(VZ_TARGET):.1f}" x2="{L+pw_}" '
              f'y2="{y2(VZ_TARGET):.1f}" stroke="var(--good)" '
              'stroke-width="2" stroke-dasharray="7 5"/>')
    g2.append(f'<text x="{L+pw_+8:.1f}" y="{y2(VZ_TARGET)+4:.1f}" '
              f'font-size="12.5" font-weight="600" fill="var(--good)">'
              f'цель · {VZ_TARGET}%</text>')
    ends2 = sorted(((pctw[u][-1], u) for u in names), reverse=True)
    ly2, prev2 = {}, y2(VZ_TARGET) - 17
    for v, u in ends2:
        yy = y2(v)
        if prev2 is not None and prev2 + 17 > yy:
            yy = prev2 + 17
        ly2[u] = yy
        prev2 = yy
    for u in names:
        c = colors[u]
        pts_attr = " ".join(f"{x(i):.1f},{y2(v):.1f}"
                            for i, v in enumerate(pctw[u]))
        if n_w > 1:
            g2.append(f'<polyline points="{pts_attr}" fill="none" '
                      f'stroke="{c}" stroke-width="2.5" '
                      'stroke-linejoin="round" stroke-linecap="round"/>')
        for i, v in enumerate(pctw[u]):
            g2.append(f'<circle cx="{x(i):.1f}" cy="{y2(v):.1f}" r="4.5" '
                      f'fill="{c}" stroke="var(--surface)" stroke-width="2">'
                      f'<title>{e(short(names[u]))}, неделя с '
                      f'{weeks[i].strftime("%d.%m")}: {round(v)}% от '
                      f'личного эталона</title></circle>')
        g2.append(f'<text x="{x(n_w-1)+10:.1f}" y="{ly2[u]+4:.1f}" '
                  f'font-size="12.5" font-weight="600" fill="{c}">'
                  f'{e(short(names[u]))} · {round(pctw[u][-1])}%</text>')
    svg2 = (f'<svg viewBox="0 0 {W} {H2}" '
            f'style="width:100%;max-width:1100px" role="img">'
            + "".join(g2) + "</svg>")

    return (f"<style>{VISUAL_CSS}</style>"
            '<p class="sub" style="max-width:1000px">Неделя под контролем: '
            'сверху — кто ближе к эталону прямо сейчас (и кто отклонился '
            'от своего темпа — балл с понедельника к этому же дню прошлых '
            'трёх недель). Клик по строке раскрывает, что именно '
            'промониторить. Ниже — баллы по неделям, менеджеры линиями. '
            'Последняя точка — текущая неделя, она ещё копится.</p>'
            '<h2>Эта неделя</h2><section class="card">'
            '<table><thead><tr><th>Менеджер</th><th>% от эталона</th>'
            f'<th>Балл к {["пн","вт","ср","чт","пт","сб","вс"][wd]}</th>'
            '<th>Своя норма</th><th>Отклонение</th>'
            '<th>Продаж</th><th>Вердикт</th></tr></thead><tbody>'
            + "".join(trs) + "</tbody></table>"
            '<p class="vz-note">Сортировка — по близости к эталону этой '
            'недели. Норма — средний балл этого же менеджера к этому же '
            'дню недели за три прошлые недели: выходные сравниваются '
            'с выходными. «Принять меры» — минус 30% к своему темпу и '
            'хуже; клик по строке — разбор по составляющим.</p></section>'
            '<h2>По неделям</h2><section class="card">' + svg +
            '<p class="vz-note">Точка — баллы менеджера за неделю (не '
            'накопительно): просадка видна падением линии. Наведите на '
            'точку — баллы, % от эталона и продажи недели. Баллы дня: до '
            '10 за новую живую заявку (60% настойчивость, 40% скорость); '
            'до <b>100</b> за смену статуса «Паспорт» / «Договор» / '
            '«Оплата обеспечительного» (70% скорость звонка после '
            'статуса, 30% охват; задним числом — ноль); по <b>10</b> за '
            'звонок клиенту на открытых торгах (до 30 в день на заявку). '
            'Веса середины воронки и торгов подняты 29.08 по проверке на '
            'реальных продажах. Сами продажи в балл не входят. Снимок — '
            'каждый вечер в 23:55.</p></section>'
            '<h2>Путь к эталону</h2><section class="card">' + svg2 +
            '<p class="vz-note">Та же неделя, но мера — не свой прошлый '
            'темп, а личный эталон: сколько процентов от «сделал всё по '
            'правилам с этим же потоком» менеджер выбрал. Пунктир — цель '
            '35%: уровень лучших недель отдела; 100% недостижимы (звонок '
            'каждому в первые 15 минут). Линия должна ползти к пунктиру — '
            'кто под ним и не растёт, тот и есть кандидат на разбор.</p>'
            '</section>'
            + _vz_funnel(conn, names, colors, now)
            + """<script>
(function(){
  var rs = document.querySelectorAll('.vz-row');
  for (var i = 0; i < rs.length; i++) rs[i].addEventListener('click', function(){
    var d = document.getElementById('vzd-' + this.getAttribute('data-u'));
    if (d) d.hidden = !d.hidden;
  });
})();
</script>""")


def page_visual_locked(conn):        # не используется с 02.09.2026:
    # пин с Visual снят решением Тимофея, вернуть — заменить page_visual
    # на page_visual_locked в ROP_PAGES
    import json as _json
    body = page_visual(conn)
    payload = _json.dumps(_tm_encrypt(body, TIMINGS_PIN))
    return (f"<style>{TIMINGS_CSS}</style>"
            + TIMINGS_GATE_JS.replace("__PAYLOAD__", payload))


def page_taymingi(conn, period="base", show_tabs=True):
    now = dt.datetime.now(VLD)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        since = day0 - dt.timedelta(days=now.weekday())
        label = f"с понедельника ({ru_date(since)})"
        won_since, won_label = since, label
        reset_note = " Окно обнуляется каждый понедельник."
    elif period == "month":
        since = day0.replace(day=1)
        label = f"с 1 {MONTHS[now.month - 1]}"
        won_since, won_label = since, label
        reset_note = " Окно обнуляется 1-го числа."
    else:
        since = now - dt.timedelta(days=TIMINGS_DAYS)
        label = f"за последние {TIMINGS_DAYS} дней"
        won_since = now - dt.timedelta(days=TIMINGS_WON_DAYS)
        won_label = f"за последние {TIMINGS_WON_DAYS} дней"
        reset_note = ""

    tabs = "".join(
        f'<a class="tab{" on" if k == period else ""}" href="{f}">{t}</a>'
        for k, f, t in TIMINGS_PERIODS)
    tabs = (f'<nav class="tabs" style="margin-bottom:14px">{tabs}</nav>'
            if show_tabs else "")

    blocks = [_tm_first_touch_block(conn, now, since, label),
              _tm_step0_block(conn, now, since)]
    for sl in TIMINGS_SLICES:
        rows = _tm_rows(conn, sl["st"], sl["succ"], since)
        blocks.append(
            f'<h2>{e(sl["head"])}</h2><section class="card">'
            + _tm_rank_table(_tm_by_mgr(rows), sl["target"],
                             etalon="эталон 30–60 мин"
                             if sl["st"] == "8" else None)
            + '<p class="tm-note">«Звонит через» — медиана времени от смены '
            'статуса до первого исходящего звонка клиенту. Звонки считаются '
            'до следующего статуса. «Довёл» — следующим статусом заявка ушла '
            'дальше по воронке (перескок через шаг тоже считается). Заявки, '
            f'где статус сменился быстрее {TIMINGS_MIN_WINDOW} минут, '
            'в средние по звонкам не входят — звонок туда не помещается '
            'физически; их число — в колонке «Задним числом».</p>'
            + _tm_details(rows, now) + "</section>")

    blocks.append(_tm_step4_block(conn, now, since))
    blocks.append(_tm_torgi_block(conn, now, since, label))

    # Блок «Только выигранные» убран решением Тимофея 28.08.2026
    # (код в бэкапе dash.py.bak-nowon, вернуть — скопировать оттуда).

    rows_card = _tm_rows(
        conn, "10", ("12", "13", "CONVERTED"), since,
        endcond="s.stage_id IN ('12','13') OR s.stage_semantic IN ('F','S')")
    best_card = _tm_best_card(rows_card, "до торгов или покупки", label)

    return (f"<style>{TIMINGS_CSS}</style>" + tabs
            + '<p class="sub" style="max-width:1000px">Договорная стадия по '
            'шагам: «Паспорт» → «Договор» → «Оплата обеспечительного»'
            ' → «На торгах». '
            'По каждому шагу: кто как быстро берёт телефон после смены '
            'статуса, сколько звонков делает и кто доводит заявку дальше. '
            f'Окно — <b>{label}</b>.{reset_note} <b>Оговорка:</b> статусами '
            'в реальном времени пользуются не все — у кого статус живёт '
            'минуты (колонка «Задним числом»), тот проставляет его пачкой '
            'в конце, скорость по таким заявкам не измерить, а долю '
            'доведённых нельзя сравнивать в лоб. <b>Балл</b> (по нему и даётся '
            '«лучший»): 70% — скорость первого звонка после смены статуса, '
            '30% — охват (по заявке в статусе вообще был звонок). Веса '
            'выведены из продаж за апрель–август: звонок в первые 15 минут — '
            '66% продаж, позже суток — 10%; а третий и далее звонок в рамках '
            'одного статуса продажу уже не добавляет, поэтому звонковая '
            'часть меряет охват, а не число звонков. Главный столбец стадии '
            'подсвечен красным, второй зачётный — голубым: в шагах 1–3 '
            'главное — скорость, на стадии торгов — звонки.</p>' 
            + best_card + "".join(blocks))


def _tm_encrypt(text, pin):
    """PBKDF2(пин) → HMAC-SHA256-поток (CTR) → XOR; тег — HMAC(key, 'mac'+ct)."""
    import os as _os, hmac as _hmac, struct as _struct, base64 as _b64
    import hashlib as _hl
    salt, iters = _os.urandom(16), 200_000
    key = _hl.pbkdf2_hmac("sha256", pin.encode(), salt, iters, 32)
    data = text.encode()
    ks = b"".join(_hmac.new(key, _struct.pack(">I", i), _hl.sha256).digest()
                  for i in range(len(data) // 32 + 1))
    ct = bytes(a ^ b for a, b in zip(data, ks))
    tag = _hmac.new(key, b"mac" + ct, _hl.sha256).hexdigest()
    return {"s": salt.hex(), "n": iters,
            "c": _b64.b64encode(ct).decode(), "t": tag}


TIMINGS_GATE_JS = """
<div id="tm-lock" class="card tm-lock">
  <p style="margin-top:0">Страница закрыта пин-кодом.</p>
  <form id="tm-form" style="display:inline">
    <input id="tm-pin" type="password" inputmode="numeric" autocomplete="off"
           maxlength="8" placeholder="····"><button type="submit">Открыть</button>
  </form>
  <p id="tm-err" class="tm-bad" hidden>Неверный пин-код</p>
  <p id="tm-nossl" class="tm-bad" hidden>Нужен https: браузер не даёт
    расшифровать страницу по открытому соединению.</p>
</div>
<div id="tm-body"></div>
<script>
var TM = __PAYLOAD__;
function h2b(h){var a=new Uint8Array(h.length/2);
  for(var i=0;i<a.length;i++)a[i]=parseInt(h.substr(2*i,2),16);return a;}
function b2h(b){var s="",v=new Uint8Array(b);
  for(var i=0;i<v.length;i++)s+=v[i].toString(16).padStart(2,"0");return s;}
function b64(s){var r=atob(s),a=new Uint8Array(r.length);
  for(var i=0;i<r.length;i++)a[i]=r.charCodeAt(i);return a;}
async function tmTry(pin){
  var te=new TextEncoder();
  var base=await crypto.subtle.importKey("raw",te.encode(pin),"PBKDF2",false,
    ["deriveBits"]);
  var bits=await crypto.subtle.deriveBits({name:"PBKDF2",hash:"SHA-256",
    salt:h2b(TM.s),iterations:TM.n},base,256);
  var hk=await crypto.subtle.importKey("raw",bits,
    {name:"HMAC",hash:"SHA-256"},false,["sign"]);
  var ct=b64(TM.c);
  var mi=new Uint8Array(3+ct.length);mi.set(te.encode("mac"));mi.set(ct,3);
  if(b2h(await crypto.subtle.sign("HMAC",hk,mi))!==TM.t)return false;
  var nb=Math.ceil(ct.length/32),jobs=[];
  for(var i=0;i<nb;i++){var c=new Uint8Array(4);
    new DataView(c.buffer).setUint32(0,i);
    jobs.push(crypto.subtle.sign("HMAC",hk,c));}
  var blocks=await Promise.all(jobs),out=new Uint8Array(ct.length);
  for(var i=0;i<nb;i++){var bl=new Uint8Array(blocks[i]);
    for(var j=0;j<32&&32*i+j<ct.length;j++)out[32*i+j]=ct[32*i+j]^bl[j];}
  var body=document.getElementById("tm-body");
  body.innerHTML=new TextDecoder().decode(out);
  var sc=body.querySelectorAll("script");
  for(var i=0;i<sc.length;i++){var ns=document.createElement("script");
    ns.text=sc[i].text;sc[i].parentNode.replaceChild(ns,sc[i]);}
  document.getElementById("tm-lock").remove();
  try{localStorage.setItem("ropbot_tm_pin",pin);}catch(e){}
  return true;
}
(function(){
  if(!(window.crypto&&crypto.subtle)){
    document.getElementById("tm-nossl").hidden=false;return;}
  var saved=null;
  try{saved=localStorage.getItem("ropbot_tm_pin");}catch(e){}
  if(saved)tmTry(saved).catch(function(){});
  document.getElementById("tm-form").addEventListener("submit",function(ev){
    ev.preventDefault();
    var p=document.getElementById("tm-pin").value.trim();
    if(!p)return;
    tmTry(p).then(function(ok){
      document.getElementById("tm-err").hidden=ok;
      if(!ok)document.getElementById("tm-pin").select();
    });
  });
})();
</script>
"""


def page_taymingi_locked(conn, period="base"):
    import json as _json
    body = page_taymingi(conn, period)  # свой <style> уже внутри
    payload = _json.dumps(_tm_encrypt(body, TIMINGS_PIN))
    return (f"<style>{TIMINGS_CSS}</style>"
            + TIMINGS_GATE_JS.replace("__PAYLOAD__", payload))



# Вкладка KPI на дашборде менеджера: те же «Тайминги», но без пин-кода.
# Решение Тимофея 28.08.2026; 28.08 же добавлено окно «Текущий месяц»
# (обнуляется 1-го числа). Контент у всех менеджеров одинаковый —
# считаем один раз за процесс сборки.
_KPI_CACHE = {}

KPI_PERIODS = [("base", "kpi.html", "60 дней"),
               ("month", "kpi-m.html", "Текущий месяц"),
               ("week", "kpi-w.html", "Текущая неделя"),
               ("days", "kpi-d.html", "По дням")]


def _kpi_page(conn, period):
    if period not in _KPI_CACHE:
        nav = "".join(
            f'<a class="tab{" on" if k == period else ""}" href="{f}">{t}</a>'
            for k, f, t in KPI_PERIODS)
        _KPI_CACHE[period] = (
            f'<nav class="tabs" style="margin-bottom:14px">{nav}</nav>'
            + page_taymingi(conn, period, show_tabs=False))
    return _KPI_CACHE[period]


def page_kpi(conn, uid):
    return _kpi_page(conn, "base")


def page_kpi_m(conn, uid):
    return _kpi_page(conn, "month")


def page_kpi_w(conn, uid):
    return _kpi_page(conn, "week")


def _kpi_day_body(conn, now, day):
    """Все блоки KPI, окно — один день (заявки/переводы этого дня)."""
    u = day + dt.timedelta(days=1)
    label = f"за {day.strftime('%d.%m')}"
    body = (_tm_first_touch_block(conn, now, day, label, until=u)
            + _tm_step0_block(conn, now, day, until=u))
    for sl in TIMINGS_SLICES:
        rows = _tm_rows(conn, sl["st"], sl["succ"], day, until=u)
        body += (f'<h2>{e(sl["head"])}</h2><section class="card">'
                 + _tm_rank_table(_tm_by_mgr(rows), sl["target"])
                 + _tm_details(rows, now) + "</section>")
    body += _tm_step4_block(conn, now, day, until=u)
    body += _tm_torgi_block(conn, now, day, label, until=u)
    return body


def page_kpi_days(conn, uid):
    """Подвкладка «По дням»: кнопка на каждый день текущего месяца,
    по клику — срез KPI по заявкам и переводам этого дня. Звонки по событию
    дня считаются до следующего статуса, даже если он наступил позже."""
    if "days" in _KPI_CACHE:
        return _KPI_CACHE["days"]
    now = dt.datetime.now(VLD)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days, d = [], day0.replace(day=1)
    while d <= day0:
        days.append(d)
        d += dt.timedelta(days=1)

    nav = "".join(
        f'<a class="tab{" on" if k == "days" else ""}" href="{f}">{t}</a>'
        for k, f, t in KPI_PERIODS)
    btns, secs = [], []
    for i, d in enumerate(days):
        on = d == day0
        btns.append(f'<button class="mbtn{" on" if on else ""}" '
                    f'data-tmday="{i}">{d.strftime("%d.%m")}</button>')
        secs.append(f'<div class="tmday" id="tmday-{i}"'
                    + ("" if on else " hidden") + ">"
                    + _kpi_day_body(conn, now, d) + "</div>")
    return _KPI_CACHE.setdefault("days", (
        f'<style>{TIMINGS_CSS}</style>'
        f'<nav class="tabs" style="margin-bottom:14px">{nav}</nav>'
        '<p class="sub" style="max-width:1000px">Срез по дням текущего '
        'месяца: заявки, созданные в выбранный день, и переводы статусов '
        'этого дня. Звонки по каждому событию считаются до следующего '
        'статуса, даже если он случился позже, — поэтому цифры прошлых '
        'дней могут дозревать. Окно обнуляется 1-го числа.</p>'
        f'<div class="mbtns">{"".join(btns)}</div>'
        + "".join(secs) +
        """<script>
(function(){
  var bs = document.querySelectorAll('.mbtn[data-tmday]');
  for (var i = 0; i < bs.length; i++) bs[i].addEventListener('click', function(){
    for (var j = 0; j < bs.length; j++) bs[j].classList.remove('on');
    var ds = document.querySelectorAll('.tmday');
    for (var j = 0; j < ds.length; j++) ds[j].hidden = true;
    this.classList.add('on');
    document.getElementById('tmday-' + this.getAttribute('data-tmday')).hidden = false;
  });
})();
</script>"""))




# ── вкладка РОПа «Instruction» (06.09.2026, переделана 09.09.2026) ───────────
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
INS_NEW_FROM = 10       # рабочее окно для новой заявки (Тимофей 09.09: 10–18 Влд),
INS_NEW_TO = 18         # заявки вне окна не оцениваются (как в KPI)
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
     f"Первый звонок в первые {INS_NEW_MIN} минут. Считаются только заявки, "
     f"пришедшие в рабочее время {INS_NEW_FROM}–{INS_NEW_TO} Влд (как в KPI); "
     "вечерние и ночные сюда не входят. Инструкция: 1-й звонок совершается "
     "сразу после поступления заявки."),
    ("talk", "Анализ разговора",
     "бюджет, срок, безопасность, договор",
     "В первом содержательном разговоре (от 90 секунд, разобран картой) "
     "установлен бюджет, спрошен срок покупки, показана безопасность "
     "(трансляция / видеосвязь) и предложен договор — все четыре. Алгоритм "
     "тот же, что в KPI, шаг 0. Инструкция для «Необработанного»: спрашиваем "
     "БЮДЖЕТ, СРОКИ, закрываем БЕЗОПАСНОСТЬ, предлагаем ВИДЕОЗВОНОК и ДОГОВОР."),
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
.ins-c.ins-m{padding:4px 8px;text-align:left}
.ins-c.ins-m small{display:block;text-align:center;margin-top:2px}
.ins-i{display:flex;justify-content:space-between;gap:8px;font-size:11.5px;line-height:1.35}
.ins-i i{font-style:normal;color:var(--ink2)}
.ins-i b{font-size:11.5px;display:inline;line-height:inherit;font-weight:700}
.ins-i b.g{color:#1d8a1d}.ins-i b.y{color:#a86f00}.ins-i b.r{color:var(--bad)}
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
        # Решение Тимофея 09.09 (вариант 1): считаем только заявки, пришедшие
        # в рабочее окно, как в KPI. Вечерние/ночные утром ложатся очередью,
        # и 30 минут на десяток заявок мерили бы не реакцию, а размер очереди.
        if not (INS_NEW_FROM <= t0.astimezone(VLD).hour < INS_NEW_TO):
            continue
        wm = (_tm_workmin(t0, fc, INS_NEW_FROM, INS_NEW_TO) if fc
              else _tm_workmin(t0, now, INS_NEW_FROM, INS_NEW_TO))
        if fc is None and wm < INS_NEW_MIN:
            continue                                   # ещё успевает
        ok = fc is not None and wm <= INS_NEW_MIN
        why = ("" if ok else "звонка не было" if fc is None
               else f"первый звонок через {_ins_dur(wm * 60)} рабочего времени")
        out.append(("new", uid, t0.astimezone(VLD).date(), ok, lid, title,
                    t0, why))

    # анализ разговора — те же строки и признаки, что в KPI (шаг 0, S0_SQL)
    s0cols = ("lead_id t0 t_end next_stage next_name title phone uid "
              "mname mlast calls talks dogovor fc lc bq ta ss").split()
    dept5 = _tm_dept5(conn)
    for r in (dict(zip(s0cols, x)) for x in conn.execute(
            S0_SQL, {"since": since, "until": now + dt.timedelta(days=1)})):
        if r["uid"] not in dept5 or not r["bq"]:
            continue                     # разговора с картой ещё не было
        flags = {"Бюджет": r["bq"] != "no", "Срок": bool(r["ta"]),
                 "Безопасность": bool(r["ss"]), "Договор": bool(r["dogovor"])}
        miss = {"Бюджет": "бюджет не установлен", "Срок": "срок не спросил",
                "Безопасность": "безопасность не показал",
                "Договор": "договор не предложил"}
        why = ", ".join(v for k, v in miss.items() if not flags[k])
        # девятый элемент — признаки по пунктам: ячейка показывает процент
        # по каждому (просьба Тимофея 09.09), а не один общий
        out.append(("talk", r["uid"], r["t0"].astimezone(VLD).date(),
                    not why, r["lead_id"], r["title"], r["t0"], why, flags))

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

    def detail(did, label, who, plabel, brief, full, cs):
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
                f'({e(brief)})</h4><p class="dim" style="margin:0 0 8px">'
                f'{e(full)}</p>{body}</div>')

    head = ('<th style="text-align:left">Менеджер</th>'
            '<th class="rule total" title="Среднее по проверкам, где были случаи">'
            'По инструкции<small>среднее</small></th>'
            + "".join(f'<th class="rule" title="{e(full)}">{e(label)}'
                      f'<small>{e(brief)}</small></th>'
                      for _, label, brief, full in INS_RULES))

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
            for key, label, brief, full in INS_RULES:
                cs = cell.get((key, u), [])
                if not cs:
                    tds.append('<td class="ins-c n"><b>—</b></td>')
                    continue
                okn, n = sum(1 for c in cs if c[3]), len(cs)
                did = f"insd-{pk}-{key}-{u}"
                if key == "talk":
                    # по каждому пункту свой процент; в общий «По инструкции»
                    # идёт среднее четырёх, цвет ячейки — по нему же
                    items = list(cs[0][8].keys())
                    shares = {k: 100.0 * sum(1 for c in cs if c[8][k]) / n
                              for k in items}
                    pct = sum(shares.values()) / len(shares)
                    pcts.append(pct)
                    lines = "".join(
                        f'<span class="ins-i"><i>{k}</i>'
                        f'<b class="{cls(v)}">{round(v)}%</b></span>'
                        for k, v in shares.items())
                    tds.append(f'<td class="ins-c ins-m {cls(pct)}" data-d="{did}">'
                               f'{lines}<small>{n} разг.</small></td>')
                else:
                    pct = 100.0 * okn / n
                    pcts.append(pct)
                    tds.append(f'<td class="ins-c {cls(pct)}" data-d="{did}">'
                               f'<b>{round(pct)}%</b><small>{okn} из {n}</small></td>')
                details.append(detail(did, label, who, plabel, brief, full, cs))
            if pcts:
                a = sum(pcts) / len(pcts)
                tot = (f'<td class="ins-c total {cls(a)}" style="cursor:default">'
                       f'<b>{round(a)}%</b><small>{len(pcts)} '
                       f'{plural(len(pcts), "проверка", "проверки", "проверок")}'
                       '</small></td>')
            else:
                tot = '<td class="ins-c n total"><b>—</b></td>'
            row = (f'<td class="mname">{e(short(who)) if u != "all" else "<b>Отдел</b>"}</td>'
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
</script>""")


ROP_PAGES = {"kontrol": page_kontrol, "menedzhery": page_managers,
             "soobshcheniya": page_sms,
             "slepok": page_slepok, "etalon": page_etalon,
             "kachestvo": page_kachestvo, "zamery": page_zamery,
             "taymingi": lambda c: page_taymingi_locked(c, "base"),
             "taymingi-w": lambda c: page_taymingi_locked(c, "week"),
             "taymingi-m": lambda c: page_taymingi_locked(c, "month"),
             "visual": page_visual,
             "instruction": page_instruction}
ADMIN_PAGES = {"hub": page_hub}

PAGES = {"zayavki": page_queue, "skript": page_script,
         "razgovory": page_talks, "pravila": page_rules,
         "dashboard": page_mgr_dash, "kpi": page_kpi, "kpi-m": page_kpi_m,
         "kpi-w": page_kpi_w, "kpi-d": page_kpi_days}


def build_page(conn, kind, uid, mgr, page, now, built):
    """HTML одной страницы."""
    if kind == "admin":
        return shell("hub", "Все панели", built, ADMIN_PAGES["hub"](conn),
                     ADMIN_TABS, wide=True)
    if kind == "rop":
        # варианты «Таймингов» (недельное/месячное окно) в шапке подсвечивают
        # общую вкладку «Тайминги»
        tab = "taymingi" if page.startswith("taymingi") else page
        return shell(tab, "Контроль отдела", built, ROP_PAGES[page](conn),
                     ROP_TABS, wide=True)
    body = (page_queue(conn, uid, now) if page == "zayavki"
            else PAGES[page](conn, uid))
    tab = "kpi" if page.startswith("kpi") else page
    return shell(tab, mgr, built, body, wide=True)


# что собираем: kind -> [(страница, имя файла)]
PAGE_SET = {
    "all": {"admin": [("hub", "index.html")],
            "rop": [("menedzhery", "index.html"),
                    ("soobshcheniya", "soobshcheniya.html"),
                    ("etalon", "etalon.html"),
                    ("kachestvo", "kachestvo.html"),
                    ("zamery", "zamery.html"),
                    ("taymingi", "taymingi.html"),
                    ("taymingi-w", "taymingi-w.html"),
                    ("taymingi-m", "taymingi-m.html"),
                    ("visual", "visual.html"),
                    ("instruction", "instruction.html")],
            "manager": [("dashboard", "index.html"), ("skript", "skript.html"),
                        ("kpi", "kpi.html"), ("kpi-m", "kpi-m.html"),
                        ("kpi-w", "kpi-w.html"), ("kpi-d", "kpi-d.html")]},
    # «живые» страницы — их не жалко пересобирать каждую минуту
    "now": {"rop": [("menedzhery", "index.html"),
                    ("soobshcheniya", "soobshcheniya.html")],
            "manager": [("dashboard", "index.html")]},
}


def bundle(conn, mode):
    """Все страницы одним процессом, tar-потоком в stdout."""
    now = dt.datetime.now(VLD)
    built = now.strftime("%d.%m.%Y в %H:%M") + " (Влд)"
    toks = conn.execute(
        """SELECT t.token, t.kind, t.portal_user_id, m.name, m.last_name
             FROM dash_tokens t
             LEFT JOIN managers m ON m.portal_user_id = t.portal_user_id
            WHERE t.active ORDER BY t.kind""").fetchall()
    tf = tarfile.open(fileobj=sys.stdout.buffer, mode="w|")
    done = []
    for token, kind, uid, nm, ln in toks:
        mgr = human_name(nm, ln) or f"Менеджер {uid}"
        for page, fname in PAGE_SET[mode].get(kind, []):
            try:
                data = build_page(conn, kind, uid, mgr, page, now, built).encode()
            except Exception as exc:            # одна страница не валит сборку
                sys.stderr.write(f"ОШИБКА {kind}/{page} {token}: {exc}\n")
                continue
            info = tarfile.TarInfo(f"{token}/{fname}")
            info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
            tf.addfile(info, io.BytesIO(data))
            done.append(token)
    tf.close()
    if done:
        conn.execute("UPDATE dash_tokens SET built_at = now() WHERE token = ANY(%s)",
                     (list(set(done)),))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token")
    ap.add_argument("--page", default=None)
    ap.add_argument("--bundle", choices=sorted(PAGE_SET))
    args = ap.parse_args()

    conn = db()
    if args.bundle:
        bundle(conn, args.bundle)
        return

    if not args.token:
        sys.stderr.write("нужен --token или --bundle\n")
        sys.exit(2)
    tok = conn.execute(
        """SELECT t.portal_user_id, t.active, t.kind, m.name, m.last_name
             FROM dash_tokens t
             LEFT JOIN managers m ON m.portal_user_id = t.portal_user_id
            WHERE t.token = %s""", (args.token,)).fetchone()
    if not tok or not tok[1]:
        sys.stderr.write("токен не найден или выключен\n")
        sys.exit(2)
    uid, kind = tok[0], tok[2]
    now = dt.datetime.now(VLD)
    built = now.strftime("%d.%m.%Y в %H:%M") + " (Влд)"
    default = {"admin": "hub", "rop": "menedzhery"}.get(kind, "dashboard")
    mgr = human_name(tok[3], tok[4]) or f"Менеджер {uid}"
    sys.stdout.write(build_page(conn, kind, uid, mgr, args.page or default,
                                now, built))
    conn.execute("UPDATE dash_tokens SET built_at = now() WHERE token = %s",
                 (args.token,))


if __name__ == "__main__":
    main()
