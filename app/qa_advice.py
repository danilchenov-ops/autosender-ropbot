# -*- coding: utf-8 -*-
"""Недельный план работы по каждому менеджеру на основе QA-оценок.

Пересчитывается раз в неделю (крон в ночь на понедельник) и замораживается
в `qa_advice`: рекомендация — план тренировки, а не лента новостей, дёргаться
каждый день она не должна. Страница «Качество» только показывает готовое.

Запуск (в образ не запечён, подаётся через stdin):
    docker exec -i ropbot-collector-1 python - < /opt/ropbot/app/qa_advice.py
    ... python - --week 2026-08-24     # пересчитать конкретную неделю
    ... python - --print               # напечатать, ничего не писать
"""
import datetime as dt
import json
import re
import sys

from common import db, log

VLD = "Asia/Vladivostok"
WINDOW = 30          # окно оценок, дней
MIN_CALLS = 5        # ниже — рекомендации помечаются предварительными
MIN_OBJ = 4          # реже — категорию возражений не показываем

# категория модели (закрытый список в промте с 25.08 вечера) -> подпись
# OBJ_CATS; для старых разборов категории нет — работает регулярка
MCAT = {"цена": "цена и бюджет", "отложил": "отложенное решение",
        "утильсбор": "утильсбор и пошлины",
        "согласование": "согласование с близкими",
        "продать_свою": "сначала продать свою", "сроки": "сроки и доставка",
        "доверие": "доверие и риск", "курс": "курс и цены",
        "требования": "требования к машине"}

WORKING = ("SELECT portal_user_id FROM dash_tokens "
           "WHERE active AND kind = 'manager' AND portal_user_id IS NOT NULL")

# доля речи: норма из ТЗ; монолог — штрафной порог
TALK_LO, TALK_HI, MONO_OK = 0.40, 0.60, 90

METRICS = [
    ("a1", "Структура звонка", 0.25 / 4),
    ("a2", "Инициатива", 0.25 / 4),
    ("a3", "Доля речи", 0.25 / 4),
    ("a4", "Длина монолога", 0.25 / 4),
    ("b1", "Выявление потребности", 0.30 / 5),
    ("b2", "Презентация через выгоды", 0.30 / 5),
    ("b3", "Активное слушание", 0.30 / 5),
    ("b4", "Отработка возражений", 0.30 / 5),
    ("b5", "Чистота речи", 0.30 / 5),
    ("c1", "Попытка закрытия", 0.35 / 4),
    ("c2", "Качество следующего шага", 0.35 / 4),
    ("c3", "Квалификация", 0.35 / 4),
    ("c4", "Работа с ценой", 0.35 / 4),
    ("d1", "Подстройка под клиента", 0.10 / 3),
    ("d2", "Реакция на негатив", 0.10 / 3),
    ("d3", "Энергия и тон", 0.10 / 3),
]

# что именно делать. {v} — его значение, {t} — цель (лучший в отделе)
ADVICE = {
    "a1": ("Держать порядок звонка: поздороваться и назвать причину, выяснить "
           "задачу, только потом считать и предлагать, в конце — договорённость. "
           "Сейчас {v} из 10, у лучшего в отделе {t}."),
    "a2": ("Вести разговор, а не отвечать на вопросы как справочная: после "
           "каждого своего ответа задавать встречный вопрос. Сейчас {v}, "
           "в отделе доходит до {t}."),
    "b1": ("Больше открытых вопросов ДО расчёта: для чего машина, что смотрел "
           "раньше, что важно кроме цены. Сейчас {v} из 10, ориентир {t}."),
    "b2": ("Говорить выгодами под слова клиента, а не списком преимуществ: "
           "он сказал «нужен полный привод» — отвечать про это, а не про "
           "компанию. Сейчас {v}, ориентир {t}."),
    "b3": ("Возвращаться к словам клиента и резюмировать в конце: «правильно "
           "понял — до 2,5 млн, полный привод, к ноябрю». Сейчас {v}, "
           "ориентир {t}."),
    "b4": ("Не сдаваться после первого «дорого» или «подумаю»: минимум одна "
           "попытка отработки — уточняющий вопрос, обоснование, перенос "
           "на дату. Сейчас {v}, ориентир {t}."),
    "b5": ("Убрать слова-паразиты и неуверенные формулировки: «как бы», "
           "«наверное», «попробуем», «я думаю». Сейчас {v}, ориентир {t}."),
    "c1": ("В каждом разговоре явно предлагать следующий шаг — не «будем на "
           "связи», а конкретное действие. Сейчас {v}, ориентир {t}."),
    "c2": ("Договорённость должна быть с датой, временем и обязательством "
           "клиента: «в четверг в 15:00 созвонимся, вы до этого скинете "
           "документы». Сейчас {v}, ориентир {t}."),
    "c3": ("Выяснять до расчёта: кто ещё участвует в решении, бюджет и срок "
           "покупки. Сейчас {v}, ориентир {t}."),
    "c4": ("Цену называть с обоснованием — что входит в стоимость под ключ, — "
           "и не предлагать скидку первым. Сейчас {v}, ориентир {t}."),
    "d1": ("Подстраиваться под темп клиента: торопится — короче и по делу, "
           "рассуждает — не перебивать. Сейчас {v}, ориентир {t}."),
    "d2": ("На недовольство не оправдываться и не спорить, а переводить "
           "в решение: что сделаем и когда. Сейчас {v}, ориентир {t}."),
    "d3": ("Держать тон до конца разговора, особенно в финале: последние "
           "тридцать секунд клиент запоминает лучше всего. Сейчас {v}, "
           "ориентир {t}."),
}

OBJ_CATS = [
    ("цена и бюджет", r"дорог|бюджет|денег|дешевл|цена|стоимост|дорож",
     "Цена: сначала выяснить, с чем сравнивают, потом развернуть, что входит "
     "в стоимость под ключ, — и только потом обсуждать сумму."),
    ("отложенное решение", r"подума|думаю|думает|перезвон|позже|не готов|"
     r"прицен|смотрю|определил",
     "«Подумаю»: спросить, что именно взвешивает, и закрыть на дату — "
     "«давайте в четверг вернёмся, к тому времени пришлю подборку»."),
    ("утильсбор и пошлины", r"утиль|растаможк|пошлин",
     "Утильсбор: показывать расчёт «под ключ» с разбивкой, чтобы цифра "
     "не выглядела внезапной надбавкой."),
    ("согласование с близкими", r"посовет|жен|муж|супруг|семь|родител",
     "Согласование: предложить созвон втроём или отправить короткое резюме "
     "для того, кто участвует в решении."),
    ("сначала продать свою", r"продать|свою машину|старую",
     "Продажа своей машины: предложить параллельный сценарий — бронь и "
     "сроки торгов, чтобы человек не выпал на месяц."),
    ("сроки и доставка", r"срок|долго|ждать|ожидан|доставк",
     "Сроки: называть конкретные даты этапов, а не «примерно полтора месяца»."),
    ("доверие и риск", r"довер|обман|предоплат|риск|гарант|мошен|боится|"
     r"договор|санкц",
     "Доверие: проговаривать безопасность до того, как о ней спросят — "
     "договор, офис, видео с площадки. По нашим данным это самое частое "
     "возражение у выигранных сделок."),
    ("курс и цены", r"курс|подорожа|выросл|цены на сайте",
     "Курс: объяснять, как фиксируется цена и что меняется до момента торгов."),
    ("требования к машине", r"руль|клиренс|привод|вариатор|пробег|кузов",
     "Требования к машине: если не подходит конкретный лот — сразу "
     "предлагать альтернативу под названный критерий, а не заканчивать разговор."),
]

Q_AGG = f"""
  SELECT q.portal_user_id AS uid, count(*) AS n,
         avg(q.integral) AS integral, stddev_samp(q.integral) AS sd,
         avg(q.talk_share) AS talk_share, avg(q.max_mono_sec) AS mono,
         100.0 * count(*) FILTER (WHERE q.c2 >= 4)
               / NULLIF(count(*) FILTER (WHERE q.c2 IS NOT NULL), 0) AS conv,
         {", ".join(f"avg(q.{k}) AS {k}" for k, _t, _w in METRICS)}
    FROM call_qa q
   WHERE q.call_start >= %(since)s AND q.call_start < %(until)s
     AND q.portal_user_id IN ({WORKING})
   GROUP BY 1
"""

Q_OBJ = f"""
  SELECT c.portal_user_id AS uid, lower(x.obj) AS obj,
         lower(coalesce(s.raw->'objection_cats'->>(x.i - 1)::int, '')) AS mcat,
         coalesce(s.objections_handled, false) AS handled
    FROM call_scores s
    JOIN calls c ON c.id = s.call_id
    CROSS JOIN LATERAL unnest(s.objections) WITH ORDINALITY AS x(obj, i)
   WHERE c.call_start >= %(since)s AND c.call_start < %(until)s
     AND c.portal_user_id IN ({WORKING})
     AND s.outcome NOT IN ('не дозвонились', 'нецелевой')
"""

Q_EDGE = f"""
  SELECT portal_user_id AS uid, kind, call_id, integral, quotes, crm_id FROM (
    SELECT q.portal_user_id, q.call_id, q.integral, q.quotes,
           c.crm_entity_id AS crm_id,
           row_number() OVER (PARTITION BY q.portal_user_id
                              ORDER BY q.integral DESC NULLS LAST) AS hi,
           row_number() OVER (PARTITION BY q.portal_user_id
                              ORDER BY q.integral ASC NULLS LAST) AS lo
      FROM call_qa q JOIN calls c ON c.id = q.call_id
     WHERE q.call_start >= %(since)s AND q.call_start < %(until)s
       AND q.portal_user_id IN ({WORKING}) AND q.integral IS NOT NULL) t
  CROSS JOIN LATERAL (SELECT CASE WHEN hi = 1 THEN 'best'
                                  WHEN lo = 1 THEN 'worst' END AS kind) k
   WHERE k.kind IS NOT NULL
"""


def fnum(v, d=1):
    if v is None:
        return "—"
    return f"{float(v):.{d}f}".replace(".", ",")


def talk_advice(share, mono):
    """Явное направление: увеличить или уменьшить, на сколько и зачем."""
    if share is None:
        return None
    pct = round(share * 100)
    bits = []
    if share > TALK_HI:
        bits.append(
            f"Говорит {pct}% времени — это много: норма 40–60%, то есть долю "
            f"своей речи надо **снижать** примерно до 60%. Практика: после "
            f"каждого своего блока — вопрос клиенту и пауза."
        )
    elif share < TALK_LO:
        bits.append(
            f"Говорит всего {pct}% времени — это мало: клиент ведёт разговор "
            f"сам, менеджер работает справочной. Долю речи надо **повышать** "
            f"к 40–60%: вести к следующему шагу, а не только отвечать."
        )
    else:
        bits.append(f"Доля речи {pct}% — в норме (40–60%), менять не нужно.")
    if mono is not None:
        m = round(mono)
        if m > MONO_OK:
            bits.append(
                f"Самый длинный монолог в среднем {m} секунд при пороге 90 — "
                f"**сокращать**: делить на куски по полминуты и проверять "
                f"вопросом «как вам такой вариант?»."
            )
        else:
            bits.append(f"Монологи короткие (до {m} секунд) — это хорошо.")
    return " ".join(bits)


def build(conn, week_start, since, until, do_print=False):
    names = {r[0]: (r[1] or "").strip() + " " + (r[2] or "").strip()
             for r in conn.execute(f"""
        SELECT m.portal_user_id, m.name, m.last_name FROM managers m
         WHERE m.portal_user_id IN ({WORKING})""").fetchall()}
    # внутренний код «М7» из имени убираем
    for uid, nm in list(names.items()):
        names[uid] = " ".join(p for p in nm.split()
                              if not (len(p) <= 3 and p[:1] == "М")) or nm

    cur = conn.execute(Q_AGG, dict(since=since, until=until))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    if not rows:
        log.info("QA-план: оценок за окно нет, нечего считать")
        return 0
    for r in rows:
        for c in cols:
            if c not in ("uid", "n") and r[c] is not None:
                r[c] = float(r[c])

    # цель по метрике — лучший в отделе среди тех, у кого выборка не крошечная
    solid = [r for r in rows if r["n"] >= MIN_CALLS] or rows
    best = {k: max((r[k] for r in solid if r.get(k) is not None), default=None)
            for k, _t, _w in METRICS}
    dept = {}
    for k, _t, _w in METRICS:
        vals = [(r[k], r["n"]) for r in rows if r.get(k) is not None]
        dept[k] = (sum(v * n for v, n in vals) / sum(n for _v, n in vals)
                   if vals else None)

    # возражения
    objs = {}
    for uid, obj, mcat, handled in conn.execute(Q_OBJ, dict(since=since,
                                                            until=until)):
        objs.setdefault(uid, []).append((obj, MCAT.get(mcat), handled))

    edges = {}
    for uid, kind, call_id, integral, quotes, crm in conn.execute(
            Q_EDGE, dict(since=since, until=until)):
        edges[(uid, kind)] = dict(call_id=call_id, integral=float(integral),
                                  quotes=quotes or {}, crm=crm)

    written = 0
    for r in rows:
        uid = r["uid"]
        gaps = []
        for k, title, w in METRICS:
            if k in ("a3", "a4"):
                continue     # доля речи и монолог разобраны отдельным блоком
            v, t = r.get(k), best.get(k)
            if v is None or t is None or t - v < 0.4:
                continue
            gaps.append(dict(code=k.upper(), title=title, v=v, target=t,
                             dept=dept.get(k), weight=w,
                             score=(t - v) * w))
        gaps.sort(key=lambda g: -g["score"])

        strong = []
        for k, title, _w in METRICS:
            v, d = r.get(k), dept.get(k)
            if v is None or d is None or v - d < 0.4:
                continue
            strong.append(dict(code=k.upper(), title=title, v=v, dept=d,
                               diff=v - d))
        strong.sort(key=lambda s: -s["diff"])

        # возражения: категории и конкретные неотработанные формулировки
        mine = objs.get(uid, [])
        cats = []
        # (re импортирован сверху)
        for label, rx, tip in OBJ_CATS:
            hit = [h for o, mc, h in mine
                   if (mc == label if mc else re.search(rx, o))]
            if len(hit) < MIN_OBJ:
                continue
            ok = round(100 * sum(1 for h in hit if h) / len(hit))
            cats.append(dict(label=label, n=len(hit), handled=ok, tip=tip))
        cats.sort(key=lambda c: (c["handled"], -c["n"]))
        raw_bad = {}
        for o, _mc, h in mine:
            if not h:
                raw_bad[o] = raw_bad.get(o, 0) + 1
        top_bad = sorted(raw_bad.items(), key=lambda x: -x[1])[:4]

        payload = dict(
            name=names.get(uid, str(uid)),
            n=r["n"],
            integral=r["integral"], sd=r["sd"], conv=r["conv"],
            talk_share=r["talk_share"], mono=r["mono"],
            talk_text=talk_advice(r["talk_share"], r["mono"]),
            thin=r["n"] < MIN_CALLS,
            blocks={b: r.get(b) for b in ("a1", "b4", "c2")},
            gaps=[dict(g, text=ADVICE.get(g["code"].lower(), "").format(
                v=fnum(g["v"]), t=fnum(g["target"]))) for g in gaps[:3]],
            strong=[dict(s) for s in strong[:3]],
            obj_cats=cats[:3],
            obj_bad=[dict(text=o, n=n) for o, n in top_bad],
            best=edges.get((uid, "best")) and dict(
                integral=edges[(uid, "best")]["integral"],
                crm=edges[(uid, "best")]["crm"],
                quotes=edges[(uid, "best")]["quotes"]),
            worst=edges.get((uid, "worst")) and dict(
                integral=edges[(uid, "worst")]["integral"],
                crm=edges[(uid, "worst")]["crm"],
                quotes=edges[(uid, "worst")]["quotes"]),
        )
        if do_print:
            print(json.dumps({payload["name"]: payload}, ensure_ascii=False,
                             indent=2, default=str)[:2000])
            continue
        conn.execute(
            """INSERT INTO qa_advice (week_start, portal_user_id, payload,
                                      n_calls, window_days)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (week_start, portal_user_id) DO UPDATE
                 SET payload = EXCLUDED.payload, n_calls = EXCLUDED.n_calls,
                     window_days = EXCLUDED.window_days, created_at = now()""",
            (week_start, uid, json.dumps(payload, ensure_ascii=False,
                                         default=str), r["n"], WINDOW))
        written += 1
    log.info("QA-план на неделю %s: записано %s менеджеров", week_start, written)
    return written


def main():
    args = sys.argv[1:]
    with db() as conn:
        today = conn.execute(
            f"SELECT (now() AT TIME ZONE '{VLD}')::date").fetchone()[0]
        if "--week" in args:
            week = dt.date.fromisoformat(args[args.index("--week") + 1])
        else:
            week = today - dt.timedelta(days=today.weekday())
        since = conn.execute(
            "SELECT (%s::date - %s * interval '1 day')", (week, WINDOW)
        ).fetchone()[0]
        until = conn.execute("SELECT (%s::date + interval '7 day')",
                             (week,)).fetchone()[0]
        build(conn, week, since, until, do_print="--print" in args)


if __name__ == "__main__":
    main()
