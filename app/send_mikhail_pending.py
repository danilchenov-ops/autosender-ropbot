"""Досылка Симаненко (8831) после его регистрации в боте — запускается кроном.

Когда он нажмёт «Старт» и пришлёт фамилию, бот привяжет chat_id (tg_register).
Этот скрипт замечает привязку и досылает то, что остальные уже получили:
ссылку на вкладку KPI (дедуп через tg_sent, kind='kpi_link') и два видео.
Печатает SENT_ALL — обёртка снимает себя с крона.
"""
import tg
from common import db

UID = 8831
BASE = "https://rop.autosender.ru:8443/d/{}/kpi.html"
KPI_TEXT = ("Привет! На твоём дашборде появилась новая вкладка «KPI» — тайминги "
            "за 60 дней: как быстро берём заявку в работу, сколько звонков на "
            "каждой стадии и кто доводит клиента дальше. Пин-код не нужен, "
            "страница обновляется сама.\n\n{}")
VIDEOS = ["https://www.youtube.com/watch?v=qtMXWjxmKWM",
          "https://www.youtube.com/watch?v=JvkH-sj3eXQ"]


def main():
    with db() as conn:
        row = conn.execute("SELECT chat_id FROM tg_users WHERE manager_id=%s "
                           "AND active", (UID,)).fetchone()
        if not row:
            print("WAITING")
            return
        chat = row[0]
        token = conn.execute("SELECT token FROM dash_tokens WHERE active AND "
                             "kind='manager' AND portal_user_id=%s",
                             (UID,)).fetchone()[0]
        if not conn.execute("SELECT 1 FROM tg_sent WHERE kind='kpi_link' "
                            "AND chat_id=%s AND ok", (chat,)).fetchone():
            tg.call("sendMessage", chat_id=chat,
                    text=KPI_TEXT.format(BASE.format(token)),
                    disable_web_page_preview=True)
            conn.execute("INSERT INTO tg_sent (chat_id, kind, ref_id, ok) "
                         "VALUES (%s,'kpi_link',%s,TRUE)", (chat, UID))
        for url in VIDEOS:
            tg.call("sendMessage", chat_id=chat, text=url,
                    disable_web_page_preview=False)
        print(f"SENT_ALL chat={chat}")


if __name__ == "__main__":
    main()
