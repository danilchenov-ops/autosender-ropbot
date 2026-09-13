"""Карточка оценки разговора — итог разбора «эталонного скрипта» 21.08.2026.

Пять критериев, веса пропорциональны разрыву между выигранными и проигранными
разговорами на зеркальной выборке (76 WON против 98 LOST, май-июль 2026).
Проверена на отложенной контрольной выборке: AUC 0,737 против 0,692 на
обучающей — сработала не хуже, значит не подогнана.

Шестой кандидат, «вытащена проблема клиента», в карточку НЕ вошёл: на обучающей
выборке был вторым по силе (+21,1), на контрольной разрыв сменил знак.

Балл считает код по разбору разговора, а не модель на глаз: модель отвечает
на конкретные вопросы, арифметика наша.
"""

CRITERIA = {
    "next_step": (34, "Следующий шаг"),
    "decision_maker": (22, "Выяснено, кто участвует в решении"),
    "value_to_pain": (20, "Выгода привязана к словам клиента"),
    "date_fixed": (13, "Названы дата и время следующего действия"),
    "timeline": (11, "Выяснен срок покупки"),
}


def levels(d):
    """Уровень 0/1/2 по каждому критерию из полей разбора звонка."""
    return {
        "next_step": 2 if d.get("next_step_specific") else (
            1 if d.get("next_step_proposed") else 0),
        "decision_maker": 2 if d.get("decision_maker_identified") else 0,
        "value_to_pain": 2 if d.get("linked_to_client_pain") else (
            1 if (d.get("value_props_n") or 0) >= 2 else 0),
        "date_fixed": 2 if d.get("date_time_fixed") else 0,
        "timeline": 2 if d.get("timeline_discussed") else 0,
    }


def score(d):
    """0-100 баллов и разбивка по критериям."""
    lv = levels(d)
    parts, total = {}, 0.0
    for key, (weight, title) in CRITERIA.items():
        pts = weight * lv[key] / 2.0
        parts[title] = round(pts, 1)
        total += pts
    return round(total, 1), parts
