#!/usr/bin/env bash
# Бэкап базы Виртуального РОПа.
# Cron: 0 3 * * * /opt/ropbot/db/backup.sh >> /var/log/ropbot-backup.log 2>&1
set -euo pipefail

DIR=/opt/ropbot/backups
PG=ropbot-postgres-1
DAY=$(date +%F)
mkdir -p "$DIR/full"

docker exec "$PG" pg_dump -U rop -d rop --no-owner \
  | gzip -9 > "$DIR/full/rop-$DAY.sql.gz"
echo "$(date '+%F %T') rop: $(du -h "$DIR/full/rop-$DAY.sql.gz" | cut -f1)"

# храним последние 7 копий
ls -1t "$DIR/full"/rop-*.sql.gz | tail -n +8 | xargs -r rm --

# --- проверка целостности ---
gzip -t "$DIR/full/rop-$DAY.sql.gz" || {
  echo "$(date '+%F %T') ОШИБКА: архив не распаковывается" >&2; exit 1; }

# grep -c дочитывает поток до конца — без SIGPIPE, который ловил бы pipefail
TABLES=$(zcat "$DIR/full/rop-$DAY.sql.gz" | grep -c '^CREATE TABLE ')
if [ "$TABLES" -lt 20 ]; then
  echo "$(date '+%F %T') ОШИБКА: в дампе только $TABLES таблиц, ожидалось не меньше 20" >&2
  exit 1
fi
echo "$(date '+%F %T') таблиц в дампе: $TABLES — бэкап в порядке"

# --- копия на второй сервер (195.2.74.207), храним там 14 ---
SZ=$(stat -c %s "$DIR/full/rop-$DAY.sql.gz")
if [ "$SZ" -lt 50000000 ]; then
  echo "$(date '+%F %T') ОШИБКА: дамп $((SZ/1024)) КБ — слишком мал, off-site не копирую" >&2; exit 1
fi
SSH="ssh -i /root/.ssh/relay -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes"
$SSH root@195.2.74.207 'mkdir -p /opt/backups/rop' \
 && scp -i /root/.ssh/relay -o StrictHostKeyChecking=no -q "$DIR/full/rop-$DAY.sql.gz" root@195.2.74.207:/opt/backups/rop/ \
 && $SSH root@195.2.74.207 'ls -1t /opt/backups/rop/rop-*.sql.gz | tail -n +15 | xargs -r rm --' \
 && echo "$(date '+%F %T') off-site копия на 195.2.74.207 — ок" \
 || echo "$(date '+%F %T') ОШИБКА: off-site копия не удалась" >&2
