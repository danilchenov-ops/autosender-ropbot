"""Два разговора Светлане каждое утро: лучший и худший по баллу эталонного скрипта.

Окно выборки — прошлая и текущая недели (от понедельника прошлой недели, время Влд).
Ранжирование — `call_scores.card_score` (app/card.py), 0-100. Каждый разговор уходит
в Телеграм ровно один раз: отправленные помечаются в `tg_sent`
(`kind='call_best'` / `'call_worst'`), повторно не выбираются никогда.

Запуск (код в образ не запечён, подаётся через stdin — как web/dash.py):
    docker exec -i ropbot-collector-1 python - < /opt/ropbot/app/bestworst.py
    ... python - --test   — только Тимофею, отметка «отправлено» на его чат
    ... python - --dry    — напечатать текст, ничего не слать
"""
import datetime as dt
import re
import sys

import tg
from card import CRITERIA, levels
from common import db, log

TZ = "Asia/Vladivostok"
VLD = dt.timezone(dt.timedelta(hours=10))
B24 = "https://synergosmoto.bitrix24.ru"
BOSS_CHAT = 7487296664   # Светлана
TEST_CHAT = 460128042    # Тимофей
MIN_SEC = 120            # короче — не разговор, а справка; разбирать нечего
SUM_LIMIT = 420          # обрезка резюме, чтобы письмо влезало целиком
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

# Рабочий состав отдела — тот же признак, что на панелях менеджеров
WORKING = ("SELECT portal_user_id FROM dash_tokens "
           "WHERE active AND kind = 'manager' AND portal_user_id IS NOT NULL")

POOL = f"""
  SELECT c.id, (c.call_start AT TIME ZONE %(tz)s) AS ts, c.duration, c.direction,
         c.crm_entity_type, c.crm_entity_id, s.card_score, s.raw,
         s.next_step, s.summary, m.name, m.last_name
    FROM call_scores s
    JOIN calls c     ON c.id = s.call_id
    LEFT JOIN managers m ON m.portal_user_id = c.portal_user_id
   WHERE s.card_score IS NOT NULL AND s.raw IS NOT NULL
     AND c.portal_user_id IN ({WORKING})
     AND coalesce(c.phone_kind, '') NOT IN ('junk', 'none')
     AND c.duration >= %(min_sec)s
     AND c.crm_entity_id IS NOT NULL
     AND coalesce(s.outcome, '') <> 'нецелевой'
     AND (c.call_start AT TIME ZONE %(tz)s) >= %(frm)s
     AND c.id NOT IN (SELECT ref_id FROM tg_sent
                       WHERE chat_id = %(chat)s AND ok
                         AND kind IN ('call_best', 'call_worst'))
     AND (%(ex)s::bigint IS NULL OR c.id <> %(ex)s)
   ORDER BY s.card_score {{dir}}, c.duration DESC, c.call_start DESC
   LIMIT 1
"""
COLS = ("id ts duration direction crm_entity_type crm_entity_id card_score raw "
        "next_step summary name last_name").split()


def clean(s, limit=None):
    """Markdown v1 ломается на непарных * _ [ ] ` — вырезаем их из живого текста."""
    s = re.sub(r"[*_\[\]`]", "", str(s or "")).strip()
    if limit and len(s) > limit:
        s = s[: limit - 1].rsplit(" ", 1)[0] + "…"
    return s


def human_name(name, last):
    """«М7 Дмитрий» + «Пономарь» -> «Дмитрий Пономарь»: внутренний код убираем."""
    parts = [p for p in (name or "").split() if not (len(p) <= 3 and p[:1] == "М")]
    if last and last not in parts:
        parts.append(last)
    return clean(" ".join(parts) or (name or "") or "менеджер не определён")


def ru_date(d):
    return f"{d.day} {MONTHS[d.month - 1]}"


def plural(n, one, few, many):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def mmss(sec):
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


def crm_link(kind, eid):
    path = {"LEAD": "lead", "DEAL": "deal", "CONTACT": "contact",
            "COMPANY": "company"}.get(kind or "")
    return f"{B24}/crm/{path}/details/{eid}/" if path and eid else None


def window_start(today):
    """Понедельник прошлой недели, 00:00 по Владивостоку."""
    monday = today - dt.timedelta(days=today.weekday())
    return dt.datetime.combine(monday - dt.timedelta(days=7), dt.time.min)


def pick(conn, chat, frm, order, exclude=None):
    params = {"tz": TZ, "frm": frm, "chat": chat, "min_sec": MIN_SEC, "ex": exclude}
    row = conn.execute(POOL.format(dir=order), params).fetchone()
    return dict(zip(COLS, row)) if row else None


def block(r, title, mark):
    lv = levels(r["raw"])
    got = [t for k, (w, t) in CRITERIA.items() if lv[k] == 2]
    miss = [t for k, (w, t) in CRITERIA.items() if lv[k] == 0]
    ts = r["ts"]
    score = float(r["card_score"])
    p = [f'{mark} *{title} — {score:.0f} {plural(score, "балл", "балла", "баллов")}*',
         f'{human_name(r["name"], r["last_name"])} · {ru_date(ts.date())}, '
         f'{ts.strftime("%H:%M")} · {mmss(r["duration"])} · '
         f'{"исходящий" if r["direction"] == "out" else "входящий"}']
    if got:
        p.append("✓ есть: " + clean(", ".join(t.lower() for t in got)))
    if miss:
        p.append("✗ нет: " + clean(", ".join(t.lower() for t in miss)))
    if r["next_step"]:
        p.append("Следующий шаг: " + clean(r["next_step"], 200))
    if r["summary"]:
        p.append("О чём: " + clean(r["summary"], SUM_LIMIT))
    link = crm_link(r["crm_entity_type"], r["crm_entity_id"])
    if link:
        p.append(f"[Карточка в Битриксе]({link}) — запись разговора там же, в ленте")
    return "\n".join(p)


def build(conn, chat):
    today = dt.datetime.now(VLD).date()
    frm = window_start(today)
    best = pick(conn, chat, frm, "DESC")
    worst = pick(conn, chat, frm, "ASC", exclude=best["id"] if best else None)
    if not best or not worst:
        return None, None, None
    head = (f'*Два разговора на послушать — {ru_date(today)}*\n'
            f'Выборка с {ru_date(frm.date())}: прошлая и текущая недели, '
            f'балл по эталонному скрипту. Разговор, который уже присылали, '
            f'второй раз не приходит.')
    text = "\n\n".join([head, block(best, "Лучший", "👍"),
                        block(worst, "Худший", "👎"),
                        "_Балл — про то, как вёлся разговор, а не про исход сделки. "
                        "Для планёрки, не для премии._"])
    return text, best, worst


def main():
    test = "--test" in sys.argv
    dry = "--dry" in sys.argv
    chat = TEST_CHAT if test else BOSS_CHAT
    with db() as conn:
        text, best, worst = build(conn, chat)
        if not text:
            log.info("лучший/худший: новых разговоров в окне нет — не отправляем")
            return
        if dry:
            print(text)
            return
        try:
            tg.send(chat, text, preview=False)
            ok, err = True, None
        except Exception as exc:  # noqa: BLE001
            ok, err = False, str(exc)[:300]
            log.error("лучший/худший → %s: %s", chat, err)
        for kind, r in (("call_best", best), ("call_worst", worst)):
            conn.execute("INSERT INTO tg_sent (chat_id, kind, ref_id, ok, error) "
                         "VALUES (%s, %s, %s, %s, %s)", (chat, kind, r["id"], ok, err))
        log.info("лучший/худший → %s: %s / %s, ok=%s", chat, best["id"], worst["id"], ok)


if __name__ == "__main__":
    main()
