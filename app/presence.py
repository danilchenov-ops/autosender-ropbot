"""Присутствие менеджеров: табель Битрикса и признак «в системе».

Два источника, они не заменяют друг друга:

1. `timeman.status` — настоящий табель: во сколько отметился, во сколько закрыл
   день, сколько отработал. Через REST отдаётся только ТЕКУЩИЙ день, истории нет,
   поэтому копим сами. Работает, только если люди реально отмечаются и в портале
   включён учёт рабочего времени.
2. `user.get` → `IS_ONLINE` — открытая сессия и активность в последние минуты.
   Не табель, но не требует ничего от менеджера. Годится сравнивать людей между
   собой и искать дыры в рабочем дне.

Запуск: python presence.py   (одним прогоном, ставится в крон каждые 5 минут)
"""
import datetime as dt

from common import Bitrix, db, log

VLD = dt.timezone(dt.timedelta(hours=10))


def parse_ts(v):
    if not isinstance(v, str) or not v:
        return None
    try:
        return dt.datetime.fromisoformat(v)
    except ValueError:
        return None


def parse_hms(v):
    """«01:22:46» -> секунды."""
    if not isinstance(v, str) or ":" not in v:
        return None
    try:
        h, m, s = (int(x) for x in v.split(":"))
        return h * 3600 + m * 60 + s
    except ValueError:
        return None


def online(bx, conn):
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    rows = []
    for u in bx.list_all("user.get", {"FILTER": {"ACTIVE": True}}):
        uid = int(u.get("ID"))
        is_on = str(u.get("IS_ONLINE") or "N").upper() == "Y"
        rows.append((uid, now, is_on, parse_ts(u.get("LAST_ACTIVITY_DATE")), uid))
    if not rows:
        log.warning("Присутствие: Битрикс вернул пустой список пользователей")
        return 0

    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO user_presence (portal_user_id, ts, is_online, last_activity)
               SELECT %s, %s, %s, %s
               WHERE EXISTS (SELECT 1 FROM managers WHERE portal_user_id = %s)
               ON CONFLICT (portal_user_id, ts) DO NOTHING""",
            rows,
        )
    conn.execute("DELETE FROM user_presence WHERE ts < now() - interval '120 days'")
    return sum(1 for r in rows if r[2])


def timeman(bx, conn):
    """Табель за сегодня по каждому активному менеджеру."""
    uids = [r[0] for r in conn.execute(
        "SELECT portal_user_id FROM managers WHERE active").fetchall()]
    saved = 0
    for uid in uids:
        try:
            res = (bx.call("timeman.status", {"USER_ID": uid}) or {}).get("result") or {}
        except Exception as e:  # noqa: BLE001
            log.warning("Табель %s: %s", uid, str(e)[:150])
            continue
        start = parse_ts(res.get("TIME_START"))
        if not start:
            continue  # человек ни разу не отмечался — записывать нечего
        day = start.astimezone(VLD).date()
        if day != dt.datetime.now(VLD).date():
            continue  # последняя отметка не сегодняшняя, день не начат
        conn.execute(
            """INSERT INTO timeman_days (portal_user_id, day, status, time_start,
                   time_finish, duration_sec, leaks_sec, entry_id, updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
               ON CONFLICT (portal_user_id, day) DO UPDATE SET
                 status=EXCLUDED.status, time_finish=EXCLUDED.time_finish,
                 duration_sec=EXCLUDED.duration_sec, leaks_sec=EXCLUDED.leaks_sec,
                 updated_at=now()""",
            (uid, day, res.get("STATUS"), start, parse_ts(res.get("TIME_FINISH")),
             parse_hms(res.get("DURATION")), parse_hms(res.get("TIME_LEAKS")),
             res.get("ID")),
        )
        saved += 1
    return saved


def main():
    bx = Bitrix()
    with db() as conn:
        n_online = online(bx, conn)
        n_tm = timeman(bx, conn)
    log.info("Присутствие: онлайн %s, отметок в табеле сегодня %s", n_online, n_tm)


if __name__ == "__main__":
    main()
