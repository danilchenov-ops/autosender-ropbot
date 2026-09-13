# -*- coding: utf-8 -*-
"""QA-карточка разговора: 16 метрик по четырём блокам.

Модель отвечает на вопросы, БАЛЛЫ СКЛАДЫВАЕТ КОД (решение реестра: цифры
считает код, модель только комментирует). A3 — доля речи и длина монолога —
считается механически по разметке ролей: глазомерной оценке модели
(`manager_talk_share_percent`) не доверяем, это проверено 20.08.2026.

Вызывается из app/extractor.py тем же разбором, отдельного прохода нет.
"""

BLOCKS = {
    "A": (["A1", "A2", "A3", "A4"], 0.25),
    "B": (["B1", "B2", "B3", "B4", "B5"], 0.30),
    "C": (["C1", "C2", "C3", "C4"], 0.35),
    "D": (["D1", "D2", "D3"], 0.10),
}
METRICS = [m for ms, _ in BLOCKS.values() for m in ms]

TALK_LO, TALK_HI = 0.40, 0.60     # норма доли речи менеджера
TALK_SLOPE = 0.05                 # за сколько доли речи снимается балл
MONO_OK = 90                      # порог монолога, секунд
MONO_STEP = 20                    # дальше минус балл за каждые столько секунд


ROLE_MAP = {"М": "М", "M": "М", "К": "К", "K": "К", "C": "К", "С": "К"}


def norm_roles(roles, n):
    """Приводим ответ модели к n символам: мусор выкидываем, хвост добиваем
    последней ролью, лишнее режем. Приём взят из лаборатории (lab/runner.py):
    модель систематически ошибается в счёте строк, но не в их порядке."""
    if not roles:
        return None
    r = "".join(ROLE_MAP.get(ch, "") for ch in str(roles))
    if not r:
        return None
    if abs(len(r) - n) > max(8, n * 0.35):   # разошлось слишком сильно — не верим
        return None
    if len(r) < n:
        r += r[-1] * (n - len(r))
    return r[:n]


def mgr_mask(roles, n):
    """Строка ролей модели -> список bool «реплика менеджера»."""
    r = norm_roles(roles, n)
    if not r:
        return None
    return [ch == "М" for ch in r]


def talk_metrics(segments, roles):
    """(доля слов менеджера, самый долгий монолог в секундах) или (None, None).

    Монолог — подряд идущие реплики менеджера; длина берётся по таймкодам,
    так честнее, чем по числу слов.
    """
    lines = [s for s in (segments or []) if (s.get("text") or "").strip()]
    mask = mgr_mask(roles, len(lines))
    if not mask:
        return None, None
    w_mgr = w_all = 0
    best = cur_start = cur_end = 0.0
    run = False
    for s, is_mgr in zip(lines, mask):
        words = len((s.get("text") or "").split())
        w_all += words
        if is_mgr:
            w_mgr += words
            if not run:
                run, cur_start = True, float(s.get("start") or 0)
            cur_end = float(s.get("end") or cur_start)
        elif run:
            best = max(best, cur_end - cur_start)
            run = False
    if run:
        best = max(best, cur_end - cur_start)
    if not w_all:
        return None, None
    return w_mgr / w_all, round(best, 1)


def a3_score(share):
    """A3 — только доля речи. 10 внутри нормы 40–60%, дальше пологий спад:
    по нашим данным доля речи 69% одинакова у выигранных и проигранных
    разговоров (реестр, эталонный скрипт 22.08), крутой штраф был бы враньём."""
    if share is None:
        return None
    if TALK_LO <= share <= TALK_HI:
        return 10.0
    gap = (TALK_LO - share) if share < TALK_LO else (share - TALK_HI)
    return round(max(0.0, min(10.0, 10.0 - gap / TALK_SLOPE)), 1)


def a4_score(mono):
    """A4 — только монолог: до 90 секунд десятка, дальше минус балл
    за каждые 20 секунд. Длинный монолог — место, где клиент отключается."""
    if mono is None:
        return None
    over = max(0.0, float(mono) - MONO_OK)
    return round(max(0.0, min(10.0, 10.0 - over / MONO_STEP)), 1)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(10.0, f))


def parse(raw_qa):
    """{'A1': [8, 'цитата'] | 8 | 'na', ...} -> (баллы, цитаты, список N/A)."""
    scores, quotes, na = {}, {}, []
    for m in METRICS:
        v = (raw_qa or {}).get(m)
        if isinstance(v, (list, tuple)):
            val = v[0] if v else None
            q = v[1] if len(v) > 1 else None
        elif isinstance(v, dict):
            val, q = v.get("score"), v.get("quote")
        else:
            val, q = v, None
        if isinstance(val, str) and val.strip().lower() in ("na", "n/a", "нд"):
            na.append(m)
            scores[m] = None
            continue
        scores[m] = _num(val)
        if q:
            quotes[m] = str(q)[:300]
    return scores, quotes, na


import re as _re


def _norm_q(t):
    t = _re.sub(r"[^а-яёa-z0-9 ]", " ", (t or "").lower())
    return _re.sub(r"\s+", " ", t).strip()


def verify_quotes(quotes, text):
    """Цитата-доказательство обязана находиться в расшифровке (приём из
    лаборатории эталонного скрипта: quote_fidelity). Дословно или хотя бы
    первой половиной подряд — иначе выбрасываем: по аудиту 25.08 модель
    выдумывает 13% цитат, и показывать их менеджеру нельзя."""
    if not text:
        return quotes
    tx = _norm_q(text)
    out = {}
    for k, v in (quotes or {}).items():
        nv = _norm_q(v)
        if len(nv) < 8:
            continue
        if nv in tx:
            out[k] = v
        else:
            w = nv.split()
            if len(w) >= 6 and " ".join(w[: max(3, len(w) // 2)]) in tx:
                out[k] = v
    return out


def build(raw_qa, segments, verify_text=None):
    """Полная карточка: баллы блоков, интеграл, механический A3.

    Возвращает dict или None, если модель не прислала оценок.
    """
    scores, quotes, na = parse(raw_qa)
    quotes = verify_quotes(quotes, verify_text)
    roles = (raw_qa or {}).get("roles") or None
    share, mono = talk_metrics(segments, roles)
    scores["A3"] = a3_score(share)          # код важнее модели
    scores["A4"] = a4_score(mono)
    for k in ("A3", "A4"):
        if scores.get(k) is None and k not in na:
            na.append(k)

    if not any(v is not None for v in scores.values()):
        return None

    row = dict(scores)
    total, wsum = 0.0, 0.0
    for b, (ms, w) in BLOCKS.items():
        vals = [scores[m] for m in ms if scores.get(m) is not None]
        avg = round(sum(vals) / len(vals), 2) if vals else None
        row["block_" + b.lower()] = avg
        if avg is not None:
            total += avg * w
            wsum += w
    row["integral"] = round(total / wsum, 2) if wsum else None
    row["talk_share"] = round(share, 3) if share is not None else None
    row["max_mono_sec"] = mono
    row["roles"] = roles
    row["quotes"] = quotes
    row["na"] = sorted(set(na))
    return row
