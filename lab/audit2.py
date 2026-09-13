# -*- coding: utf-8 -*-
"""Видео-приём: проверка внутри полос длительности и разбивка по типам."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runner  # noqa: E402
from audit import fetch, manager_lines, VIDEO, two_prop_p  # noqa: E402

T_OFFICE = re.compile(r'трансляц|прямой эфир|камер[аыу].{0,30}(офис|24)|онлайн.{0,12}камер|камер[аыу].{0,15}снима', re.I)
T_CALL = re.compile(r'видеосвяз|по видео.{0,15}(позвон|созвон|свяж)', re.I)
T_CAR = re.compile(r'видео.{0,10}(обзор|отч[её]т|осмотр)|видеообзор|фото.{0,5}видео|видео сним|видео с япон|запись экрана', re.I)


def main():
    rows = fetch("train") + fetch("holdout")
    by = {"WON": [], "LOST": []}
    for cid, grp, mgr, dur, card, ts, roles, segs, text in rows:
        ml = " ".join(manager_lines(roles, segs))
        by[grp].append((cid, dur, ml))
    w, l = by["WON"], by["LOST"]
    print(f"вся зеркальная выборка (train+holdout, out, май-июль): WON {len(w)} LOST {len(l)}")

    def share(rs, rx):
        return sum(1 for _, _, ml in rs if rx.search(ml)), len(rs)

    for name, rx in (("любое видео/камеры", VIDEO), ("трансляция/камеры офиса", T_OFFICE),
                     ("видеозвонок с менеджером", T_CALL), ("видео автомобиля/отчёт", T_CAR)):
        xw, nw = share(w, rx)
        xl, nl = share(l, rx)
        p, z = two_prop_p(xw, nw, xl, nl)
        print(f"{name:28} WON {100*xw/nw:5.1f}% ({xw})  LOST {100*xl/nl:5.1f}% ({xl})  "
              f"разрыв {100*xw/nw-100*xl/nl:+5.1f}  p={p:.4f}")

    print("\n-- любое видео, по полосам длительности --")
    for lo, hi in [(60, 180), (180, 300), (300, 480), (480, 10**6)]:
        ww = [r for r in w if lo <= r[1] < hi]
        ll = [r for r in l if lo <= r[1] < hi]
        if len(ww) < 6 or len(ll) < 6:
            print(f"  {lo}-{hi}: мало ({len(ww)}/{len(ll)})")
            continue
        xw, nw = share(ww, VIDEO)
        xl, nl = share(ll, VIDEO)
        print(f"  {lo:>4}-{hi:<6} WON {100*xw/nw:5.1f}% ({xw}/{nw})  "
              f"LOST {100*xl/nl:5.1f}% ({xl}/{nl})  разрыв {100*xw/nw-100*xl/nl:+5.1f}")


if __name__ == "__main__":
    main()
