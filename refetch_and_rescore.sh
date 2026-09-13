#!/bin/sh
# Разово: перечитать недосчитанные сессии, пересчитать составную оценку живьём,
# дослать баллы и переставить значки.
set -e
docker exec ropbot-collector-1 python refetch_early.py --limit 300
docker exec ropbot-collector-1 python composite.py --days 90 --limit 4000
docker exec ropbot-collector-1 python marker.py --limit 4000
echo "готово $(date)"
