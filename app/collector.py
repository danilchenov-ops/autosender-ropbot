"""Коллектор: тянет историю звонков и справочник сотрудников из Битрикс24."""
import datetime as dt
import json
import time

import crm_sync
from common import Bitrix, BitrixError, cfg, db, get_state, log, set_state
from migrate import migrate

OVERLAP = dt.timedelta(minutes=30)  # перекрытие окна, чтобы не терять звонки на границе

# CALL_FAILED_CODE: 200 — успешно, остальное считаем неуспехом
SUCCESS_CODES = {"200", "0", ""}


def parse_ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def direction_of(call_type):
    # 1 исходящий, 2 входящий, 3 входящий с перенаправлением, 4 обратный (callback)
    return "out" if str(call_type) in ("1", "4") else "in"


def sync_managers(bx, conn):
    n = 0
    for u in bx.list_all("user.get", {"FILTER": {"ACTIVE": True}}):
        full = " ".join(x for x in [u.get("NAME"), u.get("LAST_NAME")] if x).strip()
        conn.execute(
            """INSERT INTO managers (portal_user_id, name, last_name, full_name, email, department, active, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,TRUE, now())
               ON CONFLICT (portal_user_id) DO UPDATE SET
                 name=EXCLUDED.name, last_name=EXCLUDED.last_name, full_name=EXCLUDED.full_name,
                 email=EXCLUDED.email, department=EXCLUDED.department, active=TRUE, updated_at=now()""",
            (
                int(u["ID"]),
                u.get("NAME"),
                u.get("LAST_NAME"),
                full or u["ID"],
                u.get("EMAIL"),
                json.dumps(u.get("UF_DEPARTMENT") or []),
            ),
        )
        n += 1
    log.info("Сотрудников синхронизировано: %s", n)


def upsert_call(conn, c):
    call_type = c.get("CALL_TYPE")
    duration = int(c.get("CALL_DURATION") or 0)
    failed = str(c.get("CALL_FAILED_CODE") or "")
    direction = direction_of(call_type)
    is_missed = direction == "in" and (duration == 0 or failed not in SUCCESS_CODES)

    row = conn.execute(
        """INSERT INTO calls (b24_call_id, portal_user_id, call_type, direction, phone_number,
                              portal_number, call_start, duration, failed_code, is_missed,
                              crm_entity_type, crm_entity_id, crm_activity_id,
                              record_url, record_file_id, record_duration, cost, raw)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (b24_call_id) DO UPDATE SET
               duration        = EXCLUDED.duration,
               failed_code     = EXCLUDED.failed_code,
               is_missed       = EXCLUDED.is_missed,
               crm_entity_type = COALESCE(EXCLUDED.crm_entity_type, calls.crm_entity_type),
               crm_entity_id   = COALESCE(EXCLUDED.crm_entity_id,   calls.crm_entity_id),
               record_url      = COALESCE(EXCLUDED.record_url,      calls.record_url),
               record_file_id  = COALESCE(EXCLUDED.record_file_id,  calls.record_file_id),
               raw             = EXCLUDED.raw
           RETURNING id, (xmax = 0) AS inserted""",
        (
            str(c.get("ID")),
            int(c["PORTAL_USER_ID"]) if c.get("PORTAL_USER_ID") else None,
            int(call_type) if call_type else None,
            direction,
            c.get("PHONE_NUMBER"),
            c.get("PORTAL_NUMBER"),
            parse_ts(c.get("CALL_START_DATE")),
            duration,
            failed,
            is_missed,
            c.get("CRM_ENTITY_TYPE"),
            int(c["CRM_ENTITY_ID"]) if c.get("CRM_ENTITY_ID") else None,
            int(c["CRM_ACTIVITY_ID"]) if c.get("CRM_ACTIVITY_ID") else None,
            c.get("CALL_RECORD_URL") or None,
            int(c["RECORD_FILE_ID"]) if c.get("RECORD_FILE_ID") else None,
            int(c["RECORD_DURATION"]) if c.get("RECORD_DURATION") else None,
            c.get("COST"),
            json.dumps(c, ensure_ascii=False),
        ),
    ).fetchone()

    call_id = row[0]

    # В очередь на расшифровку — только содержательные разговоры с записью
    if duration >= cfg.MIN_CALL_SEC and (c.get("CALL_RECORD_URL") or c.get("RECORD_FILE_ID")):
        conn.execute(
            """INSERT INTO asr_queue (call_id, status) VALUES (%s, 'pending')
               ON CONFLICT (call_id) DO NOTHING""",
            (call_id,),
        )
    return call_id


def sync_calls(bx, conn):
    last = get_state(conn, "last_call_start")
    if last:
        since = dt.datetime.fromisoformat(last) - OVERLAP
    else:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=cfg.BACKFILL_DAYS)
        log.info("Первый запуск: забираем историю за %s дней", cfg.BACKFILL_DAYS)

    params = {
        "FILTER": {">CALL_START_DATE": since.isoformat()},
        "SORT": "CALL_START_DATE",
        "ORDER": "ASC",
    }

    count = 0
    newest = since
    for c in bx.list_all("voximplant.statistic.get", params):
        upsert_call(conn, c)
        ts = parse_ts(c.get("CALL_START_DATE"))
        if ts and ts > newest:
            newest = ts
        count += 1
        if count % 500 == 0:
            log.info("Обработано звонков: %s (последний %s)", count, newest)
            set_state(conn, "last_call_start", newest.isoformat())

    set_state(conn, "last_call_start", newest.isoformat())
    log.info("Синхронизация завершена, новых/обновлённых звонков: %s", count)
    return count


def main():
    migrate()
    bx = Bitrix()
    log.info("Коллектор запущен, опрос каждые %s сек", cfg.POLL_INTERVAL)

    while True:
        try:
            with db() as conn:
                # справочник сотрудников — раз в сутки
                last_users = get_state(conn, "last_users_sync")
                if not last_users or (
                    dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_users)
                ) > dt.timedelta(hours=24):
                    sync_managers(bx, conn)
                    set_state(conn, "last_users_sync", dt.datetime.now(dt.timezone.utc).isoformat())

                sync_calls(bx, conn)

                if cfg.SYNC_CRM:
                    try:
                        crm_sync.sync_all(bx, conn)
                    except BitrixError as e:
                        log.error("CRM не синхронизирована: %s", e)
        except BitrixError as e:
            log.error("Ошибка Битрикса: %s", e)
        except Exception as e:  # noqa: BLE001
            log.exception("Сбой цикла: %s", e)

        time.sleep(cfg.POLL_INTERVAL)


if __name__ == "__main__":
    main()
