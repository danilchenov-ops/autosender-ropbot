#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Патч smsd.py: срок годности сообщения + честная причина отказа + окно опроса."""
import io, sys

P = "/opt/ropbot/app/smsd.py"
src = io.open(P, encoding="utf-8").read()

OLD_CONST = '''DEDUP_DAYS = 30        # одна отправка на номер за месяц'''
NEW_CONST = '''DEDUP_DAYS = 30        # одна отправка на номер за месяц
TTL_SEC = 3600         # срок годности на шлюзе: не отправил за час — не надо
POLL_DAYS = 7          # насколько назад догоняем статусы'''

OLD_SEND = '''        r = requests.post(f"{API}/messages", json={
            "textMessage": {"text": body}, "phoneNumbers": [to]},
            auth=(dev["login"], dev["password"]), timeout=25)'''
NEW_SEND = '''        r = requests.post(f"{API}/messages", json={
            "textMessage": {"text": body}, "phoneNumbers": [to],
            "ttl": TTL_SEC},
            auth=(dev["login"], dev["password"]), timeout=25)'''

OLD_WIN = '''           AND o.sent_at >= now() - interval '2 days'""").fetchall()'''
NEW_WIN = '''           AND o.sent_at >= now() - interval '%s days'""" % POLL_DAYS
        ).fetchall()'''

OLD_POLL = '''            r.raise_for_status()
            st = r.json().get("state")
        except Exception as exc:  # noqa: BLE001
            log.warning("статус %s: %s", ext, exc)
            continue
        if st == "Delivered":
            conn.execute("UPDATE sms_outbox SET status='delivered', "
                         "final_at=now() WHERE id=%s", (oid,))
        elif st == "Failed":
            conn.execute("UPDATE sms_outbox SET status='failed', "
                         "final_at=now(), error='шлюз: Failed' WHERE id=%s",
                         (oid,))'''

NEW_POLL = '''            r.raise_for_status()
            data = r.json()
            st = data.get("state")
        except Exception as exc:  # noqa: BLE001
            log.warning("статус %s: %s", ext, exc)
            continue
        if st == "Delivered":
            conn.execute("UPDATE sms_outbox SET status='delivered', "
                         "final_at=now() WHERE id=%s", (oid,))
        elif st == "Failed":
            # Сохраняем причину от шлюза дословно: лимит Android, отказ
            # SMS-центра оператора, протухло по ttl — это разные болезни.
            rec = (data.get("recipients") or [{}])[0]
            why = rec.get("error") or "шлюз: Failed"
            conn.execute("UPDATE sms_outbox SET status='failed', "
                         "final_at=now(), error=%s WHERE id=%s",
                         (why[:300], oid))'''

PATCHES = [
    ("константы", OLD_CONST, NEW_CONST),
    ("ttl при отправке", OLD_SEND, NEW_SEND),
    ("окно опроса", OLD_WIN, NEW_WIN),
    ("причина отказа", OLD_POLL, NEW_POLL),
]

for name, old, new in PATCHES:
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
