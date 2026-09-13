# -*- coding: utf-8 -*-
"""Ищет, в каком разговоре реально звучала фраза. Читает JSON со stdin."""
import json
import os
import sys
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402

needles = json.load(sys.stdin)
conn = runner.db()
with conn.cursor() as c:
    c.execute("""select s.call_id, s.grp, s.direction, s.split,
                        to_char(s.call_start,'YYYY-MM'), t.text
                 from lab_sample s join transcripts t using(call_id)
                 where s.excluded is null""")
    corpus = [(r[0], r[1], r[2], r[3], r[4], runner.norm(r[5])) for r in c.fetchall()]
print(f"корпус: {len(corpus)} расшифровок\n")

for n in needles:
    need = runner.norm(n)
    best = []
    for cid, grp, dr, sp, mon, hay in corpus:
        if need in hay:
            best.append((1.0, cid, grp, dr, sp, mon))
            continue
        sm = SequenceMatcher(None, need, hay, autojunk=False)
        _, _, size = sm.find_longest_match(0, len(need), 0, len(hay))
        r = size / max(1, len(need))
        if r > 0.5:
            best.append((r, cid, grp, dr, sp, mon))
    best.sort(reverse=True)
    print(f"«{n[:75]}»")
    if not best:
        print("   НЕ НАЙДЕНО НИГДЕ")
    for r, cid, grp, dr, sp, mon in best[:3]:
        print(f"   {r:.2f}  #{cid} {grp} {dr} {sp} {mon}")
    print()
