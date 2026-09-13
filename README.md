# Виртуальный РОП — сбор и расшифровка звонков

> **Память проекта — в каталоге [`memory/`](memory/README.md).**
> Любой новой сессии Claude начинать с `cat /opt/ropbot/memory/*.md`:
> инфраструктура, обязательные правила счёта, скоринг заявок, автообзвон.

Тянет историю звонков из Битрикс24, расшифровывает записи локально на faster-whisper,
считает метрики качества разговоров. Аудио не хранит — только текст.

## Установка

```bash
# на сервере, под root
apt update && apt install -y unzip
unzip ropbot.zip -d /opt && cd /opt/ropbot
chmod +x install.sh

./install.sh              # первый запуск создаст .env
nano .env                 # впишите B24_WEBHOOK
./install.sh              # второй запуск соберёт и поднимет всё
```

## Что внутри

| Сервис | Назначение |
|---|---|
| `postgres` | база: звонки, расшифровки, метрики. Слушает только localhost |
| `collector` | опрос Битрикса каждые 5 минут + бэкфилл истории |
| `asr` | очередь расшифровки на faster-whisper |

## Проверка

```bash
docker compose logs -f collector          # идёт ли сбор
docker compose exec postgres psql -U rop -d rop

-- сколько собрано
SELECT count(*), min(call_start), max(call_start) FROM calls;

-- очередь расшифровки
SELECT status, count(*) FROM asr_queue GROUP BY status;

-- пропущенные без перезвона за неделю
SELECT manager, count(*) AS missed,
       count(*) FILTER (WHERE callback_at IS NULL) AS no_callback,
       round(avg(callback_delay_min)::numeric, 1) AS avg_delay_min
FROM v_missed_with_callback
WHERE call_start > now() - interval '7 days'
GROUP BY manager ORDER BY missed DESC;

-- сводка по менеджерам
SELECT * FROM v_manager_daily WHERE day > now() - interval '7 days' ORDER BY day DESC, out_calls DESC;
```

## Настройка скорости расшифровки

В `.env`:

- `WHISPER_MODEL` — `small` быстро и достаточно, `medium` точнее и втрое медленнее
- `WHISPER_COMPUTE` — `int8` быстро, `float32` точнее
- `MIN_CALL_SEC` — порог отсечения коротких звонков
- `ASR_CPU_LIMIT` — сколько ядер отдать расшифровке

После изменений: `docker compose up -d --force-recreate asr`

## Перепрогнать расшифровку заново

```sql
UPDATE asr_queue SET status='pending', attempts=0;
```
Работает только если `KEEP_AUDIO_DAYS > 0` и файлы ещё не удалены,
иначе записи скачаются из Битрикса заново.

## Безопасность

- Postgres не выставлен наружу, порт слушает только `127.0.0.1`
- Фаервол пропускает только SSH
- В базе лежат персональные данные клиентов — сервер должен быть в РФ
