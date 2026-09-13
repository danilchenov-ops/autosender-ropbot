"""Утренний топ заявок для руководителей — ранжирование ТОЛЬКО по поведению
клиента на сайте (модель v2b, Метрика по ClientID). Сигналы CRM в балл не входят;
карточка даёт лишь имя, город и ссылку. Заявки без ClientID — отдельной строкой.

Запуск:
    python digest.py            — всем руководителям (раз в день, повторно не шлёт)
    python digest.py --test     — только Тимофею, без записи «отправлено»
    python digest.py --force    — игнорировать защиту от повтора
"""
import datetime as dt
import re
import sys

import tg
from common import db, log
from scorer import norm_source

B24 = "https://synergosmoto.bitrix24.ru/crm/lead/details/{}/"
TEST_CHAT = 460128042  # Тимофей
MAX_LEN = 3800
GRADE_EMOJI = {"A": "🔥", "B": "🟡", "C": "⚪", "D": "🔻"}
SRC_RU = {"ad": "реклама", "search": "поиск", "direct": "напрямую", "link": "по ссылке",
          "social": "соцсети", "internal": "внутр.", "recommend": "рекомендации"}


def clean(s, limit=28):
    s = re.sub(r"[*_\[\]`]", "", s or "").strip()
    return (s[: limit - 1] + "…") if len(s) > limit else s


def lead_name(title, raw_name, form_model):
    t = (title or "").strip()
    m = re.match(r"^\d+\s*/\s*(.+?)\s*/\s*(.+)$", t)
    if m:
        who, what = m.group(1), m.group(2)
        return clean(f"{who}, {what}" if what.lower() != "другое" else who)
    if raw_name:
        return clean(raw_name + (f", {form_model}" if form_model else ""))
    if t == "Venyoo.ru":
        return "Чат на сайте" + (f", {form_model}" if form_model else "")
    return clean(t) or "Без имени"


def behavior_line(visits, days, pages, secs, saw_lot, src, campaign, device):
    bits = []
    if visits and visits > 1:
        bits.append(f"{visits} визитов" + (f" за {days} дн." if days else " за день"))
    else:
        bits.append("первый визит")
    if pages:
        bits.append(f"{pages} стр/{max(1, (secs or 0) // 60)} мин")
    if saw_lot:
        bits.append("смотрел лоты")
    s = SRC_RU.get(norm_source(src), None)
    if s:
        bits.append(s + (f" ({clean(campaign, 18)})" if campaign and s == "реклама" else ""))
    if device == "PC":
        bits.append("ПК")
    return ", ".join(bits)


def build(conn):
    now_vl = dt.datetime.now(dt.timezone(dt.timedelta(hours=10)))
    rows = conn.execute("""
        SELECT l.id, l.title, l.raw->>'NAME', l.form_model, l.ip_city,
               s.score, s.grade,
               extract(epoch from (now() - l.date_create))/3600 AS age_h,
               (SELECT count(*) FROM calls c
                 WHERE c.phone_e164 = l.phone_e164 AND c.direction = 'out'
                   AND c.call_start >= l.date_create)            AS calls_client,
               v.visits_before, v.days_since_first, v.pageviews, v.seconds,
               v.saw_lot, v.last_source, v.last_campaign, v.device, v.region
        FROM leads l
        JOIN lead_scores s ON s.lead_id = l.id AND s.model_version = 'v2b'
        JOIN lead_visits v ON v.lead_id = l.id
        WHERE l.date_create >= now() - interval '26 hours'
          AND l.source_id IS DISTINCT FROM 'PARTNER'
          AND l.phone_kind IN ('mobile','landline')
        ORDER BY s.score DESC, l.date_create DESC""").fetchall()

    no_data, junk = conn.execute("""
        SELECT count(*) FILTER (WHERE l.phone_kind IN ('mobile','landline')
                                AND NOT EXISTS (SELECT 1 FROM lead_scores s
                                                WHERE s.lead_id=l.id AND s.model_version='v2b')),
               count(*) FILTER (WHERE coalesce(l.phone_kind,'none') NOT IN ('mobile','landline'))
        FROM leads l
        WHERE l.date_create >= now() - interval '26 hours'
          AND l.source_id IS DISTINCT FROM 'PARTNER'""").fetchone()

    if not rows and not no_data:
        return None

    by_grade = {}
    for r in rows:
        by_grade[r[6]] = by_grade.get(r[6], 0) + 1

    head = (f"🎯 *Заявки за сутки по поведению на сайте* — {now_vl.strftime('%d.%m %H:%M')} Влд\n"
            f"С данными Метрики {len(rows)}: 🔥A {by_grade.get('A', 0)} · B {by_grade.get('B', 0)} · "
            f"C {by_grade.get('C', 0)} · D {by_grade.get('D', 0)}\n"
            f"Без ClientID (звонки, мессенджеры): {no_data}"
            + (f", мусорные номера: {junk}" if junk else "") + "\n\n"
            f"*Кому звонить первыми:*\n")

    lines, shown = [], 0
    for (lid, title, raw_name, form_model, ip_city, score, grade, age_h, calls_client,
         visits, days, pages, secs, saw_lot, src, campaign, device, region) in rows:
        if grade not in ("A", "B") and shown >= 6:
            continue
        city = ip_city or region or ""
        parts = [f"{GRADE_EMOJI.get(grade, '')} *{score}* — {lead_name(title, raw_name, form_model)}"
                 + (f" — {clean(city, 20)}" if city else "")]
        parts.append("   " + behavior_line(visits, days, pages, secs, saw_lot, src, campaign, device))
        flag = ("⚠️ ещё не звонили · " if (calls_client == 0 and age_h >= 2)
                else f"звонков: {calls_client} · " if calls_client else "")
        parts.append(f"   {flag}[карточка]({B24.format(lid)})")
        block = "\n".join(parts)
        if sum(len(x) + 1 for x in lines) + len(block) + len(head) > MAX_LEN:
            break
        lines.append(block)
        shown += 1

    rest = len(rows) - shown
    tail = f"\n\n…и ещё {rest} с меньшим баллом." if rest > 0 else ""
    return head + "\n\n".join(lines) + tail


def main():
    test = "--test" in sys.argv
    force = "--force" in sys.argv
    with db() as conn:
        text = build(conn)
        if not text:
            log.info("дайджест: заявок за сутки нет")
            return
        ref = int(dt.datetime.now(dt.timezone(dt.timedelta(hours=10))).strftime("%Y%m%d"))
        chats = [(TEST_CHAT,)] if test else conn.execute(
            "SELECT chat_id FROM tg_users WHERE is_boss AND active").fetchall()
        for (chat,) in chats:
            if not test and not force:
                if conn.execute("SELECT 1 FROM tg_sent WHERE kind='score_digest' AND ref_id=%s "
                                "AND chat_id=%s AND ok", (ref, chat)).fetchone():
                    continue
            try:
                tg.send(chat, text)
                ok, err = True, None
            except Exception as e:  # noqa: BLE001
                ok, err = False, str(e)[:300]
                log.error("дайджест → %s: %s", chat, err)
            if not test:
                conn.execute("INSERT INTO tg_sent (chat_id, kind, ref_id, ok, error) "
                             "VALUES (%s,'score_digest',%s,%s,%s)", (chat, ref, ok, err))
        log.info("дайджест отправлен: %s чатов%s", len(chats), " (тест)" if test else "")


if __name__ == "__main__":
    main()
