#!/bin/bash
# Полная пересборка панелей раз в 15 минут: журнал ленты «Контроль» + все
# страницы всех активных токенов одним процессом (tar-потоком), ~3 секунды.
#
# Живые страницы (дашборд менеджера, страница РОПа) отдельно пересобирает
# build_now.sh каждую минуту — оба скрипта берут один и тот же flock,
# так что наложения не бывает.
#
# Код (dash.py, control.py) в образ не запечён, подаётся через stdin.
set -u
WEB=/opt/ropbot/web

docker exec -i ropbot-collector-1 python - < "$WEB/control.py" \
    >> /var/log/ropbot/control.log 2>&1 \
    || echo "$(date -Is) ОШИБКА control.py (страницы собираем по старому журналу)"

TMP=$(mktemp -d "$WEB/.build.XXXXXX")   # тот же раздел, что и цель, — mv атомарен
trap 'rm -rf "$TMP"' EXIT

if ! docker exec -i ropbot-collector-1 python - --bundle all < "$WEB/dash.py" \
        > "$TMP/b.tar" 2>"$TMP/err" || [ ! -s "$TMP/b.tar" ]; then
    echo "$(date -Is) ОШИБКА bundle all"
    grep -v -e RequestsDependencyWarning -e 'warnings.warn' "$TMP/err" | tail -5
    exit 0
fi
grep -v -e RequestsDependencyWarning -e 'warnings.warn' "$TMP/err" | grep . | tail -5

tar -xf "$TMP/b.tar" -C "$TMP" && rm -f "$TMP/b.tar"
for f in "$TMP"/*/*.html; do
    [ -e "$f" ] || continue
    rel=${f#"$TMP/"}
    mkdir -p "$WEB/d/$(dirname "$rel")"
    chown www-data:www-data "$f"
    mv -f "$f" "$WEB/d/$rel"
done
exit 0
