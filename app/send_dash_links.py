"""Разовая рассылка личных ссылок на панели: менеджерам и Светлане (23.08.2026).

Ссылка берётся из dash_tokens по portal_user_id (менеджеры) и по kind='rop' (Светлана).
Факт отправки пишется в tg_sent (kind='dash_link'), чтобы не разослать дважды.
"""
import sys

import tg
from common import db, log

BASE = "https://rop.autosender.ru:8443/d/{}/"  # fvds.ru делит A-запись с чужим сервером
MGR_TEXT = ("Привет! По этой ссылке будет наглядный дашборд по работе. "
            "Цифры кликабельны, теперь в любую группу фильтра можно перейти "
            "одним нажатием.\n\n{}")
ROP_TEXT = ("Светлана, здравствуйте. Это ваша страница по отделу: все менеджеры "
            "в одной таблице. Цифры кликабельны — из любой можно провалиться "
            "в список заявок или разговоров. Ссылка личная и постоянная, "
            "страница обновляется сама.\n\n{}")

# кому шлём: chat_id, кто это, ссылка
SVETLANA = 7487296664
MANAGERS = [8829, 11807, 15077, 11357]   # Жернов, Попов, Пономарь, Томаш


def link_for(conn, sql, params):
    row = conn.execute(sql, params).fetchone()
    return BASE.format(row[0]) if row else None


def main():
    dry = "--dry" in sys.argv
    with db() as conn:
        plan = []
        for uid in MANAGERS:
            url = link_for(conn, "SELECT token FROM dash_tokens WHERE active "
                                 "AND kind='manager' AND portal_user_id=%s", (uid,))
            row = conn.execute("SELECT chat_id FROM tg_users WHERE manager_id=%s "
                               "AND active", (uid,)).fetchone()
            if not url or not row:
                log.warning("пропуск: менеджер %s, ссылка=%s, чат=%s", uid, url, row)
                continue
            plan.append((row[0], uid, MGR_TEXT.format(url)))
        url = link_for(conn, "SELECT token FROM dash_tokens WHERE active "
                             "AND kind='rop' LIMIT 1", ())
        if url:
            plan.append((SVETLANA, 0, ROP_TEXT.format(url)))

        for chat, ref, text in plan:
            if dry:
                print(f"--- {chat} / {ref} ---\n{text}\n")
                continue
            if conn.execute("SELECT 1 FROM tg_sent WHERE kind='dash_link' "
                            "AND chat_id=%s AND ok", (chat,)).fetchone():
                log.info("уже отправляли ссылку в чат %s — пропуск", chat)
                continue
            try:
                tg.send(chat, text, preview=False)
                ok, err = True, None
            except Exception as exc:  # noqa: BLE001
                ok, err = False, str(exc)[:300]
                log.error("ссылка → %s: %s", chat, err)
            conn.execute("INSERT INTO tg_sent (chat_id, kind, ref_id, ok, error) "
                         "VALUES (%s,'dash_link',%s,%s,%s)", (chat, ref, ok, err))
            log.info("ссылка → чат %s (менеджер %s): ok=%s", chat, ref, ok)


if __name__ == "__main__":
    main()
