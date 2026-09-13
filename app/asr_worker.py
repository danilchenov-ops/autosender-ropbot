"""Расшифровка записей разговоров на faster-whisper.

Аудио скачивается во временный файл, распознаётся и удаляется.
При KEEP_AUDIO_DAYS > 0 файл какое-то время лежит в AUDIO_DIR (полезно на этапе настройки).
"""
import json
import os
import subprocess
import tempfile
import time

import requests

from common import Bitrix, cfg, db, log
from metrics import compute_metrics

_model = None


def model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        log.info(
            "Загружаем модель %s (%s, %s потоков)",
            cfg.WHISPER_MODEL, cfg.WHISPER_COMPUTE, cfg.WHISPER_THREADS,
        )
        _model = WhisperModel(
            cfg.WHISPER_MODEL,
            device="cpu",
            compute_type=cfg.WHISPER_COMPUTE,
            cpu_threads=cfg.WHISPER_THREADS,
        )
    return _model


def take_task(conn):
    # возвращаем в очередь зависшие задачи (воркер перезапускался посреди работы)
    conn.execute(
        """UPDATE asr_queue SET status='pending', updated_at=now()
           WHERE status='processing' AND updated_at < now() - interval '30 minutes'"""
    )
    # свежие звонки вперёд: аналитика по последним неделям нужнее архива
    row = conn.execute(
        """UPDATE asr_queue q SET status = 'processing', updated_at = now()
           WHERE q.call_id = (
               SELECT q2.call_id
               FROM asr_queue q2
               JOIN calls c ON c.id = q2.call_id
               WHERE q2.status = 'pending' AND q2.attempts < 3
               ORDER BY q2.priority DESC, c.call_start DESC
               LIMIT 1 FOR UPDATE OF q2 SKIP LOCKED
           )
           RETURNING q.call_id"""
    ).fetchone()
    if not row:
        return None
    call_id = row[0]
    call = conn.execute(
        "SELECT id, record_url, record_file_id, duration FROM calls WHERE id = %s", (call_id,)
    ).fetchone()
    return call


class Gone(RuntimeError):
    """Записи больше нет ни у Билайна, ни в Битриксе — повторять бессмысленно."""


def disk_url(bx, record_file_id):
    if not record_file_id:
        return None
    try:
        data = bx.call("disk.file.get", {"id": record_file_id})
        return (data.get("result") or {}).get("DOWNLOAD_URL")
    except Exception as e:  # noqa: BLE001
        log.debug("disk.file.get %s: %s", record_file_id, e)
        return None


def fetch(url, dest):
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    return os.path.getsize(dest)


def download(bx, record_url, record_file_id, dest):
    """Сначала прямая ссылка Билайна, при неудаче — копия с Диска Битрикса.

    У Билайна записи живут ограниченный срок, поэтому 404 на архивных звонках — норма.
    """
    errors = []
    for url in filter(None, [record_url, disk_url(bx, record_file_id)]):
        try:
            return fetch(url, dest)
        except requests.HTTPError as e:
            errors.append(f"{e.response.status_code} {url[:60]}")
        except requests.RequestException as e:
            errors.append(str(e)[:80])
    raise Gone("; ".join(errors) or "нет ссылки на запись")


def probe_channels(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    try:
        return int(out.stdout.strip() or 1)
    except ValueError:
        return 1


def to_wav(src, dst, channel=None):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src]
    if channel is not None:
        cmd += ["-af", f"pan=mono|c0=c{channel}"]
    else:
        cmd += ["-ac", "1"]
    cmd += ["-ar", "16000", dst]
    subprocess.run(cmd, check=True)


def transcribe(path, speaker=None):
    segments, info = model().transcribe(
        path,
        language="ru",
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        beam_size=cfg.ASR_BEAM_SIZE,
        condition_on_previous_text=False,   # быстрее и не уходит в повторы
        initial_prompt=cfg.WHISPER_PROMPT or None,
    )
    out = []
    for s in segments:
        out.append({
            "start": round(s.start, 2),
            "end": round(s.end, 2),
            "speaker": speaker,
            "text": s.text.strip(),
        })
    return out, info


def process(conn, bx, call):
    call_id, record_url, record_file_id, duration = call
    started = time.time()

    workdir = cfg.AUDIO_DIR if cfg.KEEP_AUDIO_DAYS > 0 else tempfile.mkdtemp()
    os.makedirs(workdir, exist_ok=True)
    src = os.path.join(workdir, f"{call_id}.mp3")
    made = []

    try:
        size = download(bx, record_url, record_file_id, src)
        log.info("Звонок %s: скачано %s КБ", call_id, size // 1024)

        channels = probe_channels(src)
        stereo = channels >= 2
        segs = []

        if stereo:
            # Каналы Билайна: 0 — обычно менеджер, 1 — клиент. Уточняется на первых записях.
            for ch, who in ((0, "manager"), (1, "client")):
                wav = os.path.join(workdir, f"{call_id}_{who}.wav")
                made.append(wav)
                to_wav(src, wav, channel=ch)
                part, info = transcribe(wav, speaker=who)
                segs.extend(part)
            segs.sort(key=lambda s: s["start"])
        else:
            wav = os.path.join(workdir, f"{call_id}.wav")
            made.append(wav)
            to_wav(src, wav)
            segs, info = transcribe(wav, speaker=None)

        text = "\n".join(
            (f"[{s['speaker'] or '?'}] " if stereo else "") + s["text"] for s in segs if s["text"]
        )

        conn.execute(
            """INSERT INTO transcripts (call_id, text, segments, language, model, stereo, audio_sec, processing_sec)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (call_id) DO UPDATE SET
                 text=EXCLUDED.text, segments=EXCLUDED.segments, model=EXCLUDED.model,
                 stereo=EXCLUDED.stereo, processing_sec=EXCLUDED.processing_sec, created_at=now()""",
            (
                call_id, text, json.dumps(segs, ensure_ascii=False), "ru",
                cfg.WHISPER_MODEL, stereo, duration, round(time.time() - started, 1),
            ),
        )

        compute_metrics(conn, call_id, segs, stereo, duration)

        conn.execute(
            "UPDATE asr_queue SET status='done', updated_at=now() WHERE call_id=%s", (call_id,)
        )
        log.info(
            "Звонок %s расшифрован за %.1f сек (аудио %s сек, %sx)",
            call_id, time.time() - started, duration,
            round(duration / max(time.time() - started, 0.1), 1),
        )
    finally:
        if cfg.KEEP_AUDIO_DAYS == 0:
            for p in [src] + made:
                try:
                    os.remove(p)
                except OSError:
                    pass


def cleanup_audio():
    """Удаление старых аудиофайлов, если включено временное хранение."""
    if cfg.KEEP_AUDIO_DAYS <= 0 or not os.path.isdir(cfg.AUDIO_DIR):
        return
    cutoff = time.time() - cfg.KEEP_AUDIO_DAYS * 86400
    for name in os.listdir(cfg.AUDIO_DIR):
        p = os.path.join(cfg.AUDIO_DIR, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def main():
    bx = Bitrix()
    log.info("Расшифровщик запущен")
    while True:
        worked = False
        try:
            with db() as conn:
                task = take_task(conn)
                if task:
                    worked = True
                    call_id = task[0]
                    try:
                        process(conn, bx, task)
                    except Gone as e:
                        # записи нет ни у Билайна, ни на Диске — повторять нечего
                        conn.execute(
                            """UPDATE asr_queue SET status='skipped', attempts=attempts+1,
                                   last_error=%s, updated_at=now() WHERE call_id=%s""",
                            (str(e)[:500], call_id),
                        )
                    except Exception as e:  # noqa: BLE001
                        log.error("Звонок %s не расшифрован: %s", call_id, e)
                        conn.execute(
                            """UPDATE asr_queue
                               SET status = CASE WHEN attempts + 1 >= 3 THEN 'failed' ELSE 'pending' END,
                                   attempts = attempts + 1, last_error = %s, updated_at = now()
                               WHERE call_id = %s""",
                            (str(e)[:500], call_id),
                        )
        except Exception as e:  # noqa: BLE001
            log.exception("Сбой расшифровщика: %s", e)

        if not worked:
            cleanup_audio()
            time.sleep(cfg.ASR_IDLE_SLEEP)


if __name__ == "__main__":
    main()
