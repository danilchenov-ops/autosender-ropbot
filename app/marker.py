"""Маркировка лидов в Битриксе по скорингу — вместо телеграм-рассылки (решение Тимофея 20.08).

— Поле «Категория клиента» (UF_CRM_1787201112) заполняется лидам с 1 августа:
    A/B/C/D           — поведенческая модель v2b (точный ClientID из Метрики)
    A≈/B≈/C≈/D≈       — поведение восстановлено сопоставлением по времени (метка «примерно»)
    нет данных        — клиента не видно в Метрике
    номер непригоден  — мусорный телефон
  Заявки старше 1 августа не трогаем: до этой даты скоринга не было.

— Значок первым символом названия:
    ♛  составная оценка ≥ 60 у заявки в работе, где уже состоялся разговор —
       «живой клиент, не отпускать» (composite.py, решение Тимофея 21.08).
       Корона ставится ЛЮБОЙ заявке в работе, в том числе заведённой до августа:
       июльский лид, с которым до сих пор разговаривают, — тот самый случай,
       ради которого всё делалось.
    ⚡  класс A до звонка, ★ класс B — «кому звонить первым», только с 1 августа.
  Корона старше: как только разговор состоялся, вопрос «кому звонить первым»
  уже решён, и значок меняет смысл.
  Огонёк 🔥 и корона 👑 нельзя — Битрикс обрезает названия на четырёхбайтовых
  эмодзи (проверено 21.08 на живом лиде: название обнуляется целиком).

— При смене класса значок заменяется, при потере — снимается.

Запуск: python marker.py [--limit 300]   (в кроне каждые 10 минут; разово --limit 6000)
"""
import sys

from common import Bitrix, db, log
from migrate import migrate

FIELD = "UF_CRM_1787201112"   # «Категория клиента»
SINCE = "2026-08-01"
# Значки в названии лида. Только BMP-символы: ⚡ U+26A1, ★ U+2605, ♛ U+265B.
MARKS = {"A": "⚡", "B": "★"}
CROWN = "♛"
STRIP = "".join(MARKS.values()) + CROWN + " "

WANT_SQL = """
WITH w AS (
    SELECT l.id, l.title,
      CASE WHEN l.date_create >= %(since)s THEN
        CASE
          WHEN coalesce(l.phone_kind,'none') NOT IN ('mobile','landline') THEN 'номер непригоден'
          WHEN s.grade IS NOT NULL AND coalesce(v.match_kind,'exact') = 'time' THEN s.grade || '≈'
          WHEN s.grade IS NOT NULL THEN s.grade
          ELSE 'нет данных'
        END
      END AS want,
      CASE
        WHEN coalesce(c.crown, false) THEN %(crown)s
        WHEN l.date_create < %(since)s THEN ''
        WHEN coalesce(l.phone_kind,'none') NOT IN ('mobile','landline') THEN ''
        WHEN coalesce(v.match_kind,'exact') = 'time' THEN ''
        WHEN s.grade = 'A' THEN %(a)s
        WHEN s.grade = 'B' THEN %(b)s
        ELSE ''
      END AS sym
    FROM leads l
    LEFT JOIN lead_scores s ON s.lead_id = l.id AND s.model_version = 'v2b'
    LEFT JOIN lead_visits v ON v.lead_id = l.id
    LEFT JOIN lead_composite c ON c.lead_id = l.id
    LEFT JOIN lead_marks mm ON mm.lead_id = l.id
    WHERE l.source_id IS DISTINCT FROM 'PARTNER'
      AND l.deleted_at IS NULL           -- удалённые в Битриксе: crm.lead.update даёт «Lead is not found»
      AND (l.date_create >= %(since)s     -- обычная разметка
           OR coalesce(c.crown, false)    -- живой лид из прошлого — корону ставим
           OR mm.sym IS NOT NULL)         -- корону, которая погасла, надо снять
      -- свежий лид с ClientID ждёт обогащения из Метрики (enrich с задержкой 45 мин
      -- против недосчитанной сессии): маркируем один раз, сразу классом
      AND NOT (l.ym_client_id IS NOT NULL AND v.lead_id IS NULL
               AND l.date_create > now() - interval '75 minutes')
)
SELECT w.id, w.title, w.want, w.sym
FROM w LEFT JOIN lead_marks m ON m.lead_id = w.id
WHERE (w.want IS NOT NULL AND m.value IS DISTINCT FROM w.want)
   OR coalesce(m.sym,'') IS DISTINCT FROM w.sym
ORDER BY w.id DESC
LIMIT %(limit)s"""


def main():
    migrate()
    limit = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else 300
    bx = Bitrix()
    done = errors = streak = 0
    with db() as conn:
        rows = conn.execute(WANT_SQL, {"since": SINCE, "limit": limit, "crown": CROWN,
                                       "a": MARKS["A"], "b": MARKS["B"]}).fetchall()
        for lead_id, title, want, sym in rows:
            title = title or ""
            fields = {}
            if want is not None:
                fields[FIELD] = want
            clean = title.lstrip(STRIP)
            new_title = f"{sym} {clean}".strip() if sym else clean
            if new_title != title:
                fields["TITLE"] = new_title
                # страховка: оригинал названия сохраняем ДО правки, один раз на лид
                conn.execute(
                    """INSERT INTO lead_title_backup (lead_id, original_title)
                       VALUES (%s,%s) ON CONFLICT (lead_id) DO NOTHING""",
                    (lead_id, clean))
            else:
                new_title = None
            if not fields:
                continue
            try:
                bx.call("crm.lead.update", {"id": lead_id, "fields": fields})
            except Exception as e:  # noqa: BLE001
                if "not found" in str(e).lower():
                    # удалён в Битриксе, а sweep_deleted_leads видит только открытые —
                    # помечаем сами, чтобы не возвращаться к нему каждый прогон
                    conn.execute("UPDATE leads SET deleted_at = coalesce(deleted_at, now()) "
                                 "WHERE id = %s", (lead_id,))
                    log.warning("маркировка, лид %s: удалён в Битриксе, помечен deleted_at", lead_id)
                    continue
                errors += 1
                streak += 1
                log.warning("маркировка, лид %s: %s", lead_id, str(e)[:150])
                if streak >= 10:
                    log.error("маркировка: слишком много ошибок подряд, стоп")
                    break
                continue
            conn.execute(
                """INSERT INTO lead_marks (lead_id, value, fire, sym, updated_at)
                   VALUES (%s,%s,%s,%s,now())
                   ON CONFLICT (lead_id) DO UPDATE SET
                     value=CASE WHEN EXCLUDED.value = '' THEN lead_marks.value
                                ELSE EXCLUDED.value END,
                     fire=EXCLUDED.fire, sym=EXCLUDED.sym, updated_at=now()""",
                (lead_id, want or '', bool(sym), sym))
            if new_title is not None:
                conn.execute("UPDATE leads SET title=%s WHERE id=%s", (new_title, lead_id))
            done += 1
            streak = 0   # стоп — по ошибкам подряд, а не за прогон
    if done or errors:
        log.info("маркировка: обновлено %s лидов (ошибок %s)", done, errors)


if __name__ == "__main__":
    main()
