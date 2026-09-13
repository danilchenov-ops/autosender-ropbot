"""Сравнение моделей распознавания на одном и том же реальном звонке.

Запуск:
    docker compose run --rm asr python bench.py                    # свежий длинный звонок
    docker compose run --rm asr python bench.py 25309 small medium # конкретный звонок и модели

Скачивает запись, прогоняет через каждую модель, печатает скорость и текст.
Аудио удаляется после работы.
"""
import os
import sys
import tempfile
import time

from common import Bitrix, cfg, db, log
from asr_worker import Gone, download, probe_channels, to_wav

DEFAULT_MODELS = ["small", "medium"]


def pick_call(conn, call_id=None):
    if call_id:
        row = conn.execute(
            "SELECT id, record_url, record_file_id, duration FROM calls WHERE id = %s",
            (call_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT id, record_url, record_file_id, duration FROM calls
               WHERE record_url IS NOT NULL AND duration BETWEEN 120 AND 300
               ORDER BY call_start DESC LIMIT 1"""
        ).fetchone()
    return row


def run(model_name, wav, prompt):
    from faster_whisper import WhisperModel

    t0 = time.time()
    m = WhisperModel(model_name, device="cpu",
                     compute_type=cfg.WHISPER_COMPUTE, cpu_threads=4)
    load = time.time() - t0

    t1 = time.time()
    segments, info = m.transcribe(
        wav, language="ru", vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=cfg.ASR_BEAM_SIZE,
        condition_on_previous_text=False,
        initial_prompt=prompt or None,
    )
    text = " ".join(s.text.strip() for s in segments)
    work = time.time() - t1
    del m
    return text, load, work


def main():
    call_id = None
    models = []
    for a in sys.argv[1:]:
        if a.isdigit():
            call_id = int(a)
        else:
            models.append(a)
    models = list(dict.fromkeys(models)) or DEFAULT_MODELS

    bx = Bitrix()
    with db() as conn:
        call = pick_call(conn, call_id)

    if not call:
        print("Подходящий звонок не найден")
        return

    cid, url, file_id, duration = call
    print(f"\nЗвонок {cid}, длительность {duration} сек")

    workdir = tempfile.mkdtemp()
    src = os.path.join(workdir, "a.mp3")
    wav = os.path.join(workdir, "a.wav")

    try:
        download(bx, url, file_id, src)
    except Gone as e:
        print(f"Запись недоступна: {e}")
        return

    print(f"Каналов в записи: {probe_channels(src)}")
    to_wav(src, wav)

    variants = [(m, p) for m in models for p in (cfg.WHISPER_PROMPT, "")]

    for model_name, prompt in variants:
        tag = "с подсказкой" if prompt else "без подсказки"
        try:
            text, load, work = run(model_name, wav, prompt)
        except Exception as e:  # noqa: BLE001
            print(f"\n{model_name} ({tag}): ошибка — {e}")
            continue
        speed = duration / work if work else 0
        print("\n" + "═" * 72)
        print(f"{model_name.upper()}, {tag} — {work:.0f} сек работы, {speed:.1f}x "
              f"(загрузка модели {load:.0f} сек)")
        print("─" * 72)
        print(text[:1500])

    for p in (src, wav):
        try:
            os.remove(p)
        except OSError:
            pass
    print("\nГотово.\n")


if __name__ == "__main__":
    main()
