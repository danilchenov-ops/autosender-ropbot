# -*- coding: utf-8 -*-
"""СМС-приветствие новым лидам с телефонов менеджеров (SMSGate, Cloud server).

Решения Тимофея 27.08.2026:
- правило: новая заявка → СМС от закреплённого менеджера;
- задержка 5 минут после создания заявки («что б не сразу бахало»);
- круглосуточно;
- не чаще одной отправки на номер за 30 дней;
- если менеджер уже успел ПОГОВОРИТЬ с клиентом (исходящий, duration>0,
  после создания заявки) — СМС не шлём, помечаем skipped;
- включено сразу для подключённых телефонов (sms_devices), остальные
  менеджеры получают skipped «телефон не подключён» — РОП видит потери.

Текст утверждён 27.08 (125 зн. макс = 2 СМС):
    Здравствуйте! Это {Имя}. Автосендер. Получил вашу заявку,
    свяжусь в ближайшее время. Сохраните мой контакт. ООО Sinergos

Выключатель: файл /opt/ropbot/sms_on на хосте — крон запускает скрипт только
при его наличии (rm sms_on = стоп; touch sms_on = пуск). Внутри скрипта
защиты: только phone_kind='mobile', только лиды моложе 6 часов (после простоя
или включения старое не рассылается), LAUNCH — раньше этого момента ничего
не шлём никогда.

Запуск (в образ не запечён): docker exec -i ropbot-collector-1 python - < app/smsd.py
Крон: каждую минуту под flock.
"""
import datetime as dt
import re

import requests

from common import db, log

API = "https://api.sms-gate.app/3rdparty/v1"
TZ = "Asia/Vladivostok"
DELAY_MIN = 5          # минут после создания заявки
FRESH_H = 6            # лиды старше — не трогаем (защита после простоя)
DEDUP_DAYS = 30        # одна отправка на номер за месяц
TTL_SEC = 3600         # срок годности на шлюзе: не отправил за час — не надо
POLL_DAYS = 7          # насколько назад догоняем статусы
REPORT_WAIT_H = 1      # ждём отчёт о доставке; дальше считаем «ушло без отчёта»
FORGET_DAYS = 3        # шлюз столько помнит сообщение; дальше не спрашиваем
LAUNCH = "2026-08-27 13:45:00+10"   # до этого момента лидов не трогаем
TEXT = ("Здравствуйте! Это {name}. Автосендер. Получил вашу заявку, "
        "свяжусь в ближайшее время. Сохраните мой контакт. ООО Sinergos")

ENQ_SQL = f"""
  SELECT l.id, l.phone_e164, l.assigned_by,
         regexp_replace(m.name, '^М[0-9]+\\s*', '') AS first_name
    FROM leads l
    JOIN managers m ON m.portal_user_id = l.assigned_by
   WHERE l.date_create >= greatest(now() - interval '{FRESH_H} hours',
                                   timestamptz '{LAUNCH}')
     AND l.date_create + interval '{DELAY_MIN} minutes' <= now()
     AND l.phone_kind = 'mobile'
     AND l.assigned_by IN (SELECT portal_user_id FROM dash_tokens
                            WHERE active AND kind = 'manager')
     AND NOT EXISTS (SELECT 1 FROM sms_outbox o
                      WHERE o.lead_id = l.id AND o.rule = 'new_lead')
   ORDER BY l.date_create
"""

# Лид уже закрыт как провальный (Спам 31, Дубль 27, Недозвон JUNK и т.п.):
# менеджер успел разобраться за эти 5 минут — СМС не шлём.
CLOSED_SQL = """
  SELECT d.name FROM leads l
    LEFT JOIN crm_dict d ON d.kind = 'STATUS' AND d.status_id = l.status_id
   WHERE l.id = %(lid)s AND l.status_semantic = 'F'
"""

TALKED_SQL = """
  SELECT 1 FROM calls c JOIN leads l ON l.id = %(lid)s
   WHERE c.phone_e164 = l.phone_e164 AND c.direction = 'out'
     AND c.duration > 0 AND c.call_start >= l.date_create LIMIT 1
"""

DUP_SQL = f"""
  SELECT 1 FROM sms_outbox
   WHERE phone_e164 = %(phone)s
     AND status NOT IN ('failed', 'skipped')
     AND created_at >= now() - interval '{DEDUP_DAYS} days' LIMIT 1
"""


def skip(conn, lid, phone, uid, body, why):
    conn.execute(
        "INSERT INTO sms_outbox (rule, lead_id, phone_e164, portal_user_id, "
        "body, parts, status, error) VALUES "
        "('new_lead', %s, %s, %s, %s, 0, 'skipped', %s)",
        (lid, phone, uid, body, why))
    log.info("СМС лиду %s пропущена: %s", lid, why)


def send(conn, dev, lid, phone, uid, body):
    parts = 1 if len(body) <= 70 else 2
    try:
        to = phone if phone.startswith("+") else "+" + phone
        r = requests.post(f"{API}/messages", json={
            "textMessage": {"text": body}, "phoneNumbers": [to],
            "ttl": TTL_SEC},
            auth=(dev["login"], dev["password"]), timeout=25)
        r.raise_for_status()
        ext = r.json().get("id")
        conn.execute(
            "INSERT INTO sms_outbox (rule, lead_id, phone_e164, portal_user_id,"
            " body, parts, status, external_id, sent_at) VALUES "
            "('new_lead', %s, %s, %s, %s, %s, 'sent', %s, now())",
            (lid, phone, uid, body, parts, ext))
        log.info("СМС лиду %s ушла с телефона %s (%s)", lid, uid, ext)
    except Exception as exc:  # noqa: BLE001
        conn.execute(
            "INSERT INTO sms_outbox (rule, lead_id, phone_e164, portal_user_id,"
            " body, parts, status, error) VALUES "
            "('new_lead', %s, %s, %s, %s, %s, 'failed', %s)",
            (lid, phone, uid, body, parts, str(exc)[:300]))
        log.error("СМС лиду %s не ушла: %s", lid, exc)


def finish(conn, oid, status, err=None):
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
            if r.status_code == 404:
                # Шлюз помнит сообщения ограниченное время. Раз забыл —
                # закрываем, иначе висит в 'sent' вечно и злит сторожа.
                if hrs >= FORGET_DAYS * 24:
                    finish(conn, oid, "no_report",
                           "шлюз уже не помнит это сообщение")
                continue
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
            finish(conn, oid, "no_report", "шлюз уже не помнит это сообщение")


def main():
    with db() as conn:
        devices = {r[0]: {"login": r[1], "password": r[2]}
                   for r in conn.execute(
                       "SELECT portal_user_id, login, password FROM sms_devices"
                       " WHERE active").fetchall()}
        for lid, phone, uid, name in conn.execute(ENQ_SQL).fetchall():
            body = TEXT.format(name=name)
            closed = conn.execute(CLOSED_SQL, {"lid": lid}).fetchone()
            if closed:
                skip(conn, lid, phone, uid, body,
                     f"лид закрыт: {closed[0] or 'провальный статус'}")
            elif conn.execute(DUP_SQL, {"phone": phone}).fetchone():
                skip(conn, lid, phone, uid, body,
                     "на этот номер уже писали за последние 30 дней")
            elif conn.execute(TALKED_SQL, {"lid": lid}).fetchone():
                skip(conn, lid, phone, uid, body, "уже поговорили")
            elif uid not in devices:
                skip(conn, lid, phone, uid, body, "телефон менеджера не подключён")
            else:
                send(conn, devices[uid], lid, phone, uid, body)
        poll_status(conn)


if __name__ == "__main__":
    main()
