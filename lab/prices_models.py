#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Блок «Актуальные цены и модели».

Считает по скользящему окну 6 месяцев:
  1) Вопрос-ответ по бюджетам: «какую машину можно купить до N руб.» —
     ответ из реальных покупок (сделки WON) и из запросов в разговорах.
  2) Пары «хотел на старте X — купил Y» по выигранным сделкам.

Источники:
  - Битрикс24 crm.deal.list: выигранные сделки, поле «Марка модель»
    (UF_CRM_1490965625) — финальная покупка.
  - Postgres rop: leads.form_model / form_text (квиз: бюджет, бренды) —
    стартовое желание; call_extractions.budget_rub / car — бюджеты и модели
    из расшифрованных разговоров.

Запуск (изнутри контейнера collector, скрипт подаётся на stdin):
  cat prices_models.py | docker exec -i ropbot-collector-1 python - --json
  cat prices_models.py | docker exec -i ropbot-collector-1 python - --md

Ничего не пишет ни в CRM, ни в Telegram. Только stdout.
"""
import os, re, sys, json, warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

import requests
import psycopg
from psycopg.rows import dict_row

WINDOW_MONTHS = 6
DEAL_MODEL_UF = "UF_CRM_1490965625"   # «Марка модель»
DEAL_MODEL_UF2 = "UF_CRM_1490965648"  # «Модель» (запасное)

# Бюджетные корзины: (верхняя граница, подпись вопроса)
BUCKETS = [
    (500_000,   "до 500 000 ₽"),
    (1_000_000, "до 1 000 000 ₽"),
    (1_500_000, "до 1 500 000 ₽"),
    (2_000_000, "до 2 000 000 ₽"),
    (3_000_000, "до 3 000 000 ₽"),
    (10**12,    "от 3 000 000 ₽ и выше"),
]

BRANDS = [
    "Toyota", "Honda", "Nissan", "Mazda", "Mitsubishi", "Suzuki", "Subaru",
    "Daihatsu", "Lexus", "Kia", "Hyundai", "Genesis", "BMW", "Mercedes",
    "Audi", "Volkswagen", "Skoda", "Chevrolet", "Ford", "Volvo", "Land Rover",
    "Porsche", "Mini", "Peugeot", "Renault", "Citroen", "BYD", "Geely",
    "Haval", "Changan", "Chery", "Exeed", "Omoda", "Jaecoo", "Zeekr", "Tank",
    "Lixiang", "Li Auto", "Voyah", "Isuzu", "Infiniti", "Acura", "Jeep",
    "Fiat",
]

# Кириллица и частые кривые написания -> каноническое
CYR_MAP = {
    "тойота": "Toyota", "тоета": "Toyota", "хонда": "Honda", "ниссан": "Nissan",
    "мазда": "Mazda", "мицубиси": "Mitsubishi", "митсубиси": "Mitsubishi",
    "сузуки": "Suzuki", "субару": "Subaru", "дайхатсу": "Daihatsu",
    "лексус": "Lexus", "киа": "Kia", "хендай": "Hyundai", "хундай": "Hyundai",
    "бмв": "BMW", "мерседес": "Mercedes", "ауди": "Audi",
    "фольксваген": "Volkswagen", "шевроле": "Chevrolet", "вольво": "Volvo",
    "джили": "Geely", "хавал": "Haval", "чанган": "Changan", "чери": "Chery",
    # модели
    "прадо": "Toyota Land Cruiser Prado", "продо": "Toyota Land Cruiser Prado",
    "лиф": "Nissan Leaf", "ноут": "Nissan Note", "нот": "Nissan Note",
    "витц": "Toyota Vitz", "витс": "Toyota Vitz", "аква": "Toyota Aqua",
    "приус": "Toyota Prius", "королла": "Toyota Corolla", "камри": "Toyota Camry",
    "ноах": "Toyota Noah", "вокси": "Toyota Voxy", "филдер": "Toyota Corolla Fielder",
    "харриер": "Toyota Harrier", "хариер": "Toyota Harrier", "райз": "Toyota Raize",
    "джимни": "Suzuki Jimny", "хастлер": "Suzuki Hustler", "солио": "Suzuki Solio",
    "фит": "Honda Fit", "везел": "Honda Vezel", "визел": "Honda Vezel",
    "фрид": "Honda Freed", "степвагон": "Honda Stepwgn", "стервагон": "Honda Stepwgn",
    "форестер": "Subaru Forester", "аутбек": "Subaru Outback",
    "делика": "Mitsubishi Delica", "спортейдж": "Kia Sportage",
    "соренто": "Kia Sorento", "селтос": "Kia Seltos", "рио": "Kia Rio",
    "туссан": "Hyundai Tucson", "туксон": "Hyundai Tucson", "крета": "Hyundai Creta",
    "иксрейл": "Nissan X-Trail", "серена": "Nissan Serena",
    "демио": "Mazda Demio", "сх5": "Mazda CX-5",
    "тафт": "Daihatsu Taft", "танто": "Daihatsu Tanto", "мира": "Daihatsu Mira",
}

BRAND_RX = {b.lower(): b for b in BRANDS}


def norm_model(raw):
    """Нормализация свободного текста в 'Brand Model'. Возвращает строку или None."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s or s.lower() in ("другое", "другая", "авто", "автомобиль", "не знаю"):
        return None
    s = re.sub(r"\s+", " ", s)
    # если несколько вариантов через / или запятую — берём каждый отдельно снаружи
    low = s.lower()
    # прямое попадание кириллического словаря
    for k, v in CYR_MAP.items():
        if k in low:
            # если словарь дал полное имя (с пробелом) — это уже brand+model
            if " " in v or low.strip() == k:
                return v
            # иначе это бренд: оставим хвост как модель
            tail = re.sub(k, "", low).strip(" ,.-")
            return (v + " " + tail.title()).strip() if tail else v
    # латиница: ищем бренд
    for bl, b in BRAND_RX.items():
        m = re.search(r"\b" + re.escape(bl) + r"\b", low)
        if m:
            tail = (low[:m.start()] + " " + low[m.end():]).strip(" ,.-")
            tail = re.sub(r"\b(19|20)\d\d\b", "", tail).strip()  # убрать год
            tail = re.sub(r"\s+", " ", tail)
            return (b + " " + tail.title()).strip() if tail else b
    # ничего не распознали — вернуть как есть (title), если похоже на название
    if re.search(r"[a-zA-Zа-яА-Я]", s) and len(s) <= 40:
        return s.title()
    return None


def split_variants(raw):
    """'A / B, C' -> [A, B, C]"""
    if not raw:
        return []
    parts = re.split(r"[/,;]| или ", str(raw))
    out = []
    for p in parts:
        n = norm_model(p)
        if n and n not in out:
            out.append(n)
    return out


def brand_of(name):
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
    return bool(re.search(r"[A-Za-zА-Яа-я]{2}", tail))


def parse_budget_from_text(txt):
    """Из квизного form_text: 'Бюджет: 1 500 000 - 2 499 000' -> верхняя граница int."""
    if not txt:
        return None
    m = re.search(r"Бюджет[^\d]*([\d\s]{4,})(?:\s*-\s*([\d\s]{4,}))?", txt)
    if not m:
        return None
    def num(g):
        return int(re.sub(r"\s", "", g)) if g else None
    lo, hi = num(m.group(1)), num(m.group(2))
    v = hi or lo
    if v and v < 10_000:  # «до 500» и прочий мусор
        return None
    return v


def parse_brands_from_text(txt):
    if not txt:
        return []
    m = re.search(r"(?:Интересующие бренды|Марка)\s*:\s*([^\n]+)", txt)
    if not m:
        return []
    return split_variants(m.group(1))


def bucket_label(v):
    for hi, label in BUCKETS:
        if v <= hi:
            return label
    return BUCKETS[-1][1]


def fetch_won_deals(wh, since):
    res, start = [], 0
    while True:
        params = {
            "filter[STAGE_SEMANTIC_ID]": "S",
            "filter[>CLOSEDATE]": since.strftime("%Y-%m-%d"),
            "select[]": ["ID", "LEAD_ID", "CONTACT_ID", "CLOSEDATE", "TITLE",
                          DEAL_MODEL_UF, DEAL_MODEL_UF2],
            "start": start,
        }
        r = requests.get(wh + "crm.deal.list", params=params, timeout=30).json()
        res += r.get("result", [])
        if "next" not in r:
            break
        start = r["next"]
    return res


def main():
    fmt = "md" if "--md" in sys.argv else "json"
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=WINDOW_MONTHS * 30)

    wh = os.environ.get("BITRIX_WEBHOOK") or os.environ.get("B24_WEBHOOK")
    conn = psycopg.connect(os.environ["PG_DSN"])
    cur = conn.cursor(row_factory=dict_row)

    # --- 1. Выигранные сделки + их лиды ---------------------------------
    deals = fetch_won_deals(wh, since)
    lead_ids = [int(d["LEAD_ID"]) for d in deals if d.get("LEAD_ID") and d["LEAD_ID"] != "0"]
    leads = {}
    if lead_ids:
        cur.execute(
            "SELECT id, form_model, form_text, phone_e164 FROM leads WHERE id = ANY(%s)",
            (lead_ids,))
        for row in cur.fetchall():
            leads[row["id"]] = dict(row)

    # бюджеты/машины из разговоров по лидам выигранных сделок
    call_wish = {}   # lead_id -> (car, budget) из самого раннего разговора
    if lead_ids:
        cur.execute("""
            SELECT * FROM (
              SELECT c.crm_entity_id AS lead_id, ce.car, ce.budget_rub,
                     row_number() OVER (PARTITION BY c.crm_entity_id ORDER BY c.call_start) rn
              FROM calls c JOIN call_extractions ce ON ce.call_id = c.id
              WHERE c.crm_entity_type='LEAD' AND c.crm_entity_id = ANY(%s)
                AND (nullif(ce.car,'') IS NOT NULL OR ce.budget_rub IS NOT NULL)
            ) t WHERE rn = 1
        """, (lead_ids,))
        for row in cur.fetchall():
            call_wish[row["lead_id"]] = (row["car"], row["budget_rub"])

    pairs, bought_with_budget, bought_all = [], [], []
    unconfirmed_lists = 0
    brand_only_bought = 0
    brand_only_wanted = 0
    for d in deals:
        bought_raw = d.get(DEAL_MODEL_UF) or d.get(DEAL_MODEL_UF2)
        bought_variants = split_variants(bought_raw)
        # если в поле сделки перечислено несколько машин — это ещё список
        # пожеланий, а не финальная покупка; в «купил» не берём
        if len(bought_variants) > 1:
            unconfirmed_lists += 1
            bought = None
        else:
            bought = bought_variants[0] if bought_variants else None
        if bought and not is_full_model(bought):
            brand_only_bought += 1
            bought = None
        lid = int(d["LEAD_ID"]) if d.get("LEAD_ID") and d["LEAD_ID"] != "0" else None
        lead = leads.get(lid, {})
        # стартовое желание: квиз-бренды > form_model > первый разговор
        wanted_list = parse_brands_from_text(lead.get("form_text"))
        if not wanted_list:
            wanted_list = split_variants(lead.get("form_model"))
        cw = call_wish.get(lid)
        if not wanted_list and cw and cw[0]:
            wanted_list = split_variants(cw[0])
        # бюджет: квиз > разговор
        budget = parse_budget_from_text(lead.get("form_text"))
        if not budget and cw and cw[1]:
            budget = int(cw[1])
        if bought:
            bought_all.append(bought)
            if budget:
                bought_with_budget.append((budget, bought))
        wanted_full = [w for w in wanted_list if is_full_model(w)]
        if bought and wanted_list and not wanted_full:
            brand_only_wanted += 1
        if bought and wanted_full:
            wanted = wanted_full[0]
            same_model = wanted.lower() == bought.lower()
            same_brand = (brand_of(wanted) or "").lower() == (brand_of(bought) or "").lower()
            pairs.append({
                "deal_id": int(d["ID"]),
                "closedate": (d.get("CLOSEDATE") or "")[:10],
                "wanted": wanted,
                "wanted_variants": wanted_full,
                "bought": bought,
                "budget_rub": budget,
                "match": "модель" if same_model else ("марка" if same_brand else "другая марка"),
            })

    # --- 2. Запросы из разговоров: бюджет + модели ----------------------
    cur.execute("""
        SELECT ce.budget_rub, ce.car
        FROM call_extractions ce JOIN calls c ON c.id = ce.call_id
        WHERE c.call_start > now() - interval '%s days'
          AND ce.budget_rub IS NOT NULL AND ce.budget_rub BETWEEN 100000 AND 30000000
    """ % (WINDOW_MONTHS * 30,))
    asked_rows = cur.fetchall()

    # --- 3. Корзины -----------------------------------------------------
    def top(counter_list, n=8):
        cnt = {}
        for x in counter_list:
            cnt[x] = cnt.get(x, 0) + 1
        return [{"model": k, "n": v} for k, v in
                sorted(cnt.items(), key=lambda kv: -kv[1])[:n]]

    buckets_out = []
    for hi, label in BUCKETS:
        lo = 0
        idx = [b[0] for b in BUCKETS].index(hi)
        if idx > 0:
            lo = BUCKETS[idx - 1][0]
        asked = []
        for row in asked_rows:
            if lo < row["budget_rub"] <= hi:
                asked += [v for v in split_variants(row["car"]) if is_full_model(v)]
        bought = [m for b, m in bought_with_budget if lo < b <= hi]
        buckets_out.append({
            "budget": label,
            "range_rub": [lo, None if hi >= 10**11 else hi],
            "bought_models": top(bought),
            "asked_models": top(asked),
            "n_bought": len(bought),
            "n_asked_calls": sum(1 for r in asked_rows if lo < r["budget_rub"] <= hi),
        })

    # Q&A строки: основа — реальные покупки; если их в корзине мало (<3),
    # дополняем моделями из клиентских запросов
    qa = []
    for b in buckets_out:
        bought_names = [x["model"] for x in b["bought_models"][:6]]
        asked_names = [x["model"] for x in b["asked_models"][:6]
                       if x["model"] not in bought_names and x["n"] >= 2]
        if len(bought_names) >= 3:
            models, note = bought_names, "по реальным покупкам"
        elif bought_names:
            models = bought_names + asked_names[:5]
            note = "покупок мало, дополнено запросами клиентов"
        elif asked_names:
            models, note = asked_names, "покупок в корзине не зафиксировано; по запросам клиентов"
        else:
            continue
        qa.append({
            "q": "Какую машину можно купить %s?" % b["budget"].replace("₽", "рублей").strip(),
            "a": ", ".join(models[:8]),
            "basis": note,
        })

    result = {
        "block": "Актуальные цены и модели",
        "generated_at": now.isoformat(timespec="seconds"),
        "window": "последние %d месяцев (с %s)" % (WINDOW_MONTHS, since.strftime("%Y-%m-%d")),
        "coverage": {
            "won_deals": len(deals),
            "won_with_full_model": len(bought_all),
            "won_model_is_list_skipped": unconfirmed_lists,
            "won_brand_only_skipped": brand_only_bought,
            "won_with_model_and_budget": len(bought_with_budget),
            "pairs_wanted_bought": len(pairs),
            "pairs_lost_wanted_brand_only": brand_only_wanted,
            "calls_with_budget": len(asked_rows),
            "note": ("Считаем только записи, где известна и марка, и модель: одна марка "
                     "без модели отбрасывается (решение 22.08.2026). Извлечение бюджетов "
                     "и моделей из разговоров работает с августа 2026 — разговорная часть "
                     "окна пока короче 6 месяцев и дорастёт сама."),
        },
        "qa_budget": qa,
        "budget_buckets": buckets_out,
        "wanted_vs_bought": sorted(pairs, key=lambda p: p["closedate"], reverse=True),
        "top_bought": top(bought_all, 15),
    }

    if fmt == "json":
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return

    # --- markdown -------------------------------------------------------
    out = []
    out.append("# Актуальные цены и модели")
    out.append("")
    out.append("Окно: %s. Сформировано: %s." % (result["window"], now.strftime("%Y-%m-%d %H:%M UTC")))
    out.append("")
    out.append("## Вопрос — ответ по бюджетам")
    out.append("")
    for x in qa:
        out.append("**%s**" % x["q"])
        out.append("")
        out.append("%s _(%s)_" % (x["a"], x["basis"]))
        out.append("")
    changed = [p for p in result["wanted_vs_bought"] if p["match"] == "другая марка"]
    refined = [p for p in result["wanted_vs_bought"] if p["match"] == "марка"]
    same = [p for p in result["wanted_vs_bought"] if p["match"] == "модель"]
    out.append("## Хотел — купил: сменили марку (%d)" % len(changed))
    out.append("")
    for p in changed:
        b = " (бюджет %s ₽)" % format(p["budget_rub"], ",").replace(",", " ") if p["budget_rub"] else ""
        out.append("- Хотел на старте **%s**, в итоге купил **%s**%s — %s" %
                   (p["wanted"], p["bought"], b, p["closedate"]))
    out.append("")
    out.append("## Определились с моделью в рамках марки (%d)" % len(refined))
    out.append("")
    for p in refined:
        b = " (бюджет %s ₽)" % format(p["budget_rub"], ",").replace(",", " ") if p["budget_rub"] else ""
        out.append("- Хотел **%s**, купил **%s**%s — %s" %
                   (p["wanted"], p["bought"], b, p["closedate"]))
    out.append("")
    out.append("## Купили то, что и хотели (%d)" % len(same))
    out.append("")
    for p in same:
        b = " (бюджет %s ₽)" % format(p["budget_rub"], ",").replace(",", " ") if p["budget_rub"] else ""
        out.append("- **%s**%s — %s" % (p["bought"], b, p["closedate"]))
    out.append("")
    out.append("## Что покупают чаще всего (6 мес.)")
    out.append("")
    for t in result["top_bought"]:
        out.append("- %s — %d" % (t["model"], t["n"]))
    out.append("")
    out.append("_Считаем только записи, где известна и марка, и модель._")
    out.append("")
    out.append("_Покрытие: сделок выиграно %d, из них с полной моделью %d "
               "(отброшено: одна марка без модели %d, список из нескольких машин %d). "
               "Пар «хотел—купил» %d (ещё %d не сложилось: в заявке указана только марка). "
               "Разговоров с бюджетом %d._" %
               (len(deals), len(bought_all), brand_only_bought, unconfirmed_lists,
                len(pairs), brand_only_wanted, len(asked_rows)))
    print("\n".join(out))


if __name__ == "__main__":
    main()
