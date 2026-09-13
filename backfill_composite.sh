#!/bin/sh
# Разовый прогон: посчитать составную оценку по всем заявкам с 1 августа,
# отправить балл в поле «Скоринг» и расставить короны в названиях.
# Запускать в фоне: nohup /opt/ropbot/backfill_composite.sh &
set -e
docker exec ropbot-collector-1 python composite.py --days 90 --limit 4000
docker exec ropbot-collector-1 python marker.py --limit 4000
echo "готово $(date)"
