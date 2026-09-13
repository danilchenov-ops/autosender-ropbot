#!/usr/bin/env bash
# Оркестратор пула персональных ссылок Telegram. Крон */5.
# 1) забрать с прокладки выданные ссылки → ропбот (ClientID/yclid к вступлениям)
# 2) если свободных в пуле мало — создать через бота и долить на прокладку
# 3) контрольная сумма: число участников канала
set -euo pipefail
SSH="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -i /root/.ssh/relay root@195.2.74.207"
TOOL="python3 /opt/tglinks/pooltool.py"
BOT="docker exec -i ropbot-tgbot-1 python tg_pool.py"
TARGET=100    # держать столько свободных ссылок
MIN=60        # доливать, когда меньше этого
BATCH=40      # не больше за один прогон (≈1,5 с на ссылку через туннель)

log(){ echo "$(date '+%F %T') [tg_pool] $*"; }

# 1. выданные → ропбот
ISSUED=$($SSH "$TOOL export")
if [ "$ISSUED" != "[]" ] && [ -n "$ISSUED" ]; then
  echo "$ISSUED" | $BOT import 2>&1 | grep -v -i 'warning' || true
fi

# 2. долить пул
FREE=$($SSH "$TOOL free")
if [ "${FREE:-0}" -lt "$MIN" ]; then
  NEED=$((TARGET - FREE)); [ "$NEED" -gt "$BATCH" ] && NEED=$BATCH
  NEW=$($BOT make "$NEED" 2>/dev/null | tail -1)
  ADDED=$(echo "$NEW" | $SSH "$TOOL import")
  log "пул: было $FREE, создано $NEED, долито $ADDED"
fi

# 3. контрольная сумма
$BOT count 2>&1 | grep -v -i 'warning' || true

# 4. MAX: клики и события канала с прокладки → ропбот, привязка по времени; счётчик участников
MAXBOT="docker exec -i ropbot-tgbot-1 python max_pool.py"
MAXDATA=$($SSH "$TOOL maxexport" || echo '{}')
if [ -n "$MAXDATA" ] && [ "$MAXDATA" != "{}" ] && [ "$MAXDATA" != '{"clicks": [], "events": []}' ]; then
  echo "$MAXDATA" | $MAXBOT import 2>&1 | grep -v -i 'warning' || true
fi
MAX_TOKEN=$(grep '^MAX_BOT_TOKEN=' /opt/ropbot/.env | cut -d= -f2-)
MAX_CHAT=$(grep '^MAX_CHAT_ID=' /opt/ropbot/.env | cut -d= -f2-)
if [ -n "$MAX_TOKEN" ] && [ -n "$MAX_CHAT" ]; then
  CNT=$(curl -s -m 20 -H "Authorization: $MAX_TOKEN" "https://platform-api2.max.ru/chats/$MAX_CHAT" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("participants_count",""))' 2>/dev/null || true)
  if [ -n "$CNT" ]; then $MAXBOT count "$CNT" 2>&1 | grep -v -i 'warning' || true; fi
fi
