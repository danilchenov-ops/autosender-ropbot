# -*- coding: utf-8 -*-
"""Бенч дешёвых конфигураций: цена против цитатности.

Берёт одни и те же 6 первых разговоров (3 WON, 3 LOST) и гоняет их
по нескольким связкам «модель + размышления + вариант карточки».
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402
from anon import build_extra  # noqa: E402

CONFIGS = [
    ("gemini-3.5-flash-lite", "off", True),
    ("gemini-3.5-flash", "off", True),
    ("gemini-3.7-flash", "off", True),
]


def main():
    conn = runner.db()
    extra = build_extra(runner.manager_names(conn))
    with conn.cursor() as c:
        c.execute("""
            (SELECT s.call_id, s.grp, t.segments, t.text
             FROM lab_sample s JOIN transcripts t USING(call_id)
             WHERE s.excluded IS NULL AND s.is_first AND s.split='train'
               AND s.grp='WON' AND s.duration BETWEEN 150 AND 700
             ORDER BY s.call_id LIMIT 3)
            UNION ALL
            (SELECT s.call_id, s.grp, t.segments, t.text
             FROM lab_sample s JOIN transcripts t USING(call_id)
             WHERE s.excluded IS NULL AND s.is_first AND s.split='train'
               AND s.grp='LOST' AND s.duration BETWEEN 150 AND 700
             ORDER BY s.call_id LIMIT 3)""")
        rows = c.fetchall()
    print(f"диалогов: {len(rows)} ({[r[1] for r in rows]})", flush=True)

    with conn.cursor() as c:
        c.execute("DELETE FROM lab_bench")
        conn.commit()

    for model, think, lean in CONFIGS:
        tag = f"{model}|think={think}|lean={int(lean)}"
        for cid, grp, segs, text in rows:
            lines = runner.build_lines(segs, extra)
            prompt = runner.make_prompt(cid, lines, lean=lean)
            t0 = time.time()
            try:
                txt, tin, tout = runner.call_model(model, prompt, thinking=think)
                card = runner.parse_json(txt)
                roles = runner.norm_roles(card.get("roles") or "", len(lines))
                hits, tot = runner.quote_fidelity(card, "\n".join(lines))
                err = None
            except Exception as e:
                card, roles, hits, tot, tin, tout = None, None, 0, 0, 0, 0
                err = str(e)[:400]
            sec = round(time.time() - t0, 1)
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO lab_bench(call_id,model,card,roles,quote_hits,"
                    "quote_total,tokens_in,tokens_out,sec,err) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (cid, tag,
                     json.dumps(card, ensure_ascii=False) if card else None,
                     roles, hits, tot, tin, tout, sec, err))
                conn.commit()
            print(f"  {tag} #{cid} {grp} {sec}s цитаты {hits}/{tot} "
                  f"tok {tin}/{tout} {err or ''}", flush=True)
            time.sleep(1.0)
    print("готово", flush=True)


if __name__ == "__main__":
    main()
