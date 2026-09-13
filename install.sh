#!/usr/bin/env bash
# Установка «Виртуального РОПа» на чистый Ubuntu 24.04
set -euo pipefail

say() { printf "\n\033[1;33m▸ %s\033[0m\n" "$*"; }

say "Проверяем процессор"
MODEL=$(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')
CORES=$(nproc)
echo "  Процессор : $MODEL"
echo "  Ядер      : $CORES"

if grep -qm1 avx512f /proc/cpuinfo; then
    echo "  Инструкции: AVX-512 ✓ (лучший вариант для расшифровки)"
elif grep -qm1 avx2 /proc/cpuinfo; then
    echo "  Инструкции: AVX2 ✓"
else
    echo "  Инструкции: AVX2 НЕ НАЙДЕН ✗"
    echo "  Расшифровка будет медленной. Стоит пересоздать сервер на другом узле."
    read -rp "  Продолжить всё равно? [y/N] " a
    [[ "${a,,}" == "y" ]] || exit 1
fi

say "Ставим Docker"
if ! command -v docker >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg jq
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
    echo "  Docker уже установлен"
fi

say "Готовим конфигурацию"
cd "$(dirname "$0")"
if [[ ! -f .env ]]; then
    cp .env.example .env
    sed -i "s/^PG_PASSWORD=.*/PG_PASSWORD=$(openssl rand -hex 16)/" .env
    sed -i "s/^WHISPER_THREADS=.*/WHISPER_THREADS=$CORES/" .env
    sed -i "s/^ASR_CPU_LIMIT=.*/ASR_CPU_LIMIT=$CORES/" .env
    echo "  Создан .env — впишите в него B24_WEBHOOK и запустите скрипт снова."
    exit 0
fi

if ! grep -q '^B24_WEBHOOK=https' .env; then
    echo "  В .env не заполнен B24_WEBHOOK. Впишите вебхук Битрикса и запустите снова."
    exit 1
fi

say "Закрываем всё, кроме SSH"
if command -v ufw >/dev/null; then
    ufw --force reset >/dev/null
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    ufw allow OpenSSH >/dev/null
    ufw --force enable >/dev/null
    echo "  Фаервол включён: входящие только по SSH"
fi

say "Собираем и запускаем"
docker compose build
docker compose up -d

say "Готово"
echo "  Логи сбора       : docker compose logs -f collector"
echo "  Логи расшифровки : docker compose logs -f asr"
echo "  База             : docker compose exec postgres psql -U rop -d rop"
echo
echo "  Первый бэкфилл занимает от 10 минут. Расшифровка стартует следом."
