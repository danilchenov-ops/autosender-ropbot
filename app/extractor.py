"""Разбор расшифровок языковой моделью: договорённости, бюджет, автомобиль, город.

Ничего никому не отправляет — только пишет в базу.
Запуск как сервис: python extractor.py
Разовый прогон:   python extractor.py --once
"""
import json
import os
import re
import sys
import time
import datetime as dt

import requests

import b24_summary
import card
import qa
from common import cfg, db, log
from migrate import migrate

KEY = os.getenv("PROXYAPI_KEY", "")
BASE = "https://api.proxyapi.ru"
MODEL = os.getenv("LLM_MODEL", "gemini-3.5-flash-lite")
MIN_SEC = int(os.getenv("LLM_MIN_SEC", "90"))
SINCE_DAYS = int(os.getenv("LLM_SINCE_DAYS", "21"))
CONF_MIN = float(os.getenv("LLM_CONF_MIN", "0.6"))
DAILY_LIMIT = int(os.getenv("LLM_DAILY_LIMIT", "600"))
# «сейчас» и «завтра» — это операционная суета, а не договорённость.
# Напоминание имеет смысл, только если срок отстоит от разговора хотя бы на столько дней.
MIN_HORIZON = int(os.getenv("FOLLOWUP_MIN_DAYS", "2"))
IDLE_SLEEP = int(os.getenv("LLM_IDLE_SLEEP", "120"))

PHONE = re.compile(r"(?:\+?[78][\s\-(]*)?9\d{2}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
LONGNUM = re.compile(r"\b\d{10,}\b")
EMAIL = re.compile(r"\b[\w.\-]+@[\w\-]+\.\w+\b")


def anonymize(text):
    text = PHONE.sub("[телефон]", text)
    text = LONGNUM.sub("[номер]", text)
    return EMAIL.sub("[почта]", text)


PROMPT = """Ты разбираешь расшифровку телефонного разговора менеджера автосалона с клиентом.
Запись одноканальная, реплики не разделены по говорящим — определяй роли по смыслу.

Дата и время разговора: {when} (часовой пояс Владивосток).

Задача 1 — понять, договорились ли о следующем контакте, и когда именно.

Различай:
— НАСТОЯЩАЯ договорённость: назван срок или условие («наберите в четверг», «перезвоните
  через неделю», «свяжемся после 20-го», «завтра пришлю расчёт»).
— НЕ договорённость: вежливая отговорка без конкретики («я сам вам перезвоню»,
  «подумаю», «если что наберу», «будем на связи»), обычное прощание.
Сомневаешься — ставь agreement=false и низкую уверенность.

Задача 2 — оценить работу менеджера по чек-листу. Оценивай только то, что слышно
в разговоре, без домыслов. Если разговор не состоялся (недозвон, не туда попали,
секретарь) — outcome="не дозвонились", остальные поля false/null, score=null.

Чек-лист:
— потребность выяснена: менеджер задал вопросы о задаче клиента (что ищет, для чего,
  бюджет, сроки, что смотрел раньше), а не сразу пошёл рассказывать;
— цена названа: прозвучала конкретная сумма или вилка по предмету разговора;
— возражения: перечисли возражения клиента своими словами короткими фразами
  («дорого», «подумаю», «есть дешевле», «нужно посоветоваться»); отработаны ли они —
  менеджер ответил по сути, а не согласился и попрощался;
  к КАЖДОМУ возражению добавь категорию из закрытого списка (поле objection_cats,
  массив той же длины и в том же порядке): "цена" (дорого, бюджет, дешевле),
  "отложил" (подумаю, позже, не готов), "утильсбор" (утильсбор, пошлины,
  растаможка), "согласование" (жена, муж, семья, посоветоваться),
  "продать_свою" (сначала продать свою машину), "сроки" (долго, доставка),
  "доверие" (предоплата, риск, обман, договор), "курс" (курс, подорожало),
  "требования" (руль, привод, пробег, кузов не подходит), "другое";
— следующий шаг: что конкретно дальше и назначен ли срок;
— доля речи менеджера: на глаз, от 0 до 1 (0.5 — говорили поровну);
— вопросов менеджера: сколько содержательных вопросов он задал клиенту;
— score 0–10: 10 — потребность выяснена, цена названа, возражения отработаны,
  назначен следующий шаг с датой; 0 — менеджер не сделал ничего из этого.

Задача 3 — карточка эталонного скрипта. Пять признаков, которые на разборе
386 расшифровок отличили выигранные разговоры от проигранных. Отвечай только
по тому, что слышно; чего не было — false.

— next_step_proposed: менеджер предложил конкретное следующее действие
  (не «будем на связи», а «пришлю подборку», «жду документы»);
— next_step_specific: названо, что делает клиент И что делает менеджер
  («жду от вас паспорт, и я ставлю машину в бронь»). Одностороннее обещание — false;
— date_time_fixed: названы дата и время следующего действия, а не «на днях»;
— decision_maker_identified: выяснено, кто ещё участвует в решении о покупке
  (жена, партнёр, руководство) или что клиент решает один;
— timeline_discussed: выяснено, когда клиент планирует покупать;
— linked_to_client_pain: выгода привязана к тому, что клиент сам сказал,
  а не общий рассказ о компании;
— value_props_n: сколько разных выгод менеджер назвал.

Задача 4 — портрет разговора: кто кого ведёт. Отвечай только по услышанному,
к полям с цитатой давай ТОЧНУЮ короткую цитату из расшифровки (до 15 слов).

— questions_client: сколько содержательных вопросов задал КЛИЕНТ;
— next_step_by: кто первым предложил следующий шаг — "менеджер" / "клиент" /
  null, если шага не было;
— call_ended_by: кто свернул разговор к прощанию — "менеджер" / "клиент";
— concession: уступка менеджера. "бесплатно" — уступил без встречного
  обязательства («просто пришлю расчёт», «звоните, если что», скидка без
  условия); "обмен" — уступка с обязательством клиента («пришлю расчёт —
  и созвонимся в четверг»); "нет" — уступок не было;
— concession_quote: цитата уступки или null;
— first_pushback: прозвучал ли от клиента первый отказ/сомнение
  («дорого», «подумаю», «я сам перезвоню», «посоветуюсь»);
— first_pushback_handled: менеджер сделал хотя бы одну попытку отработки
  (вопрос, аргумент, перенос на дату), а не согласился и попрощался;
— first_pushback_quote: цитата реакции менеджера или null;
— qualified_before_price: ДО называния цены выяснены хотя бы два из трёх —
  срок покупки, бюджет, конкретный автомобиль;
— price_justified: цена названа с обоснованием, что в неё входит,
  а не голым числом;
— safety_proactive: менеджер сам, без вопроса клиента, заговорил о надёжности
  сделки (договор, офис, видео, реквизиты, история компании);
— budget_q: как установлен бюджет клиента, ровно одно значение.
  "manager_asked" — менеджер САМ спросил про бюджет клиента («на какой бюджет
  ориентируетесь», «в какие деньги хотите уложиться», «в рамках какого
  бюджета»). Ставь и тогда, когда вопрос прозвучал в середине или конце
  разговора, уже после названных клиентом сумм, и когда клиент не ответил
  внятно — важен факт вопроса менеджера.
  "client_stated" — менеджер не спрашивал, но клиент сам обозначил свою
  денежную рамку на покупку («мне нужен автомобиль за 1 500 000», «миллион
  восемьсот у меня бюджет», «рассчитываю в пределах 700 тысяч»).
  "no" — всё остальное: клиент называет цену конкретной машины с сайта или
  из объявления («увидел фит за 400 тысяч у вас на сайте»); любые суммы,
  которые называет сам менеджер (оценка, доставка, растаможка, разница
  в цене); обсуждение цены конкретного лота. Формулировка двусмысленная —
  ставь "no";
— timeline_asked: менеджер САМ спросил, когда клиент планирует покупку.
  Любая форма, включая прошедшее время («вы когда планировали покупкой
  заниматься», «в какие сроки рассматриваете»). false — если срок назвал
  клиент без вопроса менеджера или срок не обсуждался;
— safety_shown: менеджер предложил клиенту визуально убедиться в реальности
  компании — онлайн-трансляцию офиса или камеры («у нас ведётся прямая
  трансляция, можете зайти посмотреть, как работаем»), либо созвон
  по видеосвязи, чтобы показать себя, офис, стоянку. Предложение прислать
  видеообзор или фото самой машины — это НЕ safety_shown, ставь false.
  Обсуждение мессенджеров ради отправки подборки — тоже false. Если тему
  поднял сам клиент, а менеджер лишь согласился — false.

Задача 5 — оценка качества звонка как у QA-аналитика отдела продаж.
Пятнадцать метрик, каждая — целое число от 0 до 10 И короткая цитата-доказательство
из разговора (дословно, до 12 слов). Балл без опоры на конкретный фрагмент не ставь.
Если этап объективно не был нужен (клиент сам всё решил, звонок технический) —
вместо числа поставь "na".

A1 соблюдение этапов: приветствие → потребность → презентация → возражения → закрытие
A2 инициатива: менеджер ведёт разговор, а не работает справочной
B1 глубина выявления потребности: открытые вопросы ДО презентации, их качество
B2 презентация через выгоды клиента, а не перечисление характеристик
B3 активное слушание: возвращается к словам клиента, уточняет, резюмирует в конце
B4 отработка возражений: минимум одна-две попытки, не сдаётся после первого
   «дорого»/«подумаю»
B5 чистота речи. Считай строго: 10 — ни одного паразита и ни одной
   неуверенной формулировки за весь разговор; каждый повторяющийся паразит
   («как бы», «ну», «вот», «наверное», «попробуем», «я думаю») минус 1–2;
   перебивание клиента минус 2. Средний разговор должен получать 4–6, не 7
C1 попытка закрытия: была ли явная попытка договориться о следующем шаге
C2 качество следующего шага: 10 — продвижение (конкретная дата и обязательство
   клиента), 4 — размытое «созвонимся», 0 — шага нет
C3 квалификация: выяснены ли кто принимает решение, бюджет, сроки
C4 работа с ценой: обосновывает ценность, не предлагает скидку первым
D1 подстройка под темп и стиль клиента. Строго: 10 — только если видно
   явное зеркалирование (клиент торопится — менеджер сжался; клиент
   рассуждает — менеджер даёт место); просто вежливый ровный тон — это 5
D2 реакция на негатив: без оправданий и споров, перевод в решение
D3 энергия и тон на всём протяжении звонка, включая финал

Отдельно верни roles — строку РОВНО из {n_lines} символов, по одному на каждую
пронумерованную строку расшифровки по порядку: М — говорит менеджер, К — клиент.
Без пробелов и переносов. По этой строке мы сами считаем долю речи.

Верни СТРОГО JSON:
{{
  "agreement": true/false,
  "due_date": "ГГГГ-ММ-ДД" или null,
  "date_is_approximate": true/false,
  "phrase": "как срок назван словами" или null,
  "quote": "точная цитата" или null,
  "initiator": "клиент" / "менеджер" / null,
  "promised_by_manager": "что менеджер обещал сделать" или null,
  "confidence": число от 0 до 1,
  "budget_rub": число или null,
  "car": "марка и модель" или null,
  "city": "город клиента" или null,
  "need_identified": true/false,
  "questions_manager": число,
  "talk_share": число от 0 до 1,
  "price_named": true/false,
  "objections": ["строка", ...],
  "objection_cats": ["категория", ...],
  "objections_handled": true/false,
  "next_step": "что дальше" или null,
  "next_step_dated": true/false,
  "outcome": "договорились" / "думает" / "отказ" / "не дозвонились" / "нецелевой" / "другое",
  "score": число 0-10 или null,
  "next_step_proposed": true/false,
  "next_step_specific": true/false,
  "date_time_fixed": true/false,
  "decision_maker_identified": true/false,
  "timeline_discussed": true/false,
  "linked_to_client_pain": true/false,
  "value_props_n": число,
  "questions_client": число,
  "next_step_by": "менеджер" / "клиент" / null,
  "call_ended_by": "менеджер" / "клиент",
  "concession": "нет" / "обмен" / "бесплатно",
  "concession_quote": "цитата" или null,
  "first_pushback": true/false,
  "first_pushback_handled": true/false,
  "first_pushback_quote": "цитата" или null,
  "qualified_before_price": true/false,
  "price_justified": true/false,
  "safety_proactive": true/false,
  "budget_q": "manager_asked" / "client_stated" / "no",
  "timeline_asked": true/false,
  "safety_shown": true/false,
  "summary": "одно предложение о чём разговор",
  "roles": "МКМКМ...",
  "qa": {{
    "A1": [балл, "цитата"], "A2": [балл, "цитата"],
    "B1": [балл, "цитата"], "B2": [балл, "цитата"], "B3": [балл, "цитата"],
    "B4": [балл, "цитата"], "B5": [балл, "цитата"],
    "C1": [балл, "цитата"], "C2": [балл, "цитата"], "C3": [балл, "цитата"],
    "C4": [балл, "цитата"],
    "D1": [балл, "цитата"], "D2": [балл, "цитата"], "D3": [балл, "цитата"]
  }}
}}

Расшифровка:

{text}"""


# Модели с «размышлениями» (3.7-flash, pro) думают в счёт maxOutputTokens и
# без отключения дают пустой ответ или в 6 раз дороже. Бенч 25.08 (реестр)
# и ревизия эталонного скрипта 22.08: thinkingBudget=0 не роняет цитатность.
THINKING_OFF = ("3.7-flash", "pro-preview")


def ask(prompt):
    cfg = {"responseMimeType": "application/json", "maxOutputTokens": 3500}
    if any(t in MODEL for t in THINKING_OFF):
        cfg["thinkingConfig"] = {"thinkingBudget": 0}
    r = requests.post(
        f"{BASE}/google/v1beta/models/{MODEL}:generateContent",
        headers={"Authorization": f"Bearer {KEY}"},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": cfg},
        timeout=180,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usageMetadata") or {}
    raw = "".join(p.get("text", "") for p in d["candidates"][0]["content"]["parts"])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError(f"не JSON: {raw[:200]}")
        data = json.loads(m.group(0))
    return data, u.get("promptTokenCount") or 0, u.get("candidatesTokenCount") or 0


def num(v):
    try:
        return float(v) if v not in (None, "", False) else None
    except (TypeError, ValueError):
        return None


def parse_date(v):
    if not v:
        return None
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def enqueue(conn):
    """Ставим в очередь расшифровки, которых ещё не разбирали."""
    conn.execute(
        """INSERT INTO llm_queue (call_id)
           SELECT t.call_id FROM transcripts t
           JOIN calls c ON c.id = t.call_id
           WHERE c.duration >= %s
             AND c.call_start > now() - (%s || ' days')::interval
             AND length(t.text) > 300
           ON CONFLICT (call_id) DO NOTHING""",
        (MIN_SEC, SINCE_DAYS),
    )


def spent_today(conn):
    return conn.execute(
        "SELECT count(*) FROM call_extractions WHERE created_at::date = current_date"
    ).fetchone()[0]


def take(conn):
    conn.execute(
        """UPDATE llm_queue SET status='pending', updated_at=now()
           WHERE status='processing' AND updated_at < now() - interval '30 minutes'"""
    )
    row = conn.execute(
        """UPDATE llm_queue q SET status='processing', updated_at=now()
           WHERE q.call_id = (
             SELECT q2.call_id FROM llm_queue q2
             JOIN calls c ON c.id = q2.call_id
             WHERE q2.status='pending' AND q2.attempts < 3
             ORDER BY c.call_start DESC LIMIT 1 FOR UPDATE OF q2 SKIP LOCKED)
           RETURNING q.call_id"""
    ).fetchone()
    if not row:
        return None
    return conn.execute(
        """SELECT c.id, c.call_start, c.duration, c.portal_user_id,
                  c.crm_entity_type, c.crm_entity_id, c.phone_number, t.text
           FROM calls c JOIN transcripts t ON t.call_id = c.id WHERE c.id = %s""",
        (row[0],),
    ).fetchone()


def process(conn, task):
    call_id, when, duration, user_id, ent_type, ent_id, phone, text = task
    # Расшифровку подаём СТРОГО построчно — по одной реплике на строку: модель
    # возвращает роли (М/К) по строкам, и их число должно совпадать с нашим,
    # иначе долю речи посчитать нельзя (так и было в первом прогоне 25.08).
    row_seg = conn.execute(
        "SELECT segments FROM transcripts WHERE call_id=%s", (call_id,)).fetchone()
    segs = [x for x in (row_seg[0] if row_seg else None) or []
            if (x.get("text") or "").strip()]
    if segs:
        body = "\n".join(f"{i + 1}. {(x['text'] or '').strip()}"
                         for i, x in enumerate(segs))
    else:
        body = text
    # 24000 символов покрывают 99% разговоров (90-й процентиль 10103,
    # максимум 33128); прежний лимит 8000 резал 16% звонков — самых длинных,
    # и модель не видела закрытие (аудит 25.08).
    sent = anonymize(body)[:24000]
    n_sent = len(sent.split("\n")) if segs else 0
    segs = segs[:n_sent]
    prompt = PROMPT.format(when=when.strftime("%Y-%m-%d %H:%M, %A"), text=sent,
                           n_lines=n_sent or "столько же, сколько строк")
    d, tin, tout = ask(prompt)

    due = parse_date(d.get("due_date"))
    agreement = bool(d.get("agreement")) and due is not None
    conf = num(d.get("confidence")) or 0

    conn.execute(
        """INSERT INTO call_extractions (call_id, agreement, due_date, date_approximate,
              phrase, quote, initiator, promised_by_manager, confidence,
              budget_rub, car, city, model, tokens_in, tokens_out, raw)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (call_id) DO UPDATE SET
             agreement=EXCLUDED.agreement, due_date=EXCLUDED.due_date,
             quote=EXCLUDED.quote, confidence=EXCLUDED.confidence,
             budget_rub=EXCLUDED.budget_rub, car=EXCLUDED.car, city=EXCLUDED.city,
             model=EXCLUDED.model, raw=EXCLUDED.raw, created_at=now()""",
        (call_id, agreement, due, bool(d.get("date_is_approximate")),
         d.get("phrase"), d.get("quote"), d.get("initiator"),
         d.get("promised_by_manager"), conf,
         num(d.get("budget_rub")), d.get("car"), d.get("city"),
         MODEL, tin, tout, json.dumps(d, ensure_ascii=False)),
    )

    # карточка эталонного скрипта: балл считаем кодом, модель только отвечает
    # на вопросы. У несостоявшегося разговора карточки нет.
    card_score, card_parts = (None, None)
    if d.get("outcome") != "не дозвонились" and duration and duration >= 60:
        card_score, card_parts = card.score({
            "next_step_proposed": bool(d.get("next_step_proposed")),
            "next_step_specific": bool(d.get("next_step_specific")),
            "date_time_fixed": bool(d.get("date_time_fixed")),
            "decision_maker_identified": bool(d.get("decision_maker_identified")),
            "timeline_discussed": bool(d.get("timeline_discussed")),
            "linked_to_client_pain": bool(d.get("linked_to_client_pain")),
            "value_props_n": int(num(d.get("value_props_n")) or 0),
        })

    objs = d.get("objections")
    if not isinstance(objs, list):
        objs = []
    objs = [str(o)[:120] for o in objs if o]
    score = num(d.get("score"))
    conn.execute(
        """INSERT INTO call_scores (call_id, need_identified, price_named, objections,
              objections_handled, next_step, next_step_dated, outcome, score, summary,
              raw, model, talk_share, questions_manager, card_score, card)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (call_id) DO UPDATE SET
             need_identified=EXCLUDED.need_identified, price_named=EXCLUDED.price_named,
             objections=EXCLUDED.objections, objections_handled=EXCLUDED.objections_handled,
             next_step=EXCLUDED.next_step, next_step_dated=EXCLUDED.next_step_dated,
             outcome=EXCLUDED.outcome, score=EXCLUDED.score, summary=EXCLUDED.summary,
             raw=EXCLUDED.raw, model=EXCLUDED.model, talk_share=EXCLUDED.talk_share,
             questions_manager=EXCLUDED.questions_manager,
             card_score=EXCLUDED.card_score, card=EXCLUDED.card, created_at=now()""",
        (call_id, bool(d.get("need_identified")), bool(d.get("price_named")), objs,
         bool(d.get("objections_handled")), d.get("next_step"),
         bool(d.get("next_step_dated")), d.get("outcome"),
         int(score) if score is not None else None, d.get("summary"),
         json.dumps(d, ensure_ascii=False), MODEL,
         num(d.get("talk_share")), int(num(d.get("questions_manager")) or 0),
         card_score,
         json.dumps(card_parts, ensure_ascii=False) if card_parts else None),
    )

    # QA-карточка: баллы складывает код, A3 (доля речи и монолог) считается
    # механически по ролям — глазомеру модели тут не верим.
    if d.get("outcome") not in ("не дозвонились", "нецелевой") and duration >= 60:
        try:
            raw_qa = dict(d.get("qa") or {})
            raw_qa["roles"] = d.get("roles")
            row = qa.build(raw_qa, segs, verify_text=text)
            if row:
                cols = (["a1", "a2", "a3", "a4", "b1", "b2", "b3", "b4", "b5",
                         "c1", "c2", "c3", "c4", "c5", "d1", "d2", "d3"]
                        + ["block_a", "block_b", "block_c", "block_d",
                           "integral", "talk_share", "max_mono_sec", "roles"])
                vals = [row.get(c.upper()) if len(c) == 2 else row.get(c)
                        for c in cols]
                conn.execute(
                    "INSERT INTO call_qa (call_id, portal_user_id, call_start, "
                    + ", ".join(cols) + ", quotes, na, model) VALUES ("
                    + ", ".join(["%s"] * (len(cols) + 6)) + ") "
                    "ON CONFLICT (call_id) DO UPDATE SET "
                    + ", ".join(f"{c}=EXCLUDED.{c}" for c in cols)
                    + ", quotes=EXCLUDED.quotes, na=EXCLUDED.na,"
                      " model=EXCLUDED.model, created_at=now()",
                    [call_id, user_id, when] + vals
                    + [json.dumps(row["quotes"], ensure_ascii=False),
                       row["na"], MODEL])
        except Exception as ex:  # noqa: BLE001
            log.warning("Звонок %s: QA-карточка не записана: %s",
                        call_id, str(ex)[:200])

    horizon = (due - when.date()).days if due else -1
    if agreement and conf >= CONF_MIN and horizon >= MIN_HORIZON:
        conn.execute(
            """INSERT INTO followups (call_id, lead_id, manager_id, phone_e164,
                   due_date, approximate, quote, promised, confidence)
               VALUES (%s,%s,%s,phone_e164(%s),%s,%s,%s,%s,%s)
               ON CONFLICT (call_id) DO UPDATE SET
                 due_date=EXCLUDED.due_date, quote=EXCLUDED.quote,
                 promised=EXCLUDED.promised, confidence=EXCLUDED.confidence""",
            (call_id, ent_id if ent_type == 'LEAD' else None, user_id, phone,
             due, bool(d.get("date_is_approximate")), d.get("quote"),
             d.get("promised_by_manager"), conf),
        )
        log.info("Звонок %s: договорённость на %s (%s)", call_id, due, d.get("phrase"))

    # сводка разговоров в карточке: один комментарий, обновляется
    if d.get("summary") and d.get("outcome") != "не дозвонились":
        try:
            phone_e164 = conn.execute(
                "SELECT phone_e164 FROM calls WHERE id=%s", (call_id,)
            ).fetchone()[0]
            b24_summary.push(conn, ent_type, ent_id, phone_e164)
        except Exception as e:
            log.warning("Звонок %s: сводка в Битрикс не записана: %s", call_id, e)

    conn.execute("UPDATE llm_queue SET status='done', updated_at=now() WHERE call_id=%s",
                 (call_id,))


def main():
    once = "--once" in sys.argv
    if not KEY:
        log.error("Не задан PROXYAPI_KEY")
        return
    migrate()
    log.info("Разбор разговоров запущен: модель %s, звонки от %s сек за последние %s дней",
             MODEL, MIN_SEC, SINCE_DAYS)

    while True:
        worked = False
        try:
            with db() as conn:
                enqueue(conn)
                if spent_today(conn) >= DAILY_LIMIT:
                    log.info("Дневной лимит разборов исчерпан (%s)", DAILY_LIMIT)
                else:
                    task = take(conn)
                    if task:
                        worked = True
                        try:
                            process(conn, task)
                        except Exception as e:  # noqa: BLE001
                            log.error("Звонок %s: %s", task[0], str(e)[:200])
                            conn.execute(
                                """UPDATE llm_queue SET
                                     status = CASE WHEN attempts+1 >= 3 THEN 'failed' ELSE 'pending' END,
                                     attempts = attempts+1, last_error=%s, updated_at=now()
                                   WHERE call_id=%s""",
                                (str(e)[:400], task[0]),
                            )
        except Exception as e:  # noqa: BLE001
            log.exception("Сбой разбора: %s", e)

        if once and not worked:
            break
        if not worked:
            time.sleep(IDLE_SLEEP)


if __name__ == "__main__":
    main()
