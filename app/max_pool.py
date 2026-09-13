# -*- coding: utf-8 -*-
"""Учёт подписок MAX (сторона ропбота, в контейнере).

В MAX нет персональных ссылок, поэтому вступление привязывается к клику на прокладке
по окну времени. Запускается оркестратором bin/tg_pool.sh.

  python max_pool.py import  < JSON   {"clicks":[...], "events":[...]} с прокладки → max_clicks,
                                      max_channel_members; затем привязка вступлений к кликам
  python max_pool.py count N          N = participants_count канала → max_channel_counts
  python max_pool.py status
"""
import json
import re
import sys
from datetime import datetime, timezone, timedelta

from common import db, log

CHAT = -70996748460165            # канал «Автосендер Автомобили из Японии Китая и Кореи» в MAX
WINDOW_BEFORE = timedelta(minutes=15)   # клик не раньше чем за 15 мин до вступления
WINDOW_AFTER = timedelta(seconds=60)    # и не позже чем через минуту (рассинхрон часов)

ACTION = {"user_added": "join", "user_removed": "leave"}


def _camp(c):
    c = (c or "").lower()
    m = re.search(r"\d{6,}", c)
    key = m.group(0) if m else c
    return {"713638280": "hot", "713639252": "involved"}.get(key, c or None)


def _ts(x, ms=False):
    if x is None:
        return None
    return datetime.fromtimestamp(float(x) / (1000 if ms else 1), tz=timezone.utc)


def cmd_import():
    data = json.load(sys.stdin) or {}
    clicks, events = data.get("clicks") or [], data.get("events") or []
    with db() as conn:
        for c in clicks:
            conn.execute(
                """INSERT INTO max_clicks (pool_id, clicked_at, client_id, yclid, campaign, ua_hash, ip_hash)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (pool_id) DO NOTHING""",
                (c["id"], _ts(c["ts"]), c.get("cid") or None, c.get("yclid") or None,
                 _camp(c.get("campaign")), c.get("ua_hash"), c.get("ip_hash")))
        for e in events:
            try:
                raw = json.loads(e["raw"])
            except Exception:
                raw = {}
            user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
            ut = e.get("update_type") or raw.get("update_type")
            conn.execute(
                """INSERT INTO max_channel_members (pool_id, event_at, received_at, chat_id, user_id,
                                                    first_name, action, raw)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (pool_id) DO NOTHING""",
                (e["id"], _ts(e.get("ts"), ms=True) or _ts(e.get("received_at")), _ts(e.get("received_at")),
                 e.get("chat_id") or raw.get("chat_id") or CHAT, e.get("user_id") or user.get("user_id"),
                 user.get("first_name"), ACTION.get(ut, ut or "?"), json.dumps(raw, ensure_ascii=False)))
        matched = _match(conn)
    log.info("max_pool import: кликов %d, событий %d, привязано вступлений: %s", len(clicks), len(events), matched)
    print(f"import: {len(clicks)} clicks, {len(events)} events, matched {matched}")


def _match(conn):
    """Привязать новые вступления к кликам по окну времени."""
    joins = conn.execute(
        """SELECT id, event_at FROM max_channel_members
            WHERE action='join' AND match_quality IS NULL ORDER BY event_at""").fetchall()
    stats = {"exact": 0, "campaign": 0, "ambiguous": 0, "organic": 0}
    for mid, at in joins:
        cands = conn.execute(
            """SELECT id, clicked_at, client_id, yclid, campaign FROM max_clicks
                WHERE member_id IS NULL AND clicked_at BETWEEN %s AND %s
                ORDER BY clicked_at DESC""", (at - WINDOW_BEFORE, at + WINDOW_AFTER)).fetchall()
        # двойной клик одного посетителя — один кандидат
        uniq = {}
        for c in cands:
            key = c[2] or c[3] or f"#{c[0]}"
            uniq.setdefault(key, c)
        cands = list(uniq.values())
        if not cands:
            q, best = "organic", None
        elif len(cands) == 1:
            q, best = "exact", cands[0]
        else:
            best = cands[0]                       # ближайший по времени
            camps = {c[4] for c in cands}
            q = "campaign" if len(camps) == 1 else "ambiguous"
        if best:
            delay = int((at - best[1]).total_seconds())
            conn.execute("UPDATE max_clicks SET member_id=%s WHERE id=%s", (mid, best[0]))
            conn.execute(
                """UPDATE max_channel_members
                      SET click_id=%s, match_quality=%s, match_delay_s=%s,
                          client_id=%s, yclid=%s, campaign=%s
                    WHERE id=%s""",
                (best[0], q, delay,
                 best[2] if q == "exact" else None,
                 best[3] if q == "exact" else None,
                 best[4] if q in ("exact", "campaign") else None, mid))
        else:
            conn.execute("UPDATE max_channel_members SET match_quality='organic' WHERE id=%s", (mid,))
        stats[q] += 1
    return stats


def cmd_count(n):
    with db() as conn:
        conn.execute("INSERT INTO max_channel_counts (chat_id, members) VALUES (%s,%s)", (CHAT, int(n)))
    print(f"max members: {n}")


def cmd_status():
    with db() as conn:
        j = conn.execute(
            """SELECT count(*) FILTER (WHERE action='join'), count(*) FILTER (WHERE action='leave'),
                      count(*) FILTER (WHERE action='join' AND match_quality='exact'),
                      count(*) FILTER (WHERE action='join' AND match_quality='campaign'),
                      count(*) FILTER (WHERE action='join' AND match_quality='ambiguous'),
                      count(*) FILTER (WHERE action='join' AND match_quality='organic')
                 FROM max_channel_members""").fetchone()
        k = conn.execute("SELECT count(*), count(*) FILTER (WHERE member_id IS NOT NULL) FROM max_clicks").fetchone()
        c = conn.execute("SELECT members, at FROM max_channel_counts ORDER BY at DESC LIMIT 1").fetchone()
    print(f"MAX: вступлений {j[0]} (точно {j[2]}, кампания {j[3]}, неоднозначно {j[4]}, органика {j[5]}), "
          f"выходов {j[1]} | кликов {k[0]}, привязано {k[1]} | участников: {c[0] if c else '—'} ({c[1] if c else '—'})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "import": cmd_import()
    elif cmd == "count": cmd_count(sys.argv[2])
    else: cmd_status()
