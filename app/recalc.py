"""Пересчёт метрик по уже сохранённым расшифровкам. Аудио не требуется.

Запуск: docker compose run --rm collector python recalc.py
"""
from common import db, log
from metrics import compute_metrics
from migrate import migrate


def main():
    migrate()
    done = 0
    with db() as conn:
        rows = conn.execute(
            """SELECT t.call_id, t.segments, t.stereo, c.duration
               FROM transcripts t JOIN calls c ON c.id = t.call_id
               ORDER BY t.call_id"""
        ).fetchall()
        log.info("Пересчитываем метрики для %s расшифровок", len(rows))
        for call_id, segments, stereo, duration in rows:
            try:
                compute_metrics(conn, call_id, segments or [], stereo, duration)
                done += 1
            except Exception as e:  # noqa: BLE001
                log.error("Звонок %s: %s", call_id, e)
    log.info("Готово: %s", done)


if __name__ == "__main__":
    main()
