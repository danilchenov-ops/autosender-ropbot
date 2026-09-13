# -*- coding: utf-8 -*-
"""Офлайн-конверсии Метрики: факт подписки на канал → цели «Подписка ТГ» (609783020) и «Подписка MAX» (609807401).

Берёт вступления с известным ClientID (или yclid), ещё не отправленные в Метрику,
и загружает их в счётчик 56331115 как достижение цели tg_subscribed (id 609783020).
Требует OAuth-токен Метрики с правом записи. Пока права нет — пишет в лог и выходит.

Запуск: python tg_offline.py          (крон */10 через docker exec)
"""
import io
import os
import time

import requests

from common import db, log

COUNTER = 56331115
TARGET = "tg_subscribed"                  # идентификатор цели 609783020 «Подписка ТГ»
TOKEN = os.getenv("YANDEX_METRIKA_TOKEN", "").strip()
API = f"https://api-metrika.yandex.net/management/v1/counter/{COUNTER}/offline_conversions/upload"
BATCH = 500


def _upload(rows, id_type, id_col, target=TARGET):
    """rows: [(id, ts)] → загрузка одним CSV. Возвращает (ok, msg)."""
    buf = io.StringIO()
    buf.write(f"{id_col},Target,DateTime\n")
    for ident, ts in rows:
        buf.write(f"{ident},{target},{int(ts)}\n")
    r = requests.post(API, params={"client_id_type": id_type},
                      headers={"Authorization": f"OAuth {TOKEN}"},
                      files={"file": ("conv.csv", buf.getvalue().encode("utf-8"), "text/csv")},
                      timeout=60)
    if r.status_code == 403:
        return False, "403: токен Метрики без права записи — загрузка отложена"
    if r.status_code != 200:
        return False, f"{r.status_code}: {r.text[:200]}"
    return True, r.json().get("uploading", {}).get("id")


SOURCES = (
    # таблица, цель, доп. условие, метка, колонка времени
    ("tg_channel_members", "tg_subscribed", "", "TG", "coalesce(tg_date, event_at)"),
    # MAX: только однозначно привязанные к клику (один клик в окне) — чистый сигнал для стратегии
    ("max_channel_members", "max_subscribed", "AND match_quality='exact'", "MAX", "event_at"),
)


def _run(conn, table, target, extra, label, tcol):
    cid_rows = conn.execute(
        f"""SELECT id, client_id, extract(epoch FROM {tcol})
              FROM {table}
             WHERE action='join' AND metrika_sent_at IS NULL AND client_id IS NOT NULL {extra}
             ORDER BY id LIMIT %s""", (BATCH,)).fetchall()
    yc_rows = conn.execute(
        f"""SELECT id, yclid, extract(epoch FROM {tcol})
              FROM {table}
             WHERE action='join' AND metrika_sent_at IS NULL AND client_id IS NULL AND yclid IS NOT NULL {extra}
             ORDER BY id LIMIT %s""", (BATCH,)).fetchall()
    if not cid_rows and not yc_rows:
        return True
    for rows, id_type, col, idl in ((cid_rows, "CLIENT_ID", "ClientId", "ClientID"),
                                    (yc_rows, "YCLID", "Yclid", "yclid")):
        if not rows:
            continue
        ok, msg = _upload([(r[1], r[2]) for r in rows], id_type, col, target)
        if ok:
            conn.execute(f"UPDATE {table} SET metrika_sent_at=now() WHERE id = ANY(%s)", ([r[0] for r in rows],))
            log.info("tg_offline[%s]: отправлено %d по %s, загрузка #%s", label, len(rows), idl, msg)
        else:
            log.warning("tg_offline[%s]: %s (%d строк по %s ждут)", label, msg, len(rows), idl)
            if msg.startswith("403"):
                return False
    return True


def main():
    if not TOKEN:
        log.error("tg_offline: нет YANDEX_METRIKA_TOKEN"); return
    with db() as conn:
        sent_any = False
        for table, target, extra, label, tcol in SOURCES:
            before = conn.execute(f"SELECT count(*) FROM {table} WHERE metrika_sent_at IS NOT NULL").fetchone()[0]
            if not _run(conn, table, target, extra, label, tcol):
                return
            after = conn.execute(f"SELECT count(*) FROM {table} WHERE metrika_sent_at IS NOT NULL").fetchone()[0]
            sent_any = sent_any or after > before
        if not sent_any:
            log.info("tg_offline: нечего отправлять")


if __name__ == "__main__":
    main()
