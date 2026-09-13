"""Сторож продаж: перешёл лид в «ТС куплен» — спрашиваем подтверждение.

Решение Тимофея 02.09.2026. Каждый новый переход лида в стадию 13 уходит
Светлане в бот вопросом «Подтвердите покупку» с кнопками «Да»/«Нет».
Счёт продаж на панелях идёт по её ответу (вьюха v_sales), пока ответа нет —
работает автоматическое правило «был на торгах и есть сделка».

Ответы ловит tg_register.py (callback_query), здесь — только рассылка.
Запуск кроном каждые 10 минут.
"""
import datetime as dt
import os

import tg
from common import db, log
from migrate import migrate

VLD = dt.timezone(dt.timedelta(hours=10))
ROP_CHAT = int(os.getenv("SALE_CONFIRM_CHAT", "7487296664"))   # Светлана
B24 = os.getenv("B24_PORTAL", "https://synergosmoto.bitrix24.ru").rstrip("/")
# первые сутки после запуска не тянем весь хвост истории
SINCE_DAYS = int(os.getenv("SALE_CONFIRM_SINCE_DAYS", "3"))

NEW_SQL = """
WITH t13 AS (
    SELECT owner_id AS lead_id, min(created_time) AS t13
      FROM stage_history
     WHERE entity_kind = 'lead' AND stage_id = '13'
       AND created_time >= now() - (%(days)s || ' days')::interval
     GROUP BY owner_id)
SELECT t.lead_id, t.t13, l.assigned_by,
       coalesce(m.name, l.assigned_by::text) AS mgr,
       l.title, l.date_create,
       EXISTS (SELECT 1 FROM stage_history s
                WHERE s.entity_kind = 'lead' AND s.owner_id = l.id
                  AND s.stage_id = '12') AS byl_torgi,
       EXISTS (SELECT 1 FROM deals d WHERE d.lead_id = l.id) AS est_sdelka
  FROM t13 t
  JOIN leads l ON l.id = t.lead_id
  LEFT JOIN managers m ON m.portal_user_id = l.assigned_by
  LEFT JOIN sale_confirm c ON c.lead_id = t.lead_id
 WHERE c.lead_id IS NULL
 ORDER BY t.t13
"""


def clean_title(title, lead_id):
    import re
    t = re.sub(r"\+?\d[\d\s()-]{8,}\d", "", title or "").strip(" ,·-")
    return t or f"лид {lead_id}"


def main():
    migrate()
    with db() as conn:
        rows = conn.execute(NEW_SQL, {"days": SINCE_DAYS}).fetchall()
        if not rows:
            return
        for (lead_id, t13, uid, mgr, title, created,
             byl_torgi, est_sdelka) in rows:
            days = (t13 - created).total_seconds() / 86400
            flag = ("" if (byl_torgi and est_sdelka) else
                    "\n\n⚠️ Заявка не проходила «На торгах»"
                    + ("" if est_sdelka else " и без сделки")
                    + " — похоже на ошибочный статус.")
            link = (f"\n{B24}/crm/lead/details/{lead_id}/" if B24 else "")
            text = (f"Подтвердите покупку авто\n\n"
                    f"*{mgr}* — {clean_title(title, lead_id)}\n"
                    f"Заявка от {created.astimezone(VLD).strftime('%d.%m')}, "
                    f"путь {days:.0f} дн."
                    f"{flag}{link}")
            kb = {"inline_keyboard": [[
                {"text": "✅ Да", "callback_data": f"sale:yes:{lead_id}"},
                {"text": "❌ Нет", "callback_data": f"sale:no:{lead_id}"}]]}
            try:
                msg = tg.call("sendMessage", chat_id=ROP_CHAT, text=text,
                              parse_mode="Markdown",
                              disable_web_page_preview=True,
                              reply_markup=kb)
                conn.execute(
                    """INSERT INTO sale_confirm
                         (lead_id, portal_user_id, t13, chat_id, message_id,
                          sent_at)
                       VALUES (%s,%s,%s,%s,%s, now())
                       ON CONFLICT (lead_id) DO NOTHING""",
                    (lead_id, uid, t13, ROP_CHAT, msg.get("message_id")))
                log.info("спросили подтверждение: лид %s (%s)", lead_id, mgr)
            except Exception as exc:                       # noqa: BLE001
                log.error("лид %s: %s", lead_id, str(exc)[:200])


if __name__ == "__main__":
    main()
