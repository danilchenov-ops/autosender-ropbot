"""Регистрация сотрудников в боте.

Сотрудник нажимает «Старт», присылает свою фамилию — бот находит его в списке
сотрудников Битрикса и запоминает связку. Никаких уведомлений пока не рассылает.
"""
import time

import tg
from common import db, log
from migrate import migrate

HELLO = (
    "Здравствуйте! Я помогаю не терять клиентов: напоминаю о договорённостях, "
    "которые прозвучали в ваших разговорах.\n\n"
    "Чтобы я понял, кто вы, *напишите свою фамилию* — так же, как она указана в Битриксе."
)

BOUND = (
    "Готово, вы — *{name}*.\n\n"
    "Пока я только учусь и ничего не присылаю. Когда включим напоминания — "
    "получите сообщение от меня."
)

NOT_FOUND = (
    "Не нашёл такого сотрудника. Попробуйте написать фамилию точнее "
    "или полностью, как в Битриксе."
)

AMBIGUOUS = "Нашлось несколько: {names}. Напишите точнее."


def get_offset(conn):
    row = conn.execute("SELECT value FROM tg_state WHERE key='offset'").fetchone()
    return int(row[0]) if row else 0


def set_offset(conn, value):
    conn.execute(
        """INSERT INTO tg_state (key, value) VALUES ('offset', %s)
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
        (str(value),),
    )


def upsert_user(conn, chat):
    conn.execute(
        """INSERT INTO tg_users (chat_id, username, first_name, last_name)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (chat_id) DO UPDATE SET
             username=EXCLUDED.username, first_name=EXCLUDED.first_name,
             last_name=EXCLUDED.last_name, active=TRUE""",
        (chat["id"], chat.get("username"), chat.get("first_name"), chat.get("last_name")),
    )
    # заранее назначенные роли по логину
    conn.execute(
        """UPDATE tg_users u SET is_boss = TRUE FROM tg_roles r
           WHERE u.chat_id = %s AND lower(u.username) = r.username AND r.role = 'boss'""",
        (chat["id"],),
    )


def bind(conn, chat_id, text):
    q = text.strip().split()[0]
    if len(q) < 3:
        return NOT_FOUND
    rows = conn.execute(
        """SELECT portal_user_id, full_name FROM managers
           WHERE active AND (full_name ILIKE %s OR last_name ILIKE %s)""",
        (f"%{q}%", f"%{q}%"),
    ).fetchall()
    if not rows:
        return NOT_FOUND
    if len(rows) > 1:
        return AMBIGUOUS.format(names=", ".join(r[1] for r in rows[:6]))
    conn.execute("UPDATE tg_users SET manager_id=%s WHERE chat_id=%s", (rows[0][0], chat_id))
    log.info("Привязан телеграм %s к сотруднику %s", chat_id, rows[0][1])
    return BOUND.format(name=rows[0][1])


def handle(conn, upd):
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    text = (msg.get("text") or "").strip()
    upsert_user(conn, chat)

    bound = conn.execute(
        "SELECT manager_id FROM tg_users WHERE chat_id=%s", (chat["id"],)
    ).fetchone()

    if text.startswith("/start") or not text:
        tg.send(chat["id"], HELLO)
    elif bound and bound[0]:
        who = conn.execute(
            "SELECT full_name FROM managers WHERE portal_user_id=%s", (bound[0],)
        ).fetchone()
        tg.send(chat["id"], f"Вы уже записаны как *{who[0]}*. Если это ошибка — скажите руководителю.")
    else:
        tg.send(chat["id"], bind(conn, chat["id"], text))


def handle_callback(conn, cb):
    """Кнопки «Да»/«Нет» под вопросом о подтверждении покупки."""
    data = cb.get("data") or ""
    if not data.startswith("sale:"):
        tg.call("answerCallbackQuery", callback_query_id=cb["id"])
        return
    _, ans, lead_id = data.split(":", 2)
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    who = cb.get("from") or {}
    mark = "✅ Покупка подтверждена" if ans == "yes" else "❌ Не покупка"
    # сперва снимаем «часики» у кнопки — иначе Телеграм крутит их всё время,
    # пока мы пишем в базу и правим сообщение (замечено Тимофеем 02.09)
    try:
        tg.call("answerCallbackQuery", _retries=1, _timeout=10,
                callback_query_id=cb["id"], text=mark)
    except Exception:                                   # noqa: BLE001
        pass
    conn.execute(
        """UPDATE sale_confirm
              SET answer = %s, answered_at = now(), answered_by = %s
            WHERE lead_id = %s""",
        (ans, who.get("id"), int(lead_id)))
    base = (msg.get("text") or "").split("\n\n—")[0]
    try:
        tg.call("editMessageText", _retries=1, chat_id=chat_id,
                message_id=msg.get("message_id"),
                text=f"{base}\n\n— {mark}",
                disable_web_page_preview=True)
    except Exception:                                   # noqa: BLE001
        pass
    log.info("подтверждение продажи: лид %s -> %s", lead_id, ans)


# ---------------------------------------------------------------------------
# Учёт подписок на канал. Бот должен быть администратором канала —
# тогда Telegram присылает chat_member на каждое вступление и выход.
# ---------------------------------------------------------------------------
import json as _json
from datetime import datetime as _dt, timezone as _tz

_JOINED = {"member", "administrator", "creator", "restricted"}
_GONE = {"left", "kicked"}


def _action(old, new):
    if old in _GONE and new in _JOINED:
        return "join"
    if old in _JOINED and new in _GONE:
        return "leave"
    return "other"


def handle_chat_member(conn, cm):
    chat = cm.get("chat") or {}
    newm = cm.get("new_chat_member") or {}
    oldm = cm.get("old_chat_member") or {}
    user = newm.get("user") or {}
    link = cm.get("invite_link") or {}
    old, new = oldm.get("status"), newm.get("status")
    act = _action(old, new)
    tg_date = _dt.fromtimestamp(cm.get("date", 0), tz=_tz.utc) if cm.get("date") else None
    conn.execute(
        """INSERT INTO tg_channel_members
             (tg_date, chat_id, chat_title, user_id, username, first_name,
              old_status, new_status, action, invite_link, invite_name, via_request, raw)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (tg_date, chat.get("id"), chat.get("title"), user.get("id"), user.get("username"),
         user.get("first_name"), old, new, act,
         link.get("invite_link"), link.get("name"), bool(cm.get("via_join_request")),
         _json.dumps(cm, ensure_ascii=False)))
    log.info("канал %s: %s %s (%s) via=%s", chat.get("title"), act, user.get("id"),
             user.get("username"), link.get("name") or "-")


def handle_my_chat_member(conn, cm):
    """Бота добавили/сняли в чате — запоминаем chat_id и статус."""
    chat = cm.get("chat") or {}
    newm = cm.get("new_chat_member") or {}
    conn.execute(
        """INSERT INTO tg_bot_chats (chat_id, chat_type, title, bot_status, updated_at, raw)
           VALUES (%s,%s,%s,%s,now(),%s)
           ON CONFLICT (chat_id) DO UPDATE SET chat_type=EXCLUDED.chat_type,
             title=EXCLUDED.title, bot_status=EXCLUDED.bot_status,
             updated_at=now(), raw=EXCLUDED.raw""",
        (chat.get("id"), chat.get("type"), chat.get("title"), newm.get("status"),
         _json.dumps(cm, ensure_ascii=False)))
    log.info("статус бота в «%s» (%s): %s", chat.get("title"), chat.get("id"), newm.get("status"))


def handle_join_request(conn, jr):
    """Заявка на вступление (если канал с одобрением). Фиксируем, не одобряем сами."""
    chat = jr.get("chat") or {}
    user = jr.get("from") or {}
    link = jr.get("invite_link") or {}
    conn.execute(
        """INSERT INTO tg_channel_members
             (chat_id, chat_title, user_id, username, first_name, old_status, new_status,
              action, invite_link, invite_name, via_request, raw)
           VALUES (%s,%s,%s,%s,%s,'left','request','request',%s,%s,true,%s)""",
        (chat.get("id"), chat.get("title"), user.get("id"), user.get("username"),
         user.get("first_name"), link.get("invite_link"), link.get("name"),
         _json.dumps(jr, ensure_ascii=False)))
    log.info("заявка в «%s» от %s via=%s", chat.get("title"), user.get("id"), link.get("name") or "-")



def main():
    migrate()
    info = tg.me()
    log.info("Бот @%s на связи, жду регистраций", info.get("username"))

    while True:
        try:
            with db() as conn:
                offset = get_offset(conn)
                updates = tg.poll(offset + 1)
                for upd in updates:
                    try:
                        if upd.get("callback_query"):
                            handle_callback(conn, upd["callback_query"])
                        elif upd.get("chat_member"):
                            handle_chat_member(conn, upd["chat_member"])
                        elif upd.get("my_chat_member"):
                            handle_my_chat_member(conn, upd["my_chat_member"])
                        elif upd.get("chat_join_request"):
                            handle_join_request(conn, upd["chat_join_request"])
                        else:
                            handle(conn, upd)
                    except Exception as e:  # noqa: BLE001
                        log.error("Обновление %s: %s", upd.get("update_id"), e)
                    set_offset(conn, upd["update_id"])
        except Exception as e:  # noqa: BLE001
            log.error("Опрос телеграма: %s", str(e)[:200])
            time.sleep(15)


if __name__ == "__main__":
    main()
