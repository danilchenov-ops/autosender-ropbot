#!/bin/sh
# Пересборка образа с новым промтом разборщика (25.08.2026).
# Сначала build (контейнеры живут), потом короткий стоп ASR и перезапуск.
cd /opt/ropbot
echo "=== build start $(date)"
docker compose build || exit 1
echo "=== build done $(date), stopping asr"
docker compose stop asr
docker compose up -d --scale asr=2
echo "=== up done $(date)"
docker ps --format '{{.Names}} {{.Status}}'
