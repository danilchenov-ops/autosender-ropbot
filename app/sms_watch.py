# -*- coding: utf-8 -*-
"""Сторож телефонов-шлюзов: сигнал в Телеграм, когда СМС перестали уходить.

Появился после разбора 31.08.2026: телефон Симаненко сутки не забирал очередь,
43 клиента не получили приветствие, и узнали мы об этом только когда Тимофей
спросил. Смысл сторожа — чтобы спрашивать не приходилось.

С 09.09.2026 (просьба Тимофея): при поломке сторож пишет ДВОИМ —
самому менеджеру в бот уходит инструкция «что нажать, чтобы починить»
(текст зависит от типа сбоя), а руководителям — прежний сигнал с пометкой,
доставлена ли менеджеру инструкция.

Два признака беды, любого достаточно:
  1) за последние FAIL_HOURS часов у телефона FAIL_MIN и больше отказов;
  2) хоть одно сообщение висит неотправленным дольше STUCK_MIN минут
     (при ttl=1ч это значит, что оно уже протухло — клиент его не получит).

Смотрим только на свежее — сообщения не старше STALE_H часов. Всё, что
провисело дольше, это уже разобранная история, а не сегодняшняя поломка;
иначе один давний хвост сигналил бы вечно.

Повторный сигнал по одному телефону — не чаще раза в REPEAT_HOURS часов
(и менеджеру, и каждому руководителю — отсчёт у каждого чата свой).

Ключи: --test (ничего не шлём, печатаем), --force (игнорировать паузу).
Крон: каждые 15 минут под тем же выключателем /opt/ropbot/sms_on.
"""
import sys

import tg
from common import db, log

FAIL_HOURS = 3      # окно подсчёта отказов
FAIL_MIN = 3        # столько отказов в окне — уже сигнал
STUCK_MIN = 90      # минут в очереди без ответа шлюза
STALE_H = 24        # старше этого не считаем: это история, а не поломка
REPEAT_HOURS = 6    # не пилить одним и тем же чаще

SQL = f"""
  SELECT d.portal_user_id,
         m.full_name,
         (SELECT count(*) FROM sms_outbox o
           WHERE o.portal_user_id = d.portal_user_id AND o.status = 'failed'
             AND coalesce(o.final_at, o.created_at)
                 > now() - interval '{FAIL_HOURS} hours') AS fails,
         (SELECT count(*) FROM sms_outbox o
           WHERE o.portal_user_id = d.portal_user_id AND o.status = 'sent'
             AND o.sent_at < now() - interval '{STUCK_MIN} minutes'
             AND o.sent_at > now() - interval '{STALE_H} hours') AS stuck,
         (SELECT max(extract(epoch FROM now() - o.sent_at) / 60)::int
            FROM sms_outbox o
           WHERE o.portal_user_id = d.portal_user_id AND o.status = 'sent'
             AND o.sent_at > now() - interval '{STALE_H} hours') AS oldest_min,
         (SELECT o.error FROM sms_outbox o
           WHERE o.portal_user_id = d.portal_user_id AND o.status = 'failed'
             AND coalesce(o.final_at, o.created_at)
                 > now() - interval '{FAIL_HOURS} hours'
           ORDER BY coalesce(o.final_at, o.created_at) DESC LIMIT 1) AS last_err
    FROM sms_devices d
    JOIN managers m ON m.portal_user_id = d.portal_user_id
   WHERE d.active
   ORDER BY m.full_name
"""

MGR_CHAT_SQL = """
  SELECT chat_id FROM tg_users
   WHERE manager_id = %s AND active AND NOT is_boss
   ORDER BY chat_id LIMIT 1
"""

PAUSE_SQL = f"""
  SELECT 1 FROM tg_sent
   WHERE kind = 'sms_stuck' AND ref_id = %s AND chat_id = %s
     AND sent_at > now() - interval '{REPEAT_HOURS} hours' LIMIT 1
"""

MARK_SQL = """
  INSERT INTO tg_sent (chat_id, kind, ref_id, ok, error)
       VALUES (%s, 'sms_stuck', %s, %s, %s)
  ON CONFLICT (kind, ref_id, chat_id) WHERE ok
  DO UPDATE SET sent_at = now(), ok = EXCLUDED.ok, error = EXCLUDED.error
"""


def first_name(full):
    """«М2 Михаил Симаненко» → «Михаил»."""
    parts = [p for p in (full or "").split()
             if not (len(p) <= 3 and p[:1] == "М")]
    return parts[0] if parts else (full or "коллега")


def diagnose(row):
    """Тип сбоя: stuck / limit / auth / fail."""
    _, _, fails, stuck, _, last_err = row
    err = (last_err or "").lower()
    if "limit_exceeded" in err:
        return "limit"
    if ("401" in err or "unauthorized" in err or "credential" in err
            or "password" in err or "login" in err):
        return "auth"
    if stuck:
        return "stuck"
    return "fail"


BASE_STEPS = (
    "Что сделать прямо сейчас:\n"
    "1. Откройте на телефоне приложение SMS Gateway и проверьте, что "
    "включён режим Cloud server (верхний переключатель).\n"
    "2. Проверьте, что у телефона есть интернет.\n"
    "3. Настройки Android → Приложения → SMS Gateway → Батарея → "
    "«Без ограничений», и там же разрешите автозапуск.\n"
    "4. Приложение не закрывайте — просто сверните.")

EXTRA = {
    "limit": ("Судя по ошибке, телефон упёрся в лимит Android на исходящие "
              "СМС. Он отпускает сам примерно через час — главное, чтобы "
              "очередь не копилась, поэтому пункты 1–3 всё равно проверьте."),
    "auth": ("Похоже, в приложении слетел вход. Откройте SMS Gateway, "
             "войдите заново в режим Cloud server и НОВЫЙ пароль сразу "
             "отправьте Тимофею — без этого рассылка не заработает."),
}


def build_fix(row, kind):
    """Инструкция самому менеджеру."""
    _, name, fails, stuck, oldest, last_err = row
    lines = ["%s, СМС-приветствия с вашего телефона не уходят клиентам."
             % first_name(name)]
    if stuck:
        lines.append("В очереди зависло %d сообщение(й), самое старое — "
                     "%d мин: телефон не забирает их из облака." %
                     (stuck, oldest or 0))
    if fails >= FAIL_MIN:
        lines.append("Отказов за последние %d часа: %d." % (FAIL_HOURS, fails))
    lines.append("")
    lines.append(BASE_STEPS)
    if kind in EXTRA:
        lines.append("")
        lines.append(EXTRA[kind])
    lines.append("")
    lines.append("Проверка автоматическая: если через полчаса всё "
                 "заработало — больше писать не буду. Если это сообщение "
                 "пришло повторно — напишите Тимофею.")
    return "\n".join(lines)


def build(row, mgr_note):
    """Сигнал руководителям."""
    uid, name, fails, stuck, oldest, last_err = row
    lines = ["СМС не уходят: %s" % name]
    if fails >= FAIL_MIN:
        lines.append("Отказов за %d ч: %d." % (FAIL_HOURS, fails))
    if stuck:
        lines.append("Висит в очереди: %d, самое старое %d мин."
                     % (stuck, oldest or 0))
    if last_err:
        lines.append("Последняя причина от шлюза: %s" % last_err[:160])
    lines.append("")
    lines.append(mgr_note)
    return "\n".join(lines)


def send(conn, chat, uid, text, force):
    """Отправка с паузой и журналом. Возвращает 'ok'/'pause'/'error'."""
    if not force and conn.execute(PAUSE_SQL, (uid, chat)).fetchone():
        return "pause"
    try:
        # без parse_mode: tg.send(markdown=False) шлёт null,
        # Telegram отвечает «unsupported parse_mode»
        tg.call("sendMessage", chat_id=chat, text=text,
                disable_web_page_preview=True)
        ok, err = True, None
    except Exception as e:  # noqa: BLE001
        ok, err = False, str(e)[:300]
        log.error("сторож СМС → %s: %s", chat, err)
    conn.execute(MARK_SQL, (chat, uid, ok, err))
    return "ok" if ok else "error"


def main():
    test = "--test" in sys.argv
    force = "--force" in sys.argv
    with db() as conn:
        rows = conn.execute(SQL).fetchall()
        bad = [r for r in rows if r[2] >= FAIL_MIN or r[3] > 0]
        if not bad:
            log.info("сторож СМС: все телефоны в порядке (%d шт.)", len(rows))
            return
        chats = [c for (c,) in conn.execute(
            "SELECT chat_id FROM tg_users WHERE is_boss AND active").fetchall()]
        for row in bad:
            uid, name = row[0], row[1]
            kind = diagnose(row)
            fix = build_fix(row, kind)

            # 1) инструкция самому менеджеру
            mgr = conn.execute(MGR_CHAT_SQL, (uid,)).fetchone()
            if test:
                print("--- менеджеру:", mgr[0] if mgr else "НЕ В БОТЕ")
                print(fix)
                st = "ok" if mgr else "нет чата"
            elif mgr:
                st = send(conn, mgr[0], uid, fix, force)
            else:
                st = "нет чата"
            mgr_note = {
                "ok": "Менеджеру отправлена инструкция в бот (тип: %s)." % kind,
                "pause": "Инструкция менеджеру уже уходила за последние "
                         "%d ч, повтор не слал." % REPEAT_HOURS,
                "error": "Инструкцию менеджеру отправить НЕ удалось — "
                         "напишите ему сами.",
                "нет чата": "Менеджер не зарегистрирован в боте — "
                            "инструкцию не доставить, передайте на словах.",
            }[st]

            # 2) дубль руководителям
            text = build(row, mgr_note)
            if test:
                print("--- руководителям:", chats)
                print(text)
                continue
            for chat in chats:
                send(conn, chat, uid, text, force)
            log.warning("сторож СМС: сигнал по %s (отказов %d, зависло %d, "
                        "менеджеру: %s)", name, row[2], row[3], st)


if __name__ == "__main__":
    main()
