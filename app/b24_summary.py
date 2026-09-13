# -*- coding: utf-8 -*-
"""Сводка разговоров в карточке Битрикса.

Один комментарий в таймлайне на карточку: создаётся при первом разобранном
звонке, дальше только обновляется (crm.timeline.comment.update) — новые
комментарии не плодятся. Сводка собирается ПО КЛИЕНТУ (phone_e164), а не
по карточке: у одного человека бывает несколько карточек-дублей, и в каждой,
по которой был звонок, видна полная история разговоров.

Вызывается из extractor.process() после записи call_scores. Ошибка записи
в Битрикс не должна ломать разбор — вызывающий оборачивает в try/except.
"""
import hashlib

from common import Bitrix, log

MAX_ROWS = 15          # последних разговоров в сводке
HEADER = "СВОДКА РАЗГОВОРОВ (обновляется автоматически, не редактируйте)"

_b24 = None


def b24():
    global _b24
    if _b24 is None:
        _b24 = Bitrix()
    return _b24


def build_text(conn, phone_e164):
    """Текст сводки по всем состоявшимся разговорам клиента."""
    rows = conn.execute(
        """SELECT c.call_start, c.duration,
                  COALESCE(m.full_name, 'менеджер '||c.portal_user_id) AS mgr,
                  cs.summary
           FROM calls c
           JOIN call_scores cs ON cs.call_id = c.id
           LEFT JOIN managers m ON m.portal_user_id = c.portal_user_id
           WHERE c.phone_e164 = %s
             AND cs.summary IS NOT NULL
             AND COALESCE(cs.outcome, '') <> 'не дозвонились'
             AND c.duration >= 60
           ORDER BY c.call_start""",
        (phone_e164,),
    ).fetchall()
    if not rows:
        return None
    hidden = len(rows) - MAX_ROWS
    lines = [HEADER, ""]
    if hidden > 0:
        lines.append(f"…ещё {hidden} более ранних разговоров не показаны")
        rows = rows[-MAX_ROWS:]
        start_n = hidden + 1
    else:
        start_n = 1
    for i, (when, dur, mgr, summary) in enumerate(rows, start=start_n):
        mgr_short = (mgr or "").split()[-1] if mgr else "?"
        mins = round((dur or 0) / 60) or 1
        lines.append(f"{i}) {when.strftime('%d.%m %H:%M')} · {mgr_short} · "
                     f"{mins} мин — {summary.strip()}")
    return "\n".join(lines)


def push(conn, ent_type, ent_id, phone_e164):
    """Создать или обновить комментарий-сводку в карточке."""
    if ent_type not in ("LEAD", "CONTACT") or not ent_id or not phone_e164:
        return
    text = build_text(conn, phone_e164)
    if not text:
        return
    h = hashlib.md5(text.encode()).hexdigest()
    row = conn.execute(
        "SELECT comment_id, content_hash FROM b24_summary_comments "
        "WHERE entity_type=%s AND entity_id=%s",
        (ent_type, ent_id),
    ).fetchone()
    if row and row[1] == h:
        return  # ничего не изменилось
    ent = ent_type.lower()
    if row and row[0]:
        b24().call("crm.timeline.comment.update",
                   {"id": row[0], "fields": {"COMMENT": text}})
        conn.execute(
            "UPDATE b24_summary_comments SET content_hash=%s, updated_at=now() "
            "WHERE entity_type=%s AND entity_id=%s",
            (h, ent_type, ent_id))
        log.info("Сводка обновлена: %s %s", ent, ent_id)
    else:
        res = b24().call("crm.timeline.comment.add",
                         {"fields": {"ENTITY_ID": ent_id, "ENTITY_TYPE": ent,
                                     "COMMENT": text}})
        cid = res.get("result")
        conn.execute(
            """INSERT INTO b24_summary_comments
                 (entity_type, entity_id, comment_id, content_hash)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (entity_type, entity_id) DO UPDATE SET
                 comment_id=EXCLUDED.comment_id,
                 content_hash=EXCLUDED.content_hash, updated_at=now()""",
            (ent_type, ent_id, cid, h))
        log.info("Сводка создана: %s %s, комментарий %s", ent, ent_id, cid)
