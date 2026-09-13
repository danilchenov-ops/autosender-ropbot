"""Сравнение моделей на задаче «найти договорённость о следующем контакте».

Запуск: docker compose run --rm collector python llm_bench.py [сколько_звонков]
"""
import json
import os
import re
import sys
import time

import requests

from common import db, log

KEY = os.getenv("PROXYAPI_KEY", "")
BASE = "https://api.proxyapi.ru"

MODELS = [
    ("gpt-5-nano", "openai"),
    ("gpt-5-mini", "openai"),
    ("gemini-2.5-flash-lite", "google"),
    ("gemini-3.5-flash-lite", "google"),
]

# ── Обезличивание ────────────────────────────────────────────────────────────
PHONE = re.compile(r"(?:\+?[78][\s\-(]*)?9\d{2}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
LONGNUM = re.compile(r"\b\d{10,}\b")
EMAIL = re.compile(r"\b[\w.\-]+@[\w\-]+\.\w+\b")


def anonymize(text):
    text = PHONE.sub("[телефон]", text)
    text = LONGNUM.sub("[номер]", text)
    text = EMAIL.sub("[почта]", text)
    return text


PROMPT = """Ты разбираешь расшифровку телефонного разговора менеджера автосалона с клиентом.
Запись одноканальная, реплики не разделены по говорящим — определяй роли по смыслу.

Дата и время разговора: {when} (часовой пояс Владивосток).

Твоя задача — понять, договорились ли о следующем контакте, и когда именно.

Важно различать:
— НАСТОЯЩАЯ договорённость: назван срок или условие («наберите в четверг», «перезвоните
  через неделю», «свяжемся после 20-го», «жду ваше предложение завтра»).
— НЕ договорённость: вежливая отговорка без конкретики («я сам вам перезвоню»,
  «подумаю», «если что наберу», «давайте попозже как-нибудь»), прощание без обязательств.
Если сомневаешься — ставь agreement=false и низкую уверенность.

Верни СТРОГО JSON:
{{
  "agreement": true/false,
  "due_date": "ГГГГ-ММ-ДД" или null,
  "date_is_approximate": true/false,
  "phrase": "как срок был назван словами" или null,
  "quote": "точная цитата из разговора" или null,
  "initiator": "клиент" / "менеджер" / null,
  "promised_by_manager": "что менеджер обещал сделать" или null,
  "confidence": число от 0 до 1,
  "budget_rub": число или null,
  "car": "марка и модель" или null,
  "city": "город клиента" или null
}}

Считай дату относительно даты разговора. Расшифровка:

{text}"""


def call_openai(model, prompt):
    r = requests.post(
        f"{BASE}/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {KEY}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 3000,
            "reasoning_effort": "minimal",
        },
        timeout=180,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usage") or {}
    return d["choices"][0]["message"]["content"], u.get("prompt_tokens"), u.get("completion_tokens")


def call_google(model, prompt):
    r = requests.post(
        f"{BASE}/google/v1beta/models/{model}:generateContent",
        headers={"Authorization": f"Bearer {KEY}"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "maxOutputTokens": 1200},
        },
        timeout=180,
    )
    r.raise_for_status()
    d = r.json()
    u = d.get("usageMetadata") or {}
    parts = d["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts), \
        u.get("promptTokenCount"), u.get("candidatesTokenCount")


def ask(model, kind, prompt):
    t0 = time.time()
    raw, tin, tout = (call_openai if kind == "openai" else call_google)(model, prompt)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0)) if m else {"error": raw[:200]}
    return data, round(time.time() - t0, 1), tin, tout


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    if not KEY:
        print("Нет PROXYAPI_KEY")
        return

    with db() as conn:
        rows = conn.execute(
            """SELECT t.call_id, c.call_start, c.duration, t.text,
                      COALESCE(mg.full_name, c.portal_user_id::text) AS manager
               FROM transcripts t
               JOIN calls c ON c.id = t.call_id
               LEFT JOIN managers mg ON mg.portal_user_id = c.portal_user_id
               WHERE c.duration > 90 AND length(t.text) > 400
               ORDER BY c.call_start DESC LIMIT %s""",
            (n,),
        ).fetchall()

    print(f"\nРазговоров в выборке: {len(rows)}\n")
    stats = {m: {"agree": 0, "tin": 0, "tout": 0, "sec": 0.0, "err": 0} for m, _ in MODELS}

    for call_id, when, duration, text, manager in rows:
        clean = anonymize(text)[:6000]
        prompt = PROMPT.format(when=when.strftime("%Y-%m-%d %H:%M, %A"), text=clean)

        print("═" * 78)
        print(f"Звонок {call_id} · {when:%d.%m %H:%M} · {duration} сек · {manager}")
        print("─" * 78)
        print(clean[:300].replace("\n", " ") + "…")

        for model, kind in MODELS:
            try:
                d, sec, tin, tout = ask(model, kind, prompt)
            except Exception as e:  # noqa: BLE001
                print(f"  {model:24} ошибка: {str(e)[:90]}")
                stats[model]["err"] += 1
                continue
            s = stats[model]
            s["sec"] += sec
            s["tin"] += tin or 0
            s["tout"] += tout or 0
            if d.get("agreement"):
                s["agree"] += 1
                print(f"  {model:24} ДА  {d.get('due_date')} "
                      f"({d.get('phrase')}) conf={d.get('confidence')} "
                      f"{'~' if d.get('date_is_approximate') else ''}")
                if d.get("quote"):
                    print(f"  {'':24} «{str(d['quote'])[:110]}»")
                if d.get("promised_by_manager"):
                    print(f"  {'':24} обещал: {str(d['promised_by_manager'])[:90]}")
            else:
                print(f"  {model:24} нет  conf={d.get('confidence')}")
            extra = [f"{k}={d[k]}" for k in ("budget_rub", "car", "city") if d.get(k)]
            if extra:
                print(f"  {'':24} {' · '.join(str(x) for x in extra)}")
        print()

    print("═" * 78)
    print("ИТОГО")
    print("═" * 78)
    print(f"{'модель':24} {'договор.':>9} {'ошибок':>7} {'сек/зв':>7} {'ток.вх':>9} {'ток.вых':>8}")
    for model, _ in MODELS:
        s = stats[model]
        k = max(len(rows) - s["err"], 1)
        print(f"{model:24} {s['agree']:>9} {s['err']:>7} {s['sec']/k:>7.1f} "
              f"{s['tin']:>9} {s['tout']:>8}")

    print("\nЦены ₽/1М (вход/выход): gpt-5-nano 13/104 · gpt-5-mini 65/516 · "
          "gemini-2.5-flash-lite 26/129 · gemini-3.5-flash-lite 91/758")


if __name__ == "__main__":
    main()
