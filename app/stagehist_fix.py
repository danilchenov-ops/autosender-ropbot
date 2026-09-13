"""Дозаполнение статусов в stage_history для лидов.

Битрикс в crm.stagehistory.list для лидов отдаёт поле STATUS_ID, а коллектор
исторически ждал STAGE_ID — поэтому записи по лидам легли с пустым stage_id.
crm_sync.py исправлен 25.08.2026, но правка запечётся в образ только при
следующем docker compose build; до тех пор новые записи тоже идут пустыми.

Скрипт перечитывает историю из Битрикса и дозаполняет stage_id/stage_semantic
у существующих записей (и вставляет отсутствующие). Идемпотентен: уже
заполненные строки не трогает.

Запуск через stdin (в образ не запечён):
    docker exec -i ropbot-collector-1 python - --days 3 < /opt/ropbot/app/stagehist_fix.py
"""
import argparse
import datetime as dt
import sys

from common import Bitrix, db


def ts(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(v)
    except ValueError:
        return None


def ident(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def run(days):
    bx, conn = Bitrix(), db()
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    params = {
        "entityTypeId": 1,
        "order": {"CREATED_TIME": "ASC"},
        "filter": {">CREATED_TIME": since.isoformat()},
    }
    start, seen, fixed = 0, 0, 0
    while True:
        params["start"] = start
        data = bx.call("crm.stagehistory.list", params)
        result = data.get("result") or {}
        items = (result.get("items") if isinstance(result, dict) else result) or []
        for h in items:
            status = h.get("STAGE_ID") or h.get("STATUS_ID")
            sem = h.get("STAGE_SEMANTIC_ID") or h.get("STATUS_SEMANTIC_ID")
            if not status:
                continue
            cur = conn.execute(
                """INSERT INTO stage_history (id, entity_kind, owner_id, category_id,
                                              stage_id, stage_semantic, created_time)
                   VALUES (%s, 'lead', %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE
                       SET stage_id = EXCLUDED.stage_id,
                           stage_semantic = EXCLUDED.stage_semantic
                     WHERE coalesce(stage_history.stage_id, '') = ''""",
                (ident(h.get("ID")), ident(h.get("OWNER_ID")),
                 ident(h.get("CATEGORY_ID")) or 0, status, sem,
                 ts(h.get("CREATED_TIME"))))
            seen += 1
            fixed += cur.rowcount or 0
        nxt = data.get("next")
        if not items or nxt is None:
            break
        start = nxt
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M} stagehist_fix: просмотрено {seen}, "
          f"дозаполнено {fixed}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()
    try:
        run(args.days)
    except Exception as exc:
        sys.stderr.write(f"stagehist_fix: {exc}\n")
        sys.exit(1)
