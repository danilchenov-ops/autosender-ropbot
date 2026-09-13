#!/bin/bash
cd /opt/ropbot/lab
P=".venv/bin/python -u runner.py run --model gemini-3.7-flash --thinking off --lean --workers 5 --rub-in 0.29 --rub-out 2.41"
echo "=== A. LOST, обучающая ==="
$P --where "s.is_first AND s.split='train' AND s.grp='LOST'" --limit 160 --max-rub 520
echo "=== B. WON, обучающая ==="
$P --where "s.is_first AND s.split='train' AND s.grp='WON'" --max-rub 180
echo "=== C. контрольная выборка ==="
$P --where "s.is_first AND s.split='holdout'" --max-rub 260
echo "=== ВСЁ ==="
