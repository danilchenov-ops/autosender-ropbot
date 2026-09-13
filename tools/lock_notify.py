# -*- coding: utf-8 -*-
"""Уведомления о блокировке по просрочке — в Телеграм-бот ропа.

Сам сервис распределения (lead-router, 195.2.74.207) в Телеграм не ходит:
Telegram оттуда не достучаться, а бот и SOCKS-туннель живут здесь. Поэтому
обёртка lock_notify.sh раз в две минуты забирает журнал `lock_log` по ssh
и подаёт сюда через переменную ROWS.

Что кому:
  заблокирован / разблокирован — менеджеру (по manager_id ↔ tg_users), и сводкой боссам;
  массовый перевод, все в блоке — только боссам;
  отсрочка                     — никому (служебное).
Менеджеру без Телеграма (Егор) — уведомление в Битрикс.
Дедуп через tg_sent: kind='lock_event' (менеджер) / 'lock_event_boss' (боссы), ref_id = lock_log.id.
Запуск с SEED=1 — пометить всё как отправленное, не отправляя (первый запуск).
"""
import json
import os
import sys

import tg
from common import Bitrix, db
from psycopg.rows import dict_row

rows = json.loads(os.environ.get("ROWS") or "[]")
seed = os.environ.get("SEED") == "1"
if not rows:
    sys.exit(0)

BOSS_IDS = tuple(int(x) for x in os.environ.get("BOSS_IDS", "1,9187").split(","))  # Светлана, Тимофей
KIND_MGR, KIND_BOSS = "lock_event", "lock_event_boss"


def short(name):
    """«М6 Николай Попов» → «Николай»."""
    parts = [p for p in (name or "").split() if not (len(p) <= 3 and p.startswith("М"))]
    return parts[0] if parts else (name or "?")


def to_manager(ev, r):
    if ev == "заблокирован":
        return (f"Вы в блоке: {r['stale']} заявок в «Необработанном» старше 24 часов "
                f"({r['detail']}). Новые заявки не приходят, пока не разберёте.")
    if ev == "разблокирован":
        return "Блок снят: просроченных не осталось, заявки снова приходят."
    return None


def to_boss(ev, r, name):
    if ev == "заблокирован":
        return f"Блок: {name} — {r['stale']} просроченных, {r['detail']}."
    if ev == "разблокирован":
        return f"Снят блок: {name}."
    if ev == "массовый перевод":
        return f"Массовый перевод: {name} — {r['detail']}."
    if ev == "все в блоке":
        return f"Все менеджеры в блоке: {r['detail']}."
    return None


def already(conn, chat_id, kind, ref):
    return conn.execute("SELECT 1 FROM tg_sent WHERE chat_id=%s AND kind=%s AND ref_id=%s AND ok",
                        (chat_id, kind, ref)).fetchone() is not None


def mark(conn, chat_id, kind, ref, ok, err=None):
    conn.execute("INSERT INTO tg_sent(chat_id, kind, ref_id, sent_at, ok, error) "
                 "VALUES (%s,%s,%s,now(),%s,%s)", (chat_id, kind, ref, ok, err))


def send(conn, chat_id, kind, ref, text):
    if already(conn, chat_id, kind, ref):
        return "дубль"
    if seed:
        mark(conn, chat_id, kind, ref, True, "seed")
        return "seed"
    try:
        tg.send(chat_id, text)          # markdown=True: в образе старый tg.py, False не работает
        mark(conn, chat_id, kind, ref, True)
        return "ok"
    except Exception as e:  # noqa: BLE001
        mark(conn, chat_id, kind, ref, False, str(e)[:200])
        return f"ошибка: {e}"


out = []
with db() as conn:
    conn.row_factory = dict_row
    chats = {r["manager_id"]: r["chat_id"] for r in conn.execute(
        "SELECT manager_id, chat_id FROM tg_users WHERE active AND manager_id IS NOT NULL")}
    names = {r["portal_user_id"]: r["full_name"] for r in conn.execute(
        "SELECT portal_user_id, full_name FROM managers")}
    bosses = [chats[b] for b in BOSS_IDS if b in chats]

    for r in sorted(rows, key=lambda x: x["id"]):
        ev, uid, ref = r["event"], r["user_id"], r["id"]
        name = short(names.get(uid)) if uid else "—"

        text = to_manager(ev, r)
        if text and uid:
            chat = chats.get(uid)
            if chat:
                res = send(conn, chat, KIND_MGR, ref, text)
            elif already(conn, -uid, KIND_MGR, ref):
                res = "дубль"
            elif seed:
                mark(conn, -uid, KIND_MGR, ref, True, "seed"); res = "seed"
            else:
                # нет Телеграма — в Битрикс; chat_id отрицательный = портальный ID
                try:
                    Bitrix().call("im.notify.system.add", {"USER_ID": uid, "MESSAGE": text})
                    mark(conn, -uid, KIND_MGR, ref, True, "bitrix"); res = "ok (Битрикс)"
                except Exception as e:  # noqa: BLE001
                    mark(conn, -uid, KIND_MGR, ref, False, str(e)[:200]); res = f"ошибка Битрикс: {e}"
            if res not in ("дубль",):
                out.append(f"#{ref} {ev:16} → {name:12} {res}")

        text = to_boss(ev, r, name)
        if text:
            for chat in bosses:
                res = send(conn, chat, KIND_BOSS, ref, text)
                if res not in ("дубль",):
                    out.append(f"#{ref} {ev:16} → босс {chat} {res}")

for line in out:
    print(line)
