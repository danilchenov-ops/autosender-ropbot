"""Журнал ленты «Контроль» РОПа — синк перед пересборкой страниц.

Карточка в ленте = открытая запись в rop_control. Скрипт:
  1) заводит записи по новым сбоям (закрыли живого / горячий стынет);
  2) закрывает записи, по которым РОП или менеджер что-то сделали в Битриксе:
     returned — лид снова в работе; reassigned — сменился ответственный;
     called — появился исходящий по номеру; dismissed — РОП убрал кнопкой
     на странице (канал web/actd.py); confirmed_dead — отметка
     «Мёртвый, закрыт верно» в поле «Контроль РОПа»; closed — стынущий лид
     закрыли (дальше он кандидат в «закрыли живого»); expired — 5 дней
     без реакции, уходит в счётчик «пропущено».

Повторный вход той же заявки по той же причине — не раньше чем через 7 дней
после резолюции. Кнопок и бэкенда нет: РОП действует в Битриксе, лента
подхватывает при следующей пересборке (15 минут).

Правила счёта соблюдены: телефоны mobile/landline, PARTNER исключён по обоим
полям, звонки считаются по номеру клиента от первой его карточки (правило 7),
в списке 1 дубли отсечены (по клиенту есть карточка в работе или выигранная —
это не потеря), в обоих списках на клиента — одна карточка, самая свежая.

Запуск: docker exec -i ropbot-collector-1 python - < /opt/ropbot/web/control.py
"""
import datetime as dt

from common import db, log

SCORE_MIN = 60        # порог составной оценки: с какого балла заявка «живая»
COLD_HOURS = 48       # тишина по исходящим, после которой горячий «стынет»
EXPIRE_DAYS = 5       # без реакции РОПа карточка уходит в «пропущено»
REENTER_DAYS = 7      # раньше этого срока заявка в ленту повторно не входит
CLOSED_WINDOW_D = 7   # закрытые смотрим не глубже недели, чтобы не начинать с архива
DEAD_ENUM = "101"     # enum «Мёртвый, закрыт верно» поля UF_CRM_ROP_CONTROL (ID 1989)

BASE = """
  SELECT l.id, l.phone_e164, l.assigned_by, l.status_semantic, l.date_create,
         lc.score
    FROM leads l
    JOIN lead_composite lc ON lc.lead_id = l.id
   WHERE lc.score >= %(score)s
     AND l.phone_kind IN ('mobile', 'landline')
     AND l.source_id IS DISTINCT FROM 'PARTNER'
     AND coalesce(l.utm_source, '') <> 'PARTNER'
"""

# один клиент — одна карточка (самая свежая), сортировка тут не важна
DEDUP = "SELECT DISTINCT ON (phone_e164) * FROM cand ORDER BY phone_e164, date_create DESC"

CLOSED_ALIVE = f"""
WITH cand AS ({BASE}
     AND l.status_semantic = 'F'
     AND l.date_closed >= now() - make_interval(days => %(closed_w)s)
     AND NOT EXISTS (SELECT 1 FROM leads l2
                      WHERE l2.phone_e164 = l.phone_e164 AND l2.id <> l.id
                        AND l2.status_semantic IN ('P', 'S')))
{DEDUP}
"""

GOING_COLD = f"""
WITH cand AS ({BASE}
     AND l.status_semantic = 'P'
     AND EXISTS (SELECT 1 FROM calls c
                  JOIN (SELECT min(date_create) d FROM leads
                         WHERE phone_e164 = l.phone_e164) f ON TRUE
                 WHERE c.phone_e164 = l.phone_e164
                   AND c.call_start >= f.d - interval '30 minutes'
                   AND c.duration > 0)
     AND NOT EXISTS (SELECT 1 FROM calls c
                      WHERE c.phone_e164 = l.phone_e164 AND c.direction = 'out'
                        AND c.call_start >= now() - make_interval(hours => %(cold_h)s)))
{DEDUP}
"""


def enter(conn, reason, sql):
    n = conn.execute(f"""
        INSERT INTO rop_control (lead_id, reason, score_at_entry, manager_at_entry)
        SELECT c.id, %(reason)s, c.score, c.assigned_by
          FROM ({sql}) c
         WHERE NOT EXISTS (SELECT 1 FROM rop_control r
                            WHERE r.lead_id = c.id AND r.reason = %(reason)s
                              AND (r.resolved_at IS NULL
                                   -- «мёртвый» и «убрано РОПом» — навсегда
                                   OR r.resolution IN ('confirmed_dead', 'dismissed')
                                   OR r.resolved_at >= now() - make_interval(days => %(reenter)s)))
        ON CONFLICT (lead_id, reason) WHERE resolved_at IS NULL DO NOTHING
        """, {"reason": reason, "score": SCORE_MIN, "cold_h": COLD_HOURS,
              "closed_w": CLOSED_WINDOW_D, "reenter": REENTER_DAYS}).rowcount
    if n:
        log.info("Контроль: вошло %s по причине %s", n, reason)


def resolve(conn):
    rows = conn.execute("""
        SELECT r.id, r.reason, r.entered_at, r.manager_at_entry,
               l.status_semantic, l.assigned_by, l.phone_e164,
               l.raw ->> 'UF_CRM_ROP_CONTROL' AS rop_mark,
               EXISTS (SELECT 1 FROM calls c
                        WHERE c.phone_e164 = l.phone_e164 AND c.direction = 'out'
                          AND c.call_start >= r.entered_at) AS out_after_entry
          FROM rop_control r JOIN leads l ON l.id = r.lead_id
         WHERE r.resolved_at IS NULL""").fetchall()

    now = dt.datetime.now(dt.timezone.utc)
    done = {}
    for rid, reason, entered, mgr0, sem, mgr, phone, mark, out_after in rows:
        res = None
        if mark == DEAD_ENUM:
            res = "confirmed_dead"
        elif reason == "closed_alive":
            if sem in ("P", "S"):
                res = "returned"
            elif mgr != mgr0:
                res = "reassigned"
        else:  # going_cold
            if out_after:
                res = "called"
            elif sem in ("F", "S"):
                res = "closed"
            elif mgr != mgr0:
                res = "reassigned"
        if res is None and entered < now - dt.timedelta(days=EXPIRE_DAYS):
            res = "expired"
        if res:
            done.setdefault(res, []).append(rid)

    for res, ids in done.items():
        conn.execute("UPDATE rop_control SET resolved_at = now(), resolution = %s "
                     "WHERE id = ANY(%s)", (res, ids))
        log.info("Контроль: %s -> %s", len(ids), res)


def main():
    conn = db()
    resolve(conn)          # сначала резолюции: released-слоты не мешают входу
    enter(conn, "closed_alive", CLOSED_ALIVE)
    enter(conn, "going_cold", GOING_COLD)
    open_n = conn.execute("SELECT reason, count(*) FROM rop_control "
                          "WHERE resolved_at IS NULL GROUP BY 1").fetchall()
    log.info("Контроль: открыто %s", dict(open_n))


if __name__ == "__main__":
    main()
