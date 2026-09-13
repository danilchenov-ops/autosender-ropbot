"""ТЕСТ (01.09.2026): три новых поля квалификации — бюджет, сроки, безопасность.

Ничего не пишет в базу. Берёт N первых содержательных разговоров по заявкам,
прогоняет отдельный компактный промпт и печатает результат с цитатами,
чтобы Тимофей сверил решения модели с живой речью.

Определения согласованы в чате 31.08–01.09:
- бюджет: три исхода — спросил менеджер / клиент дал рамку сам / не установлен
  (цена конкретного лота и цифры менеджера бюджетом НЕ считаются);
- сроки: вопрос менеджера о сроке покупки (любая форма, в т.ч. прошедшее время);
- безопасность: менеджер предложил онлайн-трансляцию офиса или видеосвязь;
  видеообзор самой машины — НЕ безопасность.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extractor import ask, anonymize      # noqa: E402
from common import db                     # noqa: E402

N = int(os.getenv("N", "40"))

PROMPT = """Ты разбираешь расшифровку телефонного разговора менеджера
автосалона (продажа авто под заказ из-за границы) с клиентом.
Запись одноканальная, реплики не разделены по говорящим — определяй роли
по смыслу: менеджер тот, кто подбирает автомобиль и ведёт сделку.

Оцени ТРИ вещи. Отвечай строго по определениям, без домыслов.

1) budget — как установлен бюджет клиента. Ровно одно значение:
   "manager_asked" — менеджер САМ спросил про бюджет клиента:
      «на какой бюджет ориентируетесь», «в какие деньги хотите уложиться»,
      «в рамках какого бюджета», «какую сумму закладываете».
      Ставь это значение, даже если вопрос прозвучал в середине или конце
      разговора, уже после того как клиент называл какие-то суммы.
      Ставь и в том случае, если клиент на вопрос не ответил внятно —
      важен факт вопроса менеджера.
   "client_stated" — менеджер не спрашивал, но клиент САМ обозначил свою
      денежную рамку на покупку: «мне нужен автомобиль за 1 500 000»,
      «миллион восемьсот у меня бюджет», «могу рассчитывать в пределах 700
      тысяч», «хотел бы влезть в диапазон до 3 миллионов».
   "no" — бюджет не установлен. Сюда относится ВСЁ остальное, в том числе:
      клиент называет цену конкретной машины, которую увидел на сайте или
      в объявлении («увидел фит за 400 тысяч у вас на сайте», «там написано
      2 миллиона 112»); любые суммы, которые называет сам менеджер (оценка
      стоимости, доставка, растаможка, разница в цене); обсуждение цены
      конкретного лота. Если формулировка двусмысленная — ставь "no".

2) timeline_asked — true, если менеджер САМ спросил, когда клиент планирует
   покупку. Любая форма, включая прошедшее время: «когда планируете покупку»,
   «вы когда планировали покупкой заниматься», «когда хотите быть на машине»,
   «в какие сроки рассматриваете». false — если срок не спрашивали или
   клиент сам обронил срок без вопроса менеджера.

3) safety — true, если менеджер предложил клиенту визуально убедиться
   в реальности компании: онлайн-трансляция офиса или камеры в офисе
   («у нас ведётся прямая онлайн-трансляция, можете зайти посмотреть, как мы
   работаем»), либо созвон по видеосвязи/видеозвонок, чтобы показать себя,
   офис или стоянку.
   ВАЖНО: предложение прислать видеообзор автомобиля, фото или видео самой
   машины — это НЕ безопасность, ставь false. Обсуждение мессенджеров ради
   отправки подборки вариантов — тоже false. Если тему поднял сам клиент,
   а менеджер лишь согласился — ставь false и отметь это в safety_quote.

Верни СТРОГО JSON:
{{
  "budget": "manager_asked" | "client_stated" | "no",
  "budget_quote": "точная цитата из расшифровки или null",
  "timeline_asked": true|false,
  "timeline_quote": "точная цитата или null",
  "safety": true|false,
  "safety_quote": "точная цитата или null"
}}

Расшифровка:

{text}"""

SQL = """
WITH pervye AS (
  SELECT DISTINCT ON (l.id) l.id AS lead, l.assigned_by AS uid,
         c.id AS call_id, c.duration
    FROM leads l
    JOIN calls c ON c.direction='out' AND c.call_start > l.date_create
         AND c.duration >= 90
         AND ((c.crm_entity_type='LEAD' AND c.crm_entity_id=l.id)
              OR (l.phone_e164 IS NOT NULL AND c.phone_e164=l.phone_e164))
   WHERE l.date_create >= now() - interval '25 days'
     AND coalesce(l.phone_kind,'') NOT IN ('junk','none')
     AND l.source_id IS DISTINCT FROM 'PARTNER'
     AND l.source_id IS DISTINCT FROM 'CALL'
     AND l.status_id IS DISTINCT FROM '31'
     AND l.assigned_by IN (8829,8831,11357,11807,15077)
   ORDER BY l.id, c.call_start)
SELECT p.lead, p.call_id, coalesce(m.name, p.uid::text) AS mgr, tr.text
  FROM pervye p
  JOIN transcripts tr ON tr.call_id = p.call_id
  LEFT JOIN managers m ON m.portal_user_id = p.uid
 ORDER BY random() LIMIT %s
"""


def main():
    conn = db()
    rows = conn.execute(SQL, (N,)).fetchall()
    tin = tout = 0
    stat = {"manager_asked": 0, "client_stated": 0, "no": 0}
    tl = sf = 0
    print(f"разговоров: {len(rows)}\n")
    for lead, call_id, mgr, text in rows:
        try:
            d, a, b = ask(PROMPT.format(text=anonymize(text)[:24000]))
        except Exception as exc:                      # noqa: BLE001
            print(f"lead {lead}: ОШИБКА {str(exc)[:120]}")
            continue
        tin += a
        tout += b
        bud = d.get("budget", "?")
        stat[bud] = stat.get(bud, 0) + 1
        tl += bool(d.get("timeline_asked"))
        sf += bool(d.get("safety"))
        print(f"--- lead {lead} · {mgr} ---")
        print(f"  БЮДЖЕТ: {bud}")
        print(f"    цитата: {(d.get('budget_quote') or '—')[:180]}")
        print(f"  СРОКИ: {'да' if d.get('timeline_asked') else 'нет'}"
              f" | {(d.get('timeline_quote') or '—')[:140]}")
        print(f"  БЕЗОПАСНОСТЬ: {'да' if d.get('safety') else 'нет'}"
              f" | {(d.get('safety_quote') or '—')[:160]}")
    n = max(len(rows), 1)
    print("\n===== ИТОГО =====")
    print(f"бюджет: спросил менеджер {stat.get('manager_asked',0)} "
          f"({round(100*stat.get('manager_asked',0)/n)}%), "
          f"клиент сам {stat.get('client_stated',0)}, "
          f"не установлен {stat.get('no',0)}")
    print(f"сроки спросил: {tl} ({round(100*tl/n)}%)")
    print(f"безопасность: {sf} ({round(100*sf/n)}%)")
    rub = tin / 1e6 * 455 + tout / 1e6 * 2275
    print(f"токены: вход {tin}, выход {tout} -> {rub:.0f} руб "
          f"({rub/n:.2f} руб за разговор)")


if __name__ == "__main__":
    main()
