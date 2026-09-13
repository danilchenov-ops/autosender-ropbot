"""Диагностика портала Битрикс24: что реально доступно вебхуку и в каком виде.

Ничего не меняет, только читает. Телефоны и почты в выводе маскируются.
Запуск:  docker compose run --rm collector python probe.py
"""
import re

from common import Bitrix, BitrixError, log  # noqa: F401

MASK = re.compile(r"(\+?\d[\d\-\s()]{5,})")


def mask(value):
    if not isinstance(value, str):
        return value
    return MASK.sub(lambda m: m.group(0)[:4] + "***" + m.group(0)[-2:], value)


def head(title):
    print("\n" + "─" * 70)
    print(title)
    print("─" * 70)


def try_call(bx, method, params=None, quiet=False):
    try:
        return bx.call(method, params or {})
    except BitrixError as e:
        if not quiet:
            print(f"  ✗ {method}: {e}")
        return None


def sample_fields(items, limit=1):
    if not items:
        return
    it = items[0]
    filled = {k: v for k, v in it.items() if v not in (None, "", [], {})}
    print(f"  Заполненные поля первой записи ({len(filled)} из {len(it)}):")
    for k in sorted(filled):
        v = filled[k]
        v = str(v)
        if len(v) > 90:
            v = v[:90] + "…"
        print(f"    {k:28} = {mask(v)}")


def main():
    bx = Bitrix()

    head("1. Кто мы и какие права")
    d = try_call(bx, "profile")
    if d:
        r = d.get("result", {})
        print(f"  Портал   : {r.get('NAME')} {r.get('LAST_NAME')}, ID {r.get('ID')}")
        print(f"  Админ    : {r.get('ADMIN')}")
    d = try_call(bx, "scope")
    if d:
        print(f"  Права    : {', '.join(sorted(d.get('result', [])))}")

    head("2. Телефония — из чего состоит запись о звонке")
    d = try_call(bx, "voximplant.statistic.get", {
        "SORT": "CALL_START_DATE", "ORDER": "DESC", "start": 0,
    })
    if d:
        items = d.get("result") or []
        print(f"  Всего звонков в истории: {d.get('total')}")
        with_rec = [c for c in items if c.get("CALL_RECORD_URL")]
        print(f"  На первой странице записей со ссылкой: {len(with_rec)} из {len(items)}")
        sample_fields(with_rec or items)

    head("3. CRM — объёмы")
    for method, label in [
        ("crm.lead.list", "Лиды"),
        ("crm.deal.list", "Сделки"),
        ("crm.contact.list", "Контакты"),
        ("crm.company.list", "Компании"),
        ("crm.activity.list", "Дела"),
    ]:
        d = try_call(bx, method, {"start": 0})
        if d:
            print(f"  {label:10} : {d.get('total')}")

    head("4. Есть ли UTM-метки у лидов")
    d = try_call(bx, "crm.lead.list", {
        "order": {"DATE_CREATE": "DESC"},
        "select": ["ID", "TITLE", "SOURCE_ID", "STATUS_ID",
                   "UTM_SOURCE", "UTM_MEDIUM", "UTM_CAMPAIGN", "UTM_CONTENT", "UTM_TERM"],
        "start": 0,
    })
    if d:
        items = d.get("result") or []
        with_utm = [x for x in items if x.get("UTM_SOURCE")]
        print(f"  Из последних {len(items)} лидов с UTM_SOURCE: {len(with_utm)}")
        for x in items[:5]:
            print(f"    #{x.get('ID')}: source={x.get('SOURCE_ID')} "
                  f"utm={x.get('UTM_SOURCE')}/{x.get('UTM_CAMPAIGN')}")

    head("5. Воронки и стадии")
    d = try_call(bx, "crm.dealcategory.list", {})
    if d:
        cats = d.get("result") or []
        print(f"  Воронок (кроме основной): {len(cats)}")
        for c in cats[:10]:
            print(f"    {c.get('ID')}: {c.get('NAME')}")
    d = try_call(bx, "crm.status.list", {"start": 0})
    if d:
        kinds = sorted({s.get("ENTITY_ID") for s in (d.get("result") or [])})
        print(f"  Справочники стадий: {', '.join(kinds)}")

    head("6. История движения по стадиям")
    for kind, tid in (("сделки", 2), ("лиды", 1)):
        d = try_call(bx, "crm.stagehistory.list", {"entityTypeId": tid, "start": 0})
        if d:
            res = d.get("result") or {}
            items = res.get("items") if isinstance(res, dict) else res
            print(f"  {kind}: доступно, записей на странице {len(items or [])}, всего {d.get('total')}")

    head("7. Открытые линии — чаты с клиентами")
    d = try_call(bx, "crm.activity.list", {
        "filter": {"PROVIDER_ID": "IMOPENLINES_SESSION"},
        "select": ["ID", "PROVIDER_ID", "SUBJECT", "CREATED", "OWNER_TYPE_ID", "OWNER_ID"],
        "start": 0,
    })
    if d:
        print(f"  Дел от открытых линий: {d.get('total')}")
        sample_fields(d.get("result") or [])
    for m in ("imopenlines.config.list.get", "imopenlines.crm.chat.get", "im.recent.get"):
        r = try_call(bx, m, {}, quiet=True)
        print(f"  {m:32} {'доступен' if r else 'недоступен'}")

    head("8. Какие вообще бывают дела (по провайдерам)")
    d = try_call(bx, "crm.activity.list", {
        "order": {"CREATED": "DESC"},
        "select": ["ID", "PROVIDER_ID", "TYPE_ID"],
        "start": 0,
    })
    if d:
        from collections import Counter
        c = Counter((x.get("PROVIDER_ID"), x.get("TYPE_ID")) for x in (d.get("result") or []))
        for (pid, tid), n in c.most_common():
            print(f"    provider={pid} type={tid}: {n}")

    print("\nГотово. Скопируйте вывод целиком.\n")


if __name__ == "__main__":
    main()
