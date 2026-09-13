"""Разовая рассылка: вкладка KPI на дашборде менеджера (28.08.2026).

Шлём всем активным менеджерам, зарегистрированным в боте. Ссылка личная,
из dash_tokens. Факт отправки — в tg_sent (kind='kpi_link'), повторно не шлём.
"""
import sys

import tg
from common import db, log

BASE = "https://rop.autosender.ru:8443/d/{}/kpi.html"
TEXT = ("Привет! На твоём дашборде появилась новая вкладка «KPI» — тайминги "
        "за 60 дней: как быстро берём заявку в работу, сколько звонков на "
        "каждой стадии и кто доводит клиента дальше. Пин-код не нужен, "
        "страница обновляется сама.\n\n{}")


def main():
    dry = "--dry" in sys.argv
    with db() as conn:
        rows = conn.execute(
            """SELECT t.portal_user_id, t.token, u.chat_id,
                      coalesce(m.name, '') AS name
                 FROM dash_tokens t
                 JOIN tg_users u ON u.manager_id = t.portal_user_id AND u.active
                 LEFT JOIN managers m ON m.portal_user_id = t.portal_user_id
                WHERE t.active AND t.kind = 'manager'
                ORDER BY t.portal_user_id""").fetchall()
        for uid, token, chat, name in rows:
            text = TEXT.format(BASE.format(token))
            if dry:
                print(f"--- {name} (uid {uid}, chat {chat}) ---\n{text}\n")
                continue
            if conn.execute("SELECT 1 FROM tg_sent WHERE kind='kpi_link' "
                            "AND chat_id=%s AND ok", (chat,)).fetchone():
                log.info("KPI-ссылка уже уходила в чат %s — пропуск", chat)
                continue
            try:
                tg.send(chat, text, preview=False)
                ok, err = True, None
            except Exception as exc:  # noqa: BLE001
                ok, err = False, str(exc)[:300]
                log.error("KPI-ссылка → %s: %s", chat, err)
            conn.execute("INSERT INTO tg_sent (chat_id, kind, ref_id, ok, error) "
                         "VALUES (%s,'kpi_link',%s,%s,%s)", (chat, uid, ok, err))
            log.info("KPI-ссылка → чат %s (менеджер %s): ok=%s", chat, uid, ok)


if __name__ == "__main__":
    main()
