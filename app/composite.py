"""Составная оценка заявки: 0-100 баллов и корона «живой лид».

Идея Тимофея 21.08.2026: одна цифра, в которой сходятся оценка до звонка,
качество разговоров и отклик клиента, — чтобы менеджер видел живых лидов
и не отпускал их.

Как устроено
------------
Складываются не проценты, а ЛОГ-ШАНСЫ. Блок, про который данных нет, даёт
ровно ноль и оценку не искажает — это и есть «нормировать по доступному»
(решение Тимофея 21.08). Веса подобраны логистической регрессией на когорте
июнь-июль 2026 (7 828 заявок, 89 продаж, база 1,14%), проверены перекрёстно
по хешу телефона: **AUC 0,856 вне выборки**.

Три блока:
  1. Интерес до звонка — поведение на сайте, модель v2b. С 22.08 лог-шансы
     считаются ЖИВЬЁМ из текущих весов score_weights и lead_visits — ровно так,
     как при подборе весов составной оценки. Замороженные lead_scores не
     трогаются: они остаются эталоном для замера точности 2 сентября.
     Коэффициент 0,325: наивный Байес завышает разброс, честная перекрёстная
     проверка v2b даёт AUC 0,648.
  2. Разговоры — длина самого длинного, число состоявшихся, растянут ли диалог
     по дням, суммарные минуты, плюс балл карточки эталонного скрипта, когда
     разбор звонка её посчитал.
  3. Отклик клиента — сам ли перезванивает.

Чего в оценке НЕТ сознательно: числа попыток менеджера. Иначе брошенный лид
получает низкий балл, из-за низкого балла его бросают ещё вернее — и оценка
начинает измерять усердие менеджера вместо живости клиента.

Корона ♛ ставится только заявкам в работе, где уже состоялся разговор и балл
не ниже CROWN_MIN. До звонка работают ⚡/★ из marker.py — «кому звонить первым».
Четырёхбайтовые эмодзи (👑) Битрикс вырезает вместе с названием, проверено.

Запуск: python composite.py [--days 90] [--limit 500]
        python composite.py --dry            (посчитать, в CRM не писать)
"""
import json
import math
import sys

import scorer
from common import Bitrix, db, log
from migrate import migrate

FIELD = "UF_CRM_SCORING"        # «Скоринг», double 0-100, создано 21.08.2026
CROWN_MIN = 60                  # 11,2% конверсии на истории против базы 1,14%
SINCE = "2026-08-01"            # балл пишем тем же заявкам, что размечает marker.py,
                                # плюс любой заявке с короной — даже заведённой раньше
DAYS = 90                       # какой возраст заявок пересчитываем

# ── веса (лог-шансы), lab/composite_final.py ────────────────────────────────
B0 = -6.327
W = {
    "beh_lo":      0.325,   # лог-шанс модели v2b, как есть
    "beh_known":   0.212,
    "talk_2_7m":   0.285,
    "talk_7mp":    0.464,
    "talks_3_5":   0.185,
    "talks_6p":    1.337,
    "span_1d":     1.207,
    "sum_15m":     1.170,
    "in_1":        0.198,
    "in_2p":       0.682,
}
# балл карточки разговора: +0,324 лог-шанса за каждые 10 баллов сверх длительности
# (зеркальная выборка 224 разговора, holdout AUC 0,737). Ужато до 0,75 —
# критерии карточки отбирались на тех же данных, страхуемся.
CARD_PER10 = 0.324 * 0.75
# Ноль карточки — среднее по боевому потоку, а не середина шкалы. В зеркальной
# выборке эталонного скрипта разбирались только первые содержательные исходящие,
# там среднее 46,7; в бою разбирается всякий разговор от 60 секунд, и средний
# максимум по заявке 35,8 (611 заявок августа). Если оставить 50, каждая заявка
# с разобранным разговором получала бы систематический минус, которого в подборе
# весов не было. Пересмотреть при пересборке весов.
CARD_MID = 36.0

LO_MIN, LO_MAX = -6.72, -1.00   # шкала: 0 баллов — практически мёртвая заявка,
                                # 100 — предел, около 27% шанса. Порог короны 60
                                # баллов = 11,2% конверсии на истории (×9,9 к базе)

TITLES = {
    "beh_lo": "поведение на сайте", "beh_known": "клиент виден в Метрике",
    "talk_2_7m": "разговор 2-7 минут", "talk_7mp": "разговор 7+ минут",
    "talks_3_5": "3-5 разговоров", "talks_6p": "6+ разговоров",
    "span_1d": "диалог идёт не первый день", "sum_15m": "суммарно 15+ минут разговоров",
    "in_1": "клиент перезвонил сам", "in_2p": "клиент перезвонил 2+ раза",
    "card": "карточка разговора",
}

SQL = """
SELECT l.id, l.status_semantic, l.phone_e164,
       c.in_ans, c.max_dur, c.talks, c.span_d, c.talk_sec,
       c.card_score
FROM leads l
LEFT JOIN LATERAL (
    SELECT count(*) FILTER (WHERE k.direction='in' AND k.duration>0)      AS in_ans,
           coalesce(max(k.duration) FILTER (WHERE k.duration>0),0)        AS max_dur,
           count(*) FILTER (WHERE k.duration>0)                           AS talks,
           coalesce(extract(epoch FROM (max(k.call_start) FILTER (WHERE k.duration>0)
                    - min(k.call_start) FILTER (WHERE k.duration>0)))/86400.0, 0) AS span_d,
           coalesce(sum(k.duration) FILTER (WHERE k.duration>0),0)        AS talk_sec,
           max(cs.card_score)                                             AS card_score
    FROM calls k
    LEFT JOIN call_scores cs ON cs.call_id = k.id
    WHERE k.phone_e164 = l.phone_e164
      AND k.call_start >= l.date_create - interval '30 min'
) c ON true
WHERE l.date_create >= now() - (%s || ' days')::interval
  AND l.phone_kind IN ('mobile','landline')
  AND l.source_id IS DISTINCT FROM 'PARTNER'
  AND coalesce(l.utm_source,'') <> 'PARTNER'
ORDER BY l.id DESC
"""


def beh_map(conn, days):
    """lead_id -> лог-шанс поведения относительно базы, по ТЕКУЩИМ весам v2b.

    Живой расчёт, а не замороженный p_est из lead_scores: во-первых, так
    считалось при подборе весов составной оценки; во-вторых, заявка, чью
    сессию Метрика досчитала позже (грабля «enrich раньше 45 минут»,
    исправлена 22.08), получает оценку по полным данным, а не по обрезку.
    Замороженные lead_scores не трогаются — они для замера точности.
    """
    lw = scorer.load_weights(conn, "v2b")
    if not lw:
        log.warning("составная оценка: весов v2b нет, блок поведения выключен")
        return {}
    weights, _q, p0 = lw
    base_logit = math.log(p0 / (1 - p0))
    cfg = scorer.MODELS["v2b"]
    rows = conn.execute(
        cfg["sql"] + " AND l.date_create >= now() - (%s || ' days')::interval",
        (days,)).fetchall()
    out = {}
    for r in rows:
        f = cfg["features"](r)
        p = scorer.score_p(f, weights, base_logit, cfg["factors"], cfg["pair"])
        out[r[0]] = math.log(p / (1 - p)) - base_logit
    return out


def evaluate(row, beh_lo):
    """beh_lo: лог-шанс поведения из beh_map, None если клиента не видно."""
    (_id, _st, _ph, in_ans, max_dur, talks, span_d, talk_sec, card) = row
    in_ans, talks = int(in_ans or 0), int(talks or 0)
    max_dur, talk_sec = int(max_dur or 0), int(talk_sec or 0)
    span_d = float(span_d or 0)

    x = {
        "beh_lo": 0.0, "beh_known": 0.0,
        "talk_2_7m": 1.0 if 120 <= max_dur < 420 else 0.0,
        "talk_7mp": 1.0 if max_dur >= 420 else 0.0,
        "talks_3_5": 1.0 if 3 <= talks <= 5 else 0.0,
        "talks_6p": 1.0 if talks >= 6 else 0.0,
        "span_1d": 1.0 if span_d >= 1 else 0.0,
        "sum_15m": 1.0 if talk_sec >= 900 else 0.0,
        "in_1": 1.0 if in_ans == 1 else 0.0,
        "in_2p": 1.0 if in_ans >= 2 else 0.0,
    }
    if beh_lo is not None:
        x["beh_lo"] = beh_lo
        x["beh_known"] = 1.0

    lo = B0
    parts = {}
    for k, v in x.items():
        if v:
            c = W[k] * v
            lo += c
            parts[TITLES[k]] = round(c, 3)
    if card is not None:
        c = CARD_PER10 * (float(card) - CARD_MID) / 10.0
        lo += c
        parts[TITLES["card"]] = round(c, 3)

    pts = int(round(max(0.0, min(100.0, 100 * (lo - LO_MIN) / (LO_MAX - LO_MIN)))))
    p_out = 1 / (1 + math.exp(-lo))
    return pts, lo, p_out, parts, max_dur


def compute(days=DAYS):
    with db() as conn:
        beh = beh_map(conn, days)
        rows = conn.execute(SQL, (days,)).fetchall()
        changed = crowns = 0
        for r in rows:
            pts, lo, p, parts, max_dur = evaluate(r, beh.get(r[0]))
            crown = bool(pts >= CROWN_MIN and max_dur > 0 and r[1] == "P")
            crowns += crown
            cur = conn.execute(
                "SELECT score, crown FROM lead_composite WHERE lead_id=%s", (r[0],)).fetchone()
            if cur and cur[0] == pts and cur[1] == crown:
                continue
            conn.execute(
                """INSERT INTO lead_composite
                     (lead_id, score, logodds, p_est, crown, parts, first_crown_at,
                      peak_score, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s, CASE WHEN %s THEN now() END, %s, now())
                   ON CONFLICT (lead_id) DO UPDATE SET
                     score=EXCLUDED.score, logodds=EXCLUDED.logodds, p_est=EXCLUDED.p_est,
                     crown=EXCLUDED.crown, parts=EXCLUDED.parts,
                     first_crown_at=coalesce(lead_composite.first_crown_at,
                                             EXCLUDED.first_crown_at),
                     peak_score=greatest(coalesce(lead_composite.peak_score,0),
                                         EXCLUDED.score),
                     updated_at=now()""",
                (r[0], pts, round(lo, 4), round(p, 6), crown,
                 json.dumps(parts, ensure_ascii=False), crown, pts))
            changed += 1
        log.info("составная оценка: заявок %s, обновлено %s, корон %s",
                 len(rows), changed, crowns)
        return changed


def push(limit=500):
    """Отправляет балл в поле «Скоринг». Название лида не трогает — этим
    занимается marker.py, единственный владелец TITLE."""
    bx = Bitrix()
    sent = errors = streak = 0
    with db() as conn:
        rows = conn.execute(
            """SELECT c.lead_id, c.score FROM lead_composite c
               JOIN leads l ON l.id = c.lead_id
               LEFT JOIN lead_composite_sent s ON s.lead_id = c.lead_id
               WHERE s.score IS DISTINCT FROM c.score
                 AND l.deleted_at IS NULL         -- удалённые в Битриксе: «Lead is not found»
                 AND (l.date_create >= %s          -- обычная разметка
                      OR c.crown                   -- живой лид из прошлого
                      OR s.lead_id IS NOT NULL)    -- балл уже стоит, держим свежим
               ORDER BY c.lead_id DESC LIMIT %s""", (SINCE, limit)).fetchall()
        for lead_id, score in rows:
            try:
                bx.call("crm.lead.update", {"id": lead_id, "fields": {FIELD: score}})
            except Exception as e:  # noqa: BLE001
                errors += 1
                streak += 1
                log.warning("скоринг, лид %s: %s", lead_id, str(e)[:150])
                if streak >= 10:
                    log.error("скоринг: слишком много ошибок подряд, стоп")
                    break
                continue
            conn.execute(
                """INSERT INTO lead_composite_sent (lead_id, score, sent_at)
                   VALUES (%s,%s,now()) ON CONFLICT (lead_id) DO UPDATE
                   SET score=EXCLUDED.score, sent_at=now()""", (lead_id, score))
            streak = 0   # стоп — по ошибкам подряд, а не за прогон
            sent += 1
    if sent or errors:
        log.info("скоринг: отправлено %s (ошибок %s)", sent, errors)


def main():
    migrate()
    args = sys.argv[1:]
    days = int(args[args.index("--days") + 1]) if "--days" in args else DAYS
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 500
    compute(days)
    if "--dry" not in args:
        push(limit)


if __name__ == "__main__":
    main()
