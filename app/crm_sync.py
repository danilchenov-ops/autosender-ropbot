"""Синхронизация CRM: лиды, сделки, контакты, компании, история стадий, дела."""
import datetime as dt
import json

from common import cfg, get_state, log, set_state

PAGE_BUDGET = 20          # не больше 1000 записей на один проход, потом переанкоримся
UTM = ["UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM"]
# Пользовательские поля лида, нужные скорингу (попадают в raw)
UF_LEAD = [
    "UF_CRM_YANDEX_CL_ID",   # Yandex ClientID (Метрика)
    "UF_CRM_1729578832",     # metrika_client_id (старое поле)
    "UF_CRM_1622530114",     # Текст из формы
    "UF_CRM_1730191118",     # Марка модель
    "UF_CRM_1730191143",     # урл
    "UF_CRM_1742283804",     # Город по IP
    "UF_CRM_ROP_CONTROL",    # отметка РОПа в ленте «Контроль» (101=мёртвый, 103=вернуть, 105=передать)
]


def horizon():
    """Начальная точка сбора CRM — не тащим архив с 2020 года."""
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=cfg.CRM_SINCE_DAYS)


def ts(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def num(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def ident(value):
    try:
        return int(value) if value not in (None, "", "0") else None
    except (TypeError, ValueError):
        return None


def multifield(item, key):
    """PHONE/EMAIL приходят списком словарей."""
    vals = item.get(key) or []
    if isinstance(vals, list):
        return [v.get("VALUE") for v in vals if isinstance(v, dict) and v.get("VALUE")]
    return []


def sync_entity(bx, conn, method, state_key, upsert, select=None,
                date_field="DATE_MODIFY", extra_params=None):
    """Инкрементальный обход списочного метода CRM по метке времени изменения.

    Переанкоривание фильтра каждые 1000 записей избавляет от глубоких offset-ов,
    на которых Битрикс начинает тормозить.
    """
    last = get_state(conn, state_key)
    since = ts(last) or horizon()
    grand_total = 0

    while True:
        params = dict(extra_params or {})
        params["order"] = {date_field: "ASC"}
        params["filter"] = {f">{date_field}": since.isoformat()}
        if select:
            params["select"] = select

        got, start, newest = 0, 0, since
        for _ in range(PAGE_BUDGET):
            params["start"] = start
            data = bx.call(method, params)
            items = data.get("result") or []
            if isinstance(items, dict):
                items = items.get("items") or list(items.values())

            for it in items:
                upsert(conn, it)
                t = ts(it.get(date_field))
                if t and t > newest:
                    newest = t

            got += len(items)
            nxt = data.get("next")
            if not items or nxt is None:
                break
            start = nxt

        grand_total += got
        if got == 0:
            break

        # защита от зацикливания, если у пачки записей одинаковая метка времени
        since = newest if newest > since else since + dt.timedelta(seconds=1)
        set_state(conn, state_key, since.isoformat())

        if got < PAGE_BUDGET * 50:
            break

    if grand_total:
        log.info("%s: получено %s записей", method, grand_total)
    return grand_total


# ── Удалённые в Битриксе лиды ────────────────────────────────────────────────
# Списочный метод отдаёт только живые записи: лид, удалённый в CRM, у нас
# остаётся навсегда в последнем статусе и продолжает считаться открытым.
# Найдено 09.09.2026: у Томаша панель показывала 19 «Не обработан» при 13
# в CRM — шесть удалённых лидов (тестовые «сторож закрепления», спам).
# Раз в SWEEP_EVERY_H проверяем все открытые (semantic 'P') лиды по ID
# пачками; тех, кого Битрикс не вернул, помечаем deleted_at и semantic 'D'.
# status_id не трогаем — история статусов остаётся читаемой.
SWEEP_EVERY_H = 1
SWEEP_CHUNK = 50


def sweep_deleted_leads(bx, conn, force=False):
    last = get_state(conn, "sweep_deleted_leads")
    now = dt.datetime.now(dt.timezone.utc)
    if not force and last and (now - dt.datetime.fromisoformat(last)) < dt.timedelta(hours=SWEEP_EVERY_H):
        return 0
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM leads WHERE status_semantic = 'P' AND deleted_at IS NULL ORDER BY id")]
    gone = []
    for i in range(0, len(ids), SWEEP_CHUNK):
        chunk = ids[i:i + SWEEP_CHUNK]
        alive = set()
        start = 0
        while True:
            data = bx.call("crm.lead.list", {"filter": {"ID": chunk}, "select": ["ID"], "start": start})
            for it in data.get("result") or []:
                alive.add(int(it["ID"]))
            if data.get("next") is None:
                break
            start = data["next"]
        gone.extend(x for x in chunk if x not in alive)
    if gone:
        conn.execute(
            "UPDATE leads SET deleted_at = now(), status_semantic = 'D', synced_at = now() "
            "WHERE id = ANY(%s)", (gone,))
        log.info("удалённых в Битриксе лидов помечено: %s (%s)", len(gone), gone[:20])
    set_state(conn, "sweep_deleted_leads", now.isoformat())
    return len(gone)


# ── Справочники ──────────────────────────────────────────────────────────────

def sync_dicts(bx, conn):
    for s in bx.list_all("crm.status.list", {"order": {"SORT": "ASC"}}):
        conn.execute(
            """INSERT INTO crm_dict (kind, status_id, name, sort, semantics)
               VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (kind, status_id) DO UPDATE SET
                 name=EXCLUDED.name, sort=EXCLUDED.sort, semantics=EXCLUDED.semantics""",
            (s.get("ENTITY_ID"), s.get("STATUS_ID"), s.get("NAME"),
             ident(s.get("SORT")), s.get("SEMANTICS")),
        )

    data = bx.call("crm.dealcategory.list", {"order": {"SORT": "ASC"}})
    for c in data.get("result") or []:
        conn.execute(
            """INSERT INTO deal_categories (id, name, sort) VALUES (%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, sort=EXCLUDED.sort""",
            (ident(c.get("ID")), c.get("NAME"), ident(c.get("SORT"))),
        )
    # воронка по умолчанию в справочнике не возвращается
    conn.execute(
        "INSERT INTO deal_categories (id, name, sort) VALUES (0, 'Основная воронка', 0) "
        "ON CONFLICT (id) DO NOTHING"
    )
    log.info("Справочники CRM обновлены")


# ── Сущности ─────────────────────────────────────────────────────────────────

def upsert_lead(conn, l):
    phones = multifield(l, "PHONE")
    emails = multifield(l, "EMAIL")
    conn.execute(
        """INSERT INTO leads (id, title, status_id, status_semantic, source_id, assigned_by,
                              date_create, date_modify, date_closed, opportunity, currency,
                              contact_id, company_id, phone, email,
                              utm_source, utm_medium, utm_campaign, utm_content, utm_term, raw, synced_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
           ON CONFLICT (id) DO UPDATE SET
             title=EXCLUDED.title, status_id=EXCLUDED.status_id,
             status_semantic=EXCLUDED.status_semantic, assigned_by=EXCLUDED.assigned_by,
             date_modify=EXCLUDED.date_modify, date_closed=EXCLUDED.date_closed,
             opportunity=EXCLUDED.opportunity, raw=EXCLUDED.raw, synced_at=now()""",
        (
            ident(l.get("ID")), l.get("TITLE"), l.get("STATUS_ID"), l.get("STATUS_SEMANTIC_ID"),
            l.get("SOURCE_ID"), ident(l.get("ASSIGNED_BY_ID")),
            ts(l.get("DATE_CREATE")), ts(l.get("DATE_MODIFY")), ts(l.get("DATE_CLOSED")),
            num(l.get("OPPORTUNITY")), l.get("CURRENCY_ID"),
            ident(l.get("CONTACT_ID")), ident(l.get("COMPANY_ID")),
            phones[0] if phones else None, emails[0] if emails else None,
            l.get("UTM_SOURCE"), l.get("UTM_MEDIUM"), l.get("UTM_CAMPAIGN"),
            l.get("UTM_CONTENT"), l.get("UTM_TERM"),
            json.dumps(l, ensure_ascii=False),
        ),
    )


def upsert_deal(conn, d):
    conn.execute(
        """INSERT INTO deals (id, title, category_id, stage_id, stage_semantic, assigned_by,
                              date_create, date_modify, begindate, closedate, closed,
                              opportunity, currency, lead_id, contact_id, company_id, source_id,
                              utm_source, utm_medium, utm_campaign, utm_content, utm_term, raw, synced_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
           ON CONFLICT (id) DO UPDATE SET
             title=EXCLUDED.title, category_id=EXCLUDED.category_id, stage_id=EXCLUDED.stage_id,
             stage_semantic=EXCLUDED.stage_semantic, assigned_by=EXCLUDED.assigned_by,
             date_modify=EXCLUDED.date_modify, closedate=EXCLUDED.closedate, closed=EXCLUDED.closed,
             opportunity=EXCLUDED.opportunity, raw=EXCLUDED.raw, synced_at=now()""",
        (
            ident(d.get("ID")), d.get("TITLE"), ident(d.get("CATEGORY_ID")) or 0,
            d.get("STAGE_ID"), d.get("STAGE_SEMANTIC_ID"), ident(d.get("ASSIGNED_BY_ID")),
            ts(d.get("DATE_CREATE")), ts(d.get("DATE_MODIFY")),
            ts(d.get("BEGINDATE")), ts(d.get("CLOSEDATE")), d.get("CLOSED") == "Y",
            num(d.get("OPPORTUNITY")), d.get("CURRENCY_ID"),
            ident(d.get("LEAD_ID")), ident(d.get("CONTACT_ID")), ident(d.get("COMPANY_ID")),
            d.get("SOURCE_ID"),
            d.get("UTM_SOURCE"), d.get("UTM_MEDIUM"), d.get("UTM_CAMPAIGN"),
            d.get("UTM_CONTENT"), d.get("UTM_TERM"),
            json.dumps(d, ensure_ascii=False),
        ),
    )


def upsert_contact(conn, c):
    conn.execute(
        """INSERT INTO contacts (id, full_name, phones, emails, company_id, assigned_by,
                                 date_create, date_modify, raw)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (id) DO UPDATE SET
             full_name=EXCLUDED.full_name, phones=EXCLUDED.phones, emails=EXCLUDED.emails,
             date_modify=EXCLUDED.date_modify, raw=EXCLUDED.raw""",
        (
            ident(c.get("ID")),
            " ".join(x for x in [c.get("NAME"), c.get("LAST_NAME")] if x).strip() or None,
            multifield(c, "PHONE"), multifield(c, "EMAIL"),
            ident(c.get("COMPANY_ID")), ident(c.get("ASSIGNED_BY_ID")),
            ts(c.get("DATE_CREATE")), ts(c.get("DATE_MODIFY")),
            json.dumps(c, ensure_ascii=False),
        ),
    )


def upsert_company(conn, c):
    conn.execute(
        """INSERT INTO companies (id, title, phones, assigned_by, date_create, date_modify, raw)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (id) DO UPDATE SET
             title=EXCLUDED.title, phones=EXCLUDED.phones,
             date_modify=EXCLUDED.date_modify, raw=EXCLUDED.raw""",
        (
            ident(c.get("ID")), c.get("TITLE"), multifield(c, "PHONE"),
            ident(c.get("ASSIGNED_BY_ID")), ts(c.get("DATE_CREATE")), ts(c.get("DATE_MODIFY")),
            json.dumps(c, ensure_ascii=False),
        ),
    )


def upsert_activity(conn, a):
    conn.execute(
        """INSERT INTO activities (id, owner_type_id, owner_id, type_id, provider_id, provider_type,
                                   direction, subject, created, end_time, completed,
                                   responsible_id, associated_entity_id, raw)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (id) DO UPDATE SET
             completed=EXCLUDED.completed, end_time=EXCLUDED.end_time, raw=EXCLUDED.raw""",
        (
            ident(a.get("ID")), ident(a.get("OWNER_TYPE_ID")), ident(a.get("OWNER_ID")),
            ident(a.get("TYPE_ID")), a.get("PROVIDER_ID"), a.get("PROVIDER_TYPE_ID"),
            ident(a.get("DIRECTION")), a.get("SUBJECT"),
            ts(a.get("CREATED")), ts(a.get("END_TIME")), a.get("COMPLETED") == "Y",
            ident(a.get("RESPONSIBLE_ID")), ident(a.get("ASSOCIATED_ENTITY_ID")),
            json.dumps(a, ensure_ascii=False),
        ),
    )


def sync_stage_history(bx, conn, kind, entity_type_id):
    state_key = f"stagehist_{kind}"
    last = get_state(conn, state_key)
    since = ts(last) or horizon()
    total, newest = 0, since

    params = {
        "entityTypeId": entity_type_id,
        "order": {"CREATED_TIME": "ASC"},
        "filter": {">CREATED_TIME": since.isoformat()},
    }
    start = 0
    for _ in range(200):  # до 10 000 записей за проход
        params["start"] = start
        data = bx.call("crm.stagehistory.list", params)
        result = data.get("result") or {}
        items = result.get("items") if isinstance(result, dict) else result
        items = items or []
        for h in items:
            conn.execute(
                """INSERT INTO stage_history (id, entity_kind, owner_id, category_id,
                                              stage_id, stage_semantic, created_time)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING""",
                (
                    ident(h.get("ID")), kind, ident(h.get("OWNER_ID")),
                    ident(h.get("CATEGORY_ID")) or 0,
                    # у лидов Битрикс называет поле STATUS_ID, у сделок STAGE_ID
                    h.get("STAGE_ID") or h.get("STATUS_ID"),
                    h.get("STAGE_SEMANTIC_ID") or h.get("STATUS_SEMANTIC_ID"),
                    ts(h.get("CREATED_TIME")),
                ),
            )
            t = ts(h.get("CREATED_TIME"))
            if t and t > newest:
                newest = t
        total += len(items)
        nxt = data.get("next")
        if not items or nxt is None:
            break
        start = nxt

    if total:
        set_state(conn, state_key, newest.isoformat())
        log.info("История стадий (%s): %s записей", kind, total)
    return total


def sync_all(bx, conn):
    sync_dicts(bx, conn)

    lead_select = ["*"] + UTM + UF_LEAD + ["PHONE", "EMAIL"]
    deal_select = ["*"] + UTM

    sync_entity(bx, conn, "crm.lead.list", "sync_leads", upsert_lead, select=lead_select)
    sync_entity(bx, conn, "crm.deal.list", "sync_deals", upsert_deal, select=deal_select)
    sync_entity(bx, conn, "crm.contact.list", "sync_contacts", upsert_contact,
                select=["*", "PHONE", "EMAIL"])
    sync_entity(bx, conn, "crm.company.list", "sync_companies", upsert_company,
                select=["*", "PHONE"])
    sync_entity(bx, conn, "crm.activity.list", "sync_activities", upsert_activity,
                date_field="CREATED", select=["*"])

    sync_stage_history(bx, conn, "lead", 1)
    sync_stage_history(bx, conn, "deal", 2)
    sweep_deleted_leads(bx, conn)
