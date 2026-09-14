#!/opt/ropbot/venv-gsc/bin/python
"""Google Search Console — чтение данных по autosender.ru.

Доступ: сервисный аккаунт gsc-reader@autosender-1713165947569.iam.gserviceaccount.com,
ключ /opt/ropbot/gsc-sa.json (права «Ограниченные» выданы в интерфейсе Search Console).

Использование:
  gsc.py sites                       — список ресурсов, доступных сервисному аккаунту
  gsc.py query [опции]               — запрос к Search Analytics

Опции query:
  --days N            период: последние N дней, со сдвигом 3 дня назад (лаг данных). По умолчанию 28
  --start YYYY-MM-DD  --end YYYY-MM-DD   явный период, важнее --days
  --dim a,b           разрезы: query, page, country, device, date, searchAppearance. По умолчанию query
  --limit N           строк, максимум 25000. По умолчанию 50
  --type              web (по умолчанию), image, video, news, discover, googleNews
  --site URL          ресурс; по умолчанию первый из sites
  --json              выдать сырой JSON вместо таблицы

Базис чисел: клики и показы — Google Поиск, позиция средняя по показам.
Данные отстают на 2-3 дня, поэтому период по умолчанию заканчивается позавчера-позапозавчера.
"""
import argparse
import datetime as dt
import json
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build

KEY = "/opt/ropbot/gsc-sa.json"
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
LAG_DAYS = 3


def service():
    creds = service_account.Credentials.from_service_account_file(KEY, scopes=SCOPES)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def list_sites(svc):
    return svc.sites().list().execute().get("siteEntry", [])


def main():
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("cmd", choices=["sites", "query"])
    p.add_argument("--site")
    p.add_argument("--days", type=int, default=28)
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--dim", default="query")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--type", default="web")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    svc = service()

    if a.cmd == "sites":
        entries = list_sites(svc)
        if a.json:
            print(json.dumps(entries, ensure_ascii=False, indent=2))
        else:
            for e in entries:
                print(f"{e['siteUrl']}\t{e['permissionLevel']}")
        return

    site = a.site
    if not site:
        entries = list_sites(svc)
        if not entries:
            sys.exit("сервисному аккаунту не выдан доступ ни к одному ресурсу")
        site = entries[0]["siteUrl"]

    if a.start and a.end:
        start, end = a.start, a.end
    else:
        end_d = dt.date.today() - dt.timedelta(days=LAG_DAYS)
        start_d = end_d - dt.timedelta(days=a.days - 1)
        start, end = start_d.isoformat(), end_d.isoformat()

    dims = [d.strip() for d in a.dim.split(",") if d.strip()]
    body = {
        "startDate": start,
        "endDate": end,
        "dimensions": dims,
        "rowLimit": a.limit,
        "type": a.type,
    }
    resp = svc.searchanalytics().query(siteUrl=site, body=body).execute()
    rows = resp.get("rows", [])

    if a.json:
        print(json.dumps({"site": site, "start": start, "end": end, "rows": rows},
                         ensure_ascii=False, indent=2))
        return

    print(f"# {site}  {start}..{end}  type={a.type}  строк={len(rows)}")
    print("\t".join(dims + ["clicks", "impressions", "ctr%", "position"]))
    for r in rows:
        keys = r.get("keys", [])
        print("\t".join(keys + [
            str(int(r["clicks"])),
            str(int(r["impressions"])),
            f"{r['ctr'] * 100:.2f}",
            f"{r['position']:.1f}",
        ]))


if __name__ == "__main__":
    main()
