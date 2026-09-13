# -*- coding: utf-8 -*-
"""Пул персональных пригласительных ссылок Telegram (сторона ропбота, в контейнере).

Запускается оркестратором bin/tg_pool.sh, который ходит на сервер прокладки по SSH.

  python tg_pool.py import   < JSON   выданные ссылки с прокладки → tg_invite_links,
                                      дописать client_id/yclid вступившим
  python tg_pool.py make N   > JSON   создать N одноразовых ссылок в канале
  python tg_pool.py count             getChatMemberCount → tg_channel_counts
  python tg_pool.py status            кратко: свободных/выдано/вступило
"""
import json
import re
import sys
from datetime import datetime, timezone

import tg
from common import db, log

CHAT = -1001774094574          # канал «Автосендер Автомобили из Японии, Кореи и Китая»


def cmd_import():
    items = json.load(sys.stdin)
    if not items:
        print("import: 0"); return
    with db() as conn:
        for it in items:
            issued = datetime.fromtimestamp(it["issued_at"], tz=timezone.utc) if it.get("issued_at") else None
            conn.execute(
                """INSERT INTO tg_invite_links (link, chat_id, name, campaign, personal, pool_id,
                                                issued_at, client_id, yclid, ua_hash, ip_hash, note)
                   VALUES (%s,%s,%s,%s,true,%s,%s,%s,%s,%s,%s,'персональная, выдана прокладкой')
                   ON CONFLICT (link) DO UPDATE SET
                     issued_at = EXCLUDED.issued_at, client_id = EXCLUDED.client_id,
                     yclid = EXCLUDED.yclid, campaign = EXCLUDED.campaign,
                     ua_hash = EXCLUDED.ua_hash, ip_hash = EXCLUDED.ip_hash, pool_id = EXCLUDED.pool_id""",
                (it["link"], CHAT, it.get("name"), _camp(it.get("campaign")), it.get("id"),
                 issued, it.get("cid") or None, it.get("yclid") or None,
                 it.get("ua_hash"), it.get("ip_hash")))
        # дописать вступившим ClientID/yclid/кампанию по ссылке
        n = conn.execute(
            """UPDATE tg_channel_members m
                  SET client_id = i.client_id, yclid = i.yclid,
                      campaign = coalesce(m.campaign, i.campaign, i.name)
                 FROM tg_invite_links i
                WHERE i.link = m.invite_link
                  AND (m.client_id IS NULL OR m.campaign IS NULL)
                  AND (i.client_id IS NOT NULL OR i.campaign IS NOT NULL OR i.name IS NOT NULL)""").rowcount
    log.info("tg_pool import: %d ссылок, дополнено вступлений: %d", len(items), n)
    print(f"import: {len(items)} links, enriched {n} joins")


def _camp(c):
    """utm_campaign с прокладки → короткая метка."""
    c = (c or "").lower()
    m = re.search(r"\d{6,}", c)          # Директ отдаёт tk_713639252 — берём цифры
    key = m.group(0) if m else c
    return {"713638280": "hot", "713639252": "involved"}.get(key, c or None)


def cmd_make(n):
    out = []
    with db() as conn:
        seq = conn.execute("SELECT coalesce(max(pool_id),0) FROM tg_invite_links WHERE personal").fetchone()[0] or 0
        for i in range(n):
            seq += 1
            r = tg.call("createChatInviteLink", chat_id=CHAT, name=f"p{seq}", member_limit=1)
            link = r["invite_link"]
            conn.execute(
                """INSERT INTO tg_invite_links (link, chat_id, name, personal, note)
                   VALUES (%s,%s,%s,true,'персональная, в пуле') ON CONFLICT (link) DO NOTHING""",
                (link, CHAT, f"p{seq}"))
            out.append({"link": link, "name": f"p{seq}"})
    log.info("tg_pool make: создано %d ссылок", len(out))
    print(json.dumps(out))


def cmd_count():
    cnt = tg.call("getChatMemberCount", chat_id=CHAT)
    with db() as conn:
        conn.execute("INSERT INTO tg_channel_counts (chat_id, members) VALUES (%s,%s)", (CHAT, int(cnt)))
    print(f"members: {cnt}")


def cmd_status():
    with db() as conn:
        a = conn.execute("SELECT count(*) FILTER (WHERE issued_at IS NULL), count(*) FILTER (WHERE issued_at IS NOT NULL) FROM tg_invite_links WHERE personal").fetchone()
        j = conn.execute("SELECT count(*) FILTER (WHERE action='join'), count(*) FILTER (WHERE action='leave'), count(*) FILTER (WHERE action='join' AND client_id IS NOT NULL) FROM tg_channel_members").fetchone()
        c = conn.execute("SELECT members, at FROM tg_channel_counts ORDER BY at DESC LIMIT 1").fetchone()
    print(f"ссылок в базе: свободных {a[0]}, выдано {a[1]} | вступлений {j[0]}, выходов {j[1]}, с ClientID {j[2]} | участников канала: {c[0] if c else '—'} ({c[1] if c else '—'})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "import": cmd_import()
    elif cmd == "make": cmd_make(int(sys.argv[2]))
    elif cmd == "count": cmd_count()
    else: cmd_status()
