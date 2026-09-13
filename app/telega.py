# -*- coding: utf-8 -*-
"""Посевы Telega.in — учёт размещений и цена подписчика.

  python telega.py link <slug> [YYYY-MM-DD]      создать именованную ссылку TG seed_<slug>_<date>
  python telega.py add tg|max <slug> <price> <published 'YYYY-MM-DD HH:MM' MSK> [ocid] [name] [fmt] [views]
  python telega.py views <ocid> <views>          дописать просмотры
  python telega.py report                        экономика по размещениям (v_telega_econ)
"""
import sys
from datetime import datetime, timedelta, timezone

import tg
from common import db, log

CHAT = -1001774094574
MSK = timezone(timedelta(hours=3))


def cmd_link(slug, date=None):
    date = date or datetime.now(MSK).strftime("%Y-%m-%d")
    name = f"seed_{slug}_{date}"[:32]
    r = tg.call("createChatInviteLink", chat_id=CHAT, name=name)
    link = r["invite_link"]
    with db() as conn:
        conn.execute(
            """INSERT INTO tg_invite_links (link, chat_id, name, campaign, personal, note)
               VALUES (%s,%s,%s,%s,false,'посев telega.in') ON CONFLICT (link) DO NOTHING""",
            (link, CHAT, name, f"telega_{slug}"))
    log.info("telega link: %s → %s", name, link)
    print(link)
    return link


def cmd_add(platform, slug, price, published, ocid=None, name=None, fmt=None, views=None, link=None):
    pub = datetime.strptime(published, "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
    if platform == "tg" and not link:
        link = cmd_link(slug, pub.strftime("%Y-%m-%d"))
    with db() as conn:
        conn.execute(
            """INSERT INTO telega_placements (ocid, platform, slug, channel_name, fmt, price, published_at, invite_link, views)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (ocid) DO UPDATE SET price=EXCLUDED.price, published_at=EXCLUDED.published_at,
                 invite_link=coalesce(EXCLUDED.invite_link, telega_placements.invite_link),
                 views=coalesce(EXCLUDED.views, telega_placements.views)""",
            (int(ocid) if ocid else None, platform, slug, name, fmt, float(price), pub, link,
             int(views) if views else None))
    print("ok", platform, slug, price, pub.isoformat(), link or "")


def cmd_views(ocid, views):
    with db() as conn:
        conn.execute("UPDATE telega_placements SET views=%s WHERE ocid=%s", (int(views), int(ocid)))
    print("ok")


def cmd_report():
    with db() as conn:
        rows = conn.execute(
            """SELECT platform, slug, fmt, price, to_char(published_at AT TIME ZONE 'Europe/Moscow','DD.MM HH24:MI') pub,
                      views, subs, cost_per_sub, cpm, window_closed, tg_joins, tg_leaves, m0, m1, ads_joins, base_joins
               FROM v_telega_econ""").fetchall()
    print(f"{'plat':4} {'канал':28} {'фмт':7} {'цена':>9} {'публ.':>11} {'просм.':>7} {'подп.':>5} {'₽/подп':>7} {'CPM':>5} окно  детали")
    for r in rows:
        det = (f"joins {r[10]} leaves {r[11]}" if r[0] == "tg"
               else f"{r[12]}→{r[13]} ads {r[14]} base {r[15]}")
        print(f"{r[0]:4} {r[1][:28]:28} {(r[2] or ''):7} {float(r[3]):9.0f} {r[4]:>11} {r[5] or '-':>7} "
              f"{r[6] if r[6] is not None else '-':>5} {r[7] or '-':>7} {r[8] or '-':>5} {'закр' if r[9] else 'откр'}  {det}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "report": cmd_report()
    elif a[0] == "link": cmd_link(a[1], a[2] if len(a) > 2 else None)
    elif a[0] == "add": cmd_add(*a[1:])
    elif a[0] == "views": cmd_views(a[1], a[2])
    else: print(__doc__)
