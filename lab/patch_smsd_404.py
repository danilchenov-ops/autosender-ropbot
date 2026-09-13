#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Патч smsd.py: закрывать сообщения, о которых шлюз уже забыл.

Одно сообщение от 27.08 висело в 'sent' четвёртые сутки: шлюз на запрос
отвечает 404, raise_for_status бросал исключение, и до ветки разбора
состояния дело не доходило — сообщение не закрывалось никогда.
"""
import io, sys

P = "/opt/ropbot/app/smsd.py"
src = io.open(P, encoding="utf-8").read()

OLD = '''            r = requests.get(f"{API}/messages/{ext}",
                             auth=(login, pw), timeout=20)
            r.raise_for_status()
            data = r.json()
            st = data.get("state")'''

NEW = '''            r = requests.get(f"{API}/messages/{ext}",
                             auth=(login, pw), timeout=20)
            if r.status_code == 404:
                # Шлюз помнит сообщения ограниченное время. Раз забыл —
                # закрываем, иначе висит в 'sent' вечно и злит сторожа.
                if hrs >= FORGET_DAYS * 24:
                    finish(conn, oid, "no_report",
                           "шлюз уже не помнит это сообщение")
                continue
            r.raise_for_status()
            data = r.json()
            st = data.get("state")'''

if NEW in src and OLD not in src:
    print("уже применено")
    sys.exit(0)
if src.count(OLD) != 1:
    print("НЕ НАЙДЕНО, совпадений:", src.count(OLD))
    sys.exit(1)

src = src.replace(OLD, NEW)
compile(src, P, "exec")
io.open(P, "w", encoding="utf-8").write(src)
print("ok: обработка 404, записано и компилируется")
