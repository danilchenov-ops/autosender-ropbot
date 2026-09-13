#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Патч dash.py: показать новый статус no_report на вкладке «Сообщения».

31.08.2026: smsd.py научился закрывать сообщения статусом no_report —
телефон отправил, но оператор не вернул отчёт о доставке. Без этой правки
РОП видел бы в ленте сырое «no_report», а в таблице не сходились бы цифры.
"""
import io, sys

P = "/opt/ropbot/web/dash.py"
src = io.open(P, encoding="utf-8").read()

OLD_DICT = '''    "delivered": ("доставлено", "sms-ok"),
    "failed": ("ошибка", "sms-bad"),'''
NEW_DICT = '''    "delivered": ("доставлено", "sms-ok"),
    "no_report": ("ушла, отчёта нет", "sms-ok"),
    "failed": ("ошибка", "sms-bad"),'''

OLD_CNT = '''               count(*) FILTER (WHERE status = 'delivered'),'''
NEW_CNT = '''               count(*) FILTER (WHERE status IN ('delivered', 'no_report')),'''

OLD_TH = '''    <th>Доставлено</th><th>Ошибки</th><th>Пропущено</th><th>Телефон</th>'''
NEW_TH = '''    <th>Ушло</th><th>Ошибки</th><th>Пропущено</th><th>Телефон</th>'''

OLD_NOTE = '''    «Пропущено» — правило не дало отправить: на этот номер уже писали
    в последние 30 дней, либо телефон менеджера не подключён.</p>'''
NEW_NOTE = '''    «Ушло» — телефон отправил сообщение; часть операторов не возвращает
    отчёт о доставке, такие тоже считаются здесь.
    «Пропущено» — правило не дало отправить: на этот номер уже писали
    в последние 30 дней, либо телефон менеджера не подключён.</p>'''

for name, old, new in [("словарь статусов", OLD_DICT, NEW_DICT),
                       ("подсчёт", OLD_CNT, NEW_CNT),
                       ("заголовок", OLD_TH, NEW_TH),
                       ("пояснение", OLD_NOTE, NEW_NOTE)]:
    if new in src and old not in src:
        print("уже применено:", name)
        continue
    if src.count(old) != 1:
        print("НЕ НАЙДЕНО:", name, "совпадений:", src.count(old))
        sys.exit(1)
    src = src.replace(old, new)
    print("ok:", name)

compile(src, P, "exec")
io.open(P, "w", encoding="utf-8").write(src)
print("записано и компилируется")
