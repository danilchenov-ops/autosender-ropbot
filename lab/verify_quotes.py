# -*- coding: utf-8 -*-
"""Проверка цитат готового документа по исходным расшифровкам.

Читает JSON [{"call_id":.., "kind":"won|lost", "text":".."}] с stdin
и для каждой цитаты проверяет:
  1) группу разговора — WON или LOST, совпадает ли с заявленной;
  2) наличие фразы в расшифровке — дословно, с нормализацией пунктуации,
     а при неудаче — по длинному общему куску (документ допускает мелкую
     правку пунктуации и раскрытие сокращений).
"""
import json
import os
import sys
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402

data = json.load(sys.stdin)
conn = runner.db()
ids = sorted({d["call_id"] for d in data})
with conn.cursor() as c:
    c.execute("""select s.call_id, s.grp, s.direction, s.split,
                        to_char(s.call_start,'YYYY-MM'), t.text
                 from lab_sample s join transcripts t using(call_id)
                 where s.call_id = ANY(%s)""", (ids,))
    meta = {r[0]: r for r in c.fetchall()}

bad_grp, exact, fuzzy, missing = [], 0, [], []
for d in data:
    m = meta.get(d["call_id"])
    if not m:
        missing.append((d["call_id"], "нет в выборке", d["text"][:60]))
        continue
    _, grp, direction, split, mon, text = m
    if grp.lower() != d["kind"]:
        bad_grp.append((d["call_id"], grp, d["kind"]))
    hay = runner.norm(text)
    need = runner.norm(d["text"])
    if need in hay:
        exact += 1
        continue
    sm = SequenceMatcher(None, need, hay, autojunk=False)
    _, _, size = sm.find_longest_match(0, len(need), 0, len(hay))
    ratio = size / max(1, len(need))
    if ratio >= 0.55:
        fuzzy.append((d["call_id"], round(ratio, 2), d["text"][:70]))
    else:
        missing.append((d["call_id"], round(ratio, 2), d["text"][:70]))

print(f"всего цитат: {len(data)}")
print(f"дословно найдено: {exact}")
print(f"найдено с расхождением в пунктуации: {len(fuzzy)}")
for cid, r, t in fuzzy:
    print(f"   #{cid} совпадение {r}: {t}")
print(f"НЕ найдено: {len(missing)}")
for cid, r, t in missing:
    print(f"   #{cid} совпадение {r}: {t}")
print(f"группа указана неверно: {len(bad_grp)}")
for cid, real, claimed in bad_grp:
    print(f"   #{cid} на самом деле {real}, в документе {claimed}")

print("\n--- контекст использованных разговоров ---")
for cid in ids:
    m = meta.get(cid)
    if m:
        print(f"  #{cid:6} {m[1]:4} {m[2]:3} {m[3]:8} {m[4]}")
