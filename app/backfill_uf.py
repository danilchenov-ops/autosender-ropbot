"""Бэкфилл UF-полей лидов (ClientID Метрики, текст формы и др.) в leads.raw.

Дотягивает поля из crm.lead.list для лидов за N дней и вливает их в raw —
generated-колонки (ym_client_id, form_text, ...) пересчитываются сами.
Идемпотентен, можно перезапускать.

Запуск в контейнере:
    docker exec -d ropbot-collector-1 sh -c 'python backfill_uf.py 365 > /tmp/backfill_uf.log 2>&1'
"""
import datetime as dt
import json
import sys

from common import Bitrix, db, log
from crm_sync import UF_LEAD


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 365
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    bx = Bitrix()
    done = 0

    with db() as conn:
        anchor = since
        while True:
            params = {
                "order": {"DATE_CREATE": "ASC"},
                "filter": {">DATE_CREATE": anchor.isoformat()},
                "select": ["ID", "DATE_CREATE"] + UF_LEAD,
            }
            got, start, newest = 0, 0, anchor
            for _ in range(20):  # переанкоривание каждые ~1000 записей
                params["start"] = start
                data = bx.call("crm.lead.list", params)
                items = data.get("result") or []
                for it in items:
                    patch = {k: it.get(k) for k in UF_LEAD}
                    conn.execute(
                        "UPDATE leads SET raw = coalesce(raw, '{}'::jsonb) || %s::jsonb WHERE id = %s",
                        (json.dumps(patch, ensure_ascii=False), int(it["ID"])),
                    )
                    try:
                        t = dt.datetime.fromisoformat(it["DATE_CREATE"])
                        if t > newest:
                            newest = t
                    except (ValueError, KeyError):
                        pass
                got += len(items)
                nxt = data.get("next")
                if not items or nxt is None:
                    break
                start = nxt

            done += got
            if got == 0:
                break
            anchor = newest if newest > anchor else anchor + dt.timedelta(seconds=1)
            log.info("бэкфилл UF: %s лидов (дошёл до %s)", done, anchor.date())

    log.info("бэкфилл UF завершён: %s лидов", done)


if __name__ == "__main__":
    main()
