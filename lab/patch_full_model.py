#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Патч prices_models.py: считать только записи с маркой И моделью."""
import io, sys

P = "/opt/ropbot/lab/prices_models.py"
src = io.open(P, encoding="utf-8").read()

OLD_BRAND = '''def brand_of(name):
    if not name:
        return None
    first = name.split()[0]
    return BRAND_RX.get(first.lower(), first)'''

NEW_BRAND = '''def brand_of(name):
    """Марка в начале названия или None, если марка не распознана."""
    if not name:
        return None
    low = name.lower()
    for bl, b in BRAND_RX.items():
        if " " in bl and (low == bl or low.startswith(bl + " ")):
            return b
    first = name.split()[0]
    return BRAND_RX.get(first.lower())


def is_full_model(name):
    """True, только если известна И марка, И модель.

    Одна марка без модели («Toyota», «Honda») не годится — решение
    Тимофея 22.08.2026. Названия без распознанной марки («Discovery»,
    «Toyotaaris») тоже отбрасываем: марка формально неизвестна.
    """
    if not name:
        return False
    b = brand_of(name)
    if not b:
        return False
    tail = name[len(b):].strip(" ,.-")
    return bool(re.search(r"[A-Za-zА-Яа-я]{2}", tail))'''

OLD_LOOP = '''    pairs, bought_with_budget, bought_all = [], [], []
    unconfirmed_lists = 0
    for d in deals:'''

NEW_LOOP = '''    pairs, bought_with_budget, bought_all = [], [], []
    unconfirmed_lists = 0
    brand_only_bought = 0
    brand_only_wanted = 0
    for d in deals:'''

OLD_B = '''            bought = bought_variants[0] if bought_variants else None
        lid = int('''

NEW_B = '''            bought = bought_variants[0] if bought_variants else None
        if bought and not is_full_model(bought):
            brand_only_bought += 1
            bought = None
        lid = int('''

OLD_PAIR = '''        if bought and wanted_list:
            wanted = wanted_list[0]'''

NEW_PAIR = '''        wanted_full = [w for w in wanted_list if is_full_model(w)]
        if bought and wanted_list and not wanted_full:
            brand_only_wanted += 1
        if bought and wanted_full:
            wanted = wanted_full[0]'''

OLD_WV = '''                "wanted_variants": wanted_list,'''
NEW_WV = '''                "wanted_variants": wanted_full,'''

OLD_ASK = '''                asked += split_variants(row["car"])'''
NEW_ASK = '''                asked += [v for v in split_variants(row["car"]) if is_full_model(v)]'''

OLD_COV = '''            "won_with_model": len(bought_all),
            "won_model_is_list_skipped": unconfirmed_lists,
            "won_with_model_and_budget": len(bought_with_budget),
            "pairs_wanted_bought": len(pairs),
            "calls_with_budget": len(asked_rows),
            "note": ("Извлечение бюджетов и моделей из разговоров работает с августа 2026 — "
                     "разговорная часть окна пока короче 6 месяцев и дорастёт сама."),'''

NEW_COV = '''            "won_with_full_model": len(bought_all),
            "won_model_is_list_skipped": unconfirmed_lists,
            "won_brand_only_skipped": brand_only_bought,
            "won_with_model_and_budget": len(bought_with_budget),
            "pairs_wanted_bought": len(pairs),
            "pairs_lost_wanted_brand_only": brand_only_wanted,
            "calls_with_budget": len(asked_rows),
            "note": ("Считаем только записи, где известна и марка, и модель: одна марка "
                     "без модели отбрасывается (решение 22.08.2026). Извлечение бюджетов "
                     "и моделей из разговоров работает с августа 2026 — разговорная часть "
                     "окна пока короче 6 месяцев и дорастёт сама."),'''

OLD_FOOT = '''    out.append("_Покрытие: сделок выиграно %d, модель покупки заполнена в %d (ещё %d "
               "пропущено: в поле список из нескольких машин), пар «хотел—купил» %d, "
               "разговоров с бюджетом %d._" %
               (len(deals), len(bought_all), unconfirmed_lists, len(pairs), len(asked_rows)))'''

NEW_FOOT = '''    out.append("_Считаем только записи, где известна и марка, и модель._")
    out.append("")
    out.append("_Покрытие: сделок выиграно %d, из них с полной моделью %d "
               "(отброшено: одна марка без модели %d, список из нескольких машин %d). "
               "Пар «хотел—купил» %d (ещё %d не сложилось: в заявке указана только марка). "
               "Разговоров с бюджетом %d._" %
               (len(deals), len(bought_all), brand_only_bought, unconfirmed_lists,
                len(pairs), brand_only_wanted, len(asked_rows)))'''

PATCHES = [
    ("brand_of", OLD_BRAND, NEW_BRAND),
    ("counters", OLD_LOOP, NEW_LOOP),
    ("bought_filter", OLD_B, NEW_B),
    ("pair_filter", OLD_PAIR, NEW_PAIR),
    ("wanted_variants", OLD_WV, NEW_WV),
    ("asked_filter", OLD_ASK, NEW_ASK),
    ("coverage", OLD_COV, NEW_COV),
    ("footer", OLD_FOOT, NEW_FOOT),
]

for name, old, new in PATCHES:
    if new in src and old not in src:
        print("skip (already applied):", name)
        continue
    n = src.count(old)
    if n != 1:
        print("FAIL:", name, "occurrences:", n)
        sys.exit(1)
    src = src.replace(old, new)
    print("ok:", name)

io.open(P, "w", encoding="utf-8").write(src)
compile(src, P, "exec")
print("written and compiles")
