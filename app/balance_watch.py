# -*- coding: utf-8 -*-
"""Сторож баланса ProxyAPI: ловит момент, когда деньги кончились, и шлёт
Тимофею в @ironbossAS_bot. Эндпоинт баланса нам закрыт (403), поэтому
детектируем по факту: минимальный пробный запрос к модели возвращает
402 Payment Required — значит баланс на нуле и разбор разговоров встал.

Запуск (в образ не запечён, подаётся через stdin):
    docker exec -i ropbot-collector-1 python - < /opt/ropbot/app/balance_watch.py

Крон: каждые 15 минут. Антиспам — не чаще раза в 3 часа (tg_sent).
"""
import datetime as dt
import os

import requests

import tg
from common import db, log

KEY = os.getenv("PROXYAPI_KEY", "")
BASE = "https://api.proxyapi.ru"
MODEL = os.getenv("LLM_MODEL", "gemini-3.7-flash")
BOSS_CHAT = 460128042          # Тимофей (Tim_danilchen); фолбэк — любой is_boss
ALERT_KIND = "balance_low"
COOLDOWN_H = 3                 # не чаще раза в 3 часа


def ping():
    """(код, тело) минимального запроса к модели. 402 → баланс кончился."""
    try:
        r = requests.post(
            f"{BASE}/google/v1beta/models/{MODEL}:generateContent",
            headers={"Authorization": f"Bearer {KEY}"},
            json={"contents": [{"parts": [{"text": "ok"}]}],
                  "generationConfig": {"maxOutputTokens": 3500,
                                       "thinkingConfig": {"thinkingBudget": 0}}},
            timeout=40)
        return r.status_code, r.text[:200]
    except requests.RequestException as e:
        return None, str(e)[:200]


def stalled(conn):
    """Разбор реально встал: за час ни одной новой карточки, а очередь pending
    не пуста. Failed не считаем: один звонок, упавший на сетевом сбое, сутки
    держал бы условие и каждый тихий вечерний час давал ложный алерт (22.09)."""
    row = conn.execute(
        """SELECT (SELECT count(*) FROM call_extractions
                     WHERE created_at > now() - interval '1 hour'),
                  (SELECT count(*) FROM llm_queue WHERE status = 'pending')"""
    ).fetchone()
    return row[0] == 0 and row[1] > 0


def recently_alerted(conn):
    return conn.execute(
        """SELECT 1 FROM tg_sent WHERE kind=%s AND ok
             AND sent_at > now() - (%s || ' hours')::interval LIMIT 1""",
        (ALERT_KIND, COOLDOWN_H)).fetchone() is not None


def bosses(conn):
    rows = [r[0] for r in conn.execute(
        "SELECT chat_id FROM tg_users WHERE is_boss AND active").fetchall()]
    return rows or [BOSS_CHAT]


def main():
    with db() as conn:
        code, body = ping()
        low = code == 402 or stalled(conn)
        if not low:
            log.info("Баланс ProxyAPI: пробный запрос %s, всё в порядке", code)
            return
        if recently_alerted(conn):
            log.info("Баланс низкий (%s), но уже предупреждали за %s ч — молчу",
                     code, COOLDOWN_H)
            return
        reason = ("баланс исчерпан (402 Payment Required)" if code == 402
                  else f"разбор встал (за час ни одной карточки, пробный запрос {code})")
        head = ("⚠️ *ProxyAPI: закончились деньги*" if code == 402
                else "⚠️ *Разбор разговоров встал*")
        if code == 402:
            tail = ("Скрипт, оценка качества и договорённости не считаются, "
                    "пока не пополните баланс.\n\n"
                    "Пополнить: https://proxyapi.ru — после этого разборы "
                    "догонят сами.")
        else:
            tail = ("Это не деньги: пробный запрос проходит. Похоже на сеть "
                    "или сам сервис — смотреть `docker logs ropbot-llm-1` "
                    "и очередь llm_queue. Разборы догонят сами, как только "
                    "пойдёт.")
        text = (
            head + "\n\n"
            f"Разбор разговоров остановлен — {reason}.\n"
            + tail)
        sent_ok = False
        for chat in bosses(conn):
            try:
                tg.send(chat, text)
                sent_ok = True
            except Exception as e:  # noqa: BLE001
                log.warning("Не смог отправить алерт баланса в %s: %s",
                            chat, str(e)[:150])
        conn.execute(
            "INSERT INTO tg_sent (chat_id, kind, ref_id, ok, error) "
            "VALUES (%s,%s,%s,%s,%s)",
            (BOSS_CHAT, ALERT_KIND,
             dt.datetime.utcnow().strftime("%Y%m%d%H"), sent_ok,
             None if sent_ok else "send failed"))
        log.warning("Баланс ProxyAPI низкий (%s) — Тимофей уведомлён: %s",
                    code, sent_ok)


if __name__ == "__main__":
    main()
