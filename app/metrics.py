"""Метрики разговора по правилам.

Работает и с раздельными каналами, и с одноканальной записью.
Всё, что требует ролей (доля монолога менеджера, перебивания), при моно остаётся пустым;
остальное считается по тексту целиком.
"""
import re

STOPWORDS = [
    "не знаю", "не могу сказать", "я не в курсе", "это не ко мне",
    "перезвоните позже", "нам это не интересно", "сами посмотрите на сайте",
    "у нас так принято", "я вам уже говорил", "ничем не могу помочь",
    "это не мой вопрос", "не занимаюсь этим",
]

QUESTION_WORD = re.compile(
    r"\b(какой|какая|какое|какие|сколько|когда|почему|зачем|где|куда|кто|что именно|"
    r"как вы|расскажите|подскажите|уточните|верно ли|правильно ли|интересует ли|"
    r"рассматривали|планируете|готовы ли|удобно ли)\b", re.I
)

PRICE = re.compile(
    r"(\d[\d\s]{2,})\s*(тысяч|тыс|руб|₽)|\b(миллион|лям|стоимост|цена|цену|ценник|"
    r"по деньгам|бюджет|предоплат|рассрочк|кредит)\w*", re.I
)

NEXT_STEP = re.compile(
    r"\b(перезвон\w*|наберу|набер[её]м|скину|отправлю|пришлю|вышлю|"
    r"завтра|послезавтра|в понедельник|во вторник|в среду|в четверг|в пятницу|"
    r"договорил\w*|встрет\w*|подъед\w*|подойд[её]те|во сколько|созвон\w*)\b", re.I
)

MONOLOGUE_SEC = 60      # монолог менеджера, когда роли известны
BLOCK_GAP = 1.5         # пауза, разрывающая непрерывный кусок речи


def compute_metrics(conn, call_id, segments, stereo, duration):
    segments = [s for s in segments if (s.get("text") or "").strip()]
    if not segments:
        return

    manager_sec = client_sec = 0.0
    words_m = words_c = words_total = 0
    questions_m = questions_total = 0
    interruptions = 0
    speech_sec = 0.0
    longest_pause = 0.0
    longest_monologue = 0.0
    longest_block = 0.0

    run_speaker, run_start, run_end = None, None, None
    block_start, prev_end = segments[0]["start"], None

    for s in segments:
        text = s["text"].strip()
        dur = max(s["end"] - s["start"], 0)
        words = len(text.split())
        who = s.get("speaker")

        speech_sec += dur
        words_total += words

        is_question = "?" in text or bool(QUESTION_WORD.search(text))
        if is_question:
            questions_total += 1

        if who == "manager":
            manager_sec += dur
            words_m += words
            if is_question:
                questions_m += 1
        elif who == "client":
            client_sec += dur
            words_c += words

        # непрерывный кусок речи без заметной паузы — прокси монолога при моно
        if prev_end is not None:
            gap = s["start"] - prev_end
            longest_pause = max(longest_pause, gap)
            if gap > BLOCK_GAP:
                longest_block = max(longest_block, prev_end - block_start)
                block_start = s["start"]
            if gap < -0.3:
                interruptions += 1

        # самый длинный монолог менеджера, когда роли известны
        if who == run_speaker:
            run_end = s["end"]
        else:
            if run_speaker == "manager" and run_start is not None:
                longest_monologue = max(longest_monologue, run_end - run_start)
            run_speaker, run_start, run_end = who, s["start"], s["end"]

        prev_end = s["end"]

    if run_speaker == "manager" and run_start is not None:
        longest_monologue = max(longest_monologue, run_end - run_start)
    if prev_end is not None:
        longest_block = max(longest_block, prev_end - block_start)

    full_text = " ".join(s["text"] for s in segments)
    low = full_text.lower()

    talk_ratio = None
    if stereo and (manager_sec + client_sec) > 0:
        talk_ratio = round(manager_sec / (manager_sec + client_sec), 3)

    silence_ratio = None
    if duration and duration > 0:
        silence_ratio = round(max(duration - speech_sec, 0) / duration, 3)

    wpm = round(words_total / (speech_sec / 60.0), 1) if speech_sec > 5 else None

    # при моно «монолог» определяем по непрерывному куску речи
    monologue_flag = (longest_monologue >= MONOLOGUE_SEC) if stereo \
        else (longest_block >= MONOLOGUE_SEC)

    conn.execute(
        """INSERT INTO call_metrics (call_id, manager_talk_ratio, longest_pause_sec, silence_ratio,
                                     interruptions, words_manager, words_client, questions_manager,
                                     monologue_flag, stopwords,
                                     words_total, segments_count, wpm, questions_total,
                                     longest_block_sec, price_mentioned, next_step_hint, has_roles,
                                     computed_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
           ON CONFLICT (call_id) DO UPDATE SET
             manager_talk_ratio=EXCLUDED.manager_talk_ratio,
             longest_pause_sec=EXCLUDED.longest_pause_sec,
             silence_ratio=EXCLUDED.silence_ratio,
             interruptions=EXCLUDED.interruptions,
             words_manager=EXCLUDED.words_manager,
             words_client=EXCLUDED.words_client,
             questions_manager=EXCLUDED.questions_manager,
             monologue_flag=EXCLUDED.monologue_flag,
             stopwords=EXCLUDED.stopwords,
             words_total=EXCLUDED.words_total,
             segments_count=EXCLUDED.segments_count,
             wpm=EXCLUDED.wpm,
             questions_total=EXCLUDED.questions_total,
             longest_block_sec=EXCLUDED.longest_block_sec,
             price_mentioned=EXCLUDED.price_mentioned,
             next_step_hint=EXCLUDED.next_step_hint,
             has_roles=EXCLUDED.has_roles,
             computed_at=now()""",
        (
            call_id, talk_ratio, round(longest_pause, 1), silence_ratio,
            interruptions if stereo else None,
            words_m or None, words_c or None,
            questions_m if stereo else None,
            monologue_flag,
            [w for w in STOPWORDS if w in low],
            words_total, len(segments), wpm, questions_total,
            round(longest_block, 1),
            bool(PRICE.search(full_text)),
            bool(NEXT_STEP.search(full_text)),
            bool(stereo),
        ),
    )
