#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Патч smsd.py: различать «ушло, отчёта нет» и «висит в очереди».

Разбор 31.08.2026 показал: шлюз держит три неокончательных состояния —
Pending (телефон ещё не забрал), Processed, Sent (телефон отправил, но
оператор не вернул отчёт о доставке). Последнее — нормальный успешный
исход, а у нас он навсегда оставался в 'sent' и выглядел как поломка:
18 таких висело от 5 до 100 часов. Теперь Sent через час закрываем как
'no_report', и в 'sent' остаётся только то, что реально застряло.
"""
import io, sys

P = "/opt/ropbot/app/smsd.py"
src = io.open(P, encoding="utf-8").read()

OLD_CONST = '''POLL_DAYS = 7          # насколько назад догоняем статусы'''
NEW_CONST = '''POLL_DAYS = 7          # насколько назад догоняем статусы
REPORT_WAIT_H = 1      # ждём отчёт о доставке; дальше считаем «ушло без отчёта»
FORGET_DAYS = 3        # шлюз столько помнит сообщение; дальше не спрашиваем'''

OLD = '''def poll_status(conn):
    """Догоняем статусы доставок за последние 2 суток."""
    rows = conn.execute("""
        SELECT o.id, o.external_id, d.login, d.password
          FROM sms_outbox o JOIN sms_devices d USING (portal_user_id)
         WHERE o.status = 'sent' AND o.external_id IS NOT NULL
           AND o.sent_at >= now() - interval '%s days'""" % POLL_DAYS
        ).fetchall()
    for oid, ext, login, pw in rows:
        try:
            r = requests.get(f"{API}/messages/{ext}",
                             auth=(login, pw), timeout=20)
            r.raise_for_status()
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

NEW = '''def finish(conn, oid, status, err=None):
    conn.execute("UPDATE sms_outbox SET status=%s, final_at=now(), "
                 "error=coalesce(%s, error) WHERE id=%s",
                 (status, err[:300] if err else None, oid))


def poll_status(conn):
    """Догоняем статусы у шлюза и доводим каждое сообщение до финала.

    Финалы: delivered (оператор подтвердил), failed (причина от шлюза),
    no_report (телефон отправил, отчёта о доставке нет — нормальный исход,
    многие операторы отчёт не возвращают). Всё, что осталось в 'sent', —
    это реально застрявшее в очереди телефона, и это ловит sms_watch.py.
    """
    rows = conn.execute("""
        SELECT o.id, o.external_id, d.login, d.password,
               extract(epoch FROM now() - o.sent_at) / 3600.0
          FROM sms_outbox o JOIN sms_devices d USING (portal_user_id)
         WHERE o.status = 'sent' AND o.external_id IS NOT NULL
           AND o.sent_at >= now() - interval '%s days'""" % POLL_DAYS
        ).fetchall()
    for oid, ext, login, pw, hrs in rows:
        try:
            r = requests.get(f"{API}/messages/{ext}",
                             auth=(login, pw), timeout=20)
            r.raise_for_status()
            data = r.json()
            st = data.get("state")
        except Exception as exc:  # noqa: BLE001
            log.warning("статус %s: %s", ext, exc)
            continue
        if st == "Delivered":
            finish(conn, oid, "delivered")
        elif st == "Failed":
            # Сохраняем причину от шлюза дословно: лимит Android, отказ
            # SMS-центра оператора, протухло по ttl — это разные болезни.
            rec = (data.get("recipients") or [{}])[0]
            finish(conn, oid, "failed", rec.get("error") or "шлюз: Failed")
        elif st == "Sent" and hrs >= REPORT_WAIT_H:
            finish(conn, oid, "no_report", "телефон отправил, отчёта о доставке нет")
        elif st is None and hrs >= FORGET_DAYS * 24:
            finish(conn, oid, "no_report", "шлюз уже не помнит это сообщение")'''

for name, old, new in [("константы", OLD_CONST, NEW_CONST),
                       ("poll_status", OLD, NEW)]:
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
