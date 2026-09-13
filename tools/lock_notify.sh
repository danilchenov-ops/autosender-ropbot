#!/bin/bash
# Забирает журнал блокировок с lead-router (195.2.74.207) и отдаёт его боту ропа.
# Крон: /etc/cron.d/ropbot-lock-notify, каждые 2 минуты, под flock.
# Первый запуск: SEED=1 bash lock_notify.sh — пометить старое как отправленное.
set -euo pipefail
ROWS=$(ssh -i /root/.ssh/relay -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
      root@195.2.74.207 'cd /opt/lead-router && sudo -u leadrouter ./venv/bin/python router.py --lock-events 200 2>/dev/null')
[ -n "$ROWS" ] || exit 0
docker exec -e ROWS="$ROWS" -e SEED="${SEED:-0}" -i ropbot-collector-1 python - < /opt/ropbot/tools/lock_notify.py \
  2> >(grep -v "RequestsDependencyWarning\|warnings.warn" >&2)
