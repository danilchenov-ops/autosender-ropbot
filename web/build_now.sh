#!/bin/bash
# Быстрая пересборка «живых» страниц: дашборд менеджера и страница РОПа.
# Один процесс на всю пачку (tar-потоком), ~2 секунды. Ставится на каждую минуту.
# Тяжёлое (страница «Скрипт», хаб, журнал rop_control) собирает build.sh раз в 15 минут.
set -u
WEB=/opt/ropbot/web
TMP=$(mktemp -d "$WEB/.build.XXXXXX")   # тот же раздел, что и цель, — mv атомарен
trap 'rm -rf "$TMP"' EXIT

if ! docker exec -i ropbot-collector-1 python - --bundle now < "$WEB/dash.py" \
        > "$TMP/b.tar" 2>"$TMP/err" || [ ! -s "$TMP/b.tar" ]; then
    echo "$(date -Is) ОШИБКА bundle now"
    grep -v -e RequestsDependencyWarning -e 'warnings.warn' "$TMP/err" | tail -3
    exit 0
fi

tar -xf "$TMP/b.tar" -C "$TMP" && rm -f "$TMP/b.tar"
for f in "$TMP"/*/*.html; do
    [ -e "$f" ] || continue
    rel=${f#"$TMP/"}
    mkdir -p "$WEB/d/$(dirname "$rel")"
    chown www-data:www-data "$f"
    mv -f "$f" "$WEB/d/$rel"      # атомарная подмена: читатель не видит половину
done
exit 0
