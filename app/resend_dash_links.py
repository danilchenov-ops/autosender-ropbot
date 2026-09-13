"""Перерассылка ссылок на панели после смены адреса (25.08.2026).

Старые ссылки вели на danilchenov.fvds.ru. У этого имени в зоне FirstVDS
оказалась вторая A-запись на чужой сервер, поэтому примерно каждое второе
открытие упиралось в «сайт небезопасен». Ушли на rop.autosender.ru:8443 —
домен наш, сертификат свой.

Отдельный kind в tg_sent ('dash_link_v2'), чтобы защита от повторов
от первой рассылки не мешала, но и эта не ушла дважды.

Разовый скрипт. Запуск: python3 app/resend_dash_links.py [--dry]
"""
import sys

import tg
from common import db, log

BASE = "https://rop.autosender.ru:8443/d/{}/"
KIND = "dash_link_v2"

MGR_TEXT = (
    "Ссылка на дашборд поменялась — старая иногда открывалась с ошибкой "
    "«сайт небезопасен». Дело было в адресе, не в вашем телефоне или "
    "браузере. Эта рабочая, сохраните её вместо прежней:\n\n{}\n\n"
    "Страница та же самая и обновляется сама."
)
ROP_TEXT = (
    "Светлана, здравствуйте. Ссылка на страницу по отделу поменялась — "
    "старая иногда открывалась с ошибкой «сайт небезопасен», это была "
    "проблема адреса, не вашего браузера. Эта рабочая, сохраните её "
    "вместо прежней:\n\n{}\n\nСтраница та же самая и обновляется сама."
)

SVETLANA = 7487296664
MANAGERS = [8829, 11807, 15077, 11357]   # Жернов, Попов, Пономарь, Томаш


def main():
    dry = "--dry" in sys.argv
    with db() as conn:
        plan = []
        for uid in MANAGERS:
            row = conn.execute(
                "SELECT token FROM dash_tokens WHERE active AND kind='manager' "
                "AND portal_user_id=%s", (uid,)).fetchone()
            chat = conn.execute(
                "SELECT chat_id FROM tg_users WHERE manager_id=%s AND active",
                (uid,)).fetchone()
            if not row or not chat:
                log.warning("пропуск: менеджер %s, токен=%s, чат=%s",
                            uid, row, chat)
                continue
            plan.append((chat[0], uid, MGR_TEXT.format(BASE.format(row[0]))))

        row = conn.execute("SELECT token FROM dash_tokens WHERE active "
                           "AND kind='rop' LIMIT 1").fetchone()
        if row:
            plan.append((SVETLANA, 0, ROP_TEXT.format(BASE.format(row[0]))))

        for chat, ref, text in plan:
            if dry:
                print(f"--- чат {chat} / менеджер {ref} ---\n{text}\n")
                continue
            if conn.execute("SELECT 1 FROM tg_sent WHERE kind=%s "
                            "AND chat_id=%s AND ok", (KIND, chat)).fetchone():
                log.info("новую ссылку в чат %s уже слали — пропуск", chat)
                continue
            try:
                tg.send(chat, text, preview=False)
                ok, err = True, None
            except Exception as exc:  # noqa: BLE001
                ok, err = False, str(exc)[:300]
                log.error("ссылка → %s: %s", chat, err)
            conn.execute("INSERT INTO tg_sent (chat_id, kind, ref_id, ok, error) "
                         "VALUES (%s,%s,%s,%s,%s)", (chat, KIND, ref, ok, err))
            log.info("новая ссылка → чат %s (менеджер %s): ok=%s", chat, ref, ok)


if __name__ == "__main__":
    main()
